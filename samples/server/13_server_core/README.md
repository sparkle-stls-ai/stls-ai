# 13_server_core — core-only, direction-labeled, bidirectional sample

Server half of a minimal 2-peer-node scenario using **only `node/stls-core`**
— no `stls-a2a`, no `stls-mcp`. Demonstrates the shape the NaaS federation
PoC actually needs: two nodes that label their own transport direction
(`"c2s"`/`"s2c"`, issue #12) and exchange a message **in both directions
over one request/response**, instead of the request/response-only pattern
`stls-a2a`/`stls-mcp` use (which don't need `direction` at all — see
[`samples/client/13_client_core/README.md`](../../client/13_client_core/README.md)
for the full client-side rationale and the real-world validation matrix).

Message bodies follow the **DIDComm v2 plaintext message shape**
(`id`/`type`/`from`/`to`/`created_time`/`body`) — format-compatibility
exploration only, not real DIDComm crypto (no JWE/JWS, no DID resolution).
`StlsTransport` (AES-256-GCM + HMAC, direction-labeled) is the only
encryption layer. Real DIDComm integration is explicitly out of scope for
`stls-core` — see the project's "DIDComm evolution" backlog note.

## Routes (hand-rolled with `node:http`, no framework)

| Route | Used by | Handler |
|---|---|---|
| `POST /stls/handshake` | `base`, `arqit_py` | `performHandshake()` |
| `POST /stls/pqc/handshake` | `pqc` | `performPqcHandshake()` |
| `POST /ping` | all | decrypts the client's `c2s` envelope, logs it, replies with a `s2c`-encrypted "pong" (the original text uppercased + a marker) |

Both handshake calls are made with `direction="s2c"` so the resulting
`StlsTransport` encrypts outgoing traffic with the s2c key and decrypts
incoming traffic with the c2s key — the opposite of the client (see the
client README). This requires `direction` to be threaded through
`SessionCache.put()`/`performHandshake()`/`performPqcHandshake()`, which
didn't exist before this sample (see issue #11's comment thread for the
exact files touched in `node/stls-core`) — it only existed on
`StlsTransport`'s own constructor until now.

## Install

```bash
cd samples/server/13_server_core
npm install
```

`stls-core` is a `file:` dependency on `node/stls-core` — build it first:
```bash
cd /path/to/stls-ai/node/stls-core && npm install && npm run build
```

## Running

```bash
cd samples/server/13_server_core
npx tsx --env-file=.env server.ts
```

`KEY_PROVIDER_NAME=base|pqc|arqit_py` — same switch as every other sample,
set in `.env` or overridden inline.

For `arqit_py`, this script only talks to an already-running Python
sidecar via `ARQIT_SIDECAR_SOCKET_SERVER` — launch it yourself (same
pattern as [`samples/server/12_server_mcp_ms`](../12_server_mcp_ms/README.md#running)):

```bash
cd samples/server/12_server_mcp_ms/server_mcp_ms_1   # cwd matters: relative device-config paths resolve against it
ARQIT_SIDECAR_TOKEN=some-shared-secret-1 \
ARQIT_PY_SERVER_CONFIG=./aq-sf-node-mcp-server-ms-1.json \
KEY_PROVIDER_NAME=arqit_py \
python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-13-server-core.sock --token some-shared-secret-1
```

**Device reuse, not a dedicated pair:** unlike every other `1x_` sample,
this one reuses `12_server_mcp_ms/server_mcp_ms_1`'s already-provisioned
Arqit device (`sf-node-mcp-server-ms-1`) instead of a dedicated identity.
Provisioning a brand-new device isn't self-serviceable (it needs an
Arqit-side registration step, not just running a script against an
existing config) — swap `ARQIT_PY_SERVER_CONFIG`/the client's
`ARQIT_PY_CLIENT_CONFIG` for a freshly-provisioned pair if/when one
becomes available.

## Validated (2026-11-12)

All three providers, real network round-trips (real PQC KEM exchange,
real Arqit device auth + bilocation/P2P key agreement for `arqit_py`) —
see the client README for the full matrix and command transcript.
