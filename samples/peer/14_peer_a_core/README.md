# 14_peer_a_core / 14_peer_b_core — two independent channels, symmetric peers

Companion to [`samples/{server,client}/13_{server,client}_core`](../../server/13_server_core/README.md),
testing a **different shape of bidirectionality** than 13_ — see that
README first for the DIDComm-shape rationale and the device-reuse
convention, both shared with this sample.

## Why this sample exists (vs. 13_)

13_ has **one** session/connection: a fixed client and a fixed server,
made bidirectional by labeling the two directions of that SAME session
with `direction` (`"c2s"`/`"s2c"`, issue #12) — the request and the
reply-carried-in-the-same-HTTP-response use independent keys.

This sample instead has **two fully independent channels**, each
strictly one-way, and — the actual point of it — **both peers run both
roles**. There is no fixed "the client"/"the server": `peer_a` and
`peer_b` run the exact same script (`peer.ts`, byte-identical in both
directories), differing only in `.env` (`PORT`, `PEER_URL`, `INITIATE`).
This is the shape a NaaS federation PoC between two genuinely symmetric
peer nodes needs — unlike 13_, where one side is permanently cast as
"the client" and the other as "the server".

**`direction` is deliberately NOT used anywhere in this sample.** Each
of the two channels only ever flows one way (its sender always
`encrypt()`s, its receiver always `decrypt()`s), so each channel's
`StlsTransport` is built with `direction` left unset — plain
`getTransport({...})`/`performHandshake(...)` with no direction
argument, the same single-shared-key mode `stls-a2a`/`stls-mcp` already
use. Here `direction` would only be a descriptive label (which way this
particular channel flows) — it adds no real cryptographic separation,
because the two channels are *already* cryptographically independent
by construction: two separate handshakes mean two separate
session_ids and two separate derived keys, regardless of any label. In
13_, by contrast, `direction` is load-bearing: without it, the single
session's two directions would share the very same key (see 13_'s T17
test: a transport can't decrypt its own output specifically *because*
of `direction` — remove it, and it could).

## Flow

With `peer_a` started with `INITIATE=1` (`peer_b` is not):

1. Both peers start listening — `/notify`, `/stls/handshake`,
   `/stls/pqc/handshake` all exist on **both** peers, since both can be
   contacted.
2. `peer_a`, acting as a CLIENT, opens **channel #1** toward `peer_b`
   (its own handshake, its own session_id/keys) and `POST`s a
   DIDComm-shaped "ping" to `peer_b`'s `/notify`.
3. `peer_b`'s `/notify` handler decrypts it on channel #1, replies with
   a bare technical ack on that *same* HTTP response (no application
   payload travels back here — that would just be 13_'s pattern
   again), then reacts by becoming a CLIENT **itself**: opens
   **channel #2** (a brand-new, independent handshake/session) toward
   `peer_a` and `POST`s the modified "pong" to `peer_a`'s `/notify`.
4. `peer_a`'s `/notify` handler decrypts the pong on channel #2 — a
   *different* `StlsTransport` than the one it used to send the ping
   on channel #1 — completing the round trip via two independent
   channels.

Both processes exit (`process.exit(0)`) once their part of the
exchange completes — `peer_a` after receiving the pong, `peer_b` right
after successfully sending it — so the whole thing runs as a clean,
one-shot, scriptable check like every other sample.

## Install

```bash
cd samples/peer/14_peer_a_core && npm install
cd samples/peer/14_peer_b_core && npm install
```

`stls-core` is a `file:` dependency — build it first:
```bash
cd /path/to/stls-ai/node/stls-core && npm install && npm run build
```

## Running (`base`/`pqc` — no sidecar needed)

```bash
# terminal 1
cd samples/peer/14_peer_b_core
KEY_PROVIDER_NAME=base npx tsx --env-file=.env peer.ts

# terminal 2 (after peer_b is listening)
cd samples/peer/14_peer_a_core
KEY_PROVIDER_NAME=base npx tsx --env-file=.env peer.ts
```

`peer_a`'s output ends with `RESULT: PASS` (or an uncaught exception on
failure); `peer_b` logs its own side and exits once it has sent the
pong. Swap `base` for `pqc` to exercise the real X25519+ML-KEM-768 KEM
exchange on both channels (two independent KEM negotiations, one per
channel).

## Running (`arqit_py`) — 4 sidecars, non-obvious device pairing

Each peer plays **both** roles (contacted as a server, initiates as a
client), so each one needs **two** sidecars — one per role, each
bound to its own Arqit device identity (`ARQIT_SIDECAR_SOCKET_SERVER`/
`_CLIENT` + matching `ARQIT_SIDECAR_TOKEN_SERVER`/`_CLIENT`, read by
this script itself, not by the sidecar — same convention as
[`samples/server/12_server_mcp_ms`](../../server/12_server_mcp_ms/README.md)).
That's **4 sidecar processes total** for one run of this sample.

**The non-obvious part, found the hard way:** Arqit's bilocation/P2P
key agreement only works between two *specific, already-paired*
devices — not an open mesh where any client device can reach any
server device. `samples/server/12_server_mcp_ms`/
`samples/client/12_client_mcp_ms` provisioned exactly two such pairs:

