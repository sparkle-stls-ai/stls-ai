# 14_peer_b_core

See [`samples/peer/14_peer_a_core/README.md`](../14_peer_a_core/README.md)
for the full documentation — architecture, why `direction` isn't used
here, the 4-sidecar/device-pairing setup for `arqit_py`, running
instructions, and the validated results table. `peer.ts` in this
directory is byte-identical to `14_peer_a_core`'s; the two peers differ
only in `.env` (`PEER_NAME`, `PORT`, `PEER_URL`, `INITIATE`, and — for
`arqit_py` — which Arqit device pair each role uses).
