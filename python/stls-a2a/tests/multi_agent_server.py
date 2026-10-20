from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
import os

import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)
backend = os.getenv("TASK_STORE_BACKEND", "memory").lower().strip()
logger.info(f'Task Store Backend: {backend}')

from stls_a2a import create_secure_a2a_endpoint
from stls_a2a import KeySessionProvider
from stls_a2a import KeySessionProviderFactory
from stls_a2a import AgentResponse
from stls_a2a import to_a2a
from stls_a2a import start_task_store_cleanup
from stls_a2a import AuthConfig


def multi_agent_example():
    
    key_session_provider:KeySessionProvider=KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,port=8081)

    # Crea app FastAPI
    app = FastAPI(title="Multi-Agent Server")
    # starto l'utiliry per la pulizia del taskstore
    start_task_store_cleanup(app=app)

    # Crea agent "calculator"
    from mock_agent import MockAgent
    
    calculator = MockAgent(
        name="calculator",
        system_instruction="Performs mathematical calculations"
    )
    
    # Wrapper
    async def calculator_handler(request):
        result = await calculator.process_message(
            request.method,
            request.params
        )
        return AgentResponse(result=result, id=request.id)
    
    # Monta endpoint A2A
    create_secure_a2a_endpoint(
        app=app,
        agent=calculator_handler,
        key_session_provider=key_session_provider,
        path='/agents/calculator/secure-a2a',  # ← Endpoint A2A
        framework='custom'
    )

    # monta agent adk google
    from adk_google_agents import agent as adk_agent
    
    create_secure_a2a_endpoint(
            app=app,
            agent=adk_agent,
            key_session_provider=key_session_provider,
            path='/agents/adk-agent/secure-a2a',  # ← Endpoint A2A
            framework='google-adk',
            enable_plain_endpoint=True
        )

    # monta agent langchain
    from langchain_agents import agent as lc_agent
    create_secure_a2a_endpoint(
            app=app,
            agent=lc_agent,
            key_session_provider=key_session_provider,
            path='/agents/lc-agent/secure-a2a',  # ← Endpoint A2A
            framework='langchain',
            auth=AuthConfig(
                bearer_tokens={"my-secret-token"},
                api_keys={"key-abc123"},
                realm="LangChain Agent")
        )
    
    # monta agente semantic layer
    from semantic_agents import agent as sk_agent
    create_secure_a2a_endpoint(
            app=app,
            agent=sk_agent,
            key_session_provider=key_session_provider,
            path='/agents/sk-agent/secure-a2a',  # ← Endpoint A2A
            framework='semantic-kernel'
        )
    
    # monta invoices classifier
    from invoices_agents import invoices_classifier_agent
    create_secure_a2a_endpoint(
            app=app,
            agent=invoices_classifier_agent,
            key_session_provider=key_session_provider,
            path='/agents/invoices-classifier/secure-a2a',  # ← Endpoint A2A
            framework='google-adk'
        )

    print("\n" + "="*70)
    print("📡 Multi-Agent Server - Porta 8001")
    print("="*70)
    print("\n🌐 Endpoint Globali:")
    print("  - Root - Cards:  http://localhost:8001/.well-known/agent-card.json")
    print("\n🤖 Agente: calculator")
    print("  - Secure A2A:    http://localhost:8001/agents/calculator/secure-a2a")
    print("\n🤖 Agente: adk-agent")
    print("  - Secure A2A:    http://localhost:8001/agents/adk-agent/secure-a2a")
    print("\n🤖 Agente: lc-agent")
    print("  - Secure A2A:    http://localhost:8001/agents/lc-agent/secure-a2a")
    print("\n🤖 Agente: sk-agent")
    print("  - Secure A2A:    http://localhost:8001/agents/sk-agent/secure-a2a")
    print("\n🤖 Agente: invoices-classifier")
    print("  - Secure A2A:    http://localhost:8001/agents/invoices-classifier/secure-a2a")
    uvicorn.run(app, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    multi_agent_example()