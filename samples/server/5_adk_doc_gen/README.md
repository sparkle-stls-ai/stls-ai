# ADK Document & Chart Generator Agent — Secure A2A

A Google ADK agent exposed as a **secure A2A endpoint** via `stls_a2a`. The
agent searches the web and generates documents (`.docx`, `.xlsx`, `.md`,
`.pdf`) and charts (`.png`, interactive `.html`, `.pdf`) from the results,
attaching the generated files directly to the A2A response as artifacts.

## What it demonstrates

- An ADK root agent (`docs_agent`) delegating web search to a sub-agent (`search_agent`, via `AgentTool` + `google_search`) and calling 7 document/chart `FunctionTool`s
- `before_model_callback`/`after_model_callback` hooks (`strip_binary_parts`, `inject_document_artifacts`) that strip MIME types Gemini can't ingest from history, and inject newly generated files as response `Part`s
- Wrapping the agent with `stls_a2a.create_secure_a2a_endpoint()`, with `KEY_PROVIDER_NAME=arqit_py` by default (not `base`/`pqc` — see Configuration below)
- Postgres-backed API key auth (`ApiKeyManager` + `PostgresLoader`), optional and disabled unless `AUTH_PG_DSN` is set
- A Redis-backed task store (`TASK_STORE_BACKEND=redis`)

## Files

| File | Purpose |
|---|---|
| [`server.py`](server.py) | FastAPI app. Builds the `arqit_py` `KeySessionProvider` (SERVER side), mounts the agent at `/agents/docs-generator/secure-a2a`, and exposes `/auth/health` + `/auth/refresh` for the Postgres-backed API key manager. |
| [`agent_docs.py`](agent_docs.py) | The ADK agent itself: `root_agent` (`docs_agent`) + `search_agent` sub-agent, instructions, and the model callbacks. |
| [`document_tools.py`](document_tools.py) | The 7 `FunctionTool`s (document/chart builders) plus the ADK artifact save/track plumbing (`_save_and_track`). |
| `aq-sf-agent-server5*.json` | This agent's provisioned Arqit device files (config/metadata/policy/properties) — see `python/stls-core/README.md` for what each one is. |

## Prerequisites

- Python 3.11+, the shared root venv (`stls-ai/.venv`) activated, with `stls_core`+`stls_a2a` installed editable and the `[arqit_py]` extra — see `python/stls-a2a/README.md#installation`
- `google-adk`, `fastapi`, `uvicorn`, `python-dotenv`
- `python-docx`, `openpyxl`, `matplotlib`, `plotly`, `reportlab` (document/chart generation)
- Redis reachable at `REDIS_URL` (task store backend; set `TASK_STORE_BACKEND=memory` to skip it)
- A Gemini key/project for `google-adk` (`GOOGLE_API_KEY`, or `GOOGLE_GENAI_USE_VERTEXAI=TRUE` + ADC for Vertex)
- Optional: a reachable Postgres (`AUTH_PG_DSN`) if you want Postgres-backed API key auth instead of the legacy static config — leave it empty to disable

```bash
pip install google-adk fastapi uvicorn python-dotenv python-docx openpyxl matplotlib plotly reportlab
```

## Configuration

This sample ships its own `.env` (and a secret-free `.env.example` if you
need to recreate it) — edit the values that matter for your environment
rather than creating one from scratch:

| Variable | Default here | Notes |
|---|---|---|
| `PORT` / `BASE_URL` | `8005` / `http://localhost:${PORT}` | `BASE_URL` must be set (absolute) — ADK's `RemoteA2aAgent` rejects a relative card `url` otherwise. |
| `AGENT_MODEL` | `gemini-3.5-flash` | Passed straight to the ADK `Agent`. |
| `KEY_PROVIDER_NAME` | `arqit_py` | **Not `base`** — this sample is provisioned with real Arqit device files and expects the key-exchange sidecar running. Switch to `base` for a zero-dependency dev run (no sidecar needed) or `pqc` for the hybrid KEM provider. |
| `ARQIT_PY_SERVER_CONFIG` | path to `aq-sf-agent-server5.json` | Only used when `KEY_PROVIDER_NAME=arqit_py`. |
| `KEY_SESSION_PORT` | `8081` | Port the key-session provider listens on. |
| `TASK_STORE_BACKEND` | `redis` | `memory` to skip Redis entirely. |
| `AUTH_PG_DSN` | set | Leave empty to disable Postgres-backed API key auth (falls back to legacy static `AuthConfig`). |
| `GOOGLE_API_KEY` / `GOOGLE_GENAI_USE_VERTEXAI` | — | Gemini credentials for `google-adk`. |

## Running it

If using `arqit_py` (the shipped default), start the key-exchange sidecar first — see `python/stls-core/README.md` / `sidecar/`. Then:

```bash
cd samples/server/5_adk_doc_gen
python server.py
```

This starts the FastAPI app on port **8005** (per `.env`) and exposes:

| Endpoint | Description |
|---|---|
| `GET /.well-known/agent-card.json` | Agent card (discovery) |
| `POST /agents/docs-generator/secure-a2a` | Encrypted A2A endpoint (only endpoint mounted — no plain `/a2a` sibling here) |
| `GET /auth/health` | Health of the Postgres/Redis-backed API key manager group |
| `POST /auth/refresh` | Force a refresh of all API key managers |

### Switching to `base` for a quick local run (no Arqit device/sidecar)

```bash
KEY_PROVIDER_NAME=base python server.py
```

## Testing with sofia-client-secure

1. Start this agent server (`python server.py`, port 8005 as above).
2. Register it in `sofia-client-secure`'s `sf_agents_config` table with `secureUrl` → `http://localhost:8005/agents/docs-generator/secure-a2a` (no plain `url` — this agent only exposes the encrypted endpoint).
3. Match `KEY_PROVIDER_NAME` (and, for `arqit_py`, the sidecar) on `sofia-client-secure`'s Python secure transport service.
4. Follow `sofia-client-secure`'s own README to start its services, open the chat UI, pick `docs-generator`, and ask e.g. "Search for Apple's last 4 quarterly revenues and generate a PDF report with a bar chart" or "Create an Excel sheet comparing Tesla and Ford stock performance this year."
5. Generated files come back as attached artifacts in the response — the debug drawer (or any A2A client that renders `Part.inline_data`) lets you download them.

## Available tools (`document_tools.py`)

| Tool | Output | Key args |
|---|---|---|
| `generate_docx_report` | `.docx` | `title`, `sections` |
| `generate_xlsx_report` | `.xlsx` | `title`, `sheets` |
| `generate_md_report` | `.md` | `title`, `sections` |
| `generate_pdf_report` | `.pdf` (text + embedded charts) | `title`, `sections`, `charts` |
| `generate_chart_png` | `.png` | `title`, `chart_type`, `categories`, `series` |
| `generate_chart_html` | interactive Plotly `.html` | `title`, `chart_type`, `categories`, `series` |
| `generate_chart_pdf` | `.pdf` (chart only) | `title`, `chart_type`, `categories`, `series` |

`sections`/`sheets`/`charts` structures are documented in `root_agent`'s
instruction string in [`agent_docs.py`](agent_docs.py) — the agent is
prompted with the exact JSON shapes it must pass to each tool.
