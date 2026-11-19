# 16_server_device_agent — one Arqit device, N replicas, via a standalone owner

Demonstrates the `ARQIT_PY_DEVICE_ROLE` toggle and the standalone
device-agent process added for issue #38 (see
`python/stls-core/CHANGELOG.md` 0.1.10 and
`python/stls-core/sidecar/arqit_device_agent.py`'s own docstring):
**one** Arqit device identity, owned by **one** process, with any
number of application replicas reading session keys purely through a
shared Redis-backed store — never calling
`KeyProviderServerArqitPy.start()`/authenticating the device
themselves.

This is a different scenario from
[`samples/server/10_server_ms/`](../10_server_ms/README.md), which
uses **two different devices** (one per replica, no sharing at all).
Here there is exactly **one** device, shared by **two** application
replicas that never touch it directly.

**Validated end-to-end against the real Arqit cloud and a real Redis
(2026-11-19)**, using a dedicated device pair registered specifically
for this sample (never the throwaway race-test device —
`aq-sf-device-agent-16-server`/`aq-sf-device-agent-16-client`, config
JSON already included in `device_agent/`/
[`samples/client/16_client_device_agent/`](../../client/16_client_device_agent/)):
real client authentication, real bilocation+P2P negotiation, the
resulting key landing in the device-agent's Redis-backed store, and a
separate consumer session — that never called `start()` or
authenticated the device at all — resolving the exact same key via
`hint_kid`. See
[`sidecar/ARQIT_PEERING_NOTES.md`](../../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md)'s
2026-11-19 follow-up for the one gotcha hit along the way: a
brand-new device pair needs an explicit provisioning transition
before it can peer at all, not just registration (see "Prerequisites"
below).

## Layout

```
device_agent/
  .env.example      owns the device — launches
                     python/stls-core/sidecar/arqit_device_agent.py directly,
                     no agent.py/server.py of its own
server_consumer_1/
  agent.py          minimal ADK agent (one `ping` tool, echoes the message back)
  server.py         FastAPI app + create_secure_a2a_endpoint — IDENTICAL
                     code to server_ms_1/server.py, see its own docstring
  .env.example      KEY_PROVIDER_NAME=arqit_py, ARQIT_PY_DEVICE_ROLE=consumer
server_consumer_2/   identical layout, different port — same device, same
                     Redis namespace as server_consumer_1
```

## Why `server.py` needed no code changes

`ARQIT_PY_DEVICE_ROLE=consumer` is read inside
`KeySessionProviderFactory.get_session_provider()` itself — the exact
call every `arqit_py` sample's `server.py` already makes via
`create_secure_a2a_endpoint()`/`SecureSessionManager`. Setting the env
var is the entire change; `server_consumer_1/server.py` and
`server_consumer_2/server.py` are byte-for-byte the same shape as
`10_server_ms/server_ms_1/server.py`.

## Prerequisites

Same shared-venv setup as every other `arqit_py` sample — see
[`10_server_ms/README.md`](../10_server_ms/README.md)'s "Python
environment" section, but you ALSO need the `[redis]` extra (plain
`[arqit_py]` alone only gets you fakeredis via `[dev]`, not the real
client):

```bash
cd python/stls-core
uv pip install -e ".[arqit_py,redis,dev]" \
  --find-links arqit_wheels/arqit-skaplatform-common/ \
  --find-links arqit_wheels/arqit-skaplatform-policy/ \
  --find-links arqit_wheels/arqit-skaplatform-registration/ \
  --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
  --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
  --find-links arqit_wheels/arqit-skaplatform-peering/ \
  --find-links arqit_wheels/arqit-skaplatform-monitoring/ \
  --python "$VENV/bin/python"
```

