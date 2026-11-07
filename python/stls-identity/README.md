# stls-identity

DID + Verifiable Credential identity layer for the STLS-AI ecosystem.

Fourth package in the monorepo, on top of `stls-core`: adds a
persistent, third-party-verifiable identity to agents — separate from
authorization — without touching `stls-core`, `stls-a2a` or
`stls-mcp`. `ApiKeyManager`/`KeyRecord` remain fully operational in
parallel throughout the migration.

## What it does

| Capability | Module | Status |
|---|---|---|
| DID Document (`did:web`), Ed25519 key bootstrap | `stls_id_did.py` | ✅ tested |
| Identity Credential (`NetworkAuthorityCredential`) | `stls_id_issuer.py` | ✅ tested |
| Operational Credential — Grantor → Requestor delegation, depth-limit 1 | `stls_id_delegation.py` | ✅ tested |
| Revocation via status list (bitstring on Redis, 5 min MRI) | `stls_id_status.py` | ✅ tested (real Redis) |
| Verifiable Presentation (Identity + Operational bundle) | `stls_id_presentation.py` | ✅ tested |
| Identity Bridge (HTTP: DID Document + status list) | `stls_id_bridge.py`, `stls_id_postgres_store.py` | ✅ tested (real Postgres) |
| Trust Anchor Registry — recognized Authorities + key history | `stls_id_trust_registry_*.py` | ✅ tested (real Postgres), hooked up live to the Sentinel |
| Verification (`Sentinel`) — signature, validity, revocation, delegation chain | `stls_id_verifier.py` | ✅ tested |

Canonicalization: real RFC 8785 (JCS) via the `rfc8785` library, not an
approximation — necessary so that bridges implemented independently on
different platforms converge on the same signed bytes. Signature:
Ed25519, proof type `DataIntegrityProof` /
`cryptosuite: eddsa-jcs-2022`.

## What it does NOT do (yet)

Listed explicitly so it doesn't look like it was forgotten:

- **VP bundle wire format inside `stls-a2a`** — the model
  (`VerifiablePresentation`) and the verification logic exist and are
  tested; how it travels in an actual A2A request (header? dedicated
  field?) is not yet specified in `stls-a2a`.
- **Single-agent key rotation** — key history only exists for
  Governing Authorities in the Trust Anchor Registry. An agent that
  rotates its own key doesn't have the same mechanism (its DID
  Documents have no history).
- **Hardware-backed keystore** — `IdentityKeystoreBase` only has
  software backends (`memory`, `redis`); every credential honestly
  declares `keyStorageTier: TIER_1_SOFTWARE`, never a higher tier.
- **Registry governance beyond Option A** — only the centralized
  registry (operated by an anchor member) is implemented. Quorum
  (Option B) and the transparency log (Option C) remain design-only.
- **PQC for identity signing** — credentials are signed with Ed25519
  only (classical). The Ed25519+ML-DSA hybrid discussed for alignment
  with `stls-core`'s PQC handshake is not implemented.
- **Selective disclosure / ZKP, DIDComm receipts** — explicitly
  Phase 5/Full, never in scope for this version.

## Installation

In the monorepo, with `uv`:

```bash
cd python/stls-identity
uv venv
uv sync --extra dev
```

`[tool.uv.sources]` resolves `stls-core` from the sibling folder
(`../stls-core`) in editable mode — no extra manual step needed.

## Quick usage

```python
from stls_identity import (
    IdentityManager, InMemoryIdentityKeystore,
    bootstrap_signing_key, build_did_document,
)

keystore = InMemoryIdentityKeystore()  # in production: RedisIdentityKeystore
key, pub, _ = bootstrap_signing_key("my-agent", keystore)
doc = build_did_document(
    host="sofia.sparkle.example.com",
    agent_path_segments=["agents", "my-agent"],
    governing_authority_did="did:web:sofia.sparkle.example.com",
    signing_public_key=pub,
)
```

Three complete, runnable examples in `examples/`:

- `basic_identity_and_delegation.py` — DID, Identity Credential,
  Operational Credential, all in a single flow
- `federated_grantor_external_authority.py` — an external Grantor
  (Operator C), with and without onboarding into the Trust Anchor
  Registry
- `internal_grantor_no_registry_needed.py` — a Grantor internal to
  Sparkle: no external bridge, no registry involved

```bash
uv run python examples/basic_identity_and_delegation.py
```

## Tests

```bash
uv run pytest -v
```

Requires reachable Redis and Postgres for the tests that actually use
them (`test_status_list_redis.py`, `test_postgres_store.py`,
`test_trust_registry.py`, `test_bridge.py`) — see `.env.example` for
the variables (`IDENTITY_TEST_PG_DSN`, `STATUS_LIST_TEST_REDIS_URL`,
loaded automatically by `tests/conftest.py`). Tests that don't touch
external infrastructure still run with nothing configured.

68 tests as of 0.1.0, all against real crypto/DB/cache where relevant
— never against Python stubs for the parts that matter (signing,
canonicalization, persistence, revocation).

## Structure

```
src/stls_identity/
    stls_id_models.py              Pydantic models (DID, VC, VP, TrustAnchorEntry)
    stls_id_did.py                 DID Document, key bootstrap, multibase encode/decode
    stls_id_keystore.py            signing-key persistence (no-TTL, not ArqitKeyStoreBase)
    stls_id_issuer.py              IdentityManager — Identity Credential issuance
    stls_id_delegation.py          DelegationManager — Operational Credential, depth-1
    stls_id_status.py              status list (Redis bitstring, MRI)
    stls_id_presentation.py        Verifiable Presentation construction/signing
    stls_id_verifier.py            Sentinel — all verifications
    stls_id_postgres_store.py      DID Document + status list cache (Postgres)
    stls_id_bridge.py              public HTTP endpoints (FastAPI)
    stls_id_trust_registry_store.py    Trust Anchor Registry, key history (Postgres)
    stls_id_trust_registry_client.py   cache/TTL, historical fallback for the Sentinel
    stls_id_trust_registry_bridge.py   read-only HTTP endpoint for the registry
examples/    three runnable scripts, real data generated by the code
tests/       68 tests, same "against real infrastructure" discipline as the rest of the monorepo
```

## License

Dual-licensed: [GNU AGPLv3](LICENSE) or [commercial license](LICENSE-COMMERCIAL.md),
consistent with `stls-core`, `stls-a2a` and `stls-mcp`.
