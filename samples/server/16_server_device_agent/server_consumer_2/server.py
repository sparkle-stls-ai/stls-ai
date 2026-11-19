"""
server.py — server_consumer_2
-------------------------------
One of two replicas sharing ONE Arqit device identity, owned by
../device_agent/ (see this sample's README), via
ARQIT_PY_DEVICE_ROLE=consumer (issue #38). Never calls
KeyProviderServerArqitPy.start() — getSessionKeyIdentifier()/
getSessionKeyData() resolve straight from the Redis-backed store the
device-agent feeds.

Deliberately IDENTICAL code to samples/server/10_server_ms/server_ms_1/
server.py: KeySessionProviderFactory.get_session_provider() already
branches on ARQIT_PY_DEVICE_ROLE by itself — this sample exists to
demonstrate that env-only toggle, not to add any new code path.

Run:
    python server.py
"""

import os

# Anchor Arqit's relative config paths to this script's own directory
# regardless of the cwd the process was launched from — see
# ARQIT_PEERING_NOTES.md in stls-core. Irrelevant in consumer role
# (no ARQIT_PY_SERVER_CONFIG is read here at all), kept for parity
# with every other sample in case the role is ever flipped to "owner".
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
)

PORT = int(os.getenv("PORT", "8112"))
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}").rstrip("/")
AGENT_PATH = os.getenv("AGENT_PATH", "/agents/server-consumer-2/secure-a2a")


def build_app() -> FastAPI:
    from agent import agent

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "%s started on %s (port %d, KEY_PROVIDER_NAME=%s, ARQIT_PY_DEVICE_ROLE=%s)",
            agent.name, BASE_URL, PORT,
            os.getenv("KEY_PROVIDER_NAME", "pqc"),
            os.getenv("ARQIT_PY_DEVICE_ROLE", "owner"),
        )
        yield

    app = FastAPI(title=agent.name, lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # name=None -> reads KEY_PROVIDER_NAME from the environment.
    # ARQIT_PY_DEVICE_ROLE=consumer is read inside this same call
    # (KeySessionProviderFactory.get_session_provider()) — no branch
    # needed here, same code path as any other provider.
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=PORT,
    )

    create_secure_a2a_endpoint(
        app=app,
        agent=agent,
        key_session_provider=key_session_provider,
        base_url=BASE_URL,
        path=AGENT_PATH,
        framework="google-adk",
        enable_plain_endpoint=True,
    )
    return app


app = build_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
