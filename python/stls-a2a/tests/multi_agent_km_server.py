"""
multi_agent_km_server.py
------------------------
Server multi-agente per testare i loader dell'ApiKeyManager di STLS-A2A.

Ogni agente usa un backend di autenticazione diverso:

    /agents/calculator/secure-a2a     → StaticLoader  (chiavi in-memory)
    /agents/adk-agent/secure-a2a      → FileLoader    (api_keys.yaml)
    /agents/lc-agent/secure-a2a       → PostgresLoader (AUTH_PG_DSN)
    /agents/sk-agent/secure-a2a       → AuthConfig legacy (retrocompatibilità)
    /agents/invoices-classifier/...   → nessuna auth (pubblico)

Avvio rapido:

    # Solo StaticLoader e FileLoader (nessun Redis né Postgres richiesto)
    AUTH_KM_USE_REDIS=false uv run python multi_agent_km_server.py

    # Con Redis per cache (default se REDIS_URL impostato)
    REDIS_URL=redis://localhost:6379 uv run python multi_agent_km_server.py

    # Con Postgres
    AUTH_PG_DSN=postgresql://user:pass@localhost/stls uv run python multi_agent_km_server.py

Variabili d'ambiente lette dall'SDK (non più gestite nel server):
    REDIS_URL              URL Redis (default: redis://localhost:6379)
    AUTH_KM_USE_REDIS      abilita cache Redis (default: true)
    AUTH_REFRESH_INTERVAL  secondi tra refresh (default: 300)
    AUTH_KEYS_FILE         path file YAML/JSON per FileLoader
    AUTH_PG_DSN            DSN PostgreSQL per PostgresLoader
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
# Imports STLS-A2A
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
# Loaders
# ---------------------------------------------------------------------------

# StaticLoader: genera chiavi usa-e-getta a ogni avvio.
# Per chiavi stabili tra riavvii: usare FileLoader o PostgresLoader.
_raw_key_1, _ = generate_api_key("stls-static")
_raw_key_2, _ = generate_api_key("stls-static")

_static_loader = StaticLoader.from_plaintext({
    _raw_key_1: {
        "client_id":   "test-client-calculator",
        "scopes":      ["secure-a2a"],
        "endpoints":   ["/agents/calculator/secure-a2a",
                        "/agents/calculator/a2a"],
        "description": "Chiave di test per l'agente calculator",
    },
    _raw_key_2: {
        "client_id":   "admin-client",
        "scopes":      ["secure-a2a", "admin"],
        "endpoints":   [],              # wildcard: tutti gli endpoint
        "description": "Chiave admin — accesso completo",
    },
})

# FileLoader: path da AUTH_KEYS_FILE (default api_keys.yaml)
_file_loader = FileLoader(path=os.getenv("AUTH_KEYS_FILE", "api_keys.yaml"))

# PostgresLoader: solo se AUTH_PG_DSN è configurato
_pg_loader = (
    PostgresLoader(dsn=AUTH_PG_DSN, min_pool_size=1, max_pool_size=3)
    if AUTH_PG_DSN else None
)

# ---------------------------------------------------------------------------
# Manager — uno per loader, costruiti con ApiKeyManager.from_loader()
#
# from_loader() legge automaticamente dall'env:
#   REDIS_URL          → URL Redis per la cache
#   AUTH_KM_USE_REDIS  → abilita/disabilita cache
# Non è più necessario leggere queste variabili nel server.
# ---------------------------------------------------------------------------

static_manager = ApiKeyManager.from_loader(_static_loader, audit_hook=_audit)
file_manager   = ApiKeyManager.from_loader(_file_loader,   audit_hook=_audit)
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
    .add("static_manager",   static_manager)
    .add("file_manager",     file_manager)
    .add("postgres_manager", pg_manager)    # ignorato se pg_manager è None
)

# ---------------------------------------------------------------------------
# AuthConfig legacy per sk-agent (invariato — retrocompatibilità)
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
        Avvia tutti i manager all'inizio, li ferma allo shutdown.
        ApiKeyManagerGroup gestisce start/stop in ordine con una sola chiamata.
        """
        logger.info(
            "Avvio ApiKeyManagerGroup (%d manager: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )
        # Logga le chiavi StaticLoader generate (solo per test)
        logger.info("=" * 60)
        logger.info("StaticLoader — chiavi di test generate:")
        logger.info("  KEY_1 (calculator):  %s", _raw_key_1)
        logger.info("  KEY_2 (admin):       %s", _raw_key_2)
        logger.info("  Usare: X-API-Key: <valore> oppure ?api_key=<valore>")
        logger.info("=" * 60)

        async with managers:
            yield                   # ← app in esecuzione

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # ── Provider STLS ─────────────────────────────────────────────────────
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    # ── Agente calculator — StaticLoader ──────────────────────────────────
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

    # ── Agente adk-agent — FileLoader ─────────────────────────────────────
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

    # ── Agente lc-agent — PostgresLoader (o fallback legacy) ──────────────
    from langchain_agents import agent as lc_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=lc_agent,
        key_session_provider=key_session_provider,
        path="/agents/lc-agent/secure-a2a",
        framework="langchain",
        api_key_manager=pg_manager,                          # None se PG non configurato
        auth=None if pg_manager else AuthConfig(
            bearer_tokens={"legacy-bearer-token-lc"},
            api_keys={"legacy-api-key-lc"},
            realm="LangChain Agent (legacy)",
        ),
    )

    # ── Agente sk-agent — AuthConfig legacy ───────────────────────────────
    from semantic_agents import agent as sk_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sk_agent,
        key_session_provider=key_session_provider,
        path="/agents/sk-agent/secure-a2a",
        framework="semantic-kernel",
        auth=_sk_auth_legacy,
    )

    # ── Agente invoices-classifier — nessuna auth (pubblico) ──────────────
    from invoices_agents import invoices_classifier_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=invoices_classifier_agent,
        key_session_provider=key_session_provider,
        path="/agents/invoices-classifier/secure-a2a",
        framework="google-adk",
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
    print(f"   StaticLoader  → /agents/calculator/  (chiavi in-memory)")
    print(f"   FileLoader    → /agents/adk-agent/   ({os.getenv('AUTH_KEYS_FILE', 'api_keys.yaml')})")
    print(f"   PostgresLoader→ /agents/lc-agent/    ({_pg_status})")
    print(f"   AuthConfig    → /agents/sk-agent/    (legacy, invariato)")
    print(f"   Nessuna auth  → /agents/invoices-classifier/")
    print(f"\n   Redis cache: {'abilitata (' + _redis_url + ')' if _use_redis else 'disabilitata (AUTH_KM_USE_REDIS=false)'}")
    print(f"\n🌐 Endpoint globali:")
    print(f"   Cards:   http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"   Health:  http://localhost:{PORT}/auth/health")
    print(f"   Refresh: http://localhost:{PORT}/auth/refresh  (POST)")
    print(f"\n🤖 Agenti:")
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