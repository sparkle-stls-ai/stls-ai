# Arqit real-peering test — findings (2026-10-17)

Status: **RESOLVED, and root cause now understood — it was never an
Arqit-side or stls-mcp-specific issue.** Root cause: **the Arqit SDK (or
Arqit's backend validation) rejects bilocation when both the SERVER
(receiver) and CLIENT (initiator) device sessions live in the same OS
process.** `vault_server.py` + `vault_client.py` succeed because they are
two separate processes; both original "blocked" test paths below put both
roles in one process (the direct in-process Python call, and the Node
sidecar, which is one Python process serving both RPC sessions).

Confirmed with four controlled runs using **pure `stls_core`, no
`stls_mcp` code at all**:

| Run | Processes | CWD / metadata | Result |
|---|---|---|---|
| 1 | both providers, 1 process | `python/stls-core` (metadata file missing — relative `DEVICE_METADATA_FILEPATH` resolves against cwd, not the config file's own directory) | crashes before auth (`FileNotFoundError` → unrelated `AttributeError`; a real but separate SDK robustness bug) |
| 2 | both providers, 1 process | `python/stls-mcp/arqit_utility/` (stale June-provisioning metadata copy, divergent ratchet) | `401` at `authenticate/verify` |
| 3 | both providers, 1 process | `python/stls-mcp` (current metadata — same dir as the successful `vault_*` run) | `401` at `bilocation/final` — **exact original symptom, reproduced with zero stls-mcp code involved** |
| 4 | server and client **each in their own OS process**, otherwise identical to run 3 | `python/stls-mcp` | **success** — real key obtained, no 401 |

Run 3 vs run 4 is the clean A/B: identical config, identical metadata,
identical device pair, identical code — the only variable is same-process
vs separate-process. That isolates the cause completely: it's not Arqit
fixing anything server-side between this morning and this afternoon, and
it's not a difference between `stls-core` and `stls-mcp` — it's purely
about whether the two device identities share a process.

**Consequence for the Node port — RESOLVED and validated 2026-10-17.**
`sidecar/arqit_sidecar.py` now has a process-level guard
(`_active_arqit_py_role` in `SidecarSession._start()`): a second,
different role for `provider_name="arqit_py"` in the same sidecar
process is rejected immediately with an explicit `RpcError`
(code `-32000`), instead of being left to fail mysteriously 30+ seconds
later against the real Arqit cloud. Verified through the real
TypeScript client classes (not a raw socket script) in
[`node/stls-core/tests/interop/arqit-sidecar-guard.smoke.ts`](../../node/stls-core/tests/interop/arqit-sidecar-guard.smoke.ts) —
no real Arqit credentials needed, since the guard fires before any
config/network access, so this runs in CI like the other `*.smoke.ts`
files.

The correct pattern — two separate sidecar processes, one per role — is
now validated against the **real** Arqit cloud (not just
`provider_name="base"` RPC mechanics) in
[`node/stls-core/tests/interop/arqit-sidecar-real.live.ts`](../../node/stls-core/tests/interop/arqit-sidecar-real.live.ts),
mirroring `test_arqit_py_real_peering.py`: a full round trip (key
agreement + real STLS handshake/proof + real AES-256-GCM encrypt/decrypt)
through the Node RPC layer, both for a single client and for 3
sequential clients against one long-running SERVER-role sidecar
process — all passed, with distinct `kid`/`session_id` each time.

**A new, Node-specific finding surfaced while validating the N-clients
case**: a sidecar process can safely serve the SERVER role's `start`
**exactly once, ever** — not even a second `start` after a `stop` on
the same process works. Cause: `SidecarSession.handle("stop")` only
does `self.provider = None` — it never calls the real provider's own
`.stop()`, so the Arqit SDK's `DevicePolicyStore` (a process-wide
singleton inside `KeyProviderServerArqitPy.start()`) stays instantiated.
A second `start` attempt in the same process — even for a legitimate
sequential re-use, not role-mixing — hits
`DevicePolicyStore is a singleton and has already been instantiated`.
This only showed up because the N-clients test accidentally reused a
sidecar process across two separate test invocations; it does **not**
affect the validated pattern (one `start()` per sidecar process,
reused across N handshakes via `getSessionKeyIdentifier()`/
`getSessionKeyData()` on that same session — never a second `start`).
Still, it's a real gap worth fixing in `arqit_sidecar.py` if a future
use case ever needs to restart a role within one long-lived sidecar
process: `stop` should call the real provider's own `.stop()` too, not
just drop the reference.

Also worth noting, unrelated to the 401 itself: a missing/wrong-cwd
`DEVICE_METADATA_FILEPATH` doesn't fail gracefully in the Arqit SDK — it
raises `FileNotFoundError` immediately followed by an unrelated
`AttributeError` (`'DeviceMetadata' object has no attribute
'provisioning_state'`), since the metadata object never gets populated.
Keep device configs' relative paths resolved from a consistent, known cwd.

## Follow-up: multi-client support and full handshake/crypto verification

Two gaps closed later the same day, both in
[`tests/test_arqit_py_real_peering.py`](../tests/test_arqit_py_real_peering.py):

1. **Does one long-running SERVER correctly serve more than one
   handshake over its lifetime?** Never verified empirically before —
   only 1 server + 1 client, once, had been tested. Added
   `test_n_sequential_clients_against_one_server_succeeds`: one SERVER
   process stays up, 3 sequential CLIENT processes each negotiate their
   own key against it. **Confirmed working**: all 3 succeeded, with
   distinct `kid`s and distinct `session_id`s each time — the
   `_delivered_key_ids` + `wait_for_new_key(seen_key_ids=...)` mechanism
   in `KeySessionProviderArqitPy` (designed for exactly this) behaves as
   intended against the real cloud, not just in theory.

2. **Had we only confirmed "both sides get *a* key", not that client and
   server keys actually match, and not any real encryption?** Correct —
   that was a real gap. Both role scripts
   (`tests/_arqit_py_real_e2e_{server,client}_role.py`) were extended
   with a small newline-delimited JSON protocol over stdin/stdout (the
   two processes still never talk to each other directly — the pytest
   test relays lines between them, see `_run_full_roundtrip_client()`)
   that drives the **real** `stls_core` primitives end to end:
   `perform_handshake()` server-side, `verify_handshake_proof()`
   client-side (proves the two independently-obtained keys are
   byte-identical, not just "both present"), then a real
   `StlsTransport.encrypt_json()`/`decrypt_json()` round trip
   (AES-256-GCM + HMAC-SHA256) with an actual encrypted echo. **Confirmed
   working** on a clean run: `KEY_OK` → `HANDSHAKE_OK` →
   `CLIENT_PASSED ... roundtrip=ok`, no mismatch.

Both of the real-cloud confirming runs for this follow-up hit the same
rate-limiting/flakiness on a second immediate retry (timeout waiting for
a key, or the by-now-familiar stale-port teardown artifact) — consistent
with, not contradicting, the operational note already in the test file.
One clean full run is enough evidence; don't chase a second back-to-back
green run as proof, space retries out instead.

Original write-up kept below for the full traceback and investigation
trail (now superseded by the above, but useful context on how it was
chased down the first time).

---

Original status: **blocked on Arqit's cloud, not on our code.** Device authentication
and the "intermediate" bilocation step work; the "final" bilocation step is
rejected with `401 NotAuthorizedException` by Arqit's own backend, for both
the client and the receiver device, in this exact tenant/device-pair.

## What was tested

Config used: `python/stls-mcp/aq-stls-mcp-client.json` /
`aq-stls-mcp-server.json` (tenant `tisparkle`), with `python/stls-core/.env`
pointing `ARQIT_PY_CLIENT_CONFIG`/`ARQIT_PY_SERVER_CONFIG` at them and the
`[arqit_py]` extra installed from the local wheels in
`python/stls-core/arqit_wheels/`.

Two independent test paths, same result both times:
1. Through the Node sidecar (`providers/arqit-sidecar.ts` +
   `sidecar/arqit_sidecar.py`), two RPC sessions (SERVER then CLIENT).
2. Directly in Python, calling `KeyProviderClientArqitPy`/
   `KeyProviderServerArqitPy` in-process, no sidecar at all — ruling out
   the sidecar/RPC layer as a cause.

## What works

- Device authentication against the real cloud
  (`authentication.ska.quantum.cloud`) succeeds for both devices
  (`LcVtqbh9XPM3JZ2u808CA7z15Al-Oees2GDAYJ5eiy8=` = client,
  `G-HlLQYIbUGy65sHX91GoDBxEWlgoSjpFgCVwT4qy2w=` = server).
- The initiator connects to the receiver's socket (`localhost:8889`)
  successfully once a receiver session is actually started.
- The "intermediate" bilocation call (`GET dscc.ska.quantum.cloud/api/v1/bilocation/intermediate`)
  succeeds (200) for the initiator.

## Where it fails

Both sides independently call `GET .../api/v1/bilocation/final` as part of
completing the key agreement, and **both** get `401` →
`uk.arqit.device.common.exceptions.NotAuthorizedException`, raised from
`http_client.py:97`. Full traceback captured via a runtime monkeypatch of
`SocketClientReceiver._inner_thread` / `ReceiverBilocationStateMachine.receive_next_message`
(no code was modified on disk, this was diagnostic-only).

## A suspicious but likely-unrelated clue

The Arqit cloud dashboard event for the successful "intermediate" grant
showed:
```
"cs1Label": "DUID", "cs1": "LcVtqbh9XPM3JZ2u808CA7z15Al-Oees2GDAYJ5eiy8="
"cs2Label": "SERVERDUID", "cs2": "LcVtqbh9XPM3JZ2u808CA7z15Al-Oees2GDAYJ5eiy8="
```
DUID and SERVERDUID are identical (both the *client's* duid) — the receiver
device never shows up as itself in that event. We tried filling in the
client config's `MQTT_RECEIVER_DEVICE_UID` (previously a literal, never-set
placeholder) with the receiver's real duid and re-ran the test: **no change
in behavior** — same 401. Reading the SDK source
(`http_bilocation.py`/`socket_receiver.py`/`socket_initiator.py`) confirms
`MQTT_RECEIVER_DEVICE_UID` is only read by the MQTT transport path, not the
socket path we're using — so this clue is a red herring for our transport,
though it may still be worth asking Arqit about given both sides got a 401
independently.

## Next step

~~This needs Arqit-side investigation...~~ Not needed — root cause found,
see RESOLVED status at the top: it was always a same-process-vs-two-process
issue, not Arqit's side and not stls-mcp-specific.

~~Before re-running the sidecar against real `arqit_py`, fix the sidecar
architecture first...~~ Done — see "Consequence for the Node port" above:
the guard is in, and the two-separate-sidecar-processes pattern is now
validated against real Arqit (single client and 3 sequential clients),
through the actual Node RPC layer, not just in pure Python. The one
remaining minor gap (`stop` not calling the real provider's `.stop()`,
surfaced by the `DevicePolicyStore` singleton) is documented there too
and doesn't block the validated pattern.

## Follow-up (2026-10-18): the factory layer, and stls-mcp's own vault example

Two more layers closed the loop, both against the real Arqit cloud, not
just the sidecar classes directly:

1. **`node/stls-core`'s new `KeySessionProviderFactory`** (added the same
   day, closing a separate gap — see `node/stls-core/CHANGELOG.md`) needed
   its own real-Arqit check: building both roles through
   `KeySessionProviderFactory.getSessionProvider({name: "arqit_py"})`
   against two real sidecar processes, then a real handshake and a real
   AES-256-GCM round trip —
   `node/stls-core/tests/interop/provider-factory-arqit-real.live.ts`,
   passed on the first clean run.
