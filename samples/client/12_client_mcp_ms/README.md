# 12_client_mcp_ms — Node analogue of the MCP multi-server/multi-session client tests

Node/TypeScript port of
[`samples/client/11_client_mcp_ms/`](../11_client_mcp_ms/README.md).
Unlike the Python client (which must go through ADK's
`STLSMCPToolset`/`SecureToolsetManager`, since that's an
ADK-integration helper), Node's `stls-mcp` client API is already
ADK-free — both scripts here call MCP tools directly via
`@modelcontextprotocol/sdk`'s `Client`, exactly like
`node/stls-mcp/tests/vault-client.ts`'s established pattern. **No LLM,
no API key, needed for either script.**

| Script | Tests |
|---|---|
| `client_multi_server.ts` | 1 client process -> N different servers, via `stls-core`'s `KeySessionResolver` (`targetConfigs` for `arqit_py`) |
| `client_multi_session.ts` | 1 device -> N concurrent MCP sessions toward 1 server |

## Install

```bash
# build the two shared Node packages first (see
# samples/server/12_server_mcp_ms/README.md's Install section) —
# this directory's package.json depends on them via file: paths
cd samples/client/12_client_mcp_ms
npm install
```

## Running

```bash
cd samples/client/12_client_mcp_ms

# 1 client -> N servers (needs both server_mcp_ms_1 and server_mcp_ms_2 running)
npx tsx --env-file=.env client_multi_server.ts

# 1 device -> N concurrent sessions, against server_mcp_ms_1 (default)
npx tsx --env-file=.env client_multi_session.ts

# ...or against the second pair:
BASE_URL=http://localhost:8212 TOOL_NAME=ping_server_mcp_ms_2_node \
  ARQIT_PY_CLIENT_CONFIG=./aq-sf-node-mcp-client-ms-2.json \
  npx tsx --env-file=.env client_multi_session.ts
```

For `KEY_PROVIDER_NAME=arqit_py`, `client_multi_server.ts` needs ONE
already-running Python sidecar process for the CLIENT role (no fixed
`ARQIT_PY_CLIENT_CONFIG` at launch — `configPath` is supplied per call
by `KeySessionResolver`), launched from this directory (both client
device configs' metadata live here):

```bash
cd samples/client/12_client_mcp_ms
python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-node-mcp-client-ms.sock
```

`client_multi_session.ts` instead uses ONE fixed device for the whole
run — give the sidecar an `ARQIT_PY_CLIENT_CONFIG` at launch time:

```bash
ARQIT_PY_CLIENT_CONFIG=./aq-sf-node-mcp-client-ms-1.json \
  python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-node-mcp-client-ms.sock
```

## A protocol difference worth knowing

Unlike `stls-a2a`'s lazy `base`/`arqit_py` session scheme (client-side
`kid` **is** the `session_id`, no server round trip), MCP always does
an explicit server-side handshake that mints a fresh `session_id` —
so `client_multi_session.ts` asserts **unique `session_id`s for every
provider including `base`**, unlike the A2A version of this test,
which explicitly expects `base` to collide (same fixed key, same `kid`
used as `session_id`). Confirmed empirically: `base` passed with 6/6
unique `session_id`s.

## Validated (2026-10-23)

See [`12_server_mcp_ms`'s README](../../server/12_server_mcp_ms/README.md#validated-2026-10-23)
for the full matrix. The partial failure originally seen under
3-concurrent-sessions `arqit_py` load here was a real `DeviceMetadata`
singleton race in the Arqit SDK (this was the only path in the project
that ran `KeyProviderClientArqitPy.start()` in a real OS thread via the
sidecar's `run_in_executor()`, unlike every pure-Python
`asyncio.gather()` test, which never actually raced it) — root-caused
and fixed at the source in `stls_core_providers_arqit_py.py`
(process-wide serialization lock, see `python/stls-a2a/README.md`'s
"Current Limitations and Notes"). The Node sidecar needed no change of
its own to pick up the fix. **Re-ran this exact script after the fix**:
6/6 sessions passed, 0 errors, 6/6 unique `session_id`s — the original
failure is gone.
