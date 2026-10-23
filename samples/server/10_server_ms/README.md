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

### Python environment — use ONE shared venv at the repo root, not each package's own

Run every `10_`/`11_`/`12_`-series Python script (servers and clients
alike) with the **same venv at the repo root** (`stls-ai/.venv`), not
with any individual package's own `python/stls-*/.venv`. Each
package's own venv is meant for that package's own test suite in
isolation; the samples are cross-package (a `10_client_ms` script
imports both `stls_core` and `stls_a2a`) and are meant to be run
against one shared, consistent install — exactly how an actual
downstream project consuming these packages would set things up.

Set it up once (editable installs, so `.venv` always reflects whatever
is currently on disk in `python/stls-*/src/` — no reinstall needed
after pulling or editing source):

```bash
cd /path/to/stls-ai
VENV=/path/to/stls-ai/.venv   # adjust, or just use .venv/bin/python below

# 1. stls-core, WITH the [arqit_py] extra (real Arqit SKA-Platform
#    wheels, vendored under arqit_wheels/ — no PyPI package exists for
#    these, hence --find-links) AND [dev] (pytest/pytest-asyncio/
#    httpx/fastapi/uvicorn/fakeredis — only needed if you'll also run
#    stls-core's own test suite, harmless otherwise):
cd python/stls-core
uv pip install -e ".[arqit_py,dev]" \
  --find-links arqit_wheels/arqit-skaplatform-common/ \
  --find-links arqit_wheels/arqit-skaplatform-policy/ \
  --find-links arqit_wheels/arqit-skaplatform-registration/ \
  --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
  --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
  --find-links arqit_wheels/arqit-skaplatform-peering/ \
  --find-links arqit_wheels/arqit-skaplatform-monitoring/ \
  --python "$VENV/bin/python"

# 2. stls-a2a (needed for 10_*), stls-mcp (needed for 11_*) — both
#    depend on stls-core, each pulls in its own transitive deps
#    (fastapi, uvicorn, mcp, ...). stls-mcp's google-adk is its OWN
#    optional [adk] extra (only client_multi_server.py — the ADK-based
#    script — needs it; client_multi_session.py doesn't), so install
#    with it unless you specifically don't want it:
cd ../stls-a2a  && uv pip install -e . --python "$VENV/bin/python"
cd ../stls-mcp  && uv pip install -e ".[adk,dev]" --python "$VENV/bin/python"

# 3. IMPORTANT — re-apply step 1 as the LAST command. Installing
#    stls-a2a/stls-mcp can silently resolve and reinstall a
#    non-editable stls-core wheel over the editable one (observed
#    repeatedly in practice — it's a uv dependency-resolution quirk,
#    not a one-time fluke), which breaks every script needing
#    anything added to stls-core after its last published version
#    (e.g. KeySessionResolver). Re-running step 1 after 2 restores the
#    editable link:
cd ../stls-core
uv pip install -e ".[arqit_py,dev]" \
  --find-links arqit_wheels/arqit-skaplatform-common/ \
  --find-links arqit_wheels/arqit-skaplatform-policy/ \
  --find-links arqit_wheels/arqit-skaplatform-registration/ \
  --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
  --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
  --find-links arqit_wheels/arqit-skaplatform-peering/ \
  --find-links arqit_wheels/arqit-skaplatform-monitoring/ \
  --python "$VENV/bin/python"
```

Verify before running anything:
```bash
"$VENV/bin/python" -c "
import stls_core, stls_a2a, stls_mcp
print(stls_core.__file__)   # must be .../python/stls-core/src/stls_core/__init__.py
print(stls_a2a.__file__)    # must be .../python/stls-a2a/src/stls_a2a/__init__.py
print(stls_mcp.__file__)    # must be .../python/stls-mcp/src/stls_mcp/__init__.py
print('KeySessionResolver' in dir(stls_core))   # must be True
import uk.arqit.device.common   # must not raise
import google.adk               # must not raise — needed by client_agent_multi_server.py / 11_'s ADK-based client
"
```
If any `__file__` printed above points *inside* `.venv/lib/.../site-packages/` instead of the package's own `src/` directory, that package's editable link was clobbered — redo step 1 (or the relevant step) and re-verify.

Use `$VENV/bin/python` (or activate the venv) for every command below instead of a bare `python` — a bare `python` on `PATH` may resolve to a different interpreter entirely and will not have any of this installed.

### Other prerequisites

- `python-dotenv` (pulled in transitively above).
- For `KEY_PROVIDER_NAME=arqit_py`: confirmed above via
  `import uk.arqit.device.common`.
- A `GOOGLE_API_KEY` (each `.env` has a slot for it) — without it the
  session/transport layer still works end-to-end, but the ADK agent's
  final answer fails with a Gemini "No API key" error (or, if the key
  is present but its prepaid credits are exhausted, a `402
  RESOURCE_EXHAUSTED`). Both are useful, expected failure modes when
  you only care about validating the crypto/session layer, not the
  LLM's answer.

## Running one server

Each `server.py` does `os.chdir()` to its own directory on startup, so
Arqit's relative config paths (`DEVICE_METADATA_FILEPATH`, etc. inside
the `.json` config) always resolve correctly regardless of where you
launch the process from (see `ARQIT_PEERING_NOTES.md` in `stls-core` for
why this matters).

```bash
cd samples/server/10_server_ms/server_ms_1
/path/to/stls-ai/.venv/bin/python server.py
# or, from anywhere, with the venv activated:
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
