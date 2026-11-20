from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# STLS-A2A imports
# ---------------------------------------------------------------------------
from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    PostgresLoader,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
    ApiKeyManager,
    ApiKeyManagerGroup,
    default_audit_hook,
)
from stls_identity import (
    RequestPresentationVerifier,
    Sentinel,
    bootstrap_signing_key,
    decode_multibase_ed25519_public_key,
    fetch_remote_credential_status,
)

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

PORT        = int(os.getenv("PORT", 8001))
BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
AUTH_PG_DSN = os.getenv("AUTH_PG_DSN", "")

# Issue #45: require a verified DID/VC presentation (not just an API key)
# to call this agent — the most sensitive sub-agent on the platform
# (bootstraps/issues/revokes identities, onboards trust anchors). Off by
# default: the operator flips it on once the coordinator's identity has
# been bootstrapped (bootstrap_coordinator_identity.py, or just asking
# sofia_identity_agent in chat) and is ready to test it.
REQUIRE_IDENTITY_PRESENTATION = os.getenv("REQUIRE_IDENTITY_PRESENTATION", "false").strip().lower() == "true"

# ---------------------------------------------------------------------------
# Audit hook — shared, standard logging.
# In production: replace default_audit_hook() with a hook that sends to a SIEM.
# ---------------------------------------------------------------------------

_audit = default_audit_hook()

# ---------------------------------------------------------------------------
# Loaders - define where API keys come from
# ---------------------------------------------------------------------------
# PostgresLoader: only if AUTH_PG_DSN is configured
_pg_loader = (
    PostgresLoader(dsn=AUTH_PG_DSN, min_pool_size=1, max_pool_size=3)
    if AUTH_PG_DSN else None
)

pg_manager     = (
    ApiKeyManager.from_loader(_pg_loader, audit_hook=_audit)
    if _pg_loader else None
)

# ---------------------------------------------------------------------------
# ApiKeyManagerGroup — handles start/stop/health/refresh of all managers
# with a single call. None managers are silently ignored.
# ---------------------------------------------------------------------------
managers = (
    ApiKeyManagerGroup()
    .add("postgres_manager", pg_manager)
)

# ---------------------------------------------------------------------------
# Identity verifier (issue #45)
# ---------------------------------------------------------------------------
#
# BUGFIX: this used to build everything — including the coordinator's
# key, resolved via tools_identity._deps(), which owns an asyncpg pool —
# with a bare asyncio.run() call BEFORE uvicorn.run() ever starts the
# real event loop. _deps() caches its _Deps singleton (including that
# pool) at module level in tools_identity.py; every OTHER caller (every
# ADK tool function) only ever calls it from within a request-handling
# coroutine already running on uvicorn's own loop. Pre-empting that
# cache from a throwaway asyncio.run() loop bound the pool to a loop
# that closes the instant this function returns — poisoning the SAME
# cached singleton for the rest of the process's lifetime. Silent
# symptom: did_store.get() never found the coordinator's DID Document
# (despite bootstrap_coordinator_identity.py having published it
# correctly), so its key never made it into trusted_keys — "unknown or
# untrusted holder" even with a real, matching DID Document in Postgres.
#
# Fix: only the Authority's own key is resolved synchronously here
# (IdentityKeystoreFactory.build() + bootstrap_signing_key() are both
# fully sync, no event loop involved at all — confirmed in
# stls_id_keystore.py). The coordinator's key is resolved later, inside
# lifespan() below, which runs on uvicorn's REAL event loop before the
# app starts serving traffic — the same loop _deps() will keep using
# for every actual request afterward. trusted_keys is a single shared
# dict mutated in place; Sentinel stores the reference, not a copy
# (stls_id_verifier.py), so updating it after Sentinel's own
# construction still takes effect.

def _build_identity_verifier() -> tuple[RequestPresentationVerifier | None, dict | None]:
    if not REQUIRE_IDENTITY_PRESENTATION:
        logger.info("REQUIRE_IDENTITY_PRESENTATION=false — identity_verifier is None, hook fully disabled.")
        return None, None

    from stls_identity import IdentityKeystoreFactory
    from tools_identity import _AUTHORITY_VM_ID, _GOVERNING_AUTHORITY_AGENT_ID

    keystore = IdentityKeystoreFactory.build()
    _, authority_pub, _ = bootstrap_signing_key(_GOVERNING_AUTHORITY_AGENT_ID, keystore)
    trusted_keys = {_AUTHORITY_VM_ID: authority_pub}

    sentinel = Sentinel(trusted_issuer_keys=trusted_keys)
    verifier = RequestPresentationVerifier(sentinel, status_resolver=fetch_remote_credential_status)
    return verifier, trusted_keys


