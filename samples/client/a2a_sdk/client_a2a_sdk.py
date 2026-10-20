import logging
import uuid
from typing import Optional

import httpx
from dotenv import load_dotenv

from a2a.client import A2AClient, A2ACardResolver
from a2a.types import (
    Message,
    MessageSendParams,
    MessageSendConfiguration,
    Part,
    Role,
    SendMessageRequest,
    SendStreamingMessageRequest,
    TextPart,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:8002"
AGENT_CARD_URL = f"{BASE_URL}/.well-known/agent.json"


class FinancialAgentA2AClient:
    """
    Client A2A v1.0 spec-compliant per il Financial Agent remoto.
    Usa l'a2a-sdk ufficiale invece di RemoteA2aAgent di Google ADK.

    Invia chiamate standard:
      - method:  messages/send  (non message/send)
      - parts:   [{type: 'text', ...}]  (non kind: 'text')
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        bearer_token: Optional[str] = None,
    ):
        self.base_url = base_url
        self.agent_card_url = f"{base_url}/.well-known/agent.json"
        self.timeout = timeout
        self.bearer_token = bearer_token
        self._client: Optional[A2AClient] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        self._http = httpx.AsyncClient(
            timeout=self.timeout,
            headers=headers,
        )
        resolver = A2ACardResolver(self._http, self.base_url)
        agent_card = await resolver.get_agent_card()
        self._client = A2AClient(self._http, agent_card=agent_card)
        logger.info("A2A client initialized from AgentCard: %s", self.agent_card_url)
        return self

    async def __aexit__(self, *args):
        if self._http:
            await self._http.aclose()

    async def send(
        self,
        text: str,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
        blocking: bool = True,
    ):
        """
        Invia un messaggio al Financial Agent e restituisce la risposta.

        Args:
            text:       Testo della richiesta (es. "What is AAPL stock price?")
            context_id: contextId A2A per conversazioni multi-turn
            task_id:    taskId per idempotenza (opzionale)
            blocking:   Se True, attende il completamento sincrono

        Returns:
            Risposta Task dal server A2A
        """
        if not self._client:
            raise RuntimeError("Client non inizializzato. Usa 'async with'.")

        params = MessageSendParams(
            message=Message(
                messageId=str(uuid.uuid4()),
                role=Role.user,
                parts=[TextPart(type="text", text=text)],
                contextId=context_id,
            ),
            configuration=MessageSendConfiguration(
                blocking=blocking,
            ),
        )

        if task_id:
            params.taskId = task_id

        logger.info("Sending to Financial Agent: %r (contextId=%s)", text, context_id)
        request = SendMessageRequest(id=str(uuid.uuid4()), params=params)
        response = await self._client.send_message(request)
        logger.info("Response received: status=%s", getattr(response, "status", "?"))
        return response

    async def send_streaming(
        self,
        text: str,
        context_id: Optional[str] = None,
    ):
        """
        Invia un messaggio e riceve la risposta in streaming SSE.

        Yields:
            Eventi SSE TaskStatusUpdateEvent / TaskArtifactUpdateEvent
        """
        if not self._client:
            raise RuntimeError("Client non inizializzato. Usa 'async with'.")

        params = MessageSendParams(
            message=Message(
                messageId=str(uuid.uuid4()),
                role=Role.user,
                parts=[TextPart(type="text", text=text)],
                contextId=context_id,
            ),
        )

        logger.info("Streaming to Financial Agent: %r", text)
        request = SendStreamingMessageRequest(id=str(uuid.uuid4()), params=params)
        async for event in self._client.send_message_streaming(request):
            yield event


# ---------------------------------------------------------------------------
# Utilizzo standalone (senza ADK)
# ---------------------------------------------------------------------------

async def ask_financial_agent(
    question: str,
    context_id: Optional[str] = None,
    bearer_token: Optional[str] = None,
) -> str:
    """
    Helper function: invia una domanda al Financial Agent e ritorna il testo
    della risposta come stringa.

    Esempio:
        answer = await ask_financial_agent("What is the AAPL stock price?")
    """
    async with FinancialAgentA2AClient(
        base_url=BASE_URL,
        bearer_token=bearer_token,
    ) as client:
        response = await client.send(question, context_id=context_id)

        # Estrai il testo dagli artifact o dalla history del Task
        task = getattr(response, "result", None) or response
        texts = []

        artifacts = getattr(task, "artifacts", None) or []
        for artifact in artifacts:
            for part in getattr(artifact, "parts", []):
                if hasattr(part, "text"):
                    texts.append(part.text)

        if not texts:
            history = getattr(task, "history", None) or []
            for msg in history:
                if getattr(msg, "role", None) == "agent":
                    for part in getattr(msg, "parts", []):
                        if hasattr(part, "text"):
                            texts.append(part.text)

        return "\n".join(texts) if texts else str(response)


# ---------------------------------------------------------------------------
# Entrypoint di test rapido
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def _demo():
        print("=== Financial Agent A2A v1.0 client ===\n")

        # Test 1 — risposta sincrona
        answer = await ask_financial_agent("What is the AAPL stock price?")
        print(f"Answer: {answer}\n")

        # Test 2 — multi-turn con contextId
        ctx = str(uuid.uuid4())
        async with FinancialAgentA2AClient() as client:
            r1 = await client.send("Tell me about Apple Inc.", context_id=ctx)
            print(f"Turn 1: {r1}\n")

            r2 = await client.send("And Microsoft?", context_id=ctx)
            print(f"Turn 2 (same context): {r2}\n")

        # Test 3 — streaming
        print("=== Streaming test ===")
        async with FinancialAgentA2AClient() as client:
            async for event in client.send_streaming("What is the TSLA stock price?"):
                print(f"SSE event: {event}")

    asyncio.run(_demo())