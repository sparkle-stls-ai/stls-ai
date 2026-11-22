# Changelog

All relevant changes to this package, in reverse chronological order.

## [0.1.3] — 2026-11-22

Issue #51 — a generalized loader for a real Grantor agent, mirroring
issue #45's `load_identity_presenter()`.

### Added

- `load_delegation_manager(agent_id, *, governing_authority_host,
  status_list_manager, status_list_publish_url, audit_hook=None)` in
  `stls_id_delegation.py` — loads an already-bootstrapped,
  already-issued-with-`roles=["grantor"]` agent's own identity and
  returns a ready-to-use `DelegationManager`. Returns `None` if the
  agent has no signing key yet, no cached Identity Credential yet, or
  its credential doesn't declare the `"grantor"` role — same
  off-by-default posture as every other loader in this package.
  Status-list wiring (which Redis/HTTP endpoint publishes revocations)
  stays the caller's responsibility, passed through unchanged, since
  it's shared platform infrastructure, not part of "my own identity".

### Tests

- 99 tests (was 95) — 4 new covering `load_delegation_manager()`: no
  key, no cached credential, credential without the grantor role, and
  a full roundtrip that actually issues a real Operational Credential
  with it.

## [0.1.2] — 2026-11-22

Issue #49 — encryption at rest for the Redis-backed signing-key
keystore.

### Security

