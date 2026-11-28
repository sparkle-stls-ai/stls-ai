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
logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)
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

PORT        = int(os.getenv("PORT", "8080"))
AUTH_PG_DSN = os.getenv("AUTH_PG_DSN", "")

# Identity presentation (issue #42 follow-up): require every caller of
# THIS coordinator's own /secure-a2a endpoint — sofia-client-secure
# today, potentially other clients later — to present a verified
# identity, not just a PQC-encrypted session. Off by default: the
# operator flips it on once sofia-client-secure's own identity has been
# bootstrapped (identity_manager/bootstrap_sofia_client_secure_identity.py)
# and is ready to present it (see secure_transport.py's own
# identity_challenge_handler wiring).
REQUIRE_IDENTITY_PRESENTATION = os.getenv("REQUIRE_IDENTITY_PRESENTATION", "false").strip().lower() == "true"
_GOVERNING_AUTHORITY_DID = os.getenv("GOVERNING_AUTHORITY_DID", "did:web:sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_HOST = os.getenv("GOVERNING_AUTHORITY_HOST", "sofia.sparkle.example.com")
_GOVERNING_AUTHORITY_AGENT_ID = os.getenv("GOVERNING_AUTHORITY_AGENT_ID", "governing-authority")
_AUTHORITY_VM_ID = f"{_GOVERNING_AUTHORITY_DID}#key-1"


def _build_identity_verifier():
    """
    Builds the coordinator's own RequestPresentationVerifier, or None
    if REQUIRE_IDENTITY_PRESENTATION=false (the default — fully
    disabled, zero behavior change for every existing caller).

    Unlike identity_manager/server.py's own _build_identity_verifier()
    — which only ever needs to trust ONE known caller (the coordinator
    itself) and resolves that single key via a lifespan-deferred step
    (_populate_coordinator_key(), see that module's own comment for
    why the event-loop timing matters there) — this endpoint has no
    fixed set of expected callers: sofia-client-secure today,
    potentially others later. So trusted_issuer_keys only ever needs
    the Governing Authority's own key (to verify an Identity
    Credential's issuer signature); the holder's own key — whoever is
    presenting — is resolved generically via holder_key_resolver
    (issue #42), straight from that agent's own DID Document, with no
    pre-populated per-caller dict to maintain. Both steps here
    (IdentityKeystoreFactory.build() + bootstrap_signing_key()) are
    fully sync and PostgresDIDDocumentStore's pool is lazy (opened on
    first real request, on uvicorn's own loop) — no lifespan deferral
    needed at all.
    """
    if not REQUIRE_IDENTITY_PRESENTATION:
        logger.info("REQUIRE_IDENTITY_PRESENTATION=false — identity_verifier is None, hook fully disabled.")
        return None

    from stls_identity import (
        IdentityKeystoreFactory,
        PostgresDIDDocumentStore,
        RequestPresentationVerifier,
        Sentinel,
        bootstrap_signing_key,
        fetch_remote_credential_status,
        make_did_document_holder_key_resolver,
    )

    keystore = IdentityKeystoreFactory.build()
    _, authority_pub, _ = bootstrap_signing_key(_GOVERNING_AUTHORITY_AGENT_ID, keystore)

    did_store = PostgresDIDDocumentStore(dsn=os.environ.get("IDENTITY_PG_DSN", ""))
    holder_key_resolver = make_did_document_holder_key_resolver(
        did_store, governing_authority_host=_GOVERNING_AUTHORITY_HOST,
    )

    sentinel = Sentinel(
        trusted_issuer_keys={_AUTHORITY_VM_ID: authority_pub},
        holder_key_resolver=holder_key_resolver,
    )
    return RequestPresentationVerifier(sentinel, status_resolver=fetch_remote_credential_status)


def _resolve_base_url() -> str:
    """
    Resolves BASE_URL in order:
      1. BASE_URL environment variable (explicit override / local dev)
      2. GCP metadata server (running Cloud Run instance)
      3. Fallback to localhost (local development without GCP)
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
            "Starting ApiKeyManagerGroup (%d managers: %s)…",
            len(managers),
            ", ".join(managers._managers),
        )
        async with managers:
            yield

    app = FastAPI(title="Multi-Agent KM Server", lifespan=lifespan)
    start_task_store_cleanup(app=app)

    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=8081,
    )

    from agent_coordinator import root_agent as coordinator_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=coordinator_agent,
        key_session_provider=key_session_provider,
        path="/agents/sofia-admin-coordinator/secure-a2a",
        base_url=BASE_URL, # needed to build the agent card with the correct URL
        framework="google-adk",
        # api_key_manager=pg_manager
        identity_verifier=_build_identity_verifier(),
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
        else "disabled (AUTH_PG_DSN not set)"
    )
    _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    _use_redis = os.getenv("AUTH_KM_USE_REDIS", "true").lower() not in ("false", "0", "no")

    print("\n" + "=" * 70)
    print("📡 sofia-admin — Port", PORT)
    print("=" * 70)
    print(f"\n🌐 BASE_URL : {BASE_URL}")
    print(f"\n🔐 Auth backends:")
    print(f"   PostgresLoader → {_pg_status}")
    print(f"   Redis cache    : {'enabled (' + _redis_url + ')' if _use_redis else 'disabled'}")
    print(f"\n🪪 Identity presentation (issue #42): {'REQUIRED' if REQUIRE_IDENTITY_PRESENTATION else 'disabled (REQUIRE_IDENTITY_PRESENTATION=false)'}")
    print(f"\n🤖 Agents:")
    print(f"   sofia-admin   {BASE_URL}/agents/sofia-admin-coordinator/secure-a2a")
    print()

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()