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

- `stls-core` layer: real client authentication, real bilocation+P2P
  negotiation, the resulting key landing in the device-agent's
  Redis-backed store, and a separate consumer session — that never
  called `start()` or authenticated the device at all — resolving the
  exact same key via `hint_kid`.
- **Full A2A layer**: a real encrypted call through
  `server_consumer_1`'s actual HTTP endpoint (`POST .../secure-a2a`),
  `HTTP 200 OK` — the STLS handshake/decrypt/encrypt round trip
  completed correctly through a consumer-role replica that never owns
  the device. The only failure past that point was the already-known,
  expected one without `GOOGLE_API_KEY` configured (Gemini rejecting
  the downstream call) — not anything this sample validates.

See [`sidecar/ARQIT_PEERING_NOTES.md`](../../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md)'s
2026-11-19 follow-ups for the two gotchas hit along the way: a
brand-new device pair needs an explicit provisioning transition before
it can peer at all (see "Prerequisites" below), and relative
`DEVICE_METADATA_FILEPATH` can make a multi-session `asyncio.gather()`
script hang silently instead of erroring if the device's own directory
isn't the caller's cwd — worked around here by using absolute paths in
this sample's own device config, unlike the relative-path convention
used elsewhere in this repo.

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

## Client

[`samples/client/16_client_device_agent/client_session.py`](../../client/16_client_device_agent/client_session.py)
— one real, sequential A2A session (resolve key → one encrypted call),
confirmed working end-to-end:

```bash
cd samples/client/16_client_device_agent
BASE_URL=http://localhost:8111 AGENT_PATH=/agents/server-consumer-1/secure-a2a \
python client_session.py

# against server_consumer_2 instead — same client device, same command,
# just a different target replica:
BASE_URL=http://localhost:8112 AGENT_PATH=/agents/server-consumer-2/secure-a2a \
python client_session.py
```

Both resolve real session keys through the one device-agent — neither
`server_consumer_1` nor `server_consumer_2` ever calls `start()`
itself. A `GOOGLE_API_KEY`-related error at step 2 means the STLS/
consumer-role layer worked (the encrypted round trip reached the
agent code) and only the downstream Gemini call failed — expected,
not a failure of anything this sample validates.

`samples/client/10_client_ms/client_multi_session.py` is reusable too
(override `BASE_URL`/`AGENT_PATH`/`ARQIT_PY_CLIENT_CONFIG`, same
pattern its own `.env.example` shows for targeting `server_ms_2`), but
its "Test A" always opens 2 CONCURRENT sessions regardless of
`GROUP_SIZE`/`NUM_GROUPS` — a different (valid) thing to test than
this sample is about, and the reason `client_session.py` exists as the
simpler, recommended option instead.

**`KEY_STORE_REDIS_NAMESPACE` for the client is a separate concern**:
the client device is a different identity from the server device, so
it needs its OWN namespace, never the same one `device_agent`/the
consumer replicas use — reusing it collides on the device-level owner
lock (confirmed directly while validating this sample: see
`ARQIT_PEERING_NOTES.md`). Neither client script sets
`KEY_STORE_BACKEND`/`KEY_STORE_REDIS_NAMESPACE` at all by default (a
one-shot client doesn't need Redis sharing), which is the safe default
here too — only set them if you have a specific reason to.

## Validated

Confirmed end-to-end against the real Arqit cloud and a real Redis —
see the "Validated" note near the top of this file for the full list,
`stls-core` layer and the full A2A layer both. Not yet tried: two
consumer replicas resolving sessions concurrently (only one at a time
was exercised), and — deliberately never repeated — the original
two-independent-owners incident this issue is named after, now that
it's understood and fixed.
