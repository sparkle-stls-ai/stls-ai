import logging
import uuid
from typing import Optional

import httpx
from dotenv import load_dotenv

from a2a.client import Client, ClientConfig, create_client
from a2a.types import a2a_pb2

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:8004"
AGENT_CARD_URL = f"{BASE_URL}/.well-known/agent-card.json"


class FinancialAgentA2A10Client:
    """
    Client A2A 1.0 spec-compliant per il Financial Agent remoto, basato
    sull'a2a-sdk ufficiale 1.x — NON la stessa major version usata da
    ../a2a_sdk/client_a2a_sdk.py (quella implementa 0.3).

    L'SDK 1.x è una riscrittura con breaking changes: niente più
    A2AClient/TextPart Pydantic — Client/ClientFactory astratti sul
    transport, Message/Part sono protobuf (a2a.types.a2a_pb2), e i
    metodi JSON-RPC sono PascalCase (SendMessage, non message/send).

    Invia chiamate standard A2A 1.0:
      - method:  SendMessage (via Client.send_message, non un metodo
                 "streaming" separato — lo stile di invio è deciso da
                 ClientConfig.streaming, non dal metodo chiamato)
      - parts:   a2a_pb2.Part(text=...) — nessun discriminatore "kind"/
                 "type": il nome del campo oneof selezionato è l'unico
                 discriminatore nella mappatura proto-JSON

    ClientConfig.streaming è fissato alla costruzione del Client (non
    per-call): usa streaming=True per ricevere ogni StreamResponse
    (status_update/artifact_update) non appena emesso dal server invece
    di un singolo evento consolidato — vedi send_streaming().
    """

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        bearer_token: Optional[str] = None,
        streaming: bool = False,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self.bearer_token = bearer_token
        self.streaming = streaming
        self._client: Optional[Client] = None
        self._http: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        self._http = httpx.AsyncClient(timeout=self.timeout, headers=headers)
        config = ClientConfig(httpx_client=self._http, streaming=self.streaming)
        self._client = await create_client(self.base_url, client_config=config)
        logger.info("A2A 1.0 client initialized from AgentCard: %s", AGENT_CARD_URL)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.close()
        if self._http:
            await self._http.aclose()

    async def send(self, text: str, context_id: Optional[str] = None):
        """
        Invia un messaggio bloccante al Financial Agent e restituisce il
        Task finale (a2a_pb2.Task) estratto dall'unico StreamResponse
        emesso in modalità non-streaming.
        """
        if not self._client:
            raise RuntimeError("Client non inizializzato. Usa 'async with'.")

        message = a2a_pb2.Message(
            message_id=str(uuid.uuid4()),
            context_id=context_id or "",
            role=a2a_pb2.ROLE_USER,
            parts=[a2a_pb2.Part(text=text)],
        )
        request = a2a_pb2.SendMessageRequest(message=message)

        logger.info("Sending to Financial Agent: %r (contextId=%s)", text, context_id)
        task = None
        async for event in self._client.send_message(request):
            if event.HasField("task"):
                task = event.task
            elif event.HasField("message"):
                # Oneof's other branch: the agent replied without creating a Task.
                # financial_agent.py always creates one, so this shouldn't hit —
                # kept for completeness with the spec's actual contract.
                return event.message
        if task is None:
            raise RuntimeError("No task/message in SendMessage response")
        logger.info("Task %s completed with status=%s", task.id, task.status)
        return task

    async def send_streaming(self, text: str, context_id: Optional[str] = None):
        """
        Invia un messaggio e itera ogni StreamResponse non appena il
        server lo emette (SSE) — richiede un'istanza costruita con
        streaming=True. Yield un (which, event) per ciascuno, dove
        `which` è 'status_update' o 'artifact_update' (l'esito di
        event.WhichOneof('payload')).
        """
        if not self._client:
            raise RuntimeError("Client non inizializzato. Usa 'async with'.")

        message = a2a_pb2.Message(
            message_id=str(uuid.uuid4()),
            context_id=context_id or "",
            role=a2a_pb2.ROLE_USER,
            parts=[a2a_pb2.Part(text=text)],
        )
        request = a2a_pb2.SendMessageRequest(message=message)

        logger.info("Streaming to Financial Agent: %r (contextId=%s)", text, context_id)
        async for event in self._client.send_message(request):
            yield event.WhichOneof("payload"), event


def _extract_text(task) -> str:
    """Pulls the last artifact's text out of a Task proto message."""
    texts = []
    for artifact in task.artifacts:
        for part in artifact.parts:
            if part.text:
                texts.append(part.text)
    return "\n".join(texts) if texts else ""


async def ask_financial_agent(question: str, context_id: Optional[str] = None) -> str:
    """
    Helper function: invia una domanda al Financial Agent e ritorna il
    testo della risposta come stringa.

    Esempio:
        answer = await ask_financial_agent("What is the AAPL stock price?")
    """
    async with FinancialAgentA2A10Client() as client:
        task = await client.send(question, context_id=context_id)
        return _extract_text(task)


if __name__ == "__main__":
    import asyncio

    async def _demo():
        print("=== Financial Agent A2A client (a2a-sdk 1.x, A2A 1.0 wire format) ===\n")

        # Test 1 — risposta bloccante
        answer = await ask_financial_agent("What is the AAPL stock price?")
        print(f"Answer: {answer}\n")

        # Test 2 — multi-turn con contextId
        ctx = str(uuid.uuid4())
        async with FinancialAgentA2A10Client() as client:
            t1 = await client.send("Tell me about Apple Inc.", context_id=ctx)
            print(f"Turn 1 (history={len(t1.history)}): {_extract_text(t1)}\n")

            t2 = await client.send("And Microsoft?", context_id=ctx)
            print(f"Turn 2 (same context, history={len(t2.history)}): {_extract_text(t2)}\n")

        # Test 3 — streaming reale (SSE)
        print("=== Streaming test ===")
        async with FinancialAgentA2A10Client(streaming=True) as client:
            async for which, event in client.send_streaming("What is the TSLA stock price?"):
                if which == "status_update":
                    print(f"  status_update: state={event.status_update.status.state}")
                elif which == "artifact_update":
                    for part in event.artifact_update.artifact.parts:
                        print(f"  artifact_update: {part.text!r}")

    asyncio.run(_demo())
