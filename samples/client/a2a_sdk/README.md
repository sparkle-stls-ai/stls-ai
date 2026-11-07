# a2a_sdk — official A2A SDK client (plain, unencrypted)

A minimal client for the Financial Agent sample, built on the **official
`a2a-sdk`** (`A2AClient` / `A2ACardResolver`) instead of Google ADK's
`RemoteA2aAgent` or `stls_a2a`'s secure transport.

It talks the plain, unencrypted A2A endpoint — useful as a spec-compliance
sanity check (standard `message/send` JSON-RPC calls, standard
`{kind: "text"}` parts) and as a baseline to compare against the secure
transport used elsewhere in this repo.

## What it demonstrates

- Resolving an Agent Card with `A2ACardResolver` and building an `A2AClient` from it
- Sending a blocking message (`send`), a multi-turn conversation via `contextId`, and a streaming request (`send_streaming`)
- Optional `Authorization: Bearer <token>` header support (`bearer_token=`), unused against the sample server below

## Prerequisites

- Python 3.11+, the shared root venv (`stls-ai/.venv`) activated
- `a2a-sdk`, `httpx`, `python-dotenv` installed in that venv

```bash
pip install a2a-sdk httpx python-dotenv
```

- The [`2_sk_financial_agent`](../../server/2_sk_financial_agent) sample server running on port 8002 (its **plain** A2A endpoint — this client does not encrypt anything):

```bash
cd samples/server/2_sk_financial_agent
python server.py
```

## Configuration

`BASE_URL` is hardcoded to `http://localhost:8002` at the top of
`client_a2a_sdk.py` — edit it there if the server runs elsewhere. The
agent card is fetched from `{BASE_URL}/.well-known/agent.json`.

## Running it

```bash
cd samples/client/a2a_sdk
python client_a2a_sdk.py
```

The `__main__` block runs three tests against the Financial Agent in sequence:

1. **Blocking send** — `ask_financial_agent("What is the AAPL stock price?")`, prints the extracted text answer.
2. **Multi-turn** — two `send()` calls sharing one `context_id`, to confirm the server carries conversation state across turns.
3. **Streaming** — `send_streaming("What is the TSLA stock price?")`, prints each SSE event (`TaskStatusUpdateEvent` / `TaskArtifactUpdateEvent`) as it arrives.

## Using it programmatically

```python
from client_a2a_sdk import ask_financial_agent, FinancialAgentA2AClient

answer = await ask_financial_agent("What is the AAPL stock price?")

async with FinancialAgentA2AClient() as client:
    response = await client.send("Tell me about Apple Inc.", context_id="some-id")
```
