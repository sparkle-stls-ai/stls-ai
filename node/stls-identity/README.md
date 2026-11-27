# stls-identity (Node.js)

TypeScript port of [`python/stls-identity`](../../python/stls-identity/README.md) —
the DID + Verifiable Credential identity layer for the STLS AI
ecosystem: `did:web` identities, `NetworkAuthorityCredential`/
`OperationalCredential` issuance and delegation, the `Sentinel`
verifier, and the Redis/Postgres-backed stores behind them. Same wire
formats as the Python side: a Node-issued credential or presentation
verifies against Python's `Sentinel`, and vice versa.

Every compatibility claim below has been verified with a real
cross-language check (see [Testing](#testing)), not just written on
faith.

## Porting status

| Python module                          | Node                             | Status | Verified with |
|------------------------------------------|-----------------------------------|-------|-----------------|
| `stls_id_did.py`                        | `src/did.ts`                      | ✅ | real cross-language golden vector + live Node→Python/Python→Node checks |
| `stls_id_models.py`                     | `src/models.ts`                   | ✅ | plain TS interfaces (no zod), `toPydanticIsoString()` (see below) |
| `stls_id_presentation.py` (pure signing) | `src/presentation.ts`             | ✅ | real cross-language golden vector |
| `stls_id_presentation.py` (loaders)      | `src/presentation.ts` (bottom half) | ✅ | — |
| `stls_id_verifier.py` (`Sentinel`)      | `src/verifier.ts`                  | ✅ | real cross-language golden vector |
| `stls_id_request_verifier.py`           | `src/request-verifier.ts`          | ✅ | — |
| `stls_id_keystore.py`                   | `src/keystore.ts`                  | ✅ | real redis-server, plaintext + `AtRestCipher`-sealed |
| `stls_id_credential_cache.py`           | `src/credential-cache.ts`          | ✅ | real redis-server |
| `stls_id_status.py`                     | `src/status.ts`                    | ✅ | real redis-server (Lua script ported verbatim) |
| `stls_id_postgres_store.py`             | `src/postgres-store.ts`            | ⚠️ | error paths only — no Postgres server available in this environment |
| `stls_id_trust_registry_store.py`       | `src/trust-registry-store.ts`      | ⚠️ | logic verified against an in-memory fake store; Postgres error paths only |
| `stls_id_trust_registry_client.py`      | `src/trust-registry-client.ts`     | ✅ | — |
| `stls_id_issuer.py`                     | `src/issuer.ts`                    | ✅ | — |
| `stls_id_delegation.py`                 | `src/delegation.ts`                | ✅ | — |
| `stls_id_bridge.py`                     | — | ⬜ | out of scope (issue #47) — FastAPI route wiring, no Node HTTP framework chosen yet |
| `stls_id_trust_registry_bridge.py`      | — | ⬜ | out of scope (issue #47), same reason |

Legend: ✅ ported and verified with a real test in this environment · ⚠️ ported but with a part that could not be verified here · ⬜ not ported.

Also out of scope for this port: actually wiring this package into
`node/stls-a2a` or `node/stls-mcp` (issue #46 and `node/stls-a2a`'s own
gap #1 hooks — both still need `node/stls-a2a` to exist first).

## Installation

```bash
cd node/stls-identity
npm install
```

Runtime dependencies installed automatically:

| Package | Used for |
|---|---|
| `@noble/curves` | Ed25519 sign/verify/keygen (same library already used by `node/stls-core`'s `pqc` provider for X25519) |
| `bs58` | base58btc encode/decode for multibase public keys and signatures — `node/stls-core` has no base58 codec of its own |
| `canonicalize` | RFC 8785 (JCS) canonicalization — the real spec, not a hand-rolled sorted-stringify (see [below](#rfc-8785-canonicalization-a-real-implementation-not-a-sorted-stringify)) |
| `redis` | Redis backends for the keystore, credential caches, and the status list bitstring |
| `stls-core` | `AtRestCipher` (issue #49's at-rest encryption, reused directly — not reimplemented) and the audit primitives (`AuditEntry`/`AuthResult`/`defaultAuditHook`) `issuer.ts`/`delegation.ts` write to |
| `pg` (optional) | `PostgresDIDDocumentStore`/`PostgresStatusListCacheStore`/`PostgresTrustAnchorRegistryStore` — only needed if you actually construct one of those |

## Build

```bash
npm run build       # compiles src/ -> dist/
```

## Usage

### 1. Bootstrap an agent's identity and issue a credential

```ts
import {
  bootstrapSigningKey,
  buildDidDocument,
  IdentityKeystoreFactory,
  IdentityManager,
  StatusListManager,
} from "stls-identity";
import { createClient } from "redis";

const keystore = IdentityKeystoreFactory.build(); // memory by default
const { publicKey, created } = await bootstrapSigningKey("agent-087", keystore);

const doc = buildDidDocument({
  host: "sofia.sparkle.example.com",
  agentPathSegments: ["agents", "087"],
  governingAuthorityDid: "did:web:sofia.sparkle.example.com",
  signingPublicKey: publicKey,
});
// created === true the first time -> publish `doc`; false on a restart -> keep serving the existing one

const redis = createClient({ url: "redis://localhost:6379" });
await redis.connect();
const status = new StatusListManager(redis, "sofia");

const { privateKey: authorityKey } = await IdentityKeystoreFactory.build().getOrCreate("authority");
const issuer = new IdentityManager({
  governingAuthorityDid: "did:web:sofia.sparkle.example.com",
  signingKey: authorityKey,
  signingVerificationMethodId: "did:web:sofia.sparkle.example.com#key-1",
  statusListManager: status,
  statusListPublishUrl: "https://sofia.sparkle.example.com/identity/status-list",
});

const credential = await issuer.issueIdentityCredential({
  agentDid: doc.id,
  clientId: "agent-087",
  roles: ["grantor"],
});
```

### 2. Present it, and verify it (the `Sentinel`)

```ts
import { buildVerifiablePresentation, attachIdentityPresentationHeader, Sentinel } from "stls-identity";

const vp = buildVerifiablePresentation({
  holderDid: doc.id,
  holderSigningKey: /* the agent's own Ed25519 private key */,
  holderVerificationMethodId: `${doc.id}#key-1`,
  credentials: [credential],
  challenge: "server-issued-nonce",
});
const headers = attachIdentityPresentationHeader(vp); // { "X-STLS-Identity-Presentation": "<base64>" }

// --- verifier side ---
const sentinel = new Sentinel({
  trustedIssuerKeys: new Map([["did:web:sofia.sparkle.example.com#key-1", authorityPublicKey]]),
});
sentinel.verifyPresentationProof(vp, { expectedChallenge: "server-issued-nonce" });
await sentinel.verify(credential, { statusBitstringFetchTime: new Date(), isRevoked: false });
```

For the full incoming-request path (decode the header, dispatch
plain-identity vs. delegated shape, resolve each credential's status),
use `RequestPresentationVerifier` instead of calling `Sentinel`'s
pieces by hand — it matches the duck type `node/stls-a2a`'s
`identityVerifier` hook will eventually expect.

### 3. Delegation (Grantor → Requestor)

```ts
import { DelegationManager } from "stls-identity";

const delegation = new DelegationManager({
  grantorDid: doc.id, // must hold a NetworkAuthorityCredential with roles including "grantor"
  grantorSigningKey: /* the Grantor's own key */,
  grantorVerificationMethodId: `${doc.id}#key-1`,
  statusListManager: /* the Grantor's OWN status list — namespaced separately from the Authority's */,
  statusListPublishUrl: "https://sofia.sparkle.example.com/grantors/087/status-list",
});

const operational = await delegation.issueOperationalCredential({
  grantorIdentityCredential: credential, // the Grantor's own, already-verified identity credential
  requestorDid: "did:web:sofia.sparkle.example.com:agents:requestor",
  requestorClientId: "agent-requestor",
  scope: { action: "cross-connect-activate", facilityCode: "MI1" },
});
```

A `VerifiablePresentation` for a delegated call carries BOTH the
`OperationalCredential` and the Grantor's own `NetworkAuthorityCredential`
— `Sentinel.verifyDelegationChain()` needs both to run at all.

### 4. Environment-driven wiring for `node/stls-a2a`

```ts
import { identityChallengeHandlerFromEnv } from "stls-identity";

process.env.IDENTITY_AGENT_ID = "agent-087";
process.env.GOVERNING_AUTHORITY_HOST = "sofia.sparkle.example.com";

const handler = await identityChallengeHandlerFromEnv(); // undefined if IDENTITY_AGENT_ID is unset
// pass `handler` straight into node/stls-a2a's identityChallengeHandler parameter, once that package exists
```

## Environment variables

Same names as the Python side, where applicable:

```bash
# Signing-key keystore
IDENTITY_KEYSTORE_BACKEND=memory              # "memory" (default) | "redis"
IDENTITY_KEYSTORE_REDIS_URL=redis://localhost:6379
IDENTITY_KEYSTORE_REDIS_NAMESPACE=stls:identity:signingkey

# An agent's own cached identity credential (single slot)
IDENTITY_CREDENTIAL_CACHE_BACKEND=memory      # "memory" (default) | "redis"
IDENTITY_CREDENTIAL_CACHE_REDIS_URL=          # falls back to STATUS_LIST_REDIS_URL, then redis://localhost:6379
IDENTITY_CREDENTIAL_CACHE_REDIS_NAMESPACE=stls:identity:credential

# Operational credentials an agent has received via delegation (a collection)
OPERATIONAL_CREDENTIAL_CACHE_BACKEND=memory   # "memory" (default) | "redis"
OPERATIONAL_CREDENTIAL_CACHE_REDIS_URL=       # falls back to STATUS_LIST_REDIS_URL, then redis://localhost:6379
OPERATIONAL_CREDENTIAL_CACHE_REDIS_NAMESPACE=stls:identity:opcred

# Encryption at rest for the keystore (issue #49) — SAME KEK node/stls-core's own stores read.
STLS_CORE_ATREST_KEY=

# Postgres (optional — only if you construct a Postgres*Store yourself)
IDENTITY_PG_DSN=
IDENTITY_PG_DID_TABLE=stls_identity_did_documents
IDENTITY_PG_STATUS_LIST_TABLE=stls_identity_status_list_cache
TRUST_REGISTRY_PG_TABLE=sf_identity_trust_anchors

# Environment-driven identity presentation (node/stls-a2a wiring)
IDENTITY_AGENT_ID=
GOVERNING_AUTHORITY_HOST=
```

## Testing

### One command

```bash
cd node/stls-identity
npm run build
TEST_REDIS_URL=redis://localhost:6379 npm test
```

Runs every `tests/*.smoke.ts` in sequence: pure logic (JCS
canonicalization, `did.ts`, `presentation.ts`, `verifier.ts`,
`request-verifier.ts`), the real-RFC-8785 cross-language golden vector
(`tests/interop/golden-vector.smoke.ts` — no `TEST_REDIS_URL` needed),
real-Redis round trips for the keystore/credential-caches/status list
(soft-skip if `TEST_REDIS_URL` is unset), Postgres error paths (no real
Postgres needed), and the issuer/delegation managers end-to-end against
`Sentinel`. [`node-tests.yml`](../../.github/workflows/node-tests.yml)
runs this same chain in CI with a Redis service container.

### The cross-language golden vector

`tests/fixtures/golden-vector.json` is byte-identical to
`python/stls-identity/tests/fixtures/golden_vector.json` — generated
once from Python's real `IdentityManager`/`build_verifiable_presentation()`,
committed to both repos frozen. `tests/interop/golden-vector.smoke.ts`
canonicalizes it in Node and checks the result against the bytes
Python's `rfc8785.dumps()` actually produced, then verifies the
signature Python's `cryptography` library actually computed — not a
reimplementation signing its own output and checking itself.
`python/stls-identity/tests/test_golden_vector_node_interop.py` is the
mirror check on the Python side.

## Critical cross-language compatibility notes

### Pydantic's datetime round-trip format — the trickiest part of this port

**This is a real bug this port caught, not a theoretical risk.** Python's
`Sentinel.verify_signature()`/`verify_presentation_proof()` don't
canonicalize the raw bytes they received over the wire — they
re-validate the incoming JSON into a Pydantic model first (`model_validate()`),
*then* `model_copy()` + `model_dump()` it before canonicalizing. That
round-trip through Python's own `datetime` type does not reproduce
`Date.toISOString()`'s format: confirmed empirically against a real
Pydantic v2 model,

- a sub-second fraction of exactly zero is **dropped entirely**
  (`"...T12:00:00.000Z"` → `"...T12:00:00Z"`, never emitted as `.000`), and
- any non-zero fraction is **expanded to full 6-digit microseconds**,
  zero-padded on the right (`.123` → `.123000`), never left at 3-digit
  millisecond precision.

A credential or presentation signed with a plain `Date.toISOString()`
timestamp therefore signs over bytes Python's own verifier can never
reproduce — every genuinely Node-issued credential would fail to
verify in Python, discovered by a **live** Node-issue → Python-verify
check during this port (the frozen golden vector above didn't catch it,
because its timestamps were themselves generated by Pydantic in the
first place and so already "happened" to be in Pydantic's own format).

Fixed with `toPydanticIsoString()` (`src/models.ts`), used everywhere
a datetime is written into a field that ends up inside a
JCS-canonicalized, signed payload — directly (`validFrom`/`validUntil`,
a presentation's `proof.created`) or **transitively**, since a
credential's own `proof.created` becomes part of the outer
presentation's canonicalized bytes once that credential is embedded in
a `VerifiableCredential[]`.

### RFC 8785 canonicalization: a real implementation, not a sorted-stringify

Same lesson as `python/stls-identity`'s own `rfc8785` dependency: a
hand-rolled `JSON.stringify` with sorted keys looks equivalent for
simple payloads but silently diverges on non-ASCII string escaping
(`JSON.stringify` default-escapes to `\uXXXX`; RFC 8785 requires raw
UTF-8 bytes) and would make a credential signed by one platform fail
verification on another, even though both sides "canonicalized" it.
`canonicalize` (npm) is used instead — confirmed empirically to match
`rfc8785`'s behavior on both the non-ASCII and key-insertion-order
cases (`tests/jcs-canonicalization.smoke.ts`).

### Two distinct signing shapes — conflating them silently breaks verification

`signing.ts` has two function pairs, not one:

- **Credential signing** (`signCredentialPayload`/`verifyCredentialSignature`):
  the `proof` key is **entirely absent** from the canonicalized
  payload — the `CredentialProof` object is only constructed and
  attached *after* signing; its own `created` timestamp is never part
  of the signed bytes.
- **Presentation signing** (`signProofBoundPayload`/`verifyProofBoundPayload`):
  the `proof` object **is** present, with every field except
  `proofValue` itself — this is what makes a presentation's `challenge`
  tamper-evident (overwriting `proof.challenge` on a captured,
  validly-signed presentation would otherwise not change the signature
  at all).

### multibase/multicodec encoding

Public keys: multicodec prefix `0xed01` (ed25519-pub) + base58btc +
`z` prefix. Signatures: raw bytes + base58btc + `z` prefix — **no**
multicodec prefix on the signature itself, only on the public key.
`bs58` (npm) uses the same base58btc alphabet as Python's `base58`
package.

### Proof type: `DataIntegrityProof` / `eddsa-jcs-2022`, not `Ed25519Signature2020`

`Ed25519Signature2020` (as W3C specifies it) implies RDF Dataset
Canonicalization (URDNA2015) — a different, heavier canonicalization
than what this codebase actually does. `eddsa-jcs-2022` is the
correctly-named cryptosuite for "Ed25519 over JCS-canonicalized JSON".

### Trust Anchor rotation: a real transaction, not a read-then-write race

`trust-registry-store.ts`'s `put()` closes the previously-current key
(if any) and inserts the new one inside an actual `BEGIN`/`COMMIT`
transaction on a single checked-out `pg` client — not two separate
`pool.query()` calls, which could each land on a different pooled
connection and lose the atomicity this method exists for. Mirrors two
real bugs the Python port's own docstring documents from before this
was transactional there.

## Keeping in sync with the Python version

The version number in `package.json` follows
`python/stls-identity/pyproject.toml` (currently `0.1.6`) — same
convention as `stls-core`/`stls-mcp`. Not yet implemented: `bridge.ts`/
`trust-registry-bridge.ts` (the FastAPI route wiring — no Node HTTP
framework has been chosen for this package yet). `node/stls-mcp`'s own
`identityVerifier` hook (issue #46) is wired and ready to take a
`RequestPresentationVerifier` instance directly — its `verifyRequest()`
shape matches `IdentityVerifierLike` exactly. There is no
`node/stls-a2a` to wire into at all — only `python/stls-a2a` exists.

## License

Dual-licensed under the [GNU AGPLv3](LICENSE) and a
[commercial license](LICENSE-COMMERCIAL.md), consistent with
`python/stls-identity`, `stls-core` and `stls-mcp`.
