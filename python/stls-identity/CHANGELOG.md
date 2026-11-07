# Changelog

All relevant changes to this package, in reverse chronological order.

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
