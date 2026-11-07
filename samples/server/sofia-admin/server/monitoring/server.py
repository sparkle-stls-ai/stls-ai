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

# ---------------------------------------------------------------------------
# Global configuration
# ---------------------------------------------------------------------------

PORT        = int(os.getenv("PORT", 8001))
BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
AUTH_PG_DSN = os.getenv("AUTH_PG_DSN", "")

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
# FastAPI app
# ---------------------------------------------------------------------------

def build_app() -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Starts all managers at the beginning, stops them at shutdown.
        ApiKeyManagerGroup handles start/stop in order with a single call.
        """
        logger.info(
            "Starting ApiKeyManagerGroup (%d managers: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )

        async with managers:
            yield                   # ← app running

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # ── STLS Provider ─────────────────────────────────────────────────────
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    from agent_monitoring import sofia_monitoring_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sofia_monitoring_agent,
        key_session_provider=key_session_provider,
        path="/agents/sofia-monitoring/secure-a2a",
        base_url=BASE_URL, # needed to build the agent card with the correct URL
        framework="google-adk",
        api_key_manager=pg_manager
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
    print(f"   PostgresLoader→ /agents/sofia-monitoring/    ({_pg_status})")
    print(f"\n   Redis cache: {'enabled (' + _redis_url + ')' if _use_redis else 'disabled (AUTH_KM_USE_REDIS=false)'}")
    print(f"\n🌐 Global endpoints:")
    print(f"   Cards:   http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"   Health:  http://localhost:{PORT}/auth/health")
    print(f"   Refresh: http://localhost:{PORT}/auth/refresh  (POST)")
    print(f"\n🤖 Agents:")
    for name, path in [
        ("sofia-monitoring",    "/agents/sofia-monitoring/secure-a2a"),

    ]:
        print(f"   {name:<22} http://localhost:{PORT}{path}")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
