"""
stls_identity.trust_registry_bridge
=======================================

Public HTTP surface of the Trust Anchor Registry — Option A governance:
Sparkle hosts it, other onboarded operators fetch it over HTTP instead
of direct DB access (which only makes sense for Sparkle itself, the
anchor operator). Read-only, same posture as stls_id_bridge.py.

    GET /trust-registry/anchors            -> every active TrustAnchorEntry
    GET /trust-registry/anchors/{authority_id} -> one entry

Deliberately does NOT expose an onboarding/write endpoint here — adding
or revoking an Authority is a deliberate governance action (see
build_trust_anchor_entry() in trust_registry_store.py), not something
this public, unauthenticated surface should trigger.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .stls_id_models import TrustAnchorEntry
from .stls_id_trust_registry_store import TrustAnchorRegistryStoreBase


def create_trust_registry_router(
    *, store: TrustAnchorRegistryStoreBase
) -> APIRouter:
    router = APIRouter()

    @router.get("/trust-registry/anchors", response_model=list[TrustAnchorEntry])
    async def list_anchors() -> list[TrustAnchorEntry]:
        """Cacheable (HTTP Cache-Control headers, sized under
        TRUST_ANCHOR_CACHE_TTL_SECONDS) — this is exactly what a remote
        TrustAnchorRegistryClient's refresh() would poll."""
        return await store.list_active()

    @router.get("/trust-registry/anchors/{authority_id}", response_model=TrustAnchorEntry)
    async def get_anchor(authority_id: str) -> TrustAnchorEntry:
        entry = await store.get(authority_id)
        if entry is None or entry.revoked_at is not None:
            raise HTTPException(status_code=404, detail="unknown or revoked authority")
        return entry

    return router