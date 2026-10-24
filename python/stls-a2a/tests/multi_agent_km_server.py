"""
multi_agent_km_server.py
------------------------
Multi-agent server for testing STLS-A2A's ApiKeyManager loaders.

Each agent uses a different authentication backend:

    /agents/calculator/secure-a2a     → StaticLoader  (in-memory keys)
    /agents/adk-agent/secure-a2a      → FileLoader    (api_keys.yaml)
    /agents/lc-agent/secure-a2a       → PostgresLoader (AUTH_PG_DSN)
    /agents/sk-agent/secure-a2a       → legacy AuthConfig (backward compatibility)
    /agents/invoices-classifier/...   → no auth (public)

Quick start:

    # StaticLoader and FileLoader only (no Redis or Postgres required)
    AUTH_KM_USE_REDIS=false uv run python multi_agent_km_server.py

    # With Redis for caching (default if REDIS_URL is set)
    REDIS_URL=redis://localhost:6379 uv run python multi_agent_km_server.py

    # With Postgres
    AUTH_PG_DSN=postgresql://user:pass@localhost/stls uv run python multi_agent_km_server.py

Environment variables read by the SDK (no longer handled in the server):
    REDIS_URL              Redis URL (default: redis://localhost:6379)
    AUTH_KM_USE_REDIS      enables the Redis cache (default: true)
    AUTH_REFRESH_INTERVAL  seconds between refreshes (default: 300)
    AUTH_KEYS_FILE         YAML/JSON file path for FileLoader
    AUTH_PG_DSN            PostgreSQL DSN for PostgresLoader
"""

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
    AgentResponse,
    AuthConfig,
    FileLoader,
    KeySessionProvider,
    KeySessionProviderFactory,
    PostgresLoader,
    StaticLoader,
    create_secure_a2a_endpoint,
    generate_api_key,
    start_task_store_cleanup,
    to_a2a,
)
from stls_a2a import ApiKeyManager, ApiKeyManagerGroup, default_audit_hook

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
# Loaders
# ---------------------------------------------------------------------------

# StaticLoader: generates throwaway keys on every startup.
# For keys that stay stable across restarts: use FileLoader or PostgresLoader.
_raw_key_1, _ = generate_api_key("stls-static")
_raw_key_2, _ = generate_api_key("stls-static")

_static_loader = StaticLoader.from_plaintext({
    _raw_key_1: {
        "client_id":   "test-client-calculator",
        "scopes":      ["secure-a2a"],
        "endpoints":   ["/agents/calculator/secure-a2a",
                        "/agents/calculator/a2a"],
        "description": "Test key for the calculator agent",
    },
    _raw_key_2: {
        "client_id":   "admin-client",
        "scopes":      ["secure-a2a", "admin"],
        "endpoints":   [],              # wildcard: all endpoints
        "description": "Admin key — full access",
    },
})

# FileLoader: path from AUTH_KEYS_FILE (default api_keys.yaml)
_file_loader = FileLoader(path=os.getenv("AUTH_KEYS_FILE", "api_keys.yaml"))

# PostgresLoader: only if AUTH_PG_DSN is configured
_pg_loader = (
    PostgresLoader(dsn=AUTH_PG_DSN, min_pool_size=1, max_pool_size=3)
    if AUTH_PG_DSN else None
)

# ---------------------------------------------------------------------------
# Managers — one per loader, built with ApiKeyManager.from_loader()
#
# from_loader() automatically reads from the env:
#   REDIS_URL          → Redis URL for the cache
#   AUTH_KM_USE_REDIS  → enables/disables the cache
# These variables no longer need to be read in the server itself.
# ---------------------------------------------------------------------------

static_manager = ApiKeyManager.from_loader(_static_loader, audit_hook=_audit)
file_manager   = ApiKeyManager.from_loader(_file_loader,   audit_hook=_audit)
pg_manager     = (
    ApiKeyManager.from_loader(_pg_loader, audit_hook=_audit)
    if _pg_loader else None
)

# ---------------------------------------------------------------------------
# ApiKeyManagerGroup — handles start/stop/health/refresh for all managers
# with a single call. None managers are silently ignored.
# ---------------------------------------------------------------------------

managers = (
    ApiKeyManagerGroup()
    .add("static_manager",   static_manager)
    .add("file_manager",     file_manager)
    .add("postgres_manager", pg_manager)    # ignored if pg_manager is None
)

# ---------------------------------------------------------------------------
# Legacy AuthConfig for sk-agent (unchanged — backward compatibility)
# ---------------------------------------------------------------------------

