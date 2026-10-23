# Changelog

All notable changes to **stls-core (Node.js)** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html),
and tracks the version of [`python/stls-core`](../../python/stls-core/CHAGELOG.md),
of which this package is the TypeScript port.

---

## [0.1.2] — 2026-10-18

### Added (follow-up, 2026-10-23)

- **`KeySessionResolver`** (`providers/session-resolver.ts`) — the
  TypeScript twin of `python/stls_core/providers/stls_core_providers_session_resolver.py`'s
  `KeySessionResolver`: generic resolution of `{kid, kdata}` for an
  arbitrary caller-defined `targetKey`, with a per-`targetKey` async
  lock and an optional `targetConfigs={targetKey: configPath}` map for
  `arqit_py` (falls back to the sidecar's own env var when a target
  has no entry). Validated end-to-end against the real Arqit cloud —
  one sidecar process resolving two different, already-provisioned
  device identities via `configPath` (see
  `tests/interop/key-session-resolver-multi-config.live.ts`).
- **`configPath` propagated through the Arqit sidecar RPC** —
  `ArqitSidecarOptions.configPath` and `GetSessionProviderOptions.configPath`
  (`providers/arqit-sidecar.ts`, `providers/factory.ts`), sent as
  `config_path` in the `"start"` RPC payload and read by
  `arqit_sidecar.py`'s `_start()` on the Python side (see
  `python/stls-core/CHAGELOG.md`). Previously neither side of this RPC
  carried any config override at all — every connection to a given
  sidecar process was limited to whatever `ARQIT_PY_CLIENT_CONFIG`/
  `ARQIT_PY_SERVER_CONFIG` that process's own environment pointed at.

### Fixed (follow-up, 2026-10-23, continued — concurrent `arqit_py` sessions)

Confirmed the Python-side `DeviceMetadata` singleton race (see
`python/stls-core/CHAGELOG.md`) was real, and reproducible through the
Node sidecar path specifically — the Python fix (process-wide
`_ARQIT_PY_START_LOCK`) required no Node-side change, since the
sidecar is a Python process under the hood. Once that fix stopped
concurrent sessions from deadlocking, a second bug surfaced: Node had
the **exact same** FIFO-handshake-resolution gap Python did, not
independently fixed — `HandshakeRequest` carried no kid, and the
sidecar RPC's `get_session_key_identifier` method never passed one
through even after the Python-side `getSessionKeyIdentifier()` gained
support for a hint. Fixed, mirroring the Python shape exactly:

- `providers/base.ts`: `KeySessionProvider.getSessionKeyIdentifier(hintKid?: string)` —
  `KeySessionProviderBase` accepts and ignores it (single fixed key,
  nothing to disambiguate).
- `providers/pqc.ts`: same, ignored (separate handshake flow, doesn't
  use this).
- `providers/arqit-sidecar.ts`: `ArqitSidecarKeySessionProvider.getSessionKeyIdentifier(hintKid?)`
  forwards `{kid: hintKid}` over the RPC call when given.
- `handshake.ts`: `HandshakeRequest.kid?: string` added;
  `generateHandshakeRequest(clientId, kid?)`; `performHandshake()`
  passes `request.kid` through as the hint.
- `python/stls-core/sidecar/arqit_sidecar.py`'s
  `get_session_key_identifier` RPC handler now reads an optional `kid`
  from `params` and passes it as `hint_kid` to the real provider.

Confirmed fixed: re-ran `samples/client/12_client_mcp_ms/client_multi_session.ts`
with genuinely concurrent sessions (same devices as the original
failure) — 3/3 then 6/6 clean passes, 0 errors, matching the
Python-side result exactly. Regression-checked against
`test:provider-factory-smoke`, `test:arqit-sidecar-smoke` and
`test:arqit-sidecar-guard-smoke` (`base`/`pqc` and the sidecar RPC
mechanics) — all pass unchanged.

### Added (follow-up, 2026-10-19)

- **`.env.example`** — this package had none; added, documenting every
  environment variable actually read by `src/` and `tests/` (Key
  Provider, Arqit sidecar, Session Cache, Auth, Redis/PostgreSQL,
  live/interop test-script variables), mirroring
  [`python/stls-core/.env.example`](../../python/stls-core/.env.example)
  where the variable is shared, and documenting Node-specific ones
  (`ARQIT_SIDECAR_SOCKET*`, `TEST_REDIS_URL`, `ARQIT_N_CLIENTS`) where
  it isn't.

