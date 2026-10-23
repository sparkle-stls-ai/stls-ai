# 12_server_mcp_ms — Node analogue of the MCP multi-server test

Node/TypeScript port of
[`samples/server/11_server_mcp_ms/`](../11_server_mcp_ms/README.md).
Two minimal MCP servers (`@modelcontextprotocol/sdk`'s `McpServer`,
via `stls-mcp`'s `createSecureMcpApp`) used by
[`samples/client/12_client_mcp_ms/`](../../client/12_client_mcp_ms/README.md).

- `server_mcp_ms_1/` — port 8211, Arqit device `sf-node-mcp-server-ms-1` (peering port 8911)
- `server_mcp_ms_2/` — port 8212, Arqit device `sf-node-mcp-server-ms-2` (peering port 8912)

**Dedicated Node-specific Arqit devices** — separate from both the
`10_server_ms`/`11_server_mcp_ms` Python devices, same rationale as
before (no relative-path/identity-reuse ambiguity across samples).

## Install

```bash
cd samples/server/12_server_mcp_ms
npm install
```

`stls-core`/`stls-mcp` are `file:` dependencies pointing at
`node/stls-core`/`node/stls-mcp` — **build those packages first** so
`dist/` is up to date (re-run after editing their source, e.g. after
pulling a change to the shared `arqit_py`/handshake fixes):
```bash
cd /path/to/stls-ai/node/stls-core && npm install && npm run build
cd /path/to/stls-ai/node/stls-mcp  && npm install && npm run build
```

## Running

```bash
cd samples/server/12_server_mcp_ms/server_mcp_ms_1
npx tsx --env-file=.env server.ts
```

For `KEY_PROVIDER_NAME=arqit_py`, this script only talks to an
already-running Python sidecar process via
`ARQIT_SIDECAR_SOCKET_SERVER` — launch the sidecar yourself, from
**this device's own directory** (relative `DEVICE_METADATA_FILEPATH`
etc. resolve against the sidecar process's cwd, not this script's):

```bash
cd samples/server/12_server_mcp_ms/server_mcp_ms_1
ARQIT_PY_SERVER_CONFIG=./aq-sf-node-mcp-server-ms-1.json \
KEY_PROVIDER_NAME=arqit_py \
python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-node-mcp-server-ms-1.sock
```

## Validated (2026-10-23)

- `base`, `pqc`: full round trip against
  [`client_multi_server.ts`](../../client/12_client_mcp_ms/client_multi_server.ts)
  and
  [`client_multi_session.ts`](../../client/12_client_mcp_ms/client_multi_session.ts)
  — all passed, no LLM/ADK dependency needed on the Node side at all
  (unlike the Python samples, which need ADK + a Gemini key for
  `client_multi_server.py`).
- `arqit_py` + `client_multi_server.ts`: **passed** — real bilocation
  key agreement against two separate sidecar processes (one per
  device), both tool calls succeeded.
- `arqit_py` + `client_multi_session.ts` (3 concurrent sessions x 2
  rounds against the SAME long-running server instance): **partial
  failure** — one of three concurrent sessions in round 1 failed with
  `No new key available within 30s (3 already delivered previously)`,
  and round 2 then hung past a 60s timeout.

  **Root cause confirmed, and fixed at the source (2026-10-23).** This
  was credible evidence of a real concurrency bug in the Arqit SDK, not
  ordinary rate-limiting — correcting an earlier, too-quick guess. The
  Node→sidecar path was the only one in this project that actually
  created OS-thread-level concurrency at the sensitive call site
  (`arqit_sidecar.py`'s `_start()` dispatches each RPC connection
  through `loop.run_in_executor()`, a real thread per connection), which
  is why it caught something every *pure-Python* `asyncio.gather()`
  test in this project missed: `KeyProviderClientArqitPy.start()` is a
  plain, blocking, synchronous method with no `await` inside it, so
  those tests' "concurrent" sessions were silently serialized by
  Python's own cooperative scheduler, never actually racing.

  A controlled, isolated reproduction against the real cloud (dedicated
  throwaway devices, `python/stls-core/tests/test_arqit_py_same_device_concurrent_real.py`,
  using `asyncio.to_thread()` to force genuine OS-thread concurrency)
  confirmed the real cause via `py-spy` thread dumps: Arqit SDK's
  `DeviceMetadata` is a process-wide singleton and
  `AuthenticationClient.__mutex` is a class-level lock shared by every
  instance, with no `try`/`finally` around the critical section it
  guards — an unrelated exception during one `start()` call leaked that
  lock permanently, deadlocking every subsequent call in the process
  forever. **Fixed** in `stls_core_providers_arqit_py.py`: a new
  process-wide `_ARQIT_PY_START_LOCK` fully serializes `start()` for
  both client and server roles — client-side, it's held through the
  *entire* negotiation (not just authentication), since letting a
  second session authenticate while a first session's negotiation was
  still connecting also broke the handshake
  (`ConnectionRefusedError`/`PeerUnableToConnectException`). Confirmed
  fixed: the same 3-concurrent-sessions reproduction that used to
  deadlock forever now completes in ~5s with 3/3 distinct keys and no
  errors. This makes `arqit_py` session establishment fully serial
  process-wide (a throughput cost, not a correctness one) until Arqit
  fixes their SDK — see the `DeviceMetadata` singleton note in
  `python/stls-a2a/README.md`'s "Current Limitations and Notes" for the
  full write-up. The Node sidecar needed no code change: it's a Python
  process under the hood, so it picks up the fix automatically.
  **Confirmed on this exact path, not just the Python-only repro**:
  re-ran `client_multi_session.ts` itself (this file's original
  failure) after the fix — 6/6 sessions passed, 0 errors, 6/6 unique
  `session_id`s.

  **Second, independent bug found (and fixed) the same way as Python
  (2026-10-23).** Once truly concurrent, this exposed a second issue:
  the server resolved its kid via plain FIFO with no correlation to
  which incoming handshake it was for — concurrent handshakes against
  the same long-running server could get cross-wired. Node had the
  *exact same* gap as Python, not independently fixed: neither
  `node/stls-core/src/handshake.ts`'s `HandshakeRequest` nor the
  sidecar RPC's `get_session_key_identifier` passed a kid hint, even
  though the underlying Python fix already supported one. Fixed with
  the same shape as Python: `HandshakeRequest.kid?` (optional),
  `KeySessionProvider.getSessionKeyIdentifier(hintKid?)` across all
  implementations (Base, PQC ignore it; the Arqit sidecar provider
  forwards it over RPC as `{kid: hintKid}`), and the sidecar's own
  `get_session_key_identifier` handler now passes it through as
  `hint_kid` to the real Python provider. Confirmed fixed: re-ran
  `client_multi_session.ts` with genuinely concurrent sessions — 3/3
  and 6/6 clean passes, 0 errors.