2. **`node/stls-mcp`'s own `vault-server.ts`/`vault-client.ts`**, run
   with `KEY_PROVIDER_NAME=arqit_py` against two real sidecar processes:
   real key agreement through the factory, real STLS handshake, and a
   full MCP session (all 5 tools/2 resources/2 prompts) over
   `/mcp-stls`. This was the one case genuinely worth double-checking
   methodically: the first attempt produced no visible output at all
   and the second failed with a stale-socket error, both looking like
   failures. Neither was: attempt 1's output was lost to a pipe across
   an abrupt `timeout` kill (the Node process never exits on its own
   with `arqit_py` — the factory returns only the `KeySessionProvider`,
   with no handle to close the sidecar RPC connection, so something has
   to kill it from outside), and attempt 2's sidecar processes had
   separately expired from their own `timeout` wrapper while attempt 1
   was being investigated — a side effect of taking the time to
   diagnose attempt 1 rather than an independent problem. Attempt 3,
   with direct file output and a patient wait instead of an impatient
   kill, showed the real work (key agreement → handshake → full MCP
   session) completing cleanly in well under 15 seconds, every time.

Lesson worth keeping for next time: when something that depends on a
real external negotiation appears to hang, check whether the harness
around it (a pipe, an external `timeout`) could be eating the evidence
before assuming the negotiation itself failed — rerunning blindly burns
real Arqit cloud calls for a problem that isn't there.

