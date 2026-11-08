from fastapi import FastAPI
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import uvicorn

load_dotenv()

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)


from stls_a2a import create_secure_a2a_endpoint
from stls_a2a import KeySessionProvider
from stls_a2a import KeySessionProviderFactory
from stls_a2a import AgentResponse
from stls_a2a import to_a2a
from stls_a2a import start_task_store_cleanup
from stls_a2a import AuthConfig
from stls_a2a.stls_a2a_card_model import AgentSkill

def start_server():

    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=8004)

    # Create FastAPI app
    app = FastAPI(title="Custom Financial Agent")
    # start the taskstore cleanup utility
    start_task_store_cleanup(app=app)

    # mount the plain Python (no-LLM) agent. financial_agent is directly
    # callable (__call__ adapts AgentRequest -> AgentResponse) and carries
    # .name/.description, which stls_a2a's generic agent-card builder reads —
    # passing it as-is (rather than a bare wrapper function) is what keeps
    # the card from showing up as a generic "function".
    from financial_agent import agent as financial_agent

    path = '/agents/custom-agent/secure-a2a'
    create_secure_a2a_endpoint(
            app=app,
            agent=financial_agent,
            key_session_provider=key_session_provider,
            path=path,  # ← A2A endpoint
            base_url="http://localhost:8004",  # ← base URL for the endpoints
            framework='custom',
            enable_plain_endpoint=True
        )

    # The generic card builder (stls_a2a has no tool/skill introspection for
    # 'custom' agents, unlike the ADK/SK adapters) only fills name/description
    # from the agent object — list the one skill explicitly so the card isn't
    # empty.
    app._a2a_agent_registry[path].skills.append(AgentSkill(
        id="get-stock-price",
        name="get_stock_price",
        description="Looks up a stock ticker's current price via yfinance.",
        tags=["finance"],
    ))

    print("\n" + "="*70)
    print("📡 Custom Financial Agent - Port 8004")
    print("="*70)
    print("\n🌐 Global endpoints:")
    print("  - Root - Cards:  http://localhost:8004/.well-known/agent-card.json")
    print("\n🤖 Agent: custom-agent")
    print("  - Secure A2A:    http://localhost:8004/agents/custom-agent/secure-a2a")

    uvicorn.run(app, host="0.0.0.0", port=8004)


if __name__ == "__main__":
    start_server()
