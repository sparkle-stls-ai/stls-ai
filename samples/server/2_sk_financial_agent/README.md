# Semantic Kernel Financial Agent — Secure A2A

A minimal Semantic Kernel agent exposed as a **secure A2A endpoint** via `stls_a2a`. The agent answers questions about stock prices and company information, backed by live data from `yfinance`.

It's the smallest possible demo of `create_secure_a2a_endpoint()`: one FastAPI app, one Semantic Kernel agent, one plugin with two tools.

---

## What it demonstrates

- Wrapping a `semantic_kernel.agents.ChatCompletionAgent` with `stls_a2a.create_secure_a2a_endpoint()`
- A Semantic Kernel plugin (`FinancePlugin`) exposing two `@kernel_function` tools: `get_stock_price` and `get_company_info`
- Both a plain A2A endpoint and an encrypted secure-A2A endpoint served side by side from the same FastAPI app

## Files

| File | Purpose |
|---|---|
| [`server.py`](server.py) | FastAPI app. Creates a `KeySessionProvider` (SERVER side) and mounts the agent at `/agents/sk-agent/secure-a2a` via `create_secure_a2a_endpoint()`. |
| [`semantic_agents.py`](semantic_agents.py) | The Semantic Kernel agent itself: `Kernel` + `OpenAIChatCompletion` service + `FinancePlugin` + `ChatCompletionAgent`. Also runnable standalone (`python semantic_agents.py`) for a quick non-A2A sanity check. |

---

## Prerequisites

- Python 3.11+
- `stls_a2a` installed (editable install from `python/stls-a2a` in this monorepo, or the built wheel — see that package's [README](../../../python/stls-a2a/README.md#installation))
- `semantic-kernel`, `fastapi`, `uvicorn`, `python-dotenv`, `yfinance`

```bash
pip install -e ../../../python/stls-a2a
pip install semantic-kernel fastapi uvicorn python-dotenv yfinance
```

## Configuration

Create a `.env` file in this directory (or edit the existing one):

```bash
KEY_PROVIDER_NAME=base
OPENAI_API_KEY=sk-...
```

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | yes | Used by `OpenAIChatCompletion` (`semantic_agents.py` hardcodes `ai_model_id="gpt-4o"`). |
| `KEY_PROVIDER_NAME` | no (default `base`) | `base`, `arqit_py`, or `pqc` — see the `stls_a2a`/`stls_core` READMEs. `base` is a fixed-key, dev-only provider and needs no key-exchange sidecar, which is what this sample is configured for by default. |

> The `.env` shipped with this sample also defines `GOOGLE_API_KEY`; it's unused by this agent (leftover from copy-pasting across samples) and can be ignored or removed.

## Running it

```bash
python server.py
```

This starts the FastAPI app on port **8002** and exposes:

| Endpoint | Description |
|---|---|
| `GET /.well-known/agent-card.json` | Agent card (discovery) |
| `POST /agents/sk-agent/secure-a2a` | Encrypted A2A endpoint |
| `POST /agents/sk-agent/a2a` | Plain (unencrypted) A2A endpoint — enabled via `enable_plain_endpoint=True` |

### Standalone sanity check (no A2A, no server)

```bash
python semantic_agents.py
```

Runs the agent directly against a single hardcoded prompt ("What's the price of AAPL?") and prints the response — useful to confirm your `OPENAI_API_KEY` and `yfinance` connectivity work before wiring up the A2A layer.

---

## Testing with sofia-client-secure

As usual, the easiest way to drive this agent interactively is the [`sofia-client-secure`](../../client/sofia-client-secure) sample client, which talks to agents exclusively through the encrypted transport.

1. Start this agent server (`python server.py`, port 8002 as above).
2. Register it in `sofia-client-secure`'s `sf_agents_config` table with:
   - `url` → `http://localhost:8002/agents/sk-agent/a2a`
   - `secureUrl` → `http://localhost:8002/agents/sk-agent/secure-a2a`
3. Set `KEY_PROVIDER_NAME=base` on `sofia-client-secure`'s Python secure transport service too (matching this agent) — no key-exchange sidecar is needed with `base`.
4. Follow `sofia-client-secure`'s own README to start its secure transport service, Express backend, and React dev server.
5. Open the chat UI, pick the `sk-agent`, and ask something like "What's the price of AAPL?" or "Tell me about Tesla."