_sk_auth_legacy = AuthConfig(
    bearer_tokens={"sk-bearer-token"},
    api_keys={"sk-api-key-123"},
    realm="Semantic Kernel Agent (legacy AuthConfig)",
)

# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------

def build_app() -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Starts all managers at the beginning, stops them on shutdown.
        ApiKeyManagerGroup handles start/stop in order with a single call.
        """
        logger.info(
            "Starting ApiKeyManagerGroup (%d managers: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )
        # Log the generated StaticLoader keys (test only)
        logger.info("=" * 60)
        logger.info("StaticLoader — generated test keys:")
        logger.info("  KEY_1 (calculator):  %s", _raw_key_1)
        logger.info("  KEY_2 (admin):       %s", _raw_key_2)
        logger.info("  Use: X-API-Key: <value> or ?api_key=<value>")
        logger.info("=" * 60)

        async with managers:
            yield                   # ← app running

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # ── STLS provider ─────────────────────────────────────────────────────
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    # ── calculator agent — StaticLoader ────────────────────────────────────
    from mock_agent import MockAgent

    calculator = MockAgent(
        name="calculator",
        system_instruction="Performs mathematical calculations",
    )

    async def calculator_handler(request):
        result = await calculator.process_message(request.method, request.params)
        return AgentResponse(result=result, id=request.id)

    create_secure_a2a_endpoint(
        app=app,
        agent=calculator_handler,
        key_session_provider=key_session_provider,
        path="/agents/calculator/secure-a2a",
        framework="custom",
        api_key_manager=static_manager,
    )

    # ── adk-agent — FileLoader ──────────────────────────────────────────────
    from adk_google_agents import agent as adk_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=adk_agent,
        key_session_provider=key_session_provider,
        path="/agents/adk-agent/secure-a2a",
        framework="google-adk",
        enable_plain_endpoint=True,
        api_key_manager=file_manager,
    )

    # ── lc-agent — PostgresLoader (or legacy fallback) ──────────────────────
    from langchain_agents import agent as lc_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=lc_agent,
        key_session_provider=key_session_provider,
        path="/agents/lc-agent/secure-a2a",
        framework="langchain",
        api_key_manager=pg_manager,                          # None if PG is not configured
        auth=None if pg_manager else AuthConfig(
            bearer_tokens={"legacy-bearer-token-lc"},
            api_keys={"legacy-api-key-lc"},
            realm="LangChain Agent (legacy)",
        ),
    )

    # ── sk-agent — legacy AuthConfig ─────────────────────────────────────────
    from semantic_agents import agent as sk_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sk_agent,
        key_session_provider=key_session_provider,
        path="/agents/sk-agent/secure-a2a",
        framework="semantic-kernel",
        auth=_sk_auth_legacy,
    )

    # ── invoices-classifier agent — no auth (public) ────────────────────────
    from invoices_agents import invoices_classifier_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=invoices_classifier_agent,
        key_session_provider=key_session_provider,
        path="/agents/invoices-classifier/secure-a2a",
        framework="google-adk",
    )

    # ── Diagnostic endpoints ──────────────────────────────────────────────

    @app.get("/auth/health")
    async def auth_health():
        """
        Health check of the authentication system.
        Shows the status of each manager and of the Redis cache.

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
    print(f"   StaticLoader  → /agents/calculator/  (in-memory keys)")
    print(f"   FileLoader    → /agents/adk-agent/   ({os.getenv('AUTH_KEYS_FILE', 'api_keys.yaml')})")
    print(f"   PostgresLoader→ /agents/lc-agent/    ({_pg_status})")
    print(f"   AuthConfig    → /agents/sk-agent/    (legacy, unchanged)")
    print(f"   No auth       → /agents/invoices-classifier/")
    print(f"\n   Redis cache: {'enabled (' + _redis_url + ')' if _use_redis else 'disabled (AUTH_KM_USE_REDIS=false)'}")
    print(f"\n🌐 Global endpoints:")
    print(f"   Cards:   http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"   Health:  http://localhost:{PORT}/auth/health")
    print(f"   Refresh: http://localhost:{PORT}/auth/refresh  (POST)")
    print(f"\n🤖 Agents:")
    for name, path in [
        ("calculator",          "/agents/calculator/secure-a2a"),
        ("adk-agent",           "/agents/adk-agent/secure-a2a"),
        ("lc-agent",            "/agents/lc-agent/secure-a2a"),
        ("sk-agent",            "/agents/sk-agent/secure-a2a"),
        ("invoices-classifier", "/agents/invoices-classifier/secure-a2a"),
    ]:
        print(f"   {name:<22} http://localhost:{PORT}{path}")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()