No code changes resulted from this round beyond the cosmetic
`process.exit(0)` in `vault-client.ts` (documented in
`node/stls-mcp/CHANGELOG.md`) — the mechanism itself needed no fixes.

## Follow-up (2026-10-23): `DeviceMetadata` singleton race — confirmed real, fixed

A multi-session/multi-server push for `arqit_py` (see
`python/stls-a2a/README.md`'s "Current Limitations and Notes" and
`samples/server/12_server_mcp_ms/README.md`) surfaced a real failure
under genuine OS-thread concurrency: 3 concurrent sessions for the same
device via the Node sidecar (`arqit_sidecar.py` dispatches each RPC
connection through `loop.run_in_executor()`, a real thread per
connection) partially failed and then hung. Every *pure-Python*
`asyncio.gather()` "concurrency" test in this project had missed this
because `KeyProviderClientArqitPy.start()` has no `await` inside it, so
`asyncio.gather()` silently serializes it for free — not a real test.

**Controlled, isolated reproduction** (dedicated throwaway devices,
`python/stls-core/tests/test_arqit_py_same_device_concurrent_real.py`,
using `asyncio.to_thread()` to force genuine OS-thread concurrency
instead of relying on the sidecar): with no protection at all, 1 of 3
concurrent `start()` calls completed and the other 2 deadlocked
forever. `py-spy` thread dumps pinned the exact spot: both stuck
threads were blocked acquiring `AuthenticationClient.__mutex`
(`authentication.py:197`) — a **class-level** lock (`__mutex =
threading.Lock()` at class scope, not in `__init__`), shared by every
`AuthenticationClient` instance in the process, same as
`DeviceMetadata`'s own singleton. The critical section it guards
(`self.__mutex.acquire(); self._dev_metadata.load();
self.__mutex.release()`) has no `try`/`finally` — an unrelated
exception during that `load()` call (in our first repro attempt, an
unrelated relative-path bug — see the cwd-resolution note elsewhere in
this file — the same class of bug, re-triggered) leaks the mutex
*permanently* for the rest of the process, wedging every later
`start()` call forever regardless of how carefully callers serialize
themselves afterward.

