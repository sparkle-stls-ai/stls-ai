# Changelog

All notable changes to **stls_core** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.7] — 2026-11-13

Adds the server-authoritative absolute key-rotation check to
`SessionCacheBase` (issue #6, first increment: the shared server-side
check only — client-side retry for stls-mcp and the proactive SSE/
`arqit_py` scheduler piece are separate, later increments). Additive —
no existing caller needed to change; disabled by default.

### Added

- **`SESSION_KEY_MAX_AGE_SECONDS`** environment variable, both
  `InMemorySessionCache` and `RedisSessionCache`. Unset or `<=0`
  (default): no behavior change — only the existing idle TTL applies.
  `>0`: `get()` also expires a session once its AGE since creation
  (not since last access) exceeds this value, on top of, not instead
  of, the idle TTL. Server-side only, never client-settable — the
  server is the trust boundary.
  - `InMemorySessionCache`: `created_at` (new, `time.monotonic()`-based)
    is tracked per entry, set once at first insertion and never
    refreshed by `get()`/an update `put()` — unlike the sliding
    `last_access`.
  - `RedisSessionCache`: reuses the `created_at` field already written
    into every session's JSON payload (previously serialized but never
    read back). Because every instance in a multi-replica deployment
    reads the same shared Redis record, the expiry decision is
    consistent across instances with no new coordination — verified by
    a dedicated test (two independent `RedisSessionCache` objects on
    the same Redis, no lock between them, same expiry outcome).
  - Both backends: an expired-by-max-age session is deleted immediately
    and treated exactly like "not found" — the same path already used
    for idle-TTL expiry, so the existing `stls-a2a` invalidate+retry-once
    picks it up transparently with no changes on that side.

### Known gap carried forward unchanged

- Client-side retry-on-expiry only exists in `stls-a2a`
  (`stls_a2a_secure_remote.py`). `stls-mcp` (Python and Node) still has
  no retry on session expiry at all — next increment of issue #6, not
  part of this one.

## [0.1.6] — 2026-11-12

