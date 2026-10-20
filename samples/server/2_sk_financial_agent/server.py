from fastapi import FastAPI
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import uvicorn
import os

load_dotenv()
CLASSPATH= os.getenv("CLASSPATH")


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
    
    key_session_provider:KeySessionProvider=KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,port=8002)

    # Crea app FastAPI
    app = FastAPI(title="Server1 App")
    # starto l'utiliry per la pulizia del taskstore
    start_task_store_cleanup(app=app)

    
    # monta agente semantic layer
    from semantic_agents import agent as sk_agent
    create_secure_a2a_endpoint(
            app=app,
            agent=sk_agent,
            key_session_provider=key_session_provider,
            path='/agents/sk-agent/secure-a2a',  # ← Endpoint A2A
            base_url="http://localhost:8002",  # ← URL base per gli endpoint
            framework='semantic-kernel',
            enable_plain_endpoint=True
        )
    
    print("\n" + "="*70)
    print("📡 Multi-Agent Server - Porta 8002")
    print("="*70)
    print("\n🌐 Endpoint Globali:")
    print("  - Root - Cards:  http://localhost:8002/.well-known/agent-card.json")
    print("\n🤖 Agente: sk-agent")
    print("  - Secure A2A:    http://localhost:8002/agents/sk-agent/secure-a2a")
    
    uvicorn.run(app, host="0.0.0.0", port=8002)


if __name__ == "__main__":
    start_server()