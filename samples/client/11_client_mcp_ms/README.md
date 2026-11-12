# 11_client_mcp_ms — multi-server MCP client test

The MCP analogue of
[`samples/client/10_client_ms/client_agent_multi_server.py`](../10_client_ms/README.md):
validates `stls_mcp.SecureToolsetManager` — the stls-mcp twin of
`stls_a2a.SecureSessionManager`. Both are built on stls-core's non-leaking
session-establishment API (`stls_core.establish_session()` here,
`stls_core.resolve_transport()` on the A2A side — see
`python/stls-mcp/CHANGELOG.md` and `python/stls-core/CHANGELOG.md`).

This directory has two scripts:

| Script | Tests |
|---|---|
| `client_multi_server.py` | 1 client -> N different MCP servers, via `SecureToolsetManager` (necessarily through ADK) |
| `client_multi_session.py` | 1 device -> N concurrent MCP sessions toward 1 server — ADK-free, raw `mcp` SDK `ClientSession` (mirrors `python/stls-mcp/tests/vault_client.py`'s pattern, no LLM/API key needed) |

`client_multi_session.py` asserts **unique `session_id`s for every
provider including `base`**, unlike the stls-a2a version of this test:
MCP always does a real server-side handshake that mints a fresh
`session_id`, unlike stls-a2a's lazy `base`/`arqit_py` scheme where the
client-resolved `kid` **is** the `session_id`. Confirmed empirically
(`base`: 6/6 unique session_ids; `pqc`: 6/6; `arqit_py`: 6/6, real
device, real cloud).

**History worth knowing, now fully resolved (2026-10-23).** The
original `arqit_py` pass here was never proof of thread-safety on its
own: `KeyProviderClientArqitPy.start()` is a plain blocking
synchronous method with no `await` inside it, so `asyncio.gather()`
serialized it for free — it never actually raced. The path that
*did* run this under real OS-thread concurrency,
`samples/client/12_client_mcp_ms/client_multi_session.ts` (via the
Node sidecar's `run_in_executor()`), originally failed from a real
`DeviceMetadata`/`AuthenticationClient` singleton race in the Arqit
SDK — root-caused and fixed with a process-wide serialization lock in
`stls_core_providers_arqit_py.py`. That fix then exposed a *second*,
independent bug once this script was re-run under genuine
`asyncio.gather()` concurrency (now meaningful, since `start()` is no
longer accidentally serialized either): the server resolved its kid
via plain FIFO with no correlation to which incoming handshake it was
for, so concurrent handshakes against the same long-running server
could get cross-wired — "Invalid proof of possession" for a client
whose key got handed to a *different* session. Fixed by having the
client send its own already-resolved kid as a hint
(`HandshakeRequest.kid`) so arqit_py resolves that exact key directly
(see `python/stls-mcp/README.md`'s "Known limitations" section).
**Confirmed with this exact script, genuinely concurrent**: 3/3 and
6/6 sessions, 0 errors, each handshake resolving in ~0.1-0.2s. See
`python/stls-a2a/README.md`'s "Current Limitations and Notes" for the
first fix's full write-up.

Unlike stls-a2a's `SecureSessionManager` (which has a low-level,
ADK-free calling path), `STLSMCPToolset` *is* an ADK `MCPToolset`, so
there's no ADK-free way to exercise it — `client_multi_server.py` is
therefore a minimal ADK root agent with **both** toolsets attached as
`tools=[...]`, one per MCP server, built from one shared
`SecureToolsetManager`. Each server's tool is named
`ping_<server_label>` (not a shared `ping`) specifically so the LLM can
tell the two toolsets' tools apart by name.

## Prerequisites

Same shared root venv as [`10_client_ms`](../10_client_ms/README.md)
(`stls-ai/.venv`, set up once per
[`10_server_ms`'s README](../../server/10_server_ms/README.md#prerequisites)).
Both MCP servers (`server_mcp_ms_1`, `server_mcp_ms_2`) must be running
first. `client_multi_server.py` is an ADK agent (needs `google-adk`,
already in the shared venv, and a `GOOGLE_API_KEY` — copy
`.env.example` to `.env` and fill it in). `client_multi_session.py`
needs **neither** — it's the ADK-free, raw-`mcp`-SDK script (see the
table above); run it with just the venv set up and a server running.

## Running

```bash
cd samples/client/11_client_mcp_ms

# 1 client -> N servers, via ADK (needs google-adk + GOOGLE_API_KEY,
# needs both server_mcp_ms_1 and server_mcp_ms_2 running)
/path/to/stls-ai/.venv/bin/python client_multi_server.py

# 1 device -> N concurrent sessions, against server_mcp_ms_1 (default) —
# no ADK/LLM needed
/path/to/stls-ai/.venv/bin/python client_multi_session.py

# ...or against the second pair, overriding the 3 relevant variables inline:
BASE_URL=http://localhost:8202 TOOL_NAME=ping_server_mcp_ms_2 \
  ARQIT_PY_CLIENT_CONFIG=/abs/path/aq-sf-mcp-client-ms-2.json \
  /path/to/stls-ai/.venv/bin/python client_multi_session.py
```

Change `KEY_PROVIDER_NAME` in `.env` (and restart the servers with the
same value) to test `base` / `pqc` / `arqit_py` — no code changes.

## Validated (2026-10-23)

Full round trip tested against the real Arqit cloud and real MCP
servers for all three providers:

| Provider | Handshake / session | MCP tool listing | LLM final answer |
|---|---|---|---|
| `base` | OK | OK | blocked by Gemini API quota (external, unrelated) |
| `pqc` | OK | OK | blocked by Gemini API quota |
| `arqit_py` | OK — both devices (`sf-mcp-client-ms-1`, `sf-mcp-client-ms-2`) authenticated sequentially in one process, real bilocation/P2P negotiation each | OK | blocked by Gemini API quota |

Confirms `SecureToolsetManager`'s multi-device-per-process support
works end-to-end for the sequential case, exactly mirroring
`stls_a2a.SecureSessionManager`'s own validated behavior. Concurrent
use of different `arqit_py` device identities (or concurrent sessions
for the same one) in one process is now also safe — serialized
under the hood by a process-wide lock rather than requiring callers to
avoid parallelizing it themselves, see the `DeviceMetadata` singleton
note in `stls-a2a`'s README.
