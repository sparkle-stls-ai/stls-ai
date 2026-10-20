"""
stls_identity.bridge
=====================

Public HTTP surface of the identity bridge — same style as the existing
/.well-known/agent.json (AgentCard). Two endpoints, both read-only and
cacheable:

    GET /.well-known/did.json           -> this authority's own DID Document
    GET /agents/{agent_id}/did.json     -> a specific agent's DID Document
    GET /identity/status-list           -> published BitstringStatusListCredential

Everything here is intentionally stateless at the HTTP layer: the router
holds no Redis/crypto logic itself, it only serves documents that
IdentityManager / StatusListManager already produced and that a periodic
task has persisted into Postgres (see postgres_store.py). This is also
the whole "identity bridge" from the federation diagram — an external
platform only needs to replicate these response shapes to interoperate,
regardless of its own internal stack.

Wiring: create_identity_bridge_router() takes the stores as explicit
arguments and returns a router bound to them — same dependency-injection
shape as create_secure_a2a_endpoint() in stls-a2a, not a module-level
global router with hidden state.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .stls_id_models import DIDDocument, StatusListCredential
from .stls_id_postgres_store import DIDDocumentStoreBase, StatusListCacheStoreBase


def create_identity_bridge_router(
    *,
    did_store: DIDDocumentStoreBase,
    status_cache_store: StatusListCacheStoreBase,
    authority_id: str,
    authority_agent_id: str,
) -> APIRouter:
    """
    Args:
        did_store:           where agent DID Documents are read from.
        status_cache_store:  where the last published status list
                              snapshot is read from (never Redis directly
                              — see postgres_store.py's module docstring).
        authority_id:        key under which the authority's own status
                              list snapshot is cached.
        authority_agent_id:  agent_id under which the authority's own
                              DID Document was stored (bootstrap treats
                              the Governing Authority as an agent too,
                              for storage purposes).
    """
    router = APIRouter()

    @router.get("/.well-known/did.json", response_model=DIDDocument)
    async def get_authority_did_document() -> DIDDocument:
        doc = await did_store.get(authority_agent_id)
        if doc is None:
            raise HTTPException(
                status_code=404,
                detail="authority DID Document not bootstrapped yet",
            )
        return doc

    @router.get("/agents/{agent_id}/did.json", response_model=DIDDocument)
    async def get_agent_did_document(agent_id: str) -> DIDDocument:
        doc = await did_store.get(agent_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="unknown agent")
        return doc

    @router.get("/identity/status-list", response_model=StatusListCredential)
    async def get_status_list() -> StatusListCredential:
        """Serves the cached Postgres snapshot — never recomputes from
        Redis per-request. Pair with HTTP caching headers (Cache-Control
        / ETag) sized well under the 5-minute MRI so verifiers' own
        polling interval, not this endpoint, is the bottleneck."""
        doc = await status_cache_store.get(authority_id)
        if doc is None:
            raise HTTPException(
                status_code=503,
                detail="status list not published yet — refresher task has not run",
            )
        return doc

    return router