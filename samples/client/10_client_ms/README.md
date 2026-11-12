# 10_client_ms — multi-server / multi-session client tests

Two client scripts validating the two different axes of concurrent
`arqit_py` usage added to `stls_a2a.SecureSessionManager` (see
`python/stls-a2a/README.md`, "Current Limitations and Notes" ->
`arqit_py`), and confirming `base`/`pqc` already worked for both:

| Script | Tests | Device(s) used |
|---|---|---|
| `client_multi_server.py` | **1 client process -> N different servers**, driven by a plain script (no ADK) — `SecureSessionManager(arqit_client_configs={base_url: config_path})`, one session per server, sequentially | `client-ms-1` vs `server_ms_1`, `client-ms-2` vs `server_ms_2` |
| `client_multi_session.py` | **1 device/client config -> N concurrent sessions toward 1 server** — the provider's own "1:1 architecture" (each session gets its own `KeyProviderClient`, isolated kid/key, same device identity), run concurrently via `asyncio.gather` | `client-ms-1` vs `server_ms_1` by default (works against either pair) |
| `client_agent_multi_server.py` | **1 client process -> N different servers, where the client is itself an ADK agent** — a root `Agent` delegating to two `ContextualSecureRemoteA2aAgent` sub-agents that share one `SecureSessionManager(arqit_client_configs=...)` | `client-ms-1` vs `server_ms_1`, `client-ms-2` vs `server_ms_2` |

`client_multi_session.py` is adapted from
`python/stls-a2a/tests/key_providers_test.py`, pointed at our own
provisioned devices/servers, using `send_secure_message()` (non-
streaming) instead of SSE streaming — our minimal ADK agent doesn't
advertise `capabilities.streaming` in its card, and the point here is
key/session robustness under concurrency, not SSE plumbing.

## Prerequisites

