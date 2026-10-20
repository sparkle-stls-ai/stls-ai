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
)
from stls_a2a import ApiKeyManager
from stls_a2a.stls_a2a_auth_manager import ApiKeyManagerGroup, default_audit_hook

# ---------------------------------------------------------------------------
# Configurazione globale
# ---------------------------------------------------------------------------

PORT        = int(os.getenv("PORT", "8080"))
AUTH_PG_DSN = os.getenv("AUTH_PG_DSN", "")


def _resolve_base_url() -> str:
    """
    Risolve BASE_URL nell'ordine:
      1. Variabile d'ambiente BASE_URL (override esplicito / dev locale)
      2. Metadata server GCP (istanza Cloud Run in esecuzione)
      3. Fallback localhost (sviluppo locale senza GCP)
    """
    if url := os.getenv("BASE_URL"):
        return url.rstrip("/")
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/attributes/base-url",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.read().decode().strip().rstrip("/")
    except Exception:
        pass
    return f"http://localhost:{PORT}"


BASE_URL = _resolve_base_url()
logger.info("BASE_URL resolved: %s", BASE_URL)

# ---------------------------------------------------------------------------
# Audit hook
# ---------------------------------------------------------------------------

_audit = default_audit_hook()

# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_pg_loader = (
    PostgresLoader(dsn=AUTH_PG_DSN, min_pool_size=1, max_pool_size=3)
    if AUTH_PG_DSN else None
)

pg_manager = (
    ApiKeyManager.from_loader(_pg_loader, audit_hook=_audit)
    if _pg_loader else None
)

# ---------------------------------------------------------------------------
# ApiKeyManagerGroup
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
        logger.info(
            "Avvio ApiKeyManagerGroup (%d manager: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )
        async with managers:
            yield

    app = FastAPI(title="Multi-Agent Monitoring Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

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
        base_url=BASE_URL,
        framework="google-adk",
        api_key_manager=pg_manager,
    )

    @app.get("/auth/health")
    async def auth_health():
        from fastapi.responses import JSONResponse as _JR
        data = await managers.health()
        return _JR(content=data, status_code=200 if data.get("status") == "ok" else 503)

    @app.post("/auth/refresh")
    async def auth_refresh():
        refreshed = await managers.refresh_all()
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
    print("📡 sofia-monitoring — Porta", PORT)
    print("=" * 70)
    print(f"\n🌐 BASE_URL : {BASE_URL}")
    print(f"\n🔐 Auth backends:")
    print(f"   PostgresLoader → {_pg_status}")
    print(f"   Redis cache    : {'abilitata (' + _redis_url + ')' if _use_redis else 'disabilitata'}")
    print(f"\n🤖 Agenti:")
    print(f"   sofia-monitoring   {BASE_URL}/agents/sofia-monitoring/secure-a2a")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()