### Fixed — CI: Arqit sidecar step always failed, for a structural reason that can't be fixed in CI

First real run of `.github/workflows/stls-core-node-interop.yml` against
this repo (`stls-ai`) failed step `[9/11]`
(`tests/interop/arqit-sidecar-guard.smoke.ts`) with an assertion
mismatch: the sidecar rejected the `arqit_py` role request with
`ImportError: ... richiede i wheel Arqit SKA-Platform™` (the wheels
aren't installed) instead of the `ARQIT_PY_SERVER_CONFIG non impostato`
error the test expects — it exercises the *config-missing* path, which
assumes the wheels ARE present and only the config path is missing.

This isn't fixable by installing the wheels in CI: `arqit_wheels/` holds
proprietary, non-redistributable SKA-Platform™ packages (see
`.gitignore` and `python/stls-core/pyproject.toml`'s `[arqit_py]`
extra) — they're deliberately never shipped anywhere, CI included.
`.github/workflows/stls-core-node-interop.yml` now sets
`SKIP_ARQIT_SIDECAR: "1"`, so step `[9/11]` (and the already
config-gated `[10/11]`/`[11/11]`) are skipped in CI. Arqit sidecar/real
peering coverage stays local-only, run manually with the wheels
installed (`uv pip install -e ".[arqit_py]" --find-links
arqit_wheels/...`, see `python/stls-core/README.md`); CI keeps
verifying Node↔Python interoperability through the `base` and PQC
(X25519 + ML-KEM-768) providers, which need no proprietary dependency.

### Added — `KeySessionProviderFactory`/`KeyProviderFactory` (`providers/factory.ts`)

A real gap, found while porting `stls-mcp` (Node): unlike Python, which
reads `KEY_PROVIDER_NAME` through a factory, this package had every
consumer (every existing test included) instantiate the concrete
provider class directly — no env-var-driven selection existed at all.
`stls-mcp`'s example scripts had grown a local, ad-hoc helper to work
around it; closing the gap here instead, so future Node consumers don't
need to reinvent it.

- **`KeyProviderFactory.getProvider(name)`** — `"base"` | `"pqc"`.
  `"arqit_py"` deliberately throws here with a pointer to
  `KeySessionProviderFactory`, since selecting it also requires knowing
  which of the two role-specific sidecar sockets to use — not a concern
  of this lower-level, role-agnostic factory.
- **`KeySessionProviderFactory.getSessionProvider({ sessionType, name?, host?, port?, socketPath? })`**
  — creates, starts, and returns a ready `KeySessionProvider` in one call.
  For `"arqit_py"`, resolves `ARQIT_SIDECAR_SOCKET_SERVER` or
  `ARQIT_SIDECAR_SOCKET_CLIENT` depending on `sessionType` — deliberately
  NOT the single role-agnostic `ARQIT_SIDECAR_SOCKET` that
  `KeyProviderArqitSidecar` itself falls back to, because that fallback
  would let SERVER and CLIENT silently resolve to the same sidecar
  process, the one scenario already proven to fail against the real
  Arqit cloud (`DevicePolicyStore` singleton / `401`, see
  `ARQIT_PEERING_NOTES.md`).
- **Simpler than Python's factory by construction, not just by omission**:
  Python's `get_session_provider()` resolves a possibly-async `start()`
  with its own `asyncio.run()`, which only works from synchronous
  top-level code — a real, documented pitfall there (calling it from
  inside a running event loop raises). Node's version is just an `async
  function` that `await`s `start()` directly (awaiting a non-Promise
  value is a no-op), so it has no such restriction.
- **Not ported as a singleton-instance class** (Python's
  `KeyProviderFactory()`/`KeySessionProviderFactory()` pattern) — plain
  static methods instead, matching the Node-idiomatic convention this
  package already established with `SessionCacheFactory.build()`, since
  none of these factories hold per-instance state.
- **`tests/interop/provider-factory.smoke.ts`** — verifies the error
  paths (missing provider name, `arqit_py` without a role-specific
  socket, `getProvider('arqit_py')`'s redirect message) and, for real,
  that a factory-built `"base"` provider completes an actual handshake
  end to end. The `arqit_py` role-aware socket resolution was also
  verified against two real (locally-spawned) sidecar processes — each
  request reached the correct process via the correct env var — without
  needing real Arqit credentials for that specific check. Wired into
  `run-interop-tests.sh` as step `[2/11]`.
- **`node/stls-mcp`'s `vault-server.ts`/`vault-client.ts`** refactored to
  use this factory instead of their local ad-hoc helper, closing the gap
  at its actual source rather than leaving the workaround in place.
- **`tests/interop/provider-factory-arqit-real.live.ts`** — the
  wiring-only `arqit_py` checks above intentionally stop short of a real
  negotiation. This one goes all the way: builds both roles through
  `KeySessionProviderFactory.getSessionProvider({name: "arqit_py"})`
  against two real sidecar processes with a real, already-provisioned
  Arqit device pair, then runs a real handshake and a real AES-256-GCM
  round trip — passed on the first run. Wired into `run-interop-tests.sh`
  as step `[11/11]`, using its own pair of sidecar sockets (distinct from
  step `[10/11]`'s, since each sidecar process can only complete its
  SERVER-role `start` once — see the "Fixed" entry below).
- **`node/stls-mcp`'s `vault-server.ts`/`vault-client.ts`, run with
  `KEY_PROVIDER_NAME=arqit_py`** — also verified against the real Arqit
  cloud end to end (real key agreement through the factory, real STLS
  handshake, full MCP session: all 5 tools/2 resources/2 prompts over
  `/mcp-stls`), not just `base`/`pqc`. Took two attempts to confirm: the
  first appeared to hang and the second failed with a stale-socket
  error, both of which turned out to be artifacts of the investigation
  itself (output lost to a pipe across an abrupt kill; sidecar processes
  that had separately timed out), not the test failing — the third,
  cleanly captured run showed the real work completing in ~15 seconds
  with no error at any point. Surfaced one real, if minor, loose end:
  with `arqit_py`, `KeySessionProviderFactory.getSessionProvider()`
  returns only the `KeySessionProvider`, not a handle to the underlying
  sidecar RPC connection — so there's no way for the caller to close it,
  and the process never exits on its own (unlike `base`/`pqc`, which
  open no such handle). `vault-client.ts` now calls `process.exit(0)`
  after a successful run, same as a one-shot CLI script would.

### Fixed — Arqit sidecar now verified against real Arqit peering (and a same-process pitfall fixed with it)

`providers/arqit-sidecar.ts` had only ever been tested with
`provider_name=base` (RPC mechanics, deterministic fixed key) — real
Arqit SKA-Platform™ peering through the sidecar was unverified.

Closing that gap surfaced a real pitfall first, investigated and fixed
on the Python side (full write-up in
[`python/stls-core/sidecar/ARQIT_PEERING_NOTES.md`](../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md)):
a SERVER and a CLIENT session for the same Arqit device pair must
never live in the same OS process — doing so makes the real bilocation
key exchange fail with a genuine `401 NotAuthorizedException` from
Arqit's own backend. The original `arqit-sidecar.smoke.ts` structurally
allowed exactly that (one sidecar process serving both an RPC "SERVER"
and "CLIENT" session) — harmless with `provider_name=base`'s static
key, but it would have hit the real `401` the moment anyone pointed it
at real `arqit_py`.

- **`python/stls-core/sidecar/arqit_sidecar.py`** — added a
  process-level guard (`_active_arqit_py_role` in
  `SidecarSession._start()`): for `provider_name=arqit_py`, a second
  connection requesting a different role than one already served by
  this process is rejected immediately with an explicit `RpcError`
  (code `-32000`), instead of being left to fail mysteriously 30+
  seconds later against the real cloud. Does not affect `base`/`pqc` —
  the guard only applies to `arqit_py`.
- **`tests/interop/arqit-sidecar-guard.smoke.ts`** — verifies the guard
  through the real `KeyProviderServerArqitSidecar`/
  `KeyProviderClientArqitSidecar` classes, not a raw socket script. No
  real Arqit credentials needed (the guard fires before any config or
  network access), so this runs in CI like the other `*.smoke.ts` files.
- **`tests/interop/arqit-sidecar-real.live.ts`** — the correct pattern,
  validated against the real cloud: two separate sidecar processes
  (one per role), a real bilocation key agreement, a real STLS
  handshake (`performHandshake()`/`verifyHandshakeProof()` — proving
  the two independently-obtained keys are byte-identical, not just
  "both present"), and a real AES-256-GCM + HMAC-SHA256 encrypt/decrypt
  round trip. Covers both a single client and `ARQIT_N_CLIENTS`
  sequential clients against one long-running SERVER session (the real
  production shape — a long-lived server, many handshakes over its
  lifetime), asserting every `kid`/`session_id` comes out distinct.
  Follows the same opt-in `*.live.ts` convention as the Postgres live
  tests: throws immediately if `ARQIT_SIDECAR_SOCKET_SERVER`/
  `ARQIT_SIDECAR_SOCKET_CLIENT` aren't set, no automatic skip.
- **`tests/run-interop-tests.sh`** — renumbered from 8 to 9 steps;
  step `[8/9]` now also runs the guard smoke test, and a new step
  `[9/9]` runs the real-peering test automatically whenever
  `ARQIT_PY_CLIENT_CONFIG`/`ARQIT_PY_SERVER_CONFIG` are set (same
  auto-detected opt-in convention as `AUTH_PG_DSN`), starting two
  separate sidecar processes for it.
- **`README.md`** — porting-status table upgraded from ⚠️ to ✅ for the
  Arqit provider; the Usage section now shows the two-separate-processes
  pattern instead of one shared socket.

**Minor related finding, not blocking**: a sidecar process can safely
serve the SERVER role's `start` exactly once, ever — `SidecarSession`'s
`"stop"` handler only drops the Python reference (`self.provider = None`),
it never calls the real provider's own `.stop()`, so the Arqit SDK's
`DevicePolicyStore` (a process-wide singleton) stays instantiated and a
second `start` in the same process fails with
`DevicePolicyStore is a singleton and has already been instantiated` —
discovered by accidentally reusing a sidecar process across two
separate test invocations, not by the validated pattern itself (one
`start()` per sidecar process, reused across N handshakes). Documented
in `ARQIT_PEERING_NOTES.md`; not fixed, since nothing in this package
currently needs to restart a role within one long-lived sidecar process.

### Fixed — PQC handshake orchestration had never actually been tested end-to-end

`tests/interop/pqc-step1/2-*.ts` only ever exercised the raw ML-KEM/X25519
primitives directly (`mlkemEncap`, `x25519Exchange`, `deriveHybridKey`).
`performPqcHandshake()`, `generatePqcHandshakeRequest()`,
`deriveSessionKeyClient()` and `verifyPqcHandshakeProof()` — the actual
wire-format request/response objects a real STLS server/client would
exchange — had never been called at all, cross-language or otherwise.

- **`tests/interop/pqc-handshake-step1-node-request.ts`** — Node, acting
  as a real `KeyProviderClientPQC`, builds a `PQCHandshakeRequest` via
  `generatePqcHandshakeRequest()`.
- **`tests/interop/python_pqc_handshake_step2_server.py`** — Python runs
  the real `perform_pqc_handshake()` against a real
  `InMemorySessionCache`, encrypts a message with the resulting
  transport, then **stays alive and polls** for Node's envelope so it
  can decrypt it with the same transport instance — deliberately
  avoiding any attempt to serialize/reconstruct the session key across
  a process boundary, since the cache never exposes raw key material
  by design (an earlier draft of this test tried to reconstruct the
  transport from `_aes_key`/`_hmac_key` in a second process; that's not
  how HKDF or the cache work, and was dropped before it shipped).
- **`tests/interop/pqc-handshake-step3-node-verify.ts`** — Node derives
  the session key via `deriveSessionKeyClient()`, confirms
  `verifyPqcHandshakeProof()` succeeds, decrypts Python's envelope, and
  encrypts its own for Python to decrypt back.
- Wired into `tests/run-interop-tests.sh` as step `[3b/8]`, right after
  the existing raw-primitive PQC step.

### Fixed — CI failed on first real run: `liboqs-python==0.15.0` no longer on PyPI

The first actual GitHub Actions run of
`.github/workflows/stls-core-node-interop.yml` (pushed on branch
`stls-core-node-port`) failed immediately in the "Install stls-core
(Python)" step:

```
ERROR: Could not find a version that satisfies the requirement liboqs-python==0.15.0 (from versions: 0.16.0, 0.16.0.1)
ERROR: No matching distribution found for liboqs-python==0.15.0
```

This is exactly the constraint already documented in
`python/stls-core/README.md` ("PyPI ospita solo le versioni più
recenti"): the 0.15.0 wheel used in local development had since been
removed from the index, leaving only `0.16.0`/`0.16.0.1`. Fixed by
bumping the pin to `liboqs-python==0.16.0.1` and building the matching
`liboqs` C library at git tag `0.16.0` (the Python patch release
`.1` has no corresponding separate C library tag).

This combination was verified before pushing the fix — not just
assumed from the version bump — by building `liboqs` 0.16.0 into an
isolated prefix (so the working 0.15.0 install already on the
development machine was left untouched), installing
`liboqs-python==0.16.0.1` into a throwaway venv against it, and running
the full Node↔Python PQC interop test
(`tests/interop/pqc-step1-node-keygen.ts` →
`python_pqc_server_step.py` → `tests/interop/pqc-step2-node-verify.ts`):
Node and Python derived the same hybrid session_key.

The underlying lesson, consistent with the warning already in
`python/stls-core/README.md`: pinning `liboqs-python` without also
pinning (and periodically re-verifying) a matching `liboqs` C library
tag is not reproducible — PyPI's index for this package is a moving
target, keeping only the latest release(s) available.

### Fixed — BackgroundRefresher's distributed lock is now actually verified

Asked "what's left to verify?" after the PostgresLoader fix below, the
honest answer included: `ApiKeyManager.start()`/`stop()` had been
exercised repeatedly, but always inside short-lived tests where the
`BackgroundRefresher`'s refresh interval (default 300s) never actually
elapsed — so the scheduled `doRefresh()`, and the
`RedisKeyCache.tryAcquireLock()`/`releaseLock()` distributed-lock
primitives it calls, had never really run.

- **`tests/background-refresher.live.ts`** — two parts:
  1. Tests `tryAcquireLock()`/`releaseLock()` directly against a real
     Redis with two distinct simulated node ids: the second `SET NX`
     fails while the first node holds the lock; `releaseLock()` from a
     non-holder is a no-op (the Lua check-and-delete script only
     deletes if the stored value still matches the caller's node id);
     the real holder's `releaseLock()` frees it for the next acquirer.
  2. Runs a real `BackgroundRefresher` with a 1-second interval against
     a fake `ApiKeyLoader` with a call counter, waits for the interval
     to actually elapse, and confirms `loadAll()` was called, the
     record landed in Redis via `bulkReload()`, and the lock was
     released afterwards.
- Added as step [6/8] in `tests/run-interop-tests.sh` (unconditional —
  only needs the same Redis the auth and session-cache steps already
  use).

### Fixed — PostgresLoader is now actually verified

The user provided a real `AUTH_PG_DSN` (via `node/stls-core/.env`)
pointing at an existing PostgreSQL instance whose `sf_api_keys` table
already matched the documented schema exactly (shared with other
consumers of the same database, 13 pre-existing rows). This closed the
one real gap left in the port:

- **`tests/postgres-loader.live.ts`** — exercises `PostgresLoader.loadAll()`/
  `loadOne()` against a real row: `TEXT[]` → `string[]`, `JSONB` →
  object, `TIMESTAMPTZ` → `Date` all convert correctly; prefix-based
  `allowsEndpoint()` and `isExpired()` behave correctly on real data;
  revoked rows are filtered out by `loadAll()`.
- **`tests/apikeymanager-postgres.live.ts`** — exercises the full
  `PostgresLoader -> RedisKeyCache -> ApiKeyManager` chain: the
  `BackgroundRefresher`'s initial bulk load reads from the real
  Postgres row, `validate()` returns `OK`/`SCOPE_DENIED` correctly based
  on it, `health()` reports `loader=PostgresLoader`.
- Both tests are **self-contained**: they create the table if missing
  (`tests/postgres-schema.sql`, `CREATE TABLE IF NOT EXISTS`, extracted
  as the single source of truth for the schema), insert their own
  fixture row, and remove it in a `finally` block — safe to run
  repeatedly against a shared, pre-populated database. Verified by
  checking the row count before and after (13 → 14 during the test → 13
  again after cleanup).
- **`tests/run-interop-tests.sh`** — added step [6/7]: runs the two live
  Postgres tests automatically whenever `AUTH_PG_DSN` is set in the
  environment (auto-detected, no new `SKIP_*` flag needed to opt in).
- **`.github/workflows/stls-core-node-interop.yml`** — added a
  disposable `postgres:16` service container (throwaway credentials,
  not the development database used for manual verification) and wired
  `AUTH_PG_DSN` to it, so the live Postgres tests now also run in CI.
  This CI addition was not executed in this session (no way to run
  GitHub Actions from here); only the underlying test logic was
  verified, against the user's real Postgres instance.

`PostgresLoader`'s status is upgraded from ⚠️ (error paths only) to ✅
(verified) in the README's porting-status table.

### Not ported

- No remaining unported item: `PostgresLoader` is now fully verified
  (see above), and the Arqit sidecar is now verified against real Arqit
  peering too (see the Arqit sidecar entry above) — ✅ across the board
  in the status table.

## [0.1.1] — 2026-10-17

First release: a TypeScript port of `python/stls-core`, meant to secure
Node.js applications while interoperating byte-for-byte with existing
Python `stls-core` servers/clients (`stls-a2a`, `stls-mcp`).

### Critical — HKDF-Expand vs. full HKDF (`crypto.ts`)

The trickiest part of the entire port, because the failure would have
been **silent**: no exception, just a MAC/GCM tag that stops verifying
against a Python peer, easily mistaken for a bug somewhere else.

`StlsTransport` derives the AES-256 and HMAC-SHA256 keys from the
session key via `cryptography.hazmat.primitives.kdf.hkdf.HKDFExpand` on
the Python side — **only** the *expand* step of RFC 5869 §2.3, with the
session key passed in directly as the PRK (no *extract* step).

Node's `crypto` **exposes no equivalent**: `crypto.hkdfSync()`
implements the **full** HKDF (RFC 5869, extract-then-expand) and, given
the same input, produces a different derived key than `HKDFExpand`.
Using it in the wrong place would have made every Node `StlsTransport`
instance internally self-consistent (Node↔Node encrypt/decrypt would
have worked in tests) but **incompatible** with any envelope produced
by a Python peer — exactly the kind of bug a Node-only test could never
have caught.

Fix: `hkdfExpand()` in `crypto.ts` manually reimplements just the
expand step (RFC 5869 §2.3). For L=32 bytes = HashLen(SHA-256) it
reduces to a single `HMAC-SHA256(PRK, info ‖ 0x01)`, but the function
stays generic (looping over `T(i)` blocks) so it remains correct if the
derived key length ever changes.

Verified with a real vector, not by code inspection: a fixed session
key, an IV pinned via a monkeypatch of `os.urandom` on the Python side,
the same plaintext. Result: Node decrypts the envelope produced by
Python byte-for-byte (`iv`, `ciphertext`, `tag`, `mac` all identical)
and Python decrypts the envelope produced by Node with the same session
key. See `tests/interop/crypto.smoke.ts` and
`tests/interop/python_crypto_verify_node_envelope.py`.

**Non-obvious corollary**: Node's native `hkdfSync()` is instead the
**correct** choice in `providers/pqc.ts::deriveHybridKey()`, because
there the Python side uses `cryptography...hkdf.HKDF` (full
extract+expand, with the nonce as the salt) — not `HKDFExpand`. The two
derivations exist in the same package for different purposes (transport
keys vs. the hybrid PQC session key) and **require different HKDF
primitives**: using one in place of the other, in either direction,
breaks compatibility. Anyone extending this package must treat
`hkdfExpand()` (local) and `hkdfSync()` (native) as non-interchangeable,
not as two equivalent ways of "doing HKDF".

### Added

- **`crypto.ts`** — `StlsTransport`/`StlsEnvelope`: AES-256-GCM + HMAC-SHA256
  + HKDF-Expand. Verified bit-compatible with Python (see above).
- **`handshake.ts`** — the standard handshake with an HMAC
  proof-of-possession, a port of `stls_core_handshake.py`.
- **`session.ts` / `session-redis.ts`** — `InMemorySessionCache` and
  `RedisSessionCache`, the same Redis key layout as the Python side
  (`{prefix}:{session_id}` → JSON `{session_key, kid, created_at}`).
  Verified with a real `redis-server`: a session written by Node and
  read by Python, and vice versa, with a successful encryption
  round-trip on both sides.
- **`providers/base.ts`** — `KeySessionProviderBase` (fixed key, dev/test).
- **`providers/pqc.ts` / `handshake-pqc.ts`** — hybrid X25519 + ML-KEM-768.
  X25519 and ML-KEM-768 use `@noble/curves`/`@noble/post-quantum` (same
  author, raw-bytes API) instead of `pyca/cryptography` +
  `liboqs-python`. Different implementations of the same standards (RFC
  7748, NIST FIPS 203), verified interoperable at the protocol level
  with a real vector: a keypair generated by Node, ML-KEM encapsulation
  and X25519 DH performed by `liboqs-python`/`cryptography` on the
  Python side, decap/DH again on the Node side → the same final hybrid
  `session_key` (see `tests/interop/pqc-step*.ts` and
  `python_pqc_server_step.py`).
- **`providers/arqit-sidecar.ts`** + **`python/stls-core/sidecar/arqit_sidecar.py`**
  — Arqit SKA-Platform™ does not publish a Node SDK: peering stays on
  the Python side, and Node talks to a sidecar process via
  newline-delimited JSON-RPC over a Unix domain socket (one connection =
  one STLS session, the same "Option A" pattern as the Python
  `arqit_py` provider). Tested end-to-end (full handshake +
  encrypt/decrypt) with `provider_name=base` as the sidecar's backend —
  **not verified against real Arqit peering**, no SKA-Platform™
  credentials available in this development environment. Anyone
  deploying this in production with `provider_name=arqit_py` must
  validate it against a real Arqit deployment.
- **`auth/`** — a port of `stls_core.auth`: `AuthConfig`, `KeyRecord`,
  `AuditEntry`, `ValidationResult`, `hashKey`/`generateApiKey`,
  `StaticLoader`, `FileLoader` (native JSON, YAML via the optional
  `yaml` package with a JSON fallback if it is missing — the same
  behavior as the Python side with PyYAML), `RedisKeyCache`,
  `ApiKeyManager`, `ApiKeyManagerGroup`, `BackgroundRefresher` (with a
  per-namespace distributed lock, identical to `_try_acquire_lock`/
  `_release_lock` on the Python side). Verified with a real
  `redis-server`: OK/INVALID/EXPIRED/SCOPE_DENIED/REVOKED scenarios on
  `ApiKeyManager`, plus `FileLoader` against real JSON and YAML files.

### Design — deliberate choices, not a 1:1 port

- **`SessionCache` is async in Node, sync in Python.** `redis-py` is a
  synchronous/blocking client; there is no equally mature Node Redis
  client. To have a single interface valid for both the memory and the
  Redis backend, every `SessionCache` method is `Promise`-based —
  including `InMemorySessionCache`, where the implementation stays
  synchronous and the Promise resolves immediately. This propagates to
  `performHandshake()`/`performPqcHandshake()` (`await sessionCache.put(...)`).
- **`ApiKeyManager.validateRequest()` uses a `RequestLike` adapter**,
  not a concrete `Request` type: Python accepts a Starlette/FastAPI
  `Request` directly, but Node has no single canonical HTTP framework.
  `RequestLike` (`header()`, `queryParam()`, `path`, `remoteAddr`) can
  be implemented in a few lines by any framework (Express example in
  the README).

### Added (follow-up — completing the deferred items)

- **`auth/loaders-postgres.ts`** — a port of `PostgresLoader` using the
  optional `pg` package, the same table schema (`sf_api_keys`) and the
  same query as the Python side. Initially shipped with only the
  soft-fail error path tested (missing DSN or refused connection →
  empty list, never an exception — `tests/postgres-loader.smoke.ts`),
  because no Postgres server was available in the development
  environment (no root for `apt-get install postgresql`, no working
  Docker in the WSL sandbox).
- **`.github/workflows/stls-core-node-interop.yml`** +
  **`tests/run-interop-tests.sh`** — automate the entire manual test
  sequence described above (crypto, PQC, Redis session cache, auth, the
  Arqit sidecar) in a CI job triggered by changes to
  `node/stls-core/**` or `python/stls-core/**`. The job builds `liboqs`
  from source (pinned to `0.15.0`, the combination verified during this
  development), and uses Redis as a service container. The bash script
  was run and verified locally end-to-end before being wired into the
  workflow — building liboqs in CI is the only step not directly run in
  this session.