async def _populate_coordinator_key(trusted_keys: dict) -> None:
    """Adds the coordinator's own verification key to trusted_keys —
    called from lifespan(), i.e. on uvicorn's real event loop, the only
    loop _deps()'s cached asyncpg pool should ever see."""
    from tools_identity import IDENTITY_AGENT_ID, _deps

    d = await _deps()
    coordinator_doc = await d.did_store.get(IDENTITY_AGENT_ID)
    if coordinator_doc is None:
        logger.warning(
            "REQUIRE_IDENTITY_PRESENTATION=true but %r has no published DID yet — "
            "run bootstrap_coordinator_identity.py first. Every presentation will "
            "fail closed with 'unknown or untrusted holder' until then.",
            IDENTITY_AGENT_ID,
        )
        return
    vm = coordinator_doc.verification_method[0]
    trusted_keys[vm.id] = decode_multibase_ed25519_public_key(vm.public_key_multibase)
    logger.info("Identity verifier: trusted coordinator key loaded (%s).", vm.id)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

def build_app() -> FastAPI:

    identity_verifier, trusted_keys = _build_identity_verifier()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Starts all managers at the beginning, stops them at shutdown.
        ApiKeyManagerGroup handles start/stop in order with a single call.

        Also resolves the coordinator's trusted key here (issue #45),
        not before uvicorn.run() — see _build_identity_verifier()'s
        module-level comment for why that ordering matters.
        """
        logger.info(
            "Starting ApiKeyManagerGroup (%d managers: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )

        if trusted_keys is not None:
            await _populate_coordinator_key(trusted_keys)

        async with managers:
            yield                   # ← app running

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # ── Provider STLS ─────────────────────────────────────────────────────
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    from agent_identity import sofia_identity_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sofia_identity_agent,
        key_session_provider=key_session_provider,
        path="/agents/sofia-identity/secure-a2a",
        base_url=BASE_URL, # needed to build the agent card with the correct URL
        framework="google-adk",
        api_key_manager=pg_manager,
        identity_verifier=identity_verifier,
    )

    
    # ── Diagnostic endpoints ──────────────────────────────────────────────

    @app.get("/auth/health")
    async def auth_health():
        """
        Health check of the authentication system.
        Shows the status of every manager and of the Redis cache.

        HTTP 200 → all managers in "ok" status
        HTTP 503 → at least one manager "degraded" or "error"
        """
        from fastapi.responses import JSONResponse as _JR
        data = await managers.health()          # ← ApiKeyManagerGroup
        status_code = 200 if data.get("status") == "ok" else 503
        return _JR(content=data, status_code=status_code)

    @app.post("/auth/refresh")
    async def auth_refresh():
        """
        Forces an immediate reload of keys from all loaders.
        Useful after a key rotation without restarting the server.
        """
        refreshed = await managers.refresh_all()    # ← ApiKeyManagerGroup
        return {"refreshed": refreshed}

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = build_app()

    _pg_status = (
        f"postgresql ({AUTH_PG_DSN[:20]}…)" if AUTH_PG_DSN
        else "disabled (AUTH_PG_DSN not set)"
    )
    _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    _use_redis = os.getenv("AUTH_KM_USE_REDIS", "true").lower() not in ("false", "0", "no")

    print("\n" + "=" * 70)
    print("📡 Multi-Agent KM Server — Port", PORT)
    print("=" * 70)
    print(f"\n🔐 Auth backends:")
    print(f"   PostgresLoader→ /agents/sofia-identity/    ({_pg_status})")
    print(f"\n   Redis cache: {'enabled (' + _redis_url + ')' if _use_redis else 'disabled (AUTH_KM_USE_REDIS=false)'}")
    print(f"\n🪪 Identity presentation (issue #45): {'REQUIRED' if REQUIRE_IDENTITY_PRESENTATION else 'disabled (REQUIRE_IDENTITY_PRESENTATION=false)'}")
    print(f"\n🌐 Global endpoints:")
    print(f"   Cards:   http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"   Health:  http://localhost:{PORT}/auth/health")
    print(f"   Refresh: http://localhost:{PORT}/auth/refresh  (POST)")
    print(f"\n🤖 Agents:")
    for name, path in [
        ("sofia-identity",    "/agents/sofia-identity/secure-a2a"),

    ]:
        print(f"   {name:<22} http://localhost:{PORT}{path}")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()