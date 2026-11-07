# LangChain Financial Agent — Secure A2A

A minimal LangChain agent exposed as a **secure A2A endpoint** via `stls_a2a`. The agent answers questions about stock prices, backed by live data from `yfinance`.

It's the smallest possible demo of `create_secure_a2a_endpoint()` with the `langchain` framework adapter: one FastAPI app, one LangChain agent (`create_agent`), one tool.

---

## What it demonstrates

- Wrapping a LangChain agent (`langchain.agents.create_agent`) with `stls_a2a.create_secure_a2a_endpoint()` using `framework="langchain"`
- A single `@tool`-decorated function, `get_stock_price`
- Both a plain A2A endpoint and an encrypted secure-A2A endpoint served side by side from the same FastAPI app

## Files

| File | Purpose |
|---|---|
| [`server.py`](server.py) | FastAPI app. Creates a `KeySessionProvider` (SERVER side) and mounts the agent at `/agents/lc-agent/secure-a2a` via `create_secure_a2a_endpoint()`. |
| [`langchain_agents.py`](langchain_agents.py) | The LangChain agent itself: `ChatOpenAI` model + `get_stock_price` tool + `create_agent`. Also runnable standalone (`python langchain_agents.py`) for a quick non-A2A sanity check. |

---

## Prerequisites

- Python 3.11+
- `stls_a2a` installed (editable install from `python/stls-a2a` in this monorepo, or the built wheel — see that package's [README](../../../python/stls-a2a/README.md#installation))
- `langchain`, `langchain-openai`, `fastapi`, `uvicorn`, `python-dotenv`, `yfinance`

```bash
pip install -e ../../../python/stls-a2a
pip install langchain langchain-openai fastapi uvicorn python-dotenv yfinance
```

## Configuration

Copy `.env.example` to `.env` and fill in `OPENAI_API_KEY`:

```bash
cp .env.example .env
```

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | yes | Used by `ChatOpenAI` (`langchain_agents.py` hardcodes `model="gpt-4o"`). |
| `KEY_PROVIDER_NAME` | no (default `base`) | `base`, `arqit_py`, or `pqc` — see the `stls_a2a`/`stls_core` READMEs. `base` is a fixed-key, dev-only provider and needs no key-exchange sidecar, which is what this sample is configured for by default. |

## Running it

```bash
python server.py
```

This starts the FastAPI app on port **8001** and exposes:

| Endpoint | Description |
|---|---|
| `GET /.well-known/agent-card.json` | Agent card (discovery) |
| `POST /agents/lc-agent/secure-a2a` | Encrypted A2A endpoint |
| `POST /agents/lc-agent/a2a` | Plain (unencrypted) A2A endpoint — enabled via `enable_plain_endpoint=True` |

### Standalone sanity check (no A2A, no server)

```bash
python langchain_agents.py
```

Runs the agent directly against a single hardcoded prompt ("What is the price of AAPL?") and prints the response — useful to confirm your `OPENAI_API_KEY` and `yfinance` connectivity work before wiring up the A2A layer.

---

## Testing with sofia-client-secure

As usual, the easiest way to drive this agent interactively is the [`sofia-client-secure`](../../client/sofia-client-secure) sample client, which talks to agents exclusively through the encrypted transport.

1. Start this agent server (`python server.py`, port 8001 as above).
2. Register it in `sofia-client-secure`'s `sf_agents_config` table with:
   - `url` → `http://localhost:8001/agents/lc-agent/a2a`
   - `secureUrl` → `http://localhost:8001/agents/lc-agent/secure-a2a`
3. Set `KEY_PROVIDER_NAME=base` on `sofia-client-secure`'s Python secure transport service too (matching this agent) — no key-exchange sidecar is needed with `base`.
4. Follow `sofia-client-secure`'s own README to start its secure transport service, Express backend, and React dev server.
5. Open the chat UI, pick the `lc-agent`, and ask something like "What's the price of AAPL?" or "What's TSLA trading at?"

## Testing with the `a2a_sdk` client

The [`a2a_sdk`](../../client/a2a_sdk) sample client talks to the **plain** endpoint and is the quickest way to spec-check this agent without the full `sofia-client-secure` stack — just point `BASE_URL` in `client_a2a_sdk.py` at `http://localhost:8001` instead of its default `:8002`.
