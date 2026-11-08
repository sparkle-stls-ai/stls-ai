# Custom Financial Agent — Secure A2A (no LLM)

A minimal **plain Python class** — no LLM, no model call, no API key — exposed as a **secure A2A endpoint** via `stls_a2a`. The agent answers questions about stock prices, backed by live data from `yfinance`, using deterministic regex-based ticker extraction instead of an LLM.

It's the smallest possible demo of `create_secure_a2a_endpoint()` with the `custom` framework adapter: one FastAPI app, one plain Python class, one thin async wrapper function. Same pattern as `python/stls-a2a/tests/mock_agent.py` + `multi_agent_server.py`.

---

## What it demonstrates

- Wrapping an arbitrary Python object (not an AI framework agent) with `stls_a2a.create_secure_a2a_endpoint()` using `framework="custom"`
- `CustomFunctionAdapter` only needs an async callable `(request: AgentRequest) -> AgentResponse`; here that's a small wrapper around `FinancialAgent.handle()`
- Both a plain A2A endpoint and an encrypted secure-A2A endpoint served side by side from the same FastAPI app
- The only one of the `*_financial_agent` samples that needs **zero LLM credentials** and makes **zero model calls**

## Files

| File | Purpose |
|---|---|
| [`server.py`](server.py) | FastAPI app. Creates a `KeySessionProvider` (SERVER side) and mounts the agent at `/agents/custom-agent/secure-a2a` via `create_secure_a2a_endpoint(framework='custom')`. The `financial_handler` wrapper extracts the text from the A2A message and calls `FinancialAgent.handle()`. |
| [`financial_agent.py`](financial_agent.py) | The agent itself: `FinancialAgent`, a plain class with `extract_ticker()` (regex-based) and `get_stock_price()` (`yfinance` lookup). No LLM, no framework. Also runnable standalone (`python financial_agent.py`) for a quick sanity check. |

---

## How ticker extraction works (no LLM)

`FinancialAgent.extract_ticker()` tries, in order:

1. A cashtag: `$AAPL`
2. An explicit key-value: `ticker: AAPL` or `ticker=AAPL`
3. Fallback: the last 1–5 letter all-caps token in the text that isn't a common word (e.g. "What is the price of AAPL?" → `AAPL`), filtered against a small stopword list (`THE`, `FOR`, `IS`, `OF`, ...)

If nothing matches, the agent returns a short usage hint instead of an error — there's no model to fall back on to interpret an ambiguous query, so unmatched input is handled explicitly. This is a deliberately simple heuristic, not NLP — unusual phrasing or lowercase-only tickers won't be recognized; use `$ticker` or `ticker: XXX` for anything the free-text fallback might miss.

## Prerequisites

- Python 3.11+
- `stls_a2a` installed (editable install from `python/stls-a2a` in this monorepo, or the built wheel — see that package's [README](../../../python/stls-a2a/README.md#installation))
- `fastapi`, `uvicorn`, `python-dotenv`, `yfinance`

```bash
pip install -e ../../../python/stls-a2a
pip install fastapi uvicorn python-dotenv yfinance
```

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Notes |
|---|---|---|
| `KEY_PROVIDER_NAME` | no (default `base`) | `base`, `arqit_py`, or `pqc` — see the `stls_a2a`/`stls_core` READMEs. `base` is a fixed-key, dev-only provider and needs no key-exchange sidecar, which is what this sample is configured for by default. |

No LLM variable is needed — there's no model call in this sample.

## Running it

```bash
python server.py
```

This starts the FastAPI app on port **8004** and exposes:

| Endpoint | Description |
|---|---|
| `GET /.well-known/agent-card.json` | Agent card (discovery) |
| `POST /agents/custom-agent/secure-a2a` | Encrypted A2A endpoint |
| `POST /agents/custom-agent/a2a` | Plain (unencrypted) A2A endpoint — enabled via `enable_plain_endpoint=True` |

### Standalone sanity check (no A2A, no server)

```bash
python financial_agent.py
```

Runs `FinancialAgent.handle()` directly against a single hardcoded prompt ("What is the price of AAPL?") and prints the response — useful to confirm `yfinance` connectivity works before wiring up the A2A layer.

---

## Testing with sofia-client-secure

As usual, the easiest way to drive this agent interactively is the [`sofia-client-secure`](../../client/sofia-client-secure) sample client, which talks to agents exclusively through the encrypted transport.

1. Start this agent server (`python server.py`, port 8004 as above).
2. Register it in `sofia-client-secure`'s `sf_agents_config` table with:
   - `url` → `http://localhost:8004/agents/custom-agent/a2a`
   - `secureUrl` → `http://localhost:8004/agents/custom-agent/secure-a2a`
3. Set `KEY_PROVIDER_NAME=base` on `sofia-client-secure`'s Python secure transport service too (matching this agent) — no key-exchange sidecar is needed with `base`.
4. Follow `sofia-client-secure`'s own README to start its secure transport service, Express backend, and React dev server.
5. Open the chat UI, pick the `custom-agent`, and ask something like "What's the price of AAPL?" or "$TSLA" — remember, there's no LLM here, so stick to one of the three recognized patterns above.

## Testing with the `a2a_sdk` client

The [`a2a_sdk`](../../client/a2a_sdk) sample client talks to the **plain** endpoint and is the quickest way to spec-check this agent without the full `sofia-client-secure` stack — just point `BASE_URL` in `client_a2a_sdk.py` at `http://localhost:8004` instead of its default `:8002`.