| Pair | Client device | Server device |
|---|---|---|
| 1 | `sf-node-mcp-client-ms-1` | `sf-node-mcp-server-ms-1` |
| 2 | `sf-node-mcp-client-ms-2` | `sf-node-mcp-server-ms-2` |

For channel #1 (`peer_a` CLIENT → `peer_b` SERVER) to actually agree on
a key, `peer_a`'s CLIENT-role device and `peer_b`'s SERVER-role device
must be the **same pair**. Symmetrically for channel #2 (`peer_b`
CLIENT → `peer_a` SERVER). Picking devices "by number" (`peer_a` ↔ pair
1, `peer_b` ↔ pair 2) is wrong and fails — confirmed directly: it
produces `[KeySessionProviderArqitPy] hint_kid=... never became
available within 30s` on the handshake, because `peer_a`'s CLIENT
(pair 1) and `peer_b`'s SERVER (pair 2, if assigned that way) were
never paired with each other. The working assignment, each peer's two
roles coming from **different** pairs:

| | SERVER role (contacted) | CLIENT role (initiates) |
|---|---|---|
| `peer_a` | `sf-node-mcp-server-ms-2` (pair 2 — paired with `peer_b`'s CLIENT) | `sf-node-mcp-client-ms-1` (pair 1 — paired with `peer_b`'s SERVER) |
| `peer_b` | `sf-node-mcp-server-ms-1` (pair 1 — paired with `peer_a`'s CLIENT) | `sf-node-mcp-client-ms-2` (pair 2 — paired with `peer_a`'s SERVER) |

No dedicated devices were provisioned for this sample (same
not-self-serviceable-here rationale as 13_) — this reuses 12_'s two
pairs, cross-wired as the table above.

Launch all 4 sidecars (each `ARQIT_PY_{SERVER,CLIENT}_CONFIG` resolves
relative paths against *that sidecar's own* cwd — launch each from the
matching `12_*` sample directory):

```bash
# peer_a's SERVER role — device pair 2
cd samples/server/12_server_mcp_ms/server_mcp_ms_2
ARQIT_SIDECAR_TOKEN=peer-a-server-token ARQIT_PY_SERVER_CONFIG=./aq-sf-node-mcp-server-ms-2.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-14-peer-a-server.sock --token peer-a-server-token

# peer_a's CLIENT role — device pair 1
cd samples/client/12_client_mcp_ms
ARQIT_SIDECAR_TOKEN=peer-a-client-token ARQIT_PY_CLIENT_CONFIG=./aq-sf-node-mcp-client-ms-1.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-14-peer-a-client.sock --token peer-a-client-token

# peer_b's SERVER role — device pair 1
cd samples/server/12_server_mcp_ms/server_mcp_ms_1
ARQIT_SIDECAR_TOKEN=peer-b-server-token ARQIT_PY_SERVER_CONFIG=./aq-sf-node-mcp-server-ms-1.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-14-peer-b-server.sock --token peer-b-server-token

# peer_b's CLIENT role — device pair 2
cd samples/client/12_client_mcp_ms
ARQIT_SIDECAR_TOKEN=peer-b-client-token ARQIT_PY_CLIENT_CONFIG=./aq-sf-node-mcp-client-ms-2.json KEY_PROVIDER_NAME=arqit_py \
  python /path/to/python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-14-peer-b-client.sock --token peer-b-client-token
```

Then start `peer_b` first, `peer_a` second (`.env` on both already set
the matching `ARQIT_SIDECAR_SOCKET_*`/`ARQIT_SIDECAR_TOKEN_*` pairs):
```bash
cd samples/peer/14_peer_b_core && KEY_PROVIDER_NAME=arqit_py npx tsx --env-file=.env peer.ts
cd samples/peer/14_peer_a_core && KEY_PROVIDER_NAME=arqit_py npx tsx --env-file=.env peer.ts
```

## Validated (2026-11-12)

All three providers, real network round-trips on **both** independent
channels (real PQC KEM exchange x2, real Arqit device auth +
bilocation/P2P key agreement x2 for `arqit_py`, with the cross-wired
device pairing above):

| Provider | Channel #1 (`peer_a`→`peer_b`) | Channel #2 (`peer_b`→`peer_a`) | Result |
|---|---|---|---|
| `base` | fixed dev key | fixed dev key | PASS |
| `pqc` | real X25519+ML-KEM-768 | real X25519+ML-KEM-768 | PASS |
| `arqit_py` | real device auth + bilocation (pair 1) | real device auth + bilocation (pair 2) | PASS |

Example (`provider=base`), two distinct `session_id`s — proof the two
channels never share a session, let alone a key:
```
peer-a listening on port 8401 (KEY_PROVIDER_NAME=base)
[peer-a] opened its own channel toward http://localhost:8402 (session_id=2b7c276f-...) to send: {"text":"hello from peer-a (provider=base)"}
[peer-a] received on this channel (session=44c79767-...): {"text":"HELLO FROM PEER-A (PROVIDER=BASE) — ACK FROM PEER-B (separate channel)"}
[peer-a] round trip complete via two independent channels
RESULT: PASS
```
