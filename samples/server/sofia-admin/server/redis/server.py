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
# Imports STLS-A2A
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
# Configurazione globale
# ---------------------------------------------------------------------------

PORT        = int(os.getenv("PORT", 8001))
BASE_URL    = os.getenv("BASE_URL", "").rstrip("/")
AUTH_PG_DSN = os.getenv("AUTH_PG_DSN", "")

# ---------------------------------------------------------------------------
# Audit hook — shared, standard logging.
# In produzione: sostituire default_audit_hook() con un hook che invia a SIEM.
# ---------------------------------------------------------------------------

_audit = default_audit_hook()

# ---------------------------------------------------------------------------
# Loaders - definiscono da dove vengono le chiavi API
# ---------------------------------------------------------------------------
# PostgresLoader: solo se AUTH_PG_DSN è configurato
_pg_loader = (
    PostgresLoader(dsn=AUTH_PG_DSN, min_pool_size=1, max_pool_size=3)
    if AUTH_PG_DSN else None
)

pg_manager     = (
    ApiKeyManager.from_loader(_pg_loader, audit_hook=_audit)
    if _pg_loader else None
)

# ---------------------------------------------------------------------------
# ApiKeyManagerGroup — gestisce start/stop/health/refresh di tutti i manager
# con una sola chiamata. I manager None vengono ignorati silenziosamente.
# ---------------------------------------------------------------------------
managers = (
    ApiKeyManagerGroup()
    .add("postgres_manager", pg_manager)    
)

# ---------------------------------------------------------------------------
# App FastAPI
# ---------------------------------------------------------------------------

def build_app() -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Avvia tutti i manager all'inizio, li ferma allo shutdown.
        ApiKeyManagerGroup gestisce start/stop in ordine con una sola chiamata.
        """
        logger.info(
            "Avvio ApiKeyManagerGroup (%d manager: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )
      
        async with managers:
            yield                   # ← app in esecuzione

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # ── Provider STLS ─────────────────────────────────────────────────────
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    from agent_redis import sofia_redis_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sofia_redis_agent,
        key_session_provider=key_session_provider,
        path="/agents/sofia-redis/secure-a2a",
        base_url=BASE_URL, # necesserio per costruire l'agent card con URL corretto
        framework="google-adk",
        api_key_manager=pg_manager
    )

    
    # ── Endpoint diagnostica ──────────────────────────────────────────────

    @app.get("/auth/health")
    async def auth_health():
        """
        Health check del sistema di autenticazione.
        Mostra lo stato di ogni manager e della cache Redis.

        HTTP 200 → tutti i manager in stato "ok"
        HTTP 503 → almeno un manager "degraded" o "error"
        """
        from fastapi.responses import JSONResponse as _JR
        data = await managers.health()          # ← ApiKeyManagerGroup
        status_code = 200 if data.get("status") == "ok" else 503
        return _JR(content=data, status_code=status_code)

    @app.post("/auth/refresh")
    async def auth_refresh():
        """
        Forza un reload immediato delle chiavi da tutti i loader.
        Utile dopo una rotazione di chiavi senza riavviare il server.
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
        else "disabled (AUTH_PG_DSN non impostato)"
    )
    _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    _use_redis = os.getenv("AUTH_KM_USE_REDIS", "true").lower() not in ("false", "0", "no")

    print("\n" + "=" * 70)
    print("📡 Multi-Agent KM Server — Porta", PORT)
    print("=" * 70)
    print(f"\n🔐 Auth backends:")
    print(f"   PostgresLoader→ /agents/sofia-redis/    ({_pg_status})")
    print(f"\n   Redis cache: {'abilitata (' + _redis_url + ')' if _use_redis else 'disabilitata (AUTH_KM_USE_REDIS=false)'}")
    print(f"\n🌐 Endpoint globali:")
    print(f"   Cards:   http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"   Health:  http://localhost:{PORT}/auth/health")
    print(f"   Refresh: http://localhost:{PORT}/auth/refresh  (POST)")
    print(f"\n🤖 Agenti:")
    for name, path in [
        ("sofia-redis",    "/agents/sofia-redis/secure-a2a"),

    ]:
        print(f"   {name:<22} http://localhost:{PORT}{path}")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()