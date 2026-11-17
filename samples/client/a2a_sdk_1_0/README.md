# a2a_sdk_1_0 — official A2A SDK client, A2A 1.0 wire format

A minimal client for the [Custom Financial Agent](../../server/4_custom_financial_agent)
sample, built on the **official `a2a-sdk` 1.x** (`Client` / `create_client`),
the major version that implements the **A2A 1.0** spec — not 0.3, which is
what [`../a2a_sdk`](../a2a_sdk) uses.

This is the real third-party SDK interop check for issue #14's second half
("test against more complex agents"): it exercises our server's A2A 1.0
support against a genuine protobuf-based SDK client, not just our own
hand-rolled JSON-RPC requests. Running it against the real server is what
found and confirmed four previously-undetected conformance gaps (now
fixed — see `CHANGELOG.md` in `python/stls-a2a`), which the hand-rolled
checks had missed because they only checked *which* fields were present,
not whether they were wrapped the way a strict proto-JSON client expects.

## Why this needs its own venv

`a2a-sdk` 1.x is a breaking rewrite of 0.3's API — `A2AClient`/`TextPart`
are gone, replaced by a transport-agnostic `Client`/`ClientFactory` built
on raw protobuf (`a2a.types.a2a_pb2`). It cannot coexist with the 0.3.26
install already in the shared root venv (`stls-ai/.venv`), which
[`../a2a_sdk`](../a2a_sdk) depends on. Use a **separate, isolated venv**
for this sample.

```bash
python3 -m venv .venv-a2a-1.0
source .venv-a2a-1.0/bin/activate
pip install "a2a-sdk>=1.0.0" httpx python-dotenv
```

## What it demonstrates

- Resolving an Agent Card and building a `Client` via `create_client(base_url, client_config=...)` — the transport (JSON-RPC, in this server's case) is auto-selected from the card's `supportedInterfaces` array (A2A 1.0 spec §4.4.1)
- Sending a blocking message (`send`) and a multi-turn conversation via `contextId`
- Real SSE streaming (`send_streaming`, `ClientConfig(streaming=True)`) — each `StreamResponse` arrives as a `status_update` or `artifact_update`, with `status.state` as the proto's `TASK_STATE_*` enum (`TASK_STATE_SUBMITTED` → `TASK_STATE_WORKING` → ... → `TASK_STATE_COMPLETED`) — the only completion signal a strict client has, since the 1.0 proto has no separate `"final"` field at all
- Building requests/parts as raw protobuf messages (`a2a_pb2.Message`, `a2a_pb2.Part(text=...)`) — there's no Pydantic `TextPart` in this SDK major version

Streaming was broken the first time this sample was written (`ClientConfig(streaming=True)` got back empty events — no oneof field set at all) — fixed server-side; see `CHANGELOG.md` in `python/stls-a2a` and issue #14 for what that involved.

## Prerequisites

- Python 3.11+, this sample's own isolated venv (see above) activated
- The [`4_custom_financial_agent`](../../server/4_custom_financial_agent) sample server running on port 8004 — in the **shared root venv** (`stls-ai/.venv`), not this sample's isolated one:

```bash
cd samples/server/4_custom_financial_agent
source ../../../.venv/bin/activate
python server.py
```

This agent is deterministic and LLM-free (regex ticker extraction +
live `yfinance` lookup, no API key needed) — same reason
[`../a2a_sdk`](../a2a_sdk) uses `2_sk_financial_agent` but this sample
doesn't: that one needs real OpenAI/Google API keys configured, which
would make a protocol-level interop check dependent on paid external
calls for no reason.

## Configuration

`BASE_URL` is hardcoded to `http://localhost:8004` at the top of
`client_a2a_sdk_1_0.py` — edit it there if the server runs elsewhere.
The agent card is fetched from `{BASE_URL}/.well-known/agent-card.json`.

## Running it

```bash
cd samples/client/a2a_sdk_1_0
source .venv-a2a-1.0/bin/activate   # NOT the shared root venv
python client_a2a_sdk_1_0.py
```

The `__main__` block runs three tests against the Financial Agent in sequence:

1. **Blocking send** — `ask_financial_agent("What is the AAPL stock price?")`, prints the real looked-up price.
2. **Multi-turn** — two `send()` calls sharing one `context_id`, to confirm the server carries `contextId` across turns (both replies are the agent's generic fallback — `financial_agent.py`'s regex doesn't recognize "Apple Inc."/"Microsoft" as tickers; this turn verifies the context plumbing, not ticker extraction).
3. **Streaming** — `send_streaming("What is the TSLA stock price?")` on a `streaming=True` client, printing each `status_update`/`artifact_update` as it arrives over SSE.

## Using it programmatically

```python
from client_a2a_sdk_1_0 import ask_financial_agent, FinancialAgentA2A10Client

answer = await ask_financial_agent("What is the AAPL stock price?")

async with FinancialAgentA2A10Client() as client:
    task = await client.send("Tell me about Apple Inc.", context_id="some-id")
```
