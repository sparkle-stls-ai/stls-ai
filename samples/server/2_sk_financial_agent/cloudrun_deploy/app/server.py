"""
server.py – STLS-A2A / Semantic Kernel agent server.
Cloud Run compatible: binds to $PORT (default 8080).
"""
from fastapi import FastAPI
from dotenv import load_dotenv
import uvicorn
import os

load_dotenv()

CLASSPATH = os.getenv("CLASSPATH")
PORT      = int(os.getenv("PORT", "8080"))
HOST      = os.getenv("HOST", "0.0.0.0")

# Cloud Run exposes the service URL via the K_SERVICE env var and the
# metadata server.  We derive BASE_URL automatically so no external
# config is required.  Fallback: localhost (useful for local dev).
def _resolve_base_url() -> str:
    # 1. Explicit override (local dev / testing)
    if url := os.getenv("BASE_URL"):
        return url
    # 2. Cloud Run metadata server
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/attributes/base-url",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as r:
            return r.read().decode().strip()
    except Exception:
        pass
    # 3. Best-effort: K_SERVICE is set by Cloud Run but the plain URL
    #    is not directly available in env; fall back to localhost.
    return f"http://localhost:{PORT}"

BASE_URL = _resolve_base_url()

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)
logger.info(f"classpath={CLASSPATH}")
logger.info(f"Binding on {HOST}:{PORT}  base_url={BASE_URL}")


from stls_a2a import (
    create_secure_a2a_endpoint,
    KeySessionProvider,
    KeySessionProviderFactory,
    AgentResponse,
    to_a2a,
    start_task_store_cleanup,
    AuthConfig,
)


def start_server() -> None:
    key_session_provider: KeySessionProvider = (
        KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            port=PORT,
        )
    )

    app = FastAPI(title="STLS-A2A Server")
    start_task_store_cleanup(app=app)

    # ── Semantic Kernel agent ──────────────────────────────────
    from semantic_agents import agent as sk_agent

    create_secure_a2a_endpoint(
        app=app,
        agent=sk_agent,
        key_session_provider=key_session_provider,
        path="/agents/sk-agent/secure-a2a",
        base_url=BASE_URL,
        framework="semantic-kernel",
        enable_plain_endpoint=True,
    )

    # ── Startup banner ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"📡  STLS-A2A Server  –  port {PORT}")
    print("=" * 70)
    print(f"\n🌐  Agent Card : {BASE_URL}/.well-known/agent-card.json")
    print(f"🤖  sk-agent   : {BASE_URL}/agents/sk-agent/secure-a2a")
    print()

    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    start_server()