- `RedisIdentityKeystore` now seals every private key with AES-256-GCM
  before `SET` and opens it after `GET`, when `STLS_CORE_ATREST_KEY`
  is set — reuses `stls_core.AtRestCipher` (issue #20) with its own
  domain-separation label (`b"stls-identity/keystore"`), so the same
  KEK can safely serve both stores without key reuse across them.
  Opt-in: unset, keys are stored in plaintext exactly as before this
  release, with a one-time warning logged at construction.
- Dropped the base64 encoding this module previously used for the
  plaintext case — raw bytes now, same wire shape `stls_core`'s own
  `RedisKeyStore` already uses. Not backward-compatible with
  previously-written plaintext entries from 0.1.1 or earlier; see
  `samples/server/sofia-admin/server/*/identity_manager/reset_identity_data.py`
  if migrating an existing deployment's keys.
- `get()` now fails closed (returns `None`, logs the error) on a
  decryption failure (e.g. a rotated/wrong KEK) instead of raising —
  same posture as `stls_core.providers.RedisKeyStore.get()`.

### Dependencies

- Raised the `stls_core` dependency floor to `>=0.1.11` (was `>=0.1.1`)
  — needed `AtRestCipher` exported from `stls_core`'s own `__init__.py`
  (it existed since issue #20 but wasn't importable by sibling
  packages until now).

### Tests

- 95 tests (was 91) — 4 new covering `RedisIdentityKeystore` end to
  end against a real Redis (previously untested: only
  `InMemoryIdentityKeystore` had any coverage at all): plaintext
  roundtrip, encrypted roundtrip, stored-bytes-differ-from-plaintext,
  and fail-closed behavior on a wrong KEK.

## [0.1.1] — 2026-11-20

Issue #15 gap #1 — connects this package's DID/VC/Verifiable-Presentation
engine to a real `stls-a2a` call for the first time, plus its
follow-up (issue #45): generalizes the sofia-admin coordinator's
hand-rolled identity-loading pattern into reusable package primitives.
See [`python/stls-a2a` 0.1.11](../stls-a2a/CHANGELOG.md) for the
transport side of the same change.

### Added

- `RequestPresentationVerifier` (`stls_id_request_verifier.py`) — the
  server-side bridge `stls-a2a`'s `identity_verifier` hook calls into,
  checking the `X-STLS-Identity-Presentation` header against a
  server-minted challenge.
- `IdentityPresenter`/`attach_identity_presentation_header` (client
  side) in `stls_id_presentation.py`.
- `IdentityCredentialCacheBase`/`IdentityCredentialCacheFactory`
  (memory/redis backends) — cache for an agent's own current Identity
  Credential, separate lifecycle from `IdentityKeystoreBase` (TTL =
  the credential's own `validUntil`, no manual cleanup), new module
  `stls_id_credential_cache.py`.
- `load_identity_presenter(agent_id, *, governing_authority_host)` and
  `identity_challenge_handler_from_env()` in `stls_id_presentation.py`
  — one call, driven entirely by `IDENTITY_AGENT_ID`/
  `GOVERNING_AUTHORITY_HOST`, builds a `stls-a2a`
  `identity_challenge_handler` from whatever's already bootstrapped in
  the keystore/credential cache. Unset env var means "present no
  identity", same off-by-default posture as every other piece of
  issue #15/#45.
- `decode_multibase_ed25519_public_key` and `fetch_status_bit`/
  `fetch_remote_credential_status` now exported from the package
  `__init__.py` — both already existed as internal logic but weren't
  importable, forcing callers (e.g. `samples/server/sofia-admin`) to
  duplicate them locally instead of depending on the package.

### Fixed

- **Security**: `PresentationProof.challenge` wasn't covered by the
  signature (signed before being set) — a captured Verifiable
  Presentation's challenge could be forged and replayed without the
  holder's private key. Now signs over proof-minus-`proofValue` on
  both sides, so the challenge is part of what's actually signed.

### Tests

- 91 tests (was 68 as of 0.1.0) — including 10 new covering the
  credential cache and the generalized loader end to end (real signing
  key + real cached credential + Sentinel verification).

## [0.1.0] — first version

### Added

**Phase 1 — Identity**
- DID Document (`did:web`), Ed25519 signing-key bootstrap, dedicated
  persistent no-TTL keystore (not `ArqitKeyStoreBase`, different
  semantics: that one is for ephemeral session keys)
- Identity Credential (`NetworkAuthorityCredential`), issued alongside
  the existing API key, never as a replacement
- Real RFC 8785 (JCS) canonicalization, not a `sort_keys`
  approximation — necessary to make independently implemented bridges
  converge on the same signed bytes

**Phase 2 — Delegation and federation**
- Operational Credential — Grantor → Requestor delegation, depth-limit
  1 enforced at runtime (an `isinstance` check, not just convention)
- Revocation via status list — bitstring on Redis, same atomic-Lua
  pattern as `stls:revoked`, 5-minute MRI, fail-closed verification
- HTTP Identity Bridge (DID Document + status list), backed by Postgres
- Verifiable Presentation — bundle of one or two credentials with
  holder binding and replay protection via challenge
- Trust Anchor Registry — recognized Authorities (Option A,
  centralized), with key history: a rotation doesn't invalidate
  credentials signed before the rotation
- Live hookup of the registry to the `Sentinel`: `verify_signature()`
  attempts historical resolution when a key isn't in the current
  cache, before rejecting

**W185 alignment** (response to colleagues' contribution)
- Fixed `verificationMethod.controller`: the agent itself, not the
  Governing Authority
- `proof.type` → `DataIntegrityProof` / `cryptosuite: eddsa-jcs-2022`
  (was `Ed25519Signature2020`, with the wrong implicit canonicalization)
- `schemaVersion`, `principalRef`, `keyStorageTier` as declarative
  metadata on the Identity Credential
- `roles` field + explicit check in `DelegationManager`: having a
  valid identity is no longer enough to act as a Grantor

### Release notes

- Requires `stls-core` as a local dependency (`[tool.uv.sources]`,
  relative path `../stls-core`) — **the exact project name must be
  confirmed** against the real `[project.name]` in `stls-core`'s
  `pyproject.toml` before publishing to a private index
- 68 tests, against real Postgres/Redis/crypto where relevant
- Not yet included: VP bundle wire format in `stls-a2a`, per-agent
  key rotation, hardware-backed keystore, Option B/C governance,
  hybrid PQC for signing, selective disclosure