**Note**: `server_consumer_1`/`server_consumer_2` never authenticate
the device, so they technically don't need the `[arqit_py]` extra at
all (see `create_consumer_session()`'s own docstring) — only
`device_agent/` does. Installing it everywhere here is just simpler
for a local demo; a real deployment can skip it on the consumer
replicas' own image.

A real Redis reachable at `KEY_STORE_REDIS_URL`:
```bash
docker run --rm -p 6379:6379 redis:7
```

A device pair is already provided for this sample —
`device_agent/aq-sf-device-agent-16-server.json` and
[`samples/client/16_client_device_agent/aq-sf-device-agent-16-client.json`](../../client/16_client_device_agent/)
(gitignored, like every other real device config in this repo — never
committed) — already registered AND provisioned on the real Arqit
cloud, confirmed working end-to-end. If you register your own pair
instead (never reuse `_arqit_race_fixtures`'s throwaway device — that
one is dedicated to this project's own tests and has already proven
fragile under multi-process use), remember the gotcha from
`ARQIT_PEERING_NOTES.md`'s 2026-11-19 follow-up:
`RegistrationClient.register_device_v3()` alone leaves a device at
bootstrap — bilocation peering fails with `HTTP 403` until you also
run it through `AuthenticationSessionManager.authenticate_device()` →
`ProvisioningClient.transition_provisioning_state(session)` →
`authenticate_device()` again.

## Running it

Three separate processes/terminals, in this order:

```bash
# 1. The device-agent — owns the device, feeds Redis.
cd samples/server/16_server_device_agent/device_agent
cp .env.example .env   # fill in ARQIT_PY_SERVER_CONFIG
set -a; source .env; set +a
python /path/to/stls-ai/python/stls-core/sidecar/arqit_device_agent.py
# wait for: "[device-agent] device started and listening — readiness is now OK"
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/healthz   # expect 200
```

```bash
# 2. server_consumer_1
cd samples/server/16_server_device_agent/server_consumer_1
cp .env.example .env   # fill in GOOGLE_API_KEY
python server.py
```

```bash
# 3. server_consumer_2
cd samples/server/16_server_device_agent/server_consumer_2
cp .env.example .env   # fill in GOOGLE_API_KEY
python server.py
```

## Client — there's no dedicated one here, reuse `10_client_ms`'s

This sample has no `agent.py`/client of its own: the client side of an
STLS relationship is unaffected by anything in this sample (it's
always a normal `arqit_py` client device, `ARQIT_PY_DEVICE_ROLE` is a
SERVER-role-only concept — see
`create_consumer_session()`'s own docstring) — so reuse
[`client_multi_session.py`](../../client/10_client_ms/client_multi_session.py),
overriding its `BASE_URL`/`AGENT_PATH`/`ARQIT_PY_CLIENT_CONFIG` at
launch (the same override pattern its own `.env.example` already
documents for targeting `server_ms_2` instead of `server_ms_1`):

```bash
cd samples/client/10_client_ms
cp .env.example .env   # fill in GOOGLE_API_KEY; KEY_PROVIDER_NAME=arqit_py

# against server_consumer_1 (port 8111) — using the device pair
# already provided for this sample:
ARQIT_PY_CLIENT_CONFIG=../../client/16_client_device_agent/aq-sf-device-agent-16-client.json \
BASE_URL=http://localhost:8111 AGENT_PATH=/agents/server-consumer-1/secure-a2a \
python client_multi_session.py

# against server_consumer_2 (port 8112) — same client device, same
# command, just a different target replica:
ARQIT_PY_CLIENT_CONFIG=../../client/16_client_device_agent/aq-sf-device-agent-16-client.json \
BASE_URL=http://localhost:8112 AGENT_PATH=/agents/server-consumer-2/secure-a2a \
python client_multi_session.py
```

`ARQIT_PY_CLIENT_CONFIG` must point at the CLIENT half of the device
pair — the counterpart to whatever `ARQIT_PY_SERVER_CONFIG`
`device_agent/.env` uses, never `10_client_ms`'s own
`aq-sf-client-ms-*.json` (those are paired with `10_server_ms`'s
devices, a different relationship entirely). Both commands above
resolve real session keys through the one device-agent — neither
`server_consumer_1` nor `server_consumer_2` ever calls `start()`
itself.

**`KEY_STORE_REDIS_NAMESPACE` for the client is a separate concern**:
the client device is a different identity from the server device, so
it needs its OWN namespace, never the same one `device_agent`/the
consumer replicas use — reusing it collides on the device-level owner
lock (confirmed directly while validating this sample: see
`ARQIT_PEERING_NOTES.md`). `client_multi_session.py` doesn't set
`KEY_STORE_BACKEND`/`KEY_STORE_REDIS_NAMESPACE` at all by default
(a one-shot client doesn't need Redis sharing), which is the safe
default here too — only set them if you have a specific reason to.

## Validated — what's still worth doing yourself

Confirmed end-to-end against the real Arqit cloud and a real Redis
(see the "Validated" note near the top of this file): real
authentication, real bilocation+P2P negotiation, the device-agent's
key landing in Redis, and a consumer session that never called
`start()` resolving the exact same key via `hint_kid`. Not yet run
through the full `server.py`/A2A/MCP layer end-to-end (the validation
above exercised the `stls-core` provider/session layer directly, the
same way `arqit_device_agent.py`'s own unit tests do, just against the
real cloud this time) — running an actual A2A request through
`server_consumer_1`/`server_consumer_2` is the natural next check
before trusting this in production, same standing caution as every
other real `arqit_py` path in this project.
