# 17_peer_a_core / 17_peer_b_core — symmetric peers, arqit_py via the device-agent/consumer split

Byte-identical to [`samples/peer/14_peer_a_core`](../14_peer_a_core/README.md)/[`14_peer_b_core`](../14_peer_b_core/README.md)
in everything except how `arqit_py`'s SERVER role is wired up: this
sample demonstrates the device-agent/`ARQIT_PY_DEVICE_ROLE=consumer`
split (issue #38) applied to a genuinely bidirectional peer, not just
the N-identical-replicas shape [`16_server_device_agent`](../../server/16_server_device_agent/README.md)
covers. Read that sample's README first for what the device-agent/
consumer split actually is; this README only covers what's different
here.

## Why this needs its own sample, not just a 14_ variant

Each peer here plays **both** Arqit roles — contacted as a SERVER,
initiates as a CLIENT — exactly like 14_. The difference: in 14_, each
role gets its own **owner** sidecar (it authenticates/negotiates the
real device itself). Here, each peer's SERVER role instead talks to a
**consumer**-mode sidecar that never authenticates anything — a
separate, standalone `arqit_device_agent.py` process owns that device
and feeds the session keys through Redis. The CLIENT role has no
consumer-mode equivalent (negotiating a brand-new session key *is*
what being a client means), so it keeps a real owner sidecar, same as
14_.

This needed dedicated devices (not 14_'s reused `12_server_mcp_ms`
pairs): a device whose SERVER role is meant to be used by a
standalone device-agent, never by the peer process's own sidecar
directly, is a different shape than a device meant to be owned
in-line by whichever process needs it.

## Everything else, unchanged from 14_

- Flow (two independent one-way channels, ping on channel #1,
  pong-on-a-different-channel #2), DIDComm-shaped messages, `direction`
  deliberately unused — see [`14_peer_a_core/README.md`](../14_peer_a_core/README.md#why-this-sample-exists-vs-13_)
  for the full rationale, identical here.
- `peer.ts` is byte-identical between `17_peer_a_core` and
  `17_peer_b_core`, and differs from 14_'s `peer.ts` only in its own
  top comment block (documenting this difference) — no code changed.

## Install

```bash
cd samples/peer/17_peer_a_core && npm install
cd samples/peer/17_peer_b_core && npm install
```

`stls-core` is a `file:` dependency — build it first:
```bash
cd /path/to/stls-ai/node/stls-core && npm install && npm run build
```

## Running (`base`/`pqc` — no sidecar/device-agent needed)

```bash
# terminal 1
cd samples/peer/17_peer_b_core
KEY_PROVIDER_NAME=pqc npx tsx --env-file=.env peer.ts

# terminal 2 (after peer_b is listening)
cd samples/peer/17_peer_a_core
KEY_PROVIDER_NAME=pqc npx tsx --env-file=.env peer.ts
```

## Running (`arqit_py`) — 2 device-agents + 2 consumer sidecars + 2 owner sidecars

Four dedicated devices, two cross-wired pairs, same non-obvious
pairing rule 14_ already found the hard way (Arqit bilocation/P2P key
agreement only works between two specific, already-paired devices —
see [`14_peer_a_core/README.md`](../14_peer_a_core/README.md#running-arqit_py--4-sidecars-non-obvious-device-pairing)):

| Pair | SERVER-role device (owned by a device-agent) | CLIENT-role device (owner sidecar) |
|---|---|---|
| 1 | `sf-peer17-pair1-server` — owned by a device-agent, consumed by `peer_b`'s SERVER role | `sf-peer17-pair1-client` — `peer_a`'s CLIENT role |
| 2 | `sf-peer17-pair2-server` — owned by a device-agent, consumed by `peer_a`'s SERVER role | `sf-peer17-pair2-client` — `peer_b`'s CLIENT role |

Same cross-wiring as 14_ (each peer's two roles come from **different**
pairs) — only the SERVER side's ownership model changed.

**1. Start the two device-agents** (each a standalone process, no HTTP
of its own — see `arqit_device_agent.py`'s own docstring):
```bash
# owns pair1-server -- consumed by peer_b's SERVER role
ARQIT_PY_SERVER_CONFIG=/path/to/stls-ai/samples/peer/17_peer_b_core/aq-sf-peer17-pair1-server.json \
KEY_STORE_BACKEND=redis KEY_STORE_REDIS_URL=redis://localhost:6379 KEY_STORE_REDIS_NAMESPACE=peer17-pair1 \
DEVICE_AGENT_HEALTH_PORT=8081 \
python /path/to/stls-ai/python/stls-core/sidecar/arqit_device_agent.py

# owns pair2-server -- consumed by peer_a's SERVER role
ARQIT_PY_SERVER_CONFIG=/path/to/stls-ai/samples/peer/17_peer_a_core/aq-sf-peer17-pair2-server.json \
KEY_STORE_BACKEND=redis KEY_STORE_REDIS_URL=redis://localhost:6379 KEY_STORE_REDIS_NAMESPACE=peer17-pair2 \
DEVICE_AGENT_HEALTH_PORT=8082 \
python /path/to/stls-ai/python/stls-core/sidecar/arqit_device_agent.py
```

**2. Start the two SERVER-role sidecars, consumer mode** — same Redis
namespace as the matching device-agent, no `ARQIT_PY_SERVER_CONFIG` at
all (a consumer never reads the device's own config):
```bash
# peer_a's SERVER role -- consumer of pair2-server
ARQIT_SIDECAR_TOKEN=peer-a-server-token KEY_PROVIDER_NAME=arqit_py \
ARQIT_PY_DEVICE_ROLE=consumer KEY_STORE_BACKEND=redis KEY_STORE_REDIS_URL=redis://localhost:6379 KEY_STORE_REDIS_NAMESPACE=peer17-pair2 \
python /path/to/stls-ai/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-17-peer-a-server.sock

# peer_b's SERVER role -- consumer of pair1-server
ARQIT_SIDECAR_TOKEN=peer-b-server-token KEY_PROVIDER_NAME=arqit_py \
ARQIT_PY_DEVICE_ROLE=consumer KEY_STORE_BACKEND=redis KEY_STORE_REDIS_URL=redis://localhost:6379 KEY_STORE_REDIS_NAMESPACE=peer17-pair1 \
python /path/to/stls-ai/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-17-peer-b-server.sock
```

**3. Start the two CLIENT-role sidecars, owner mode** — real
negotiation, same as 14_ (each resolves relative `DEVICE_METADATA_FILEPATH`
etc. against its own cwd in general, but this sample's devices use
absolute paths, so cwd doesn't actually matter here):
```bash
# peer_a's CLIENT role -- pair1-client (paired with peer_b's SERVER role)
ARQIT_SIDECAR_TOKEN=peer-a-client-token ARQIT_PY_CLIENT_CONFIG=/path/to/stls-ai/samples/peer/17_peer_a_core/aq-sf-peer17-pair1-client.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/stls-ai/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-17-peer-a-client.sock

# peer_b's CLIENT role -- pair2-client (paired with peer_a's SERVER role)
ARQIT_SIDECAR_TOKEN=peer-b-client-token ARQIT_PY_CLIENT_CONFIG=/path/to/stls-ai/samples/peer/17_peer_b_core/aq-sf-peer17-pair2-client.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/stls-ai/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-17-peer-b-client.sock
```

**4. Start the two peers** (`.env` on both already set the matching
`ARQIT_SIDECAR_SOCKET_*`/`ARQIT_SIDECAR_TOKEN_*` pairs):
```bash
cd samples/peer/17_peer_b_core && KEY_PROVIDER_NAME=arqit_py npx tsx --env-file=.env peer.ts
cd samples/peer/17_peer_a_core && KEY_PROVIDER_NAME=arqit_py npx tsx --env-file=.env peer.ts
```

That's **6 processes total** (2 device-agents + 2 consumer sidecars +
2 owner sidecars) versus 14_'s 4 (all owner sidecars) — the cost of
demonstrating the split on a sample where each peer genuinely plays
both roles at once, rather than N identical replicas of one role.

## Validated (2026-11-19)

Real device registration + provisioning for all 4 devices, then the
full 6-process flow, both providers:

| Provider | Channel #1 (`peer_a`→`peer_b`) | Channel #2 (`peer_b`→`peer_a`) | Result |
|---|---|---|---|
| `pqc` | real X25519+ML-KEM-768 | real X25519+ML-KEM-768 | PASS |
| `arqit_py` | real device auth (pair 1, owner) + consumer resolve (pair 2) | real device auth (pair 2, owner) + consumer resolve (pair 1) | PASS |

Confirmed from the consumer sidecars' own logs: zero `"About to
authenticate"` lines on either SERVER-role sidecar — both resolved
their session purely `via hint_kid` from Redis, fed by the matching
device-agent.
