# 17_peer_b_core

See [`samples/peer/17_peer_a_core/README.md`](../17_peer_a_core/README.md)
for the full documentation — the device-agent/consumer split vs. 14_'s
all-owner sidecars, the 6-process `arqit_py` setup, running
instructions, and the validated results. `peer.ts` in this directory
is byte-identical to `17_peer_a_core`'s; the two peers differ only in
`.env` (`PEER_NAME`, `PORT`, `PEER_URL`, `INITIATE`, and — for
`arqit_py` — which Arqit device pair each role uses).