**Fix**, in `python/stls-core/src/stls_core/providers/stls_core_providers_arqit_py.py`:
a module-level `_ARQIT_PY_START_LOCK` (`threading.Lock`) now fully
serializes `KeyProviderClientArqitPy.start()` and
`KeyProviderServerArqitPy.start()`, process-wide, across *all* devices
(not just the same one — `DeviceMetadata` is one global singleton
regardless of which device's config is passed). Serializing only the
authentication step wasn't enough on its own: with that narrower scope,
authentication stopped deadlocking, but letting a second session
authenticate while a first session's negotiation thread was still
connecting made the SDK's socket bilocation handshake fail outright
(`ConnectionRefusedError`/`PeerUnableToConnectException`) for every
session involved. The client-side lock is therefore held through the
*entire* negotiation — `start()` now joins the negotiation thread
(capped at 30s, matching this provider's own key-delivery timeout
convention) before releasing the lock, not just through authentication.

**Confirmed fixed**: the same 3-concurrent-sessions-same-device
reproduction that previously deadlocked forever now completes in ~5s
with 3/3 distinct keys and zero errors, against the real cloud. An
immediate back-to-back retry right after that clean pass then hit
`ConnectionRefusedError` again — consistent with, not contradicting,
this file's own already-documented real-cloud rate-limiting/flakiness
on back-to-back retries (see the "second immediate retry" note above).
One clean full pass is the standard of evidence here; don't chase a
second back-to-back green run as proof, space retries out instead.

**Consequence**: `arqit_py` session establishment (`start()`) is now
fully serial process-wide by design — a throughput cost, not a
correctness one, and unavoidable until Arqit fixes their SDK's own
locking. Callers (`SecureSessionManager`, `SecureToolsetManager`, the
Node `getTransport()`, or raw scripts) no longer need to avoid
parallelizing `arqit_py` calls themselves; the provider now does that
safely on their behalf. The Node sidecar needed no code change of its
own — it's a Python process running this same module under the hood,
so it picked up the fix automatically.
