"""
server.py — server_ms_1
------------------------
One of the two servers used by the "1 client -> N servers" test with
SecureSessionManager (see samples/client/10_client_ms/client_multi_server.py)
and by the "1 device -> N concurrent sessions" test (see
samples/client/10_client_ms/client_multi_session.py).

Works, unmodified, with KEY_PROVIDER_NAME=base|pqc|arqit_py — the
provider choice lives entirely in this directory's .env.

Run:
    python server.py
"""

import os

# Anchor Arqit's relative config paths (DEVICE_METADATA_FILEPATH, etc.)
# to this script's own directory, regardless of the cwd the process was
# launched from — see ARQIT_PEERING_NOTES.md in stls-core.
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

PORT = int(os.getenv("PORT", "8101"))
BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}").rstrip("/")
AGENT_PATH = os.getenv("AGENT_PATH", "/agents/server-ms-1/secure-a2a")


def build_app() -> FastAPI:
    from agent import agent

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "%s started on %s (port %d, KEY_PROVIDER_NAME=%s)",
            agent.name, BASE_URL, PORT, os.getenv("KEY_PROVIDER_NAME", "pqc"),
        )
        yield

    app = FastAPI(title=agent.name, lifespan=lifespan)
    start_task_store_cleanup(app=app)

    # name=None -> reads KEY_PROVIDER_NAME from the environment: works
    # identically for base/pqc/arqit_py, no per-provider branch here.
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