Same as the server side — see
[`samples/server/10_server_ms/README.md`](../../server/10_server_ms/README.md#prerequisites)
for the full, step-by-step **shared root venv** setup (`stls-ai/.venv`,
editable installs of `stls_core`+`stls_a2a`+`stls_mcp`, the
`[arqit_py]` extra, `google-adk`) — use that same venv's `python` for
every command below, not a bare `python`. Both servers (`server_ms_1`,
`server_ms_2`) must already be running for `client_multi_server.py`;
only the one server you're targeting needs to be running for
`client_multi_session.py`.

Copy `.env.example` to `.env` in this directory (and in each
`server_ms_N/`) and fill in `GOOGLE_API_KEY` — the example files ship
with every variable except that secret already set to a sensible
default.

## `.env` reference

```bash
KEY_PROVIDER_NAME=pqc          # base | pqc | arqit_py — same switch for both scripts

# client_multi_server.py (both servers at once)
BASE_URL_1=http://localhost:8101/agents/server-ms-1
BASE_URL_2=http://localhost:8102/agents/server-ms-2
ARQIT_PY_CLIENT_CONFIG_1=/abs/path/aq-sf-client-ms-1.json   # arqit_py only
ARQIT_PY_CLIENT_CONFIG_2=/abs/path/aq-sf-client-ms-2.json   # arqit_py only

# client_multi_session.py (one server/device at a time — defaults to pair 1)
BASE_URL=http://localhost:8101
AGENT_PATH=/agents/server-ms-1
ARQIT_PY_CLIENT_CONFIG=/abs/path/aq-sf-client-ms-1.json     # arqit_py only
# optional, defaults shown:
GROUP_SIZE=3
NUM_GROUPS=2

ARQIT_PY_TRANSPORT=socket

# client_agent_multi_server.py (the client is itself an ADK agent, so it
# needs its own Gemini key — can be the same one used by the servers)
GOOGLE_API_KEY=...
```

## Running the tests

Start the server(s) first (see the server README), then:

```bash
cd samples/client/10_client_ms

# 1 client -> N servers (needs both server_ms_1 and server_ms_2 running)
python client_multi_server.py

# 1 device -> N concurrent sessions, against server_ms_1 (default)
python client_multi_session.py

# ...or against the second pair, overriding the 3 relevant variables inline:
BASE_URL=http://localhost:8102 AGENT_PATH=/agents/server-ms-2 \
  ARQIT_PY_CLIENT_CONFIG=/abs/path/aq-sf-client-ms-2.json \
  python client_multi_session.py

# 1 client agent -> N servers (needs both servers running, and each
# server_ms_N/server.py started with base_url= set — see the server
# README's "base_url= is required for agent-based clients")
python client_agent_multi_server.py
```

To test a different provider, either edit `KEY_PROVIDER_NAME` in `.env`
(and restart the matching servers with the same value) or override it
inline on both ends:

```bash
KEY_PROVIDER_NAME=arqit_py python server.py          # in each server_ms_N/ directory
KEY_PROVIDER_NAME=arqit_py python client_multi_server.py
KEY_PROVIDER_NAME=arqit_py python client_multi_session.py
```

If a command above fails with `TypeError: ... unexpected keyword argument
'arqit_client_configs'` or `ImportError: cannot import name
'establish_session'`, the venv you're using has a stale/non-editable
`stls_core`/`stls_a2a` — see the verification step in
[`10_server_ms`'s README](../../server/10_server_ms/README.md#prerequisites)
and redo the relevant `uv pip install -e` step there.

## Expected output per provider

- **`pqc`** and **`arqit_py`**: every session gets a distinct `kid`/key.
  `client_multi_session.py`'s Test A/B report "OK", all kids unique.
- **`base`**: the provider always returns the same fixed, hardcoded
  key/kid (`_BASE_KEY_ID`/`_BASE_KEY_DATA` in `stls_core`) — this is a
  zero-dependency dev/test provider by design, not meant to isolate
  sessions. `client_multi_session.py` reports this as `WARN "duplicate
  kid — expected with provider=base"`, not `FAIL`. Seeing those warnings
  with `base` is correct behavior, not a regression.
- `client_multi_server.py` prints one `OK`/`FAILED` line per server and
  a final `N/2 servers OK` — a per-server failure (e.g. missing
  `GOOGLE_API_KEY` on the ADK side) doesn't stop the other server from
  being tried, since the point is the session/transport layer, not the
  LLM's answer.

## Validated matrix (2026-10-23)

All three scripts were run against the real Arqit cloud and real ADK
agents for all three providers (9 combinations total) — twice: once
before adding `base_url=` to `server.py` (see below), and once after,
to confirm the fix didn't regress anything.

| | `base` | `pqc` | `arqit_py` |
|---|---|---|---|
| `client_multi_server.py` | PASS | PASS | PASS |
| `client_multi_session.py` | PASS (expected kid/key collisions) | PASS | ran without error (8 "concurrent" sessions) — but see the note below, this did not actually test concurrent access |
| `client_agent_multi_server.py` | session/transport PASS, final answer blocked by API quota | session/transport PASS, final answer blocked by API quota | session/transport PASS, final answer blocked by API quota |

`client_agent_multi_server.py` confirms `ContextualSecureRemoteA2aAgent`
is already adequate for the multi-server `arqit_py` scenario — **no
code change was needed in `stls_a2a`**: `arqit_client_configs` lives on
`SecureSessionManager`, and `ContextualSecureRemoteA2aAgent` already
accepts a pre-built `session_manager=` to share across sub-agents. One
real bug surfaced and was fixed along the way: `server.py` needed
`base_url=` passed to `create_secure_a2a_endpoint()` (see the server
README) — without it, ADK's `RemoteA2aAgent` rejects the card's
relative `url` field outright with `AgentCardResolutionError: Invalid
RPC URL format`.

By the second (post-fix) pass, the shared `GOOGLE_API_KEY` used across
this whole test session had run out of prepaid credits
(`402 RESOURCE_EXHAUSTED` from Gemini) — an external quota issue,
unrelated to `stls_a2a`/`stls_core`. This actually turned out to be
useful: every session/transport step that doesn't depend on the LLM's
final answer (card resolution, handshake, encrypted `POST .../secure-a2a`
returning `200 OK`, real Arqit device auth + bilocation/P2P
negotiation, `send_secure_message()`/`extract_a2a_text()` correctly
surfacing the downstream JSON-RPC error) kept succeeding exactly as
before across all 9 combinations — only the ADK agent's own call to
Gemini failed, cleanly and consistently. `client_agent_multi_server.py`
was also hardened during this pass to catch a per-server failure
(`try/except` around each `ask()` call) instead of letting one crash
the whole script, matching the other two scripts' behavior.

## Important caveats for `arqit_py`

- **Client and server roles must never share an OS process** — a hard
  constraint of the Arqit SDK itself (401 from Arqit's backend
  otherwise), see `python/stls-core/README.md` and
  `sidecar/ARQIT_PEERING_NOTES.md`. Not an issue here: servers and
  client are always separate processes.
- **`client_multi_server.py`'s multi-device-per-process use was
  originally only validated sequentially** (one device's full session
  setup completing before the next one starts — exactly what
  `SecureSessionManager`'s internal loop does). The Arqit SDK's
  `uk.arqit.device.common.device_metadata.DeviceMetadata` is a
  process-wide singleton whose `.config` is silently overwritten on
  every call; concurrent `arqit_py` client configs in the same process
  used to risk racing each other. **This is now fixed**: a
  process-wide serialization lock in `stls_core_providers_arqit_py.py`
  makes concurrent use safe (if fully serial under the hood) rather
  than merely untested — see the `arqit_py` section of
  `python/stls-a2a/README.md`'s "Current Limitations and Notes".
- **`client_multi_session.py`'s "8 simultaneous real sessions, zero
  errors" result below was weaker evidence than it looked, at the
  time.** `KeyProviderClientArqitPy.start()` is a plain, blocking,
  synchronous method with no `await` inside it — called directly from
  a coroutine passed to `asyncio.gather()` (as this script does),
  Python's cooperative scheduler ran it to completion before ever
  switching to the next coroutine, so the `DeviceMetadata.get_instance()`
  calls inside it never actually overlapped in time; `asyncio.gather()`
  here serialized exactly the part that mattered, for free, without
  anyone asking it to. A **real** OS-thread race at that call site was
  first exercised via the Node sidecar path (`arqit_sidecar.py`
  dispatches each RPC call through `loop.run_in_executor()`, a genuine
  thread per connection), where it failed — then root-caused and fixed
  with a direct, controlled, real-cloud reproduction in pure Python
  (`python/stls-core/tests/test_arqit_py_same_device_concurrent_real.py`,
  using `asyncio.to_thread()` for genuine concurrency): Arqit SDK's
  `DeviceMetadata`/`AuthenticationClient.__mutex` singletons have no
  protection against concurrent `start()` calls and no `try`/`finally`
  around the critical section that can leak the lock forever on an
  unrelated exception. Fixed with a process-wide `threading.Lock`
  serializing `start()` (full negotiation included, not just
  authentication) — confirmed by re-running the same reproduction:
  3 concurrent same-device sessions that used to deadlock forever now
  complete in ~5s with 3/3 distinct keys. See
  `samples/server/12_server_mcp_ms/README.md` and
  `python/stls-a2a/README.md`'s "Current Limitations and Notes" for the
  full write-up.
