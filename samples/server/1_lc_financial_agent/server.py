from fastapi import FastAPI
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import uvicorn
import os

load_dotenv()
CLASSPATH = os.getenv("CLASSPATH")


import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)
logger.info(f'classpath={CLASSPATH}')


from stls_a2a import create_secure_a2a_endpoint
from stls_a2a import KeySessionProvider
from stls_a2a import KeySessionProviderFactory
from stls_a2a import AgentResponse
from stls_a2a import to_a2a
from stls_a2a import start_task_store_cleanup
from stls_a2a import AuthConfig

def start_server():

    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=8001)

    # Create FastAPI app
    app = FastAPI(title="LangChain Financial Agent")
    # start the taskstore cleanup utility
    start_task_store_cleanup(app=app)

    # mount the langchain agent
    from langchain_agents import agent as lc_agent
    create_secure_a2a_endpoint(
            app=app,
            agent=lc_agent,
            key_session_provider=key_session_provider,
            path='/agents/lc-agent/secure-a2a',  # ← A2A endpoint
            base_url="http://localhost:8001",  # ← base URL for the endpoints
            framework='langchain',
            enable_plain_endpoint=True
        )

    print("\n" + "="*70)
    print("📡 LangChain Financial Agent - Port 8001")
    print("="*70)
    print("\n🌐 Global endpoints:")
    print("  - Root - Cards:  http://localhost:8001/.well-known/agent-card.json")
    print("\n🤖 Agent: lc-agent")
    print("  - Secure A2A:    http://localhost:8001/agents/lc-agent/secure-a2a")

    uvicorn.run(app, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    start_server()
