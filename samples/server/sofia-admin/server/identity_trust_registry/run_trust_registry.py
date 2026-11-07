"""
run_trust_registry.py
------------------------
Exposes the Trust Anchor Registry via HTTP — Sparkle as the
federation's anchor operator (Option A: centralized). Other platforms
(Operator B, C, ...) read from here which Governing Authorities are
recognized and their current keys.

Independent of agent_identity.py and run_identity_bridge.py: its own
configuration (.env.trust_registry), its own process. Same principle
as the other two files: external to the stls-identity package,
importing only its public API.

Simpler than the identity bridge — no periodic publishing task: there
is no bitstring to compress and cache here, the router reads Postgres
directly on every request.

Note: create_trust_registry_router(store=...) is confirmed to
genuinely work — the router builds and exposes endpoints (verified
via /docs). The only thing initially wrong was the assumed paths
(/trust-anchors instead of the real /trust-registry/anchors, below)
— corrected below after direct verification.

Real endpoints (confirmed via /docs, no longer guessed):
    GET /trust-registry/anchors               — list of recognized Authorities
    GET /trust-registry/anchors/{authority_id} — a specific Authority

Usage:
    uv run python run_trust_registry.py
    # or, with reload for development:
    uv run uvicorn run_trust_registry:app --reload --port 8100
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
load_dotenv(".env.trust_registry")

import uvicorn
from fastapi import FastAPI

from stls_identity import (
    PostgresTrustAnchorRegistryStore,
    create_trust_registry_router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

trust_registry_store = PostgresTrustAnchorRegistryStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("TRUST_REGISTRY_PG_TABLE"),
)

router = create_trust_registry_router(store=trust_registry_store)

app = FastAPI(title="STLS Trust Anchor Registry", version="0.1.0")
app.include_router(router)


@app.on_event("startup")
async def _ensure_schema() -> None:
    """Creates the table if it doesn't exist — same self-bootstrap as
    the other two processes, no manual migration required. Same table
    that onboard_trust_anchor/list_trust_anchors/revoke_trust_anchor in
    tools_identity.py write to — this process only reads, it never
    writes."""
    pool = await trust_registry_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(trust_registry_store.get_ddl())
    logger.info("Trust Anchor Registry ready — read-only, writes go through agent_identity.py")


if __name__ == "__main__":
    port = int(os.environ.get("TRUST_REGISTRY_PORT", "8100"))
    logger.info("Starting Trust Anchor Registry on http://localhost:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)