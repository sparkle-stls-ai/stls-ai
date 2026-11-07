"""
run_identity_bridge.py
------------------------
Runs the Identity Bridge (public DID Document + status list) as a
local HTTP server — same principle as agent_identity.py: a file
external to the stls-identity package, importing only its public API.

Independent of agent_identity.py: reads its own configuration from
.env.identity_bridge, not from the generic .env — so the two processes
(conversational agent, bridge server) stay separable, each with its
own configuration file, even if they run in the same directory or
repo.

NOTE: the exact signature of create_identity_bridge_router() below is
reconstructed from the project documentation, not re-verified against
the real source (lost in a sandbox reset) — to be confirmed against
stls_id_bridge.py before considering it final.

Usage:
    uv run python run_identity_bridge.py
    # or, with reload for development:
    uv run uvicorn run_identity_bridge:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os

from dotenv import load_dotenv
load_dotenv(".env.identity_bridge")

import redis.asyncio as redis_asyncio
import uvicorn
from fastapi import FastAPI

from stls_identity import (
    IdentityKeystoreFactory,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    StatusListCredential,
    StatusListManager,
    bootstrap_signing_key,
    build_did_document,
    create_identity_bridge_router,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Same env vars as tools_identity.py — no new convention introduced,
# except the two for the refresher (at the bottom of this block).
_GOVERNING_AUTHORITY_DID = os.environ.get("GOVERNING_AUTHORITY_DID", "did:web:sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_HOST = os.environ.get("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_AGENT_ID = os.environ.get("GOVERNING_AUTHORITY_AGENT_ID", "governing-authority")
_IDENTITY_AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")
_STATUS_LIST_PUBLISH_URL = os.environ.get(
    "STATUS_LIST_PUBLISH_URL", "https://sofia.sparkle.example.com/identity/status-list"
)

# Must stay BELOW the MRI (5 minutes in verifier.py's code) — otherwise an
# external verifier reading this cache might find it already stale by
# its own rules, even though we just republished it here.
_REFRESH_INTERVAL_SECONDS = int(os.environ.get("STATUS_LIST_REFRESH_INTERVAL_SECONDS", "60"))

did_store = PostgresDIDDocumentStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("IDENTITY_PG_DID_TABLE"),
)
status_cache_store = PostgresStatusListCacheStore(
    dsn=os.environ.get("IDENTITY_PG_DSN", ""),
    table=os.environ.get("IDENTITY_PG_STATUS_LIST_TABLE"),
)

# Same Redis/authority_id as tools_identity.py — MUST be the same
# bitstring that IdentityManager.revoke() updates, otherwise the
# bridge would publish another Authority's revocation state.
#
# decode_responses=False IS CRITICAL HERE: the bitstring is raw binary
# data (written with SETBIT), not text. With decode_responses=True,
# GET returns it as str, and StatusListManager.publish() passes it to
# gzip.compress() — which expects bytes, not str — raising a
# TypeError on every publish cycle (a real bug, found in the logs:
# "TypeError: a bytes-like object is required, not 'str'").
_status_list_redis = redis_asyncio.from_url(
    os.environ.get("STATUS_LIST_REDIS_URL", "redis://localhost:6379"),
    decode_responses=False,
)
status_list_manager = StatusListManager(
    redis_client=_status_list_redis, authority_id=_IDENTITY_AUTHORITY_ID
)

router = create_identity_bridge_router(
    did_store=did_store,
    status_cache_store=status_cache_store,
    authority_id=_IDENTITY_AUTHORITY_ID,
    authority_agent_id=_GOVERNING_AUTHORITY_AGENT_ID,
)

app = FastAPI(title="STLS Identity Bridge", version="0.1.0")
app.include_router(router)

_refresher_task: asyncio.Task | None = None


async def _publish_once() -> None:
    """Reads the current bitstring from Redis (StatusListManager.publish(),
    compressed+encapsulated), writes it into the Postgres cache the
    bridge serves. Without this, /identity/status-list stays empty
    forever — IdentityManager.revoke() only updates the bit on Redis,
    it doesn't publish anything externally on its own."""
    encoded_list, valid_from = await status_list_manager.publish()
    credential = StatusListCredential(
        id=_STATUS_LIST_PUBLISH_URL,
        issuer=_GOVERNING_AUTHORITY_DID,
        valid_from=valid_from,
        encoded_list=encoded_list,
    )
    await status_cache_store.put(_IDENTITY_AUTHORITY_ID, credential)
    logger.info("status list published (valid_from=%s)", valid_from.isoformat())


async def _refresh_status_list_periodically() -> None:
    while True:
        try:
            await _publish_once()
        except Exception:
            logger.exception("periodic status list publish failed, retrying next cycle")
        await asyncio.sleep(_REFRESH_INTERVAL_SECONDS)


@app.on_event("startup")
async def _ensure_schema() -> None:
    """Creates the tables if they don't exist — same self-bootstrap as
    tools_identity.py, no manual migration required."""
    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(did_store.get_ddl())

    pool2 = await status_cache_store._get_pool()
    async with pool2.acquire() as conn:
        await conn.execute(status_cache_store.get_ddl())

    # The AUTHORITY's own DID Document — this process is independent
    # of agent_identity.py (it may start first, or on its own), so it
    # can't assume someone else has already published it. Same
    # IDENTITY_KEYSTORE_BACKEND as the agent's .env ->
    # bootstrap_signing_key is idempotent (get-or-create): if the
    # agent has already run, this call retrieves the SAME key, it
    # doesn't generate a second one. Without this step, GET
    # /.well-known/did.json responds "authority DID Document not
    # bootstrapped yet" forever — not a transient error.
    keystore = IdentityKeystoreFactory.build()
    _, authority_pub, _ = bootstrap_signing_key(_GOVERNING_AUTHORITY_AGENT_ID, keystore)
    authority_document = build_did_document(
        host=_GOVERNING_AUTHORITY_HOST,
        agent_path_segments=[],  # plain DID (did:web:<host>), not an "agent" that happens to be called governing-authority
        governing_authority_did=_GOVERNING_AUTHORITY_DID,  # self-referential: root of trust, controller = itself
        signing_public_key=authority_pub,
    )
    await did_store.put(_GOVERNING_AUTHORITY_AGENT_ID, authority_document)

    # Immediate publish at startup — without this, /identity/status-list
    # would respond "not published yet" until the periodic task's first cycle.
    await _publish_once()

    global _refresher_task
    _refresher_task = asyncio.create_task(_refresh_status_list_periodically())

    logger.info(
        "Identity Bridge ready — DID store, status cache store, Authority "
        "DID Document and refresher (every %ss) initialized", _REFRESH_INTERVAL_SECONDS,
    )


@app.on_event("shutdown")
async def _stop_refresher() -> None:
    if _refresher_task is not None:
        _refresher_task.cancel()
    await _status_list_redis.aclose()


if __name__ == "__main__":
    port = int(os.environ.get("IDENTITY_BRIDGE_PORT", "8000"))
    logger.info("Starting Identity Bridge on http://localhost:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port)