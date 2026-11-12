# 13_client_core — core-only, direction-labeled, bidirectional sample

Client half of a minimal 2-peer-node scenario using **only
`node/stls-core`** — see
[`samples/server/13_server_core/README.md`](../../server/13_server_core/README.md)
for the routes, the DIDComm-shape rationale, and device-reuse note.

## What this demonstrates

- `getTransport({..., direction: "c2s"})` — establishes a session (via a
  real handshake/KEM exchange, for every provider) and builds a
  `StlsTransport` labeled `"c2s"`. The server labels its own side `"s2c"`.
  Each direction gets independent key material (issue #12); neither side
  can decrypt its own output (see `StlsTransport`'s own test suite,
  T16-T18, for the unit-level proof).
- One HTTP round trip exercises **both** directions: the request body is
  the client's `c2s`-encrypted envelope, the response body is the
  server's `s2c`-encrypted reply — not two separate connections.
- The plaintext the envelopes carry is shaped like a DIDComm v2 message
  (`id`/`type`/`from`/`to`/`created_time`/`body`, with the pong's `thid`
  referencing the ping's `id`) — exploring message-shape compatibility
  only; `StlsTransport` remains the only actual encryption.

## Install

```bash
cd samples/client/13_client_core
npm install
```

## Running

Start the server first (see its README), then:
```bash
cd samples/client/13_client_core
npx tsx --env-file=.env client.ts
```

`KEY_PROVIDER_NAME=base|pqc|arqit_py`. For `arqit_py`, launch the client
sidecar first (mirrors [`samples/client/12_client_mcp_ms`](../12_client_mcp_ms/README.md)):

```bash
cd samples/client/12_client_mcp_ms   # cwd matters, same reason as the server
ARQIT_SIDECAR_TOKEN=some-shared-secret \
ARQIT_PY_CLIENT_CONFIG=./aq-sf-node-mcp-client-ms-1.json \
KEY_PROVIDER_NAME=arqit_py \
python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-13-client-core.sock --token some-shared-secret
```

The script exits via `process.exit(0)` after printing its result — the
`arqit_py` sidecar connection is a lingering open handle with no
explicit close in this one-shot script (same as every other sample's
`client_multi_*.ts`), which would otherwise hang the process past its
single request/response.

## Validated (2026-11-12)

Real network round-trips end to end for all three providers — PASS on
each, output transcript below.

| Provider | Handshake | Result |
|---|---|---|
| `base` | `/stls/handshake`, fixed dev key | PASS |
| `pqc` | `/stls/pqc/handshake`, real X25519+ML-KEM-768 KEM exchange | PASS |
| `arqit_py` | no HTTP handshake (key resolved via the real Arqit device, through the Python sidecar) — still a real HTTP `/ping` round trip for the message exchange itself | PASS |

Example (`provider=pqc`):
```
=== 13_ core-only bidirectional ping/pong (Node) — provider=pqc ===
  session established: session_id=10a38e3a-310e-47d9-b5bc-e17a3164d219
  sending (c2s): {"text":"hello from client-core (provider=pqc)"}
  received (s2c): {"text":"HELLO FROM CLIENT-CORE (PROVIDER=PQC) — ACK FROM SERVER-CORE"}
RESULT: PASS
```

Same shape (session established, c2s sent, s2c received, PASS) confirmed
for `base` and `arqit_py` — the latter with real Arqit device
authentication and bilocation/P2P key agreement on both the client and
server sidecars.
