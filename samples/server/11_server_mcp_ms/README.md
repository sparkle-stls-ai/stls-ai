# 11_server_mcp_ms — multi-server MCP test servers

The MCP analogue of
[`samples/server/10_server_ms/`](../10_server_ms/README.md): two
independent MCP server processes used to validate the multi-server /
multi-session `arqit_py` support added to
`stls_mcp.SecureToolsetManager` (same pattern and underlying
`stls_core.KeySessionResolver` as `stls_a2a.SecureSessionManager`).
Each one is a minimal single-tool MCP server (`FastMCP`) exposed
through `stls_mcp.create_secure_mcp_app`.

They are the server side for
[`samples/client/11_client_mcp_ms/`](../../client/11_client_mcp_ms/README.md):

- `server_mcp_ms_1/` — port 8201, Arqit device `sf-mcp-server-ms-1` (peering port 8891), paired with `client-mcp-ms-1`
- `server_mcp_ms_2/` — port 8202, Arqit device `sf-mcp-server-ms-2` (peering port 8892), paired with `client-mcp-ms-2`

These are **dedicated, newly-provisioned Arqit devices**, separate from
the `10_server_ms` A2A ones — reusing those would have required either
duplicating their metadata files or chdir'ing into the A2A directory,
and mixing an MCP role onto an A2A-provisioned device adds unnecessary
ambiguity for no benefit.

## Prerequisites

Same shared root venv as [`10_server_ms`](../10_server_ms/README.md#prerequisites)
— that setup already installs `stls_mcp` (editable) alongside
`stls_core`/`stls_a2a` into the one venv at `stls-ai/.venv`. These
server scripts themselves don't need `google-adk` (they aren't ADK
agents — only `11_client_mcp_ms/client_multi_server.py` is), but it's
harmless that the shared venv has it anyway. Copy each directory's
`.env.example` to `.env` — no secret needs filling in here.

## Running one server

```bash
cd samples/server/11_server_mcp_ms/server_mcp_ms_1
/path/to/stls-ai/.venv/bin/python server.py
```

Same `KEY_PROVIDER_NAME=base|pqc|arqit_py` switch, same `os.chdir()`
anchoring for Arqit's relative config paths, as the A2A servers.

## Validated (2026-10-23)

Full session/transport round trip (STLS handshake + encrypted
`POST .../mcp-stls` + MCP tool listing) confirmed working for all three
providers against the real client in
[`11_client_mcp_ms`](../../client/11_client_mcp_ms/README.md) — see
that README for the full matrix and the one caveat (Gemini API quota
blocking the final LLM answer, unrelated to this code).
