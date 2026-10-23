# 10_server_ms — multi-server test servers

Two independent A2A server processes used to validate the multi-server /
multi-session `arqit_py` support added to `stls_a2a.SecureSessionManager`
(see `python/stls-a2a/README.md`, "Current Limitations and Notes" ->
`arqit_py`). Each one is a minimal, single-tool ADK agent exposed through
`stls_a2a.create_secure_a2a_endpoint` — the same pattern used by every
other `samples/server/*` sample.

They are the server side for the two client scripts in
[`samples/client/10_client_ms/`](../../client/10_client_ms/README.md):

- `server_ms_1/` — port 8101, Arqit device paired with `client-ms-1`
- `server_ms_2/` — port 8102, Arqit device paired with `client-ms-2`

## Layout

```
server_ms_1/
  agent.py                     minimal ADK agent (one `ping` tool, echoes the message back)
  server.py                    FastAPI app + create_secure_a2a_endpoint
  .env                         provider choice + ports + Arqit config path
  aq-sf-server-ms-1.json       Arqit device config (provisioned on the real SKA-Platform)
  aq-sf-server-ms-1_metadata.json
server_ms_2/                   identical layout, port 8102, its own device
```

Each directory ships a `.env.example` with every variable except
`GOOGLE_API_KEY` already set — copy it to `.env` and fill that one in.

## Prerequisites

- `stls_core` and `stls_a2a` importable, with the `arqit_client_configs`
  feature (added 2026-10-23) in `stls_core_providers_arqit_py.py` /
  `stls_core_providers_factory.py` / `stls_a2a_secure_remote.py`. If your
  installed copies predate it (check with
  `python -c "import inspect,stls_a2a; print(inspect.signature(stls_a2a.SecureSessionManager.__init__))"`
  — look for `arqit_client_configs`), either reinstall from source
  (`pip install -e python/stls-core -e python/stls-a2a`) or run with:
  ```bash
  export PYTHONPATH=/path/to/stls-ai/python/stls-core/src:/path/to/stls-ai/python/stls-a2a/src
  ```
- `google-adk`, `fastapi`, `uvicorn`, `python-dotenv` installed.
- For `KEY_PROVIDER_NAME=arqit_py`: the `[arqit_py]` extra (the Arqit
  SKA-Platform wheels) installed — `import uk.arqit.device.common` must
  succeed.
- A `GOOGLE_API_KEY` (each `.env` has a slot for it) — without it the
  session/transport layer still works end-to-end, but the ADK agent's
  final answer fails with a Gemini "No API key" error. That's a useful,
  expected failure mode when you only care about validating the crypto/
  session layer, not the LLM's answer.

## Running one server

Each `server.py` does `os.chdir()` to its own directory on startup, so
Arqit's relative config paths (`DEVICE_METADATA_FILEPATH`, etc. inside
the `.json` config) always resolve correctly regardless of where you
launch the process from (see `ARQIT_PEERING_NOTES.md` in `stls-core` for
why this matters).

```bash
cd samples/server/10_server_ms/server_ms_1
python server.py
# or, from anywhere:
python samples/server/10_server_ms/server_ms_1/server.py
```

Provider selection lives entirely in that directory's `.env` —
`KEY_PROVIDER_NAME=base|pqc|arqit_py`. Change it there (or override it
on the command line, e.g. `KEY_PROVIDER_NAME=arqit_py python server.py`)
and restart; no code change is needed for any of the three providers.

Start `server_ms_2` the same way, in its own directory, in a second
terminal/process.

**Important for `arqit_py`:** never run a client and a server role in
the same OS process (hard SDK-level constraint, see
`python/stls-core/README.md` and `sidecar/ARQIT_PEERING_NOTES.md`) —
here that's naturally satisfied since `server_ms_1`/`server_ms_2` and
the client scripts are always separate processes.

## `base_url=` is required for agent-based clients

`server.py` passes `base_url=BASE_URL` to `create_secure_a2a_endpoint()`.
This matters only for
[`client_agent_multi_server.py`](../../client/10_client_ms/README.md)
(the client that is itself an ADK agent using
`ContextualSecureRemoteA2aAgent`): without it, the served
`.well-known/agent-card.json` has **relative** `url`/`secureUrl` fields
(`/agents/server-ms-1/a2a`), and ADK's `RemoteA2aAgent` rejects those
outright with `AgentCardResolutionError: Invalid RPC URL format` — it
does not resolve them against the card's fetch URL. With `base_url` set,
the card exposes absolute URLs (`http://localhost:8101/agents/server-ms-1/a2a`)
and card resolution succeeds. The other two client scripts
(`client_multi_server.py`, `client_multi_session.py`) never fetch the
card at all, so they are unaffected either way.

## Verifying a server is up

```bash
curl -s http://localhost:8101/.well-known/agent-card.json | head -c 300
curl -s http://localhost:8102/.well-known/agent-card.json | head -c 300
```

A successful `arqit_py` startup log looks like:

```
[KeyProviderServerArqitPy] start config=... transport=socket
About to authenticate device <duid>
Finished successfully authenticating device duid <duid>
[KeyProviderServerArqitPy] authentication completed
[KeyProviderServerArqitPy] listening on port 88xx
[KeyProviderServerArqitPy] listen thread started
...
Uvicorn running on http://0.0.0.0:81xx
```

## Full test matrix (client side)

See [`samples/client/10_client_ms/README.md`](../../client/10_client_ms/README.md)
for the two client scripts that exercise these servers and the exact
commands to run the full base/pqc/arqit_py matrix.
