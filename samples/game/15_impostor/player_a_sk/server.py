from fastapi import FastAPI
from dotenv import load_dotenv
import uvicorn
import os

load_dotenv()

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

from stls_a2a import create_secure_a2a_endpoint
from stls_a2a import KeySessionProvider
from stls_a2a import KeySessionProviderFactory
from stls_a2a import start_task_store_cleanup

PORT = int(os.getenv("PORT", "8302"))


def start_server():
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=PORT)

    app = FastAPI(title="Player A (Semantic Kernel) — The Impostor")
    start_task_store_cleanup(app=app)

    from agent import agent as sk_agent
    create_secure_a2a_endpoint(
        app=app,
        agent=sk_agent,
        key_session_provider=key_session_provider,
        path="/agents/player-a/secure-a2a",
        base_url=f"http://localhost:{PORT}",
        framework="semantic-kernel",
        enable_plain_endpoint=True,
    )

    print("\n" + "=" * 70)
    print(f"🎭 Player A (Semantic Kernel) — Port {PORT}")
    print("=" * 70)
    print(f"  - Card:       http://localhost:{PORT}/.well-known/agent-card.json")
    print(f"  - Secure A2A: http://localhost:{PORT}/agents/player-a/secure-a2a")

    uvicorn.run(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    start_server()