Hardens `StlsTransport` (issues #10, #12) and extends its correctness
test coverage (#11). Additive — no existing caller (stls-a2a, stls-mcp,
or stls-core's own session/handshake code) needed to change.

### Added

- **`StlsTransport(..., direction="c2s" | "s2c")`** — optional parameter
  deriving four independent keys (AES+HMAC per direction) via four new
  HKDF info labels (`HKDF_INFO_{AES,HMAC}_{C2S,S2C}`), instead of the
  single shared key pair. The two peers of a session agree on their
  direction out of band (no handshake negotiation). Default is still
  `direction=None`: one shared key pair for both directions, identical
  to the previous behavior. Intended for the upcoming NaaS federation
  PoC, where two peer nodes label their own transport directions —
  not needed by the existing A2A/MCP request/response flows.

### Changed

- `encrypt()`/`decrypt()` now bind `version` and `session_id` into the
  envelope's integrity: both are fed as AES-GCM additional authenticated
  data (AAD), and the outer HMAC now covers `AAD ‖ IV ‖ ciphertext ‖ tag`
  (previously `IV ‖ ciphertext ‖ tag` only — `version`/`session_id`
  were not authenticated at all). `decrypt()` also now explicitly
  rejects an unexpected `version`. This changes `tag`/`mac` for every
  envelope, including the default (direction-less) mode — `ciphertext`
  bytes are unchanged, but **old and new code can't interoperate**
  (both sides of a session need the same `stls-core` version). No
  impact on data at rest: cached sessions store the raw `session_key`,
  never ciphertext.

## [0.1.5] — 2026-11-12

Restricts the package's public API surface (issue #5): nothing exported
from `stls_core` hands back raw session-key bytes anymore.

### Added

- **`establish_session()`/`resolve_transport()`** (`stls_core_session_establish.py`)
  — the public replacement for the old "`KeySessionResolver.resolve()`
  then feed `session_key_data` into `perform_client_handshake()`"
  two-step dance. `establish_session()` resolves the key (or, for
  `pqc`, runs the KEM handshake) AND performs the STLS handshake in one
  call; `resolve_transport()` resolves and builds the `StlsTransport`
  with NO handshake (for stls-a2a's base/arqit_py path, whose wire
  protocol has none). Neither ever returns the raw key.
- **`perform_client_handshake()`/`perform_pqc_client_handshake()`/
  `StlsHandshakeFailed`** moved here from `python/stls-mcp`'s
  `stls_mcp_transport.py` (generic STLS-protocol handshake, no
  MCP-specific knowledge) — `establish_session()` is built on them.
  `python/stls-mcp` now re-exports all three unchanged; its own public
  API is unaffected. `httpx` moves from `[dev]`-only to a real runtime
  dependency as a consequence.
- **`KeySessionProviderPQC.derive_client_key()`/`destroy_private_key_material()`**
  (`providers/stls_core_providers_pqc.py`) — the ephemeral
  X25519/ML-KEM-768 KEM private key bytes, previously exposed via
  public `x25519_private_key_bytes`/`mlkem_secret_key_bytes`
  properties, are now private attributes; these two methods are the
  only way to use them from outside the class.

### Removed

- **`KeySessionResolver`** is no longer exported from the `stls_core`
  package (`__init__.py`) — `resolve()` handed back raw key material,
  exactly what this issue closes off. The class itself is unchanged and
  still used internally, by `establish_session()`/`resolve_transport()`.

### Fixed

- `__version__` in `__init__.py` was hardcoded at `"0.1.2"`, out of sync
  with `pyproject.toml`'s version since 0.1.3 — now kept aligned.

---

## [0.1.4] — 2026-11-11

Security hardening of the Arqit sidecar's local IPC boundary (issues
#17/#18): the Unix domain socket previously accepted any local
connection with no verification at all.

### Added

- **Local socket authentication (#17)** — `arqit_sidecar.py` now
  requires a shared token (`--token`/`ARQIT_SIDECAR_TOKEN`, no default:
  the process refuses to start without one) presented via a new
  `authenticate` RPC call before any other method is accepted. On
  Linux, also verifies the connecting process's uid via `SO_PEERCRED`
  (falls back to token-only auth with a warning where unavailable).
  The socket file itself is `chmod 0600` after bind, defense-in-depth
  alongside the containing directory's permissions (a deployment
  concern).
- **Proof scope (#18)** — a connection may call `start` only once;
  role/provider/config are frozen for that connection's whole
  lifetime.
- **Pseudonymous connection identity (#18)** — a random per-connection
  id is used in all sidecar logging instead of raw OS-level
  identifiers.
- **`KeySessionProvider.releaseSessionKeyData()` (#18)** — new method
  (default no-op) called by the sidecar on `stop`/disconnect to evict
  a session's raw key from the provider's store. Implemented for
  `pqc` (wires up the previously-unused `PQCKeyStore.remove()`) and
  `arqit_py` (`ArqitKeyStoreBase.remove()`, new). Scoped per store:
  safe and immediate for in-memory stores and `RedisArqitKeyStore`'s
  client role; a documented no-op for the server role, whose Redis
  namespace is deliberately shared across horizontally-scaled
  replicas — eviction there would break that stateless-scaling
  design, not fix anything. The `base` provider's fixed key (a
  module-level constant shared by every connection) is unaffected,
  by design.

---

## [0.1.3] — 2026-11-10

Packaging-only release: no functional changes. Version bump to enable
the first publish to PyPI via Trusted Publishing.

---

## [0.1.2] — 2026-10-18

Performance and cleanup pass on `stls-core`'s hot paths, found during a
code assessment ahead of the package's migration to the `stls-ai` repo.
No wire-format or public API changes; the full unit/e2e suite (including
the PQC tests, with real `liboqs`) and the full Node↔Python
interoperability suite pass unchanged.

### Added (follow-up, 2026-10-23)

- **`KeyProviderArqitPy.create_provider_client()`/`create_provider_server()`
  now accept an `options["config_path"]` override**, taking priority
  over `ARQIT_PY_CLIENT_CONFIG`/`ARQIT_PY_SERVER_CONFIG` when present.
  Previously the device config path was read only from the process
  environment, so a single process could only ever use one `arqit_py`
  device identity for its whole lifetime. No change to the shared
  `KeyProvider` ABC signature (`options: dict` already existed there).
- **`KeySessionProviderFactory.get_session_provider()` gained an
  `options` parameter**, passed straight through to
  `create_provider_client()`/`create_provider_server()` (previously
  always called with `options={}`). Enables callers like `stls-a2a`'s
  `SecureSessionManager(arqit_client_configs=...)` to hold several
  `arqit_py` device configs in one process. Validated against the real
  Arqit cloud for sequential use (one device's session fully
  negotiated before the next starts).

### Fixed (follow-up, 2026-10-23, continued — concurrent `arqit_py` use)

The "concurrent use" question left open immediately above is now
closed: concurrent/parallel `arqit_py` sessions (same device or
different devices, same process) are safe to call, not just untested.
Two distinct, real bugs were found and fixed, both confirmed by direct
reproduction against the real Arqit cloud (see
`python/stls-a2a/README.md`'s "Current Limitations and Notes" and
`sidecar/ARQIT_PEERING_NOTES.md` for the full investigation writeups):

- **`DeviceMetadata`/`AuthenticationClient` singleton race in the Arqit
  SDK — deadlock under concurrent `start()` calls.** The Arqit SDK's
  `DeviceMetadata` is a process-wide singleton and
  `AuthenticationClient.__mutex` is a class-level lock shared by every
  instance in the process, with no `try`/`finally` around the critical
  section it guards — an unrelated exception during one `start()`
  call's critical section leaks that lock permanently, deadlocking
  every subsequent `start()` call in the process forever. **Fixed**:
  new module-level `_ARQIT_PY_START_LOCK` (`threading.Lock`) in
  `providers/stls_core_providers_arqit_py.py` serializes
  `KeyProviderClientArqitPy.start()`/`KeyProviderServerArqitPy.start()`
  process-wide, across *all* devices (not just the same one, since
  `DeviceMetadata` is one global singleton regardless of which
  device's config is passed). Client-side, the lock is held through
  the *entire* negotiation (joins the negotiation thread, capped at
  30s) — serializing only authentication wasn't sufficient on its own,
  since letting a second session authenticate while a first session's
  negotiation was still connecting broke the socket bilocation
  handshake outright (`ConnectionRefusedError`/`PeerUnableToConnectException`).
  This makes `arqit_py` session establishment fully serial
  process-wide (a throughput cost, not a correctness one) until Arqit
  fixes their own SDK's locking.
- **`KeySessionProviderArqitPy.getSessionKeyIdentifier()`: unprotected
  `_delivered_key_ids` mutation, plus a plain-FIFO resolution that
  assumes handshake requests arrive in the same order their
  negotiations completed.** On a long-running SERVER-role instance
  shared by every incoming handshake for the process's lifetime,
  concurrent handshakes now genuinely race (only possible *after* the
  fix above stopped them from deadlocking first). Two fixes:
  1. a new `self._key_lock` (`asyncio.Lock`) serializes the
     wait-then-claim-then-add sequence per instance, closing a data
     race on the `_delivered_key_ids` set itself.
  2. **New optional `hint_kid` parameter on `getSessionKeyIdentifier()`**
     (also added to the `KeySessionProvider` ABC and to `Base`/`PQC`,
     which both just ignore it) — when given, resolves that EXACT
     already-delivered key directly (polls `key_store.get(hint_kid)`)
     instead of "whichever key arrived and hasn't been claimed yet",
     which has no way to stay correct once multiple negotiations can
     finish before their matching handshake request is processed.
     `stls_core_handshake.py`'s `HandshakeRequest` gained a matching
     optional `kid` field, populated by the client with its own
     already-resolved kid and passed through by `perform_handshake()`
     as the hint. Purely additive, not a trust boundary: the HMAC
     proof-of-possession check still enforces actual key possession
     regardless of which kid the server looked up by.

Also found, in the course of building the reproduction above: an
ordering bug in the first version of the `_ARQIT_PY_START_LOCK` fix
itself — building the session object (which snapshots
`key_store.startup_seen_keys()`) *after* joining the negotiation
thread wrongly absorbed that same negotiation's own freshly-delivered
keys as "already seen" (only visible with a Redis-backed key store;
the in-memory store has no persistence to snapshot anything stale
from). Fixed by moving that construction *before* starting the
negotiation thread, restoring the original snapshot timing while
keeping the full-negotiation lock scope.

### Changed (follow-up, 2026-10-19)

- **Finished translating docstrings, comments and log/error strings from
  Italian to English** across `auth/stls_core_auth_manager.py`,
  `auth/stls_core_auth_store.py`, `providers/stls_core_providers_arqit_py.py`,
  `providers/stls_core_providers_factory.py`,
  `providers/stls_core_providers_keystore.py`,
  `providers/stls_core_providers_pqc.py`, `stls_core_handshake.py` and
  `stls_core_handshake_pqc.py`. These 8 files were missed by the
  original translation pass even though it was recorded as complete:
  most of the Italian text left behind was in `logger.info/debug/warning`
  calls and exception messages rather than docstrings, which a
  docstring-focused review had skipped over. No code logic changed,
  only string literals.

### Fixed

- **`stls_core_handshake.py`: a new `ThreadPoolExecutor` was created and
  torn down on every single handshake** (`_resolve_blocking()`, used to
  resolve the `arqit_py` provider's blocking `.get()` call) — real
  thread-creation overhead on the most frequent code path of the
  `arqit_py` provider. Now reuses asyncio's default executor
  (`loop.run_in_executor(None, ...)`) instead of a dedicated one-shot
  pool.
- **`stls_core_providers_keystore.py`: `RedisArqitKeyStore.startup_seen_keys()`
  was defined twice** in the same class — the first definition (a
  trivial `return set()`, with a docstring mislabeling it as the
  `InMemory` implementation) was always silently shadowed by the real
  one below it. Dead code, removed.
- **`stls_core_auth_manager.py`: the fire-and-forget audit task
  (`asyncio.ensure_future(self._emit_audit(audit))`) kept no reference**
  to the created `Task`, relying only on the event loop's internal
  bookkeeping — a known asyncio footgun (a task with no other strong
  reference can in principle be garbage-collected before it's
  scheduled). `ApiKeyManager` now holds a `set()` of in-flight audit
  tasks, discarding each one via `add_done_callback` on completion.

### Changed

- **`stls_core_crypto.py`: `StlsTransport._constant_time_equal` reimplemented
  a constant-time byte comparison by hand** (a pure-Python XOR loop)
  instead of using `hmac.compare_digest`, already used elsewhere in this
  same package (`stls_core_handshake.py`, `auth/stls_core_auth_models.py`)
  and in the Node port (`timingSafeEqual`). Replaced with
  `hmac.compare_digest` — faster (C implementation) and consistent with
  the rest of the codebase.
- **`stls_core_crypto.py`: dropped `cryptography.hazmat.backends.default_backend()`**
  from `StlsTransport` (`Cipher`, `HMAC`, `HKDFExpand`) — legacy/optional
  in `cryptography>=46.0.5` (the package's declared minimum version),
  one fewer object instantiated per `StlsTransport` and per
  encrypt/decrypt call.

### Operational notes

- **`__init__.py`'s `__version__` had drifted to `"0.1.0"`** while
  `pyproject.toml` already declared `0.1.1` — fixed as part of this
  version bump; both now read `0.1.2`.

### Fixed — `KeyProviderServerArqitPy.stop()` never actually released the receiver's listening socket

Surfaced while chasing down `test_arqit_py_real_peering.py` failures
against the real Arqit cloud. Two distinct, real bugs, neither of
them "cloud flakiness":

- **`_listen()`'s idle loop runs AFTER `listen_for_key_negotiation()`
  returns, not instead of it** — the SDK keeps the real listener alive
  internally; our idle loop (checking `_is_cancel_ref`) only governs
  when our own daemon thread's function returns, with no effect on the
  SDK's own socket. `stop()` set the cancel flag and returned, but
  **never called anything on the SDK's own receiver object** — the
  listening socket stayed bound for the rest of the process. Harmless
  when the provider runs in its own OS process (killing the process
  releases the socket regardless, which is why
  `test_two_separate_processes_succeeds` and
  `test_n_sequential_clients_against_one_server_succeeds` never showed
  this), but fatal for `test_same_process_reproduces_401`, the one
  caller that starts/stops the provider in-process and then needs the
  same port free again — it reliably hit `Address already in use`
  forever, no timeout long enough would have fixed it. Fixed by storing
  the SDK's receiver object (`SocketClientReceiver`/`MqttClientReceiver`,
  both expose the same `end_key_negotiation()`) and calling it from
  `stop()`, which actually `shutdown()`s and `close()`s the listening
  socket.
- **`KeyProviderServerArqitPy.start()` returns as soon as its listener
  thread is *launched*, not when the socket is actually bound and
  `listen()`ing** — the real `bind()`/`listen()` happens inside that
  background thread. The test harness's own "SERVER_READY" readiness
  signal was printed right after `start()` returned, before the socket
  was necessarily live, so the CLIENT role (a separate process) could
  race ahead and get a genuine `ConnectionRefusedError`. Not a library
  bug — `tests/_arqit_py_real_e2e_server_role.py` now actively
  connect-polls the receiver's own port before reporting ready.
- **`_wait_port_free()`'s 10–15s timeouts were simply too short**:
  Linux's TCP TIME_WAIT for a real, closed connection is a fixed ~60s
  (2×MSL), not configurable from the probing side. Bumped the default
  to 65s.

Verified: all 3 tests in `test_arqit_py_real_peering.py` pass together
against the real Arqit cloud (previously 1/3, with the other 2 failing
on teardown/connection races, not on the actual key agreement or
handshake logic). Full local suite unaffected (250 passed, 1 skipped).

---

## [0.1.1] — 2026-07-31

### Added

- **`AuthConfig`** (`stls_core_auth_models.py`) — lightweight static
  validation for authorization, with no loader/Redis/refresh:
  `bearer_tokens`, `api_keys`, timing-safe via `constant_time_equal()`.
  Originally a local class in `stls-a2a` (a pydantic `BaseModel`), moved
  here to actually be shared with `stls-mcp` — previously an almost
  identical copy existed there. Converted to a pure stdlib `@dataclass`,
  consistent with `KeyRecord`/`AuditEntry` in the same module (which
  explicitly declares "no external dependencies" — introducing pydantic
  just for this class would have violated that principle). Identical
  behavior to the original pydantic version; the only observable change:
  the default for `realm` changes from `"A2A Agent"` (specific to the
  old context) to `"STLS"` (generic) — no existing call site relied on
  the previous default. `stls-a2a` and `stls-mcp` re-import it with one
  line, with unchanged usage for anyone who already used it
  (`AuthConfig(api_keys={...})` continues to work identically).

- **`[postgres]` extra** — `asyncpg>=0.31.0` isolated into its own
  dedicated extra for `ApiKeyManager`'s `AUTH_LOADER_BACKEND=postgres`
  backend.

### Fixed

- **`asyncpg` was living, undocumented, inside the `[redis]` extra** —
  conceptually wrong bundling: the session/key cache (Redis) and the key
  data source (Postgres) are two independent backends, there's no reason
  installing one should pull in the other. Discovered when `stls-mcp`
  declared its own `[postgres]` extra as a pass-through to
  `stls_core[postgres]` — an extra that simply didn't exist (`uv`
  reported it explicitly: *"does not have an extra named postgres"*),
  leaving `PostgresLoader.load_all()` fail silently at runtime (lazy
  import of `asyncpg`, never at module level) instead of at install
  time. `[redis]` now contains only `redis>=7.4.0`.

### Operational notes

- **Editable install not rebuilt after local changes** — observed that
  `uv pip install -e ".[extra]"` may not regenerate the package
  installed in `site-packages` if the version number in
  `pyproject.toml` doesn't change, even when the sources have been
  modified (e.g. adding `AuthConfig` to `stls_core_auth_models.py` was
  not picked up by `stls-a2a`/`stls-mcp` until
  `--reinstall-package stls_core` was forced). Not specific to this
  change, but the first time it manifested concretely enough to
  document here: if you touch `stls-core` and downstream packages don't
  see the change, this is the first suspect, not a bug in the code.
- **Minimal, declared breaking change**: anyone installing `[redis]`
  expecting `asyncpg` to come along (the pre-fix behavior) must now
  request both explicitly: `uv pip install -e ".[redis,postgres]"`.

---

## [0.1.0] — 2026-06-26

Initial release.

`stls-core` is born as an extraction of the protocol-agnostic layer
(cryptography, sessions, handshake, key providers, API key
authentication) previously duplicated inside `stls-a2a`, so it can also
be shared by `stls-mcp` (STLS extension of the Model Context Protocol).
No previous version: this is the starting point of the package as an
independent unit.

### Added

- **`StlsTransport`** (`stls_core_crypto.py`) — protocol-agnostic
  symmetric cryptography: AES-256-GCM for confidentiality/authentication,
  an additional HMAC-SHA256 over the whole envelope, HKDF-Expand to
  derive distinct AES and HMAC keys from the session key (domain
  separation via the info labels `"stls-aes-256-gcm"` /
  `"stls-hmac-sha256"`). Exposes `encrypt`/`decrypt` over raw bytes and
  `encrypt_json`/`decrypt_json` as helpers for JSON-serializable
  payloads. `StlsEnvelope` with the 6 canonical fields (`version`,
  `session_id`, `iv`, `ciphertext`, `tag`, `mac`) — wire format identical
  to the one already in use in `stls-a2a` before the extraction, for
  zero breaking changes on existing clients.

- **`SessionCache`** (`stls_core_session.py`) — session cache with two
  interchangeable backends:
  - `InMemorySessionCache` — in-memory LRU+TTL, zero external
    dependencies. Configurable via `STLS_SESSION_CACHE_MAX` (default
    256) and `STLS_SESSION_TTL` (default 3600s). Default for
    development and single-instance deployments.
  - `RedisSessionCache` — Redis-backed for multi-instance deployments
    (Cloud Run, scale-out). Sliding TTL renewed on every `get()` via
    `GETEX`. Requires the `[redis]` extra. Key namespace `stls:sc:*`
    kept separate from the Arqit keystore (`stls:ks:*`) — no collision.
  - `SessionCacheFactory` — selects the backend from
    `SESSION_CACHE_BACKEND` (`memory` default | `redis`).
  - `StlsSession` as the dataclass for a single record.

- **Proof-of-key-possession handshake** (`stls_core_handshake.py`) — a
  completely new design compared to `stls-a2a`'s previous "lazy" scheme
  (client-generated session_id, embedded in the encrypted envelope):
  `HandshakeRequest` (`client_id`, `nonce`, `protocol`) sent by the
  client to `POST /stls/handshake`; the server generates its own
  `session_id` (uuid4, not enumerable or controllable by the client) and
  computes `proof = HMAC-SHA256(session_key, nonce)`, returning it in
  `HandshakeResponse` together with `kid`. The client verifies the proof
  with `verify_handshake_proof()` — proving mutual possession of the
  same key without any cryptographic material ever crossing the wire.
  `perform_handshake()` as the server-side entry point, automatically
  registering the resulting session in the shared `SessionCache`.

- **Key providers** (`providers/`):
  - `stls_core_providers_base.py` — common ABCs (`KeyProvider`,
    `KeyProviderClient`, `KeyProviderServer`, `KeySessionProvider`) and
    `KeySessionProviderBase`, the `base` provider with a fixed key for
    development/test/CI (`_BASE_KEY_DATA`, `_BASE_KEY_ID`).
  - `stls_core_providers_arqit_py.py` — `KeyProviderArqitPy` and
    `KeySessionProviderArqitPy`, integration with Arqit SKA-Platform™ via
    the native Python SDK (`uk.arqit.device.*`), **with no JVM/JPype
    dependency**. Supports the CLIENT (initiator) and SERVER (receiver)
    roles, TCP socket or MQTT transport (`ARQIT_PY_TRANSPORT`). Peering
    runs in a dedicated daemon thread so it doesn't block the asyncio
    event loop. `getSessionKeyIdentifier()` is async with a configurable
    timeout (default 30s) while waiting for the Bilocation Key.
  - `stls_core_providers_pqc.py` — **Hybrid X25519 + ML-KEM-768 PQC
    provider (NIST FIPS 203, formerly Kyber)**. Quantum-safe with no
    dependency on external services (Arqit not required). Implemented
    with `liboqs-python` for ML-KEM-768 and `pyca/cryptography` for
    X25519 (already a base dependency of stls-core). Architecture:
    `PQCKeyStore` (thread-safe dict for keys derived via the KEM),
    `KeySessionProviderPQC` (with server-side `inject_key()`),
    `KeyProviderClientPQC` (generates the X25519 + ML-KEM-768 keypairs
    on `start()`, zeroes the private keys on `stop()`),
    `KeyProviderServerPQC` (no-op `start()` — no external peering
    needed), `KeyProviderPQC` (factory). Cryptographic primitives
    exposed as public functions: `mlkem_generate_keypair()`,
    `mlkem_encap()`, `mlkem_decap()`, `x25519_generate_keypair()`,
    `x25519_exchange()`, `derive_hybrid_key()`. Optional dependency via
    the `[pqc]` extra.
  - `stls_core_providers_factory.py` — `KeyProviderFactory` and
    `KeySessionProviderFactory` (singletons). The `simple` and `arqit`
    providers (JVM-based, present in the previous `stls-a2a`
    implementation) **were not ported**: only `base`, `arqit_py` and
    `pqc` are supported.

- **PQC handshake** (`stls_core_handshake_pqc.py`) — a dedicated
  endpoint `POST /stls/pqc/handshake` with an extended wire format
  compared to the standard handshake. `PQCHandshakeRequest` adds the
  client's X25519 and ML-KEM-768 public keys; `PQCHandshakeResponse`
  includes the server's X25519 public key and the ML-KEM-768
  ciphertext. Server flow: generate an X25519 keypair → X25519 DH with
  the client's public key → ML-KEM encapsulation with the client's
  ML-KEM public key → full HKDF-SHA256 (IKM = x25519_secret ‖
  mlkem_secret, salt = nonce) → 32-byte session_key → proof =
  HMAC-SHA256(session_key, nonce) → `session_cache.put(session_id, kid,
  session_key)`. Client side: `derive_session_key_client()` (X25519 DH +
  ML-KEM decap + identical HKDF → same session_key) +
  `verify_pqc_handshake_proof()`. The PQC `kid` is a server-side
  generated UUID for every handshake — fresh and not reusable. No key
  material travels in either direction; only public keys and the KEM
  ciphertext (safe in cleartext by ML-KEM's design).

- **`HANDSHAKE_PQC_PATH` constant** (`stls_core_constants.py`) —
  `"/stls/pqc/handshake"`. `KEY_PROVIDER_NAME` updated to accept three
  values: `"base"` | `"arqit_py"` | `"pqc"`.

- **`[pqc]` extra** (`pyproject.toml`) — optional dependency on
  `liboqs-python`. **Version alignment note**: the Python wrapper's
  version must match the system's `liboqs` C library. A mismatch causes
  an `AttributeError` at import time (e.g. `liboqs-python 0.15.0` +
  `liboqs C 0.11.0` → `OQS_SIG_supports_ctx_str` not found). PyPI only
  hosts the most recent versions (0.14.1, 0.15.0); if the system has an
  older C library, `liboqs` must be built from source at the matching
  tag with `BUILD_SHARED_LIBS=ON`. Robust guard in `_check_liboqs()`:
  catches `ImportError`, `AttributeError` and `OSError` with an explicit
  diagnostic message about the fix.

- **API key authentication system** (`auth/`) — a near-verbatim port of
  the 5-module system already existing in `stls-a2a`, with only import
  path updates:
  - `stls_core_auth_models.py` — `KeyRecord` (key_id=SHA256, client_id,
    scopes, endpoints, expires_at, metadata), `AuditEntry`,
    `ValidationResult`, `hash_key()`, `generate_api_key()`.
  - `stls_core_auth_loaders.py` — the `ApiKeyLoader` ABC and three
    implementations: `StaticLoader` (in-memory, equivalent to the
    previous `AuthConfig`), `FileLoader` (YAML/JSON), `PostgresLoader`
    (asyncpg).
  - `stls_core_auth_store.py` — `RedisKeyCache`, with a lazy import of
    `redis` (never touched if `AUTH_LOADER_BACKEND=static`).
  - `stls_core_auth_manager.py` — `ApiKeyManager`, `ApiKeyManagerGroup`,
    `BackgroundRefresher` (asyncio), `default_audit_hook()`.

- **Extraction and fix of `resolve_if_awaitable`** — during the
  extraction from `stls-a2a`, the public helper for normalizing
  awaitable provider results (coroutines, Java-future-like objects with
  `.get()`, or direct values) had been renamed to the private method
  `_resolve()` on `KeySessionProviderFactory`. Bug discovered by a real
  test against live Arqit SKA-Platform™ (`task_test.py` in `stls-a2a`,
  device peering authenticated successfully, then an immediate crash on
  `AttributeError: 'KeySessionProviderFactory' object has no attribute
  'resolve_if_awaitable'`). Fix: restored as a public `@staticmethod`;
  `_resolve` kept as an internal alias for calls from
  `get_session_provider()`.

- **`pyproject.toml`** — minimal base dependencies (`cryptography`,
  `pydantic`, `python-dotenv`); extras `[redis]` (`redis`, `asyncpg`,
  optional and lazily imported), `[arqit_py]` (7 proprietary Arqit
  SKA-Platform™ wheels via `--find-links`, not redistributable), `[pqc]`
  (`liboqs-python`, optional), `[dev]` (`pytest`, `pytest-asyncio`,
  `pytest-cov`, plus `fastapi`/`uvicorn`/`httpx` needed only for the E2E
  test).

- **`.env.example`** — 6 sections: Key Provider, Arqit, Session Cache,
  Auth, Redis (optional), PostgreSQL.

- **`README.md`** — complete documentation: the package's motivation,
  features, installation with the 5 extra profiles, configuration,
  structure, usage examples (direct crypto, standard handshake, PQC
  handshake, key providers), security guarantees, test instructions.

### Changed

- **Redis made fully optional** — moved from a base dependency to the
  `[redis]` extra; the import in `RedisKeyCache.initialize()` was
  already lazy, now the declared dependency reflects that too.

### Fixed

- **`KeySessionProviderArqitPy` not reusable across multiple handshake
  rounds on the same instance** — a bug relevant to any long-running
  server using the `arqit_py` provider (discovered during development
  of `stls-mcp`, where a single server process serves multiple
  handshakes over its lifetime).

  **Symptom**: the first handshake on an `arqit_py` server works
  correctly; a second handshake (same server process, a new or
  reconnected client) would fail with a timeout
  (`getSessionKeyIdentifier()` blocked for 30s) or, worse, with
  `StlsHandshakeFailed: Proof of possession invalid` — the server
  signed the proof with an old key while the client held the new one
  just negotiated by its own peering.

  **Root cause**: `KeyProviderServerArqitPy.createProviderSession()`
  always returns the same `KeySessionProviderArqitPy` instance for the
  entire lifetime of the process (correct — it's the right design for a
  long-running server). But `getSessionKeyIdentifier()` pinned
  `self._session_key_identifier` on the **first** call and kept reusing
  it forever — every subsequent call received the same kid from the
  very first handshake, never a new one, even when peering had in the
  meantime negotiated completely different keys with a different
  client (or the same client reconnected). `_ArqitPyKeyStore.wait_for_key()`
  made the problem worse by always returning the latest key already in
  the store without distinguishing "already seen" from "new".

  **Fix**: added `_ArqitPyKeyStore.wait_for_new_key(seen_key_ids,
  timeout)`, which explicitly blocks until a key NOT in `seen_key_ids`
  arrives. Rewrote `KeySessionProviderArqitPy`: introduced
  `_pinned_key_identifier` (set ONLY by an explicit call to
  `setSessionKeyIdentifier()`, used on the CLIENT side to pin a
  session known in advance — unchanged behavior, stable across
  repeated calls) and `_delivered_key_ids` (the set of all kids already
  automatically delivered by this instance, used to always request a
  new key on subsequent calls). The fallback in `getSessionKeyData()`
  for `stls-a2a`'s lazy flow (lookup by STLS `session_id` instead of
  the Arqit kid) was preserved but made dynamic: it now always resolves
  to the last auto-resolved kid (`_last_resolved_key_identifier`), no
  longer to a permanently pinned value.

  **Validated**: multiple rounds (3+) on the same provider instance,
  each correctly resolving its own new key; explicit client-side
  pinning stable across repeated calls; `stls-a2a` backward-compatibility
  fallback working. Also confirmed end-to-end on real Arqit
  SKA-Platform™ infrastructure via `stls-mcp`: server restarted only
  once, client launched repeatedly and succeeding on subsequent rounds
  with no need to restart the server.

### Security

- **The new handshake scheme's design rationale, stated explicitly**:
  the previous "lazy" scheme (session_id chosen and sent by the client,
  embedded in the first encrypted envelope) was evaluated and judged
  insufficient — no proof-of-key-possession, an untrusted session_id
  because it was controlled by the client. The HMAC challenge/response
  scheme introduced here requires client and server to mutually prove
  possession of the same key before the session is considered
  established, with a server-generated session_id (uuid4, not
  enumerable). Authentication of the client itself (i.e.: who is allowed
  to start a handshake) remains delegated to the Arqit SKA-Platform™
  layer (device-level peering) when using the `arqit_py` provider; the
  `base` provider provides no authentication at all and should be
  considered strictly for development, testing and CI — a limitation
  explicitly documented in the README.

- **Hybrid security property of the PQC provider**: the final
  session_key depends on both components — X25519 and ML-KEM-768.
  Breaking only one of the two is not enough: both must be broken
  simultaneously to compromise the session_key. Specifically:
  - If a quantum computer broke X25519 (Shor's algorithm on an elliptic
    curve), ML-KEM-768 (lattice-based, resistant to both Shor and
    Grover) still protects it.
  - If ML-KEM-768 had an unknown vulnerability, X25519 still protects
    against classical adversaries.
  This is the same strategy adopted by TLS 1.3 with `X25519MLKEM768`
  (note: this project's cloudflared log shows exactly `X25519MLKEM768`
  as the cipher preference, consistent with stls-core's design).

  Quantum-safety comes from the lattice-based algorithm, not from the
  "quantum quality" of the entropy: `os.urandom()` via the kernel's
  CSPRNG (ChaCha20 on Linux) provides 256 bits of entropy, sufficient
  for NIST Level 3 (security equivalent to AES-192). Hardware QRNG is a
  future upgrade that can be injected via HKDF-Extract as an external
  salt with no interface changes.

### Validated

- **74 unit tests** (`tests/test_stls_core_crypto.py`,
  `test_stls_core_session.py`, `test_stls_core_handshake.py`,
  `test_stls_core_providers.py`, `test_stls_core_auth.py`) — all passed
  in a real run.
- **7 E2E tests** (`tests/test_e2e_client_server.py`) — a real FastAPI
  server in a thread + a real httpx client, covering handshake,
  encrypt/decrypt round-trip, tampering detection, isolation between
  sessions. Only the `base` provider, no external dependency required.
- **80 PQC tests** (`tests/test_pqc.py`) — 9 classes: `TestPQCKeyStore`,
  `TestX25519`, `TestDeriveHybridKey`, `TestMLKEM`,
  `TestKeySessionProviderPQC`, `TestKeyProviderLifecycle`,
  `TestPQCHandshakeModels`, `TestPerformPQCHandshake`, `TestRoundTrip`.
  Tests that require `liboqs-python` are marked with `@requires_liboqs`
  and automatically skipped if the library is unavailable or
  incompatible with the system's C library (a guard catching
  `ImportError`, `AttributeError` and `OSError`). With `liboqs-python
  0.15.0` + `liboqs C 0.15.0` built from source: 80/80 passed. Without
  liboqs: 45/80 (only the non-PQC providers and handshake).
- **Build verified**: `uv build` produces working wheel and sdist;
  installation in a clean venv and a functional smoke test both ran
  successfully.
- **Real Arqit SKA-Platform™ integration** — via `stls-a2a`
  (`task_test.py`) and `stls-mcp` (the Vault example): device
  authentication confirmed, first handshake and multiple consecutive
  rounds on the same server process.
- **PQC provider E2E integration** — via `stls-mcp` (the Vault
  example): `vault_server.py` + `vault_client.py` with
  `KEY_PROVIDER_NAME=pqc`, tools/resources/prompts discovery and full
  calls over `/mcp-stls`. **Note on liboqs alignment**: PyPI only hosts
  `liboqs-python 0.14.1` and `0.15.0`. With the system's `liboqs C
  0.11.0`, it was necessary to build `liboqs 0.15.0` from source (`git
  clone --branch 0.15.0`, `cmake -DBUILD_SHARED_LIBS=ON`, `ninja
  install`, `ldconfig`) to align the versions and eliminate the
  `AttributeError: OQS_SIG_supports_ctx_str`.
