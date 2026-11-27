# Changelog

All notable changes to **stls-identity (Node.js)** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html),
and tracks the version of [`python/stls-identity`](../../python/stls-identity/CHANGELOG.md),
of which this package is the TypeScript port.

---

## [0.1.6] — 2026-11-27

1:1 port of `python/stls-identity` 0.1.6 (issue #74): authorization
scope was never propagated from identity verification to the
tool/action executor. `RequestPresentationVerifier.verifyRequest()`
unpacked and verified the presented credential(s) — including the
`OperationalCredential`'s `scope` — then discarded all of it once
`ok` was true.

### Added

- `PresentationVerificationResult` now carries `requestorDid`,
  `grantorDid`, and `scope` alongside `ok`/`error`, populated on every
  successful `verifyRequest()` call. `scope` stays `undefined` for a
  plain identity presentation (no delegation involved) and is the
  `OperationalCredential`'s own scope object (unchanged, still opaque
  to this layer) for a delegated one.

## [0.1.5] — 2026-11-27

No `.ts` source changes. Version bump only, to stay numerically aligned
with `python/stls-identity` 0.1.5 (issue #65's dependency-guard audit,
extended to `stls-identity` after the `stls-mcp`/`stls-a2a` passes).
That fix was Python-only: six dead `try/except ImportError` guards
around already-mandatory `redis`/`asyncpg` imports, plus `httpx`
having been under-declared (only in `[dev]`, despite being used
unconditionally). `node/stls-identity` was audited for the same class
of issue and confirmed already clean — its own `pg` dynamic import is
correctly in `optionalDependencies`, every other import is a
genuinely-mandatory static one. Nothing to fix here.

## [0.1.4] — 2026-11-26

First release — TypeScript port of `python/stls-identity`'s DID/VC
identity layer (issue #47), Phases 1-5: `did.ts`, `models.ts`,
`presentation.ts`, `signing.ts`, `verifier.ts` (`Sentinel`),
`request-verifier.ts`, `keystore.ts`, `credential-cache.ts`,
`status.ts`, `postgres-store.ts`, `trust-registry-store.ts`,
`trust-registry-client.ts`, `issuer.ts` (`IdentityManager`),
`delegation.ts` (`DelegationManager`). Cross-language wire
compatibility verified with a frozen golden-vector fixture shared with
`python/stls-identity`'s own test suite, plus live Node-issue/
Python-verify and Python-issue/Node-verify checks — which caught a
real bug: Python's `Sentinel` re-validates an incoming credential/
presentation into a Pydantic model before canonicalizing it for
signature verification, and Pydantic's own datetime round-trip format
differs from plain `Date.toISOString()` (a zero-millisecond fraction
is dropped entirely; a non-zero one is expanded to 6-digit
microseconds) — fixed with `toPydanticIsoString()` in `models.ts`.
Phase 6 (`bridge.ts`'s FastAPI route wiring, and actually wiring this
package into `node/stls-a2a`/`node/stls-mcp`) stays out of scope, per
the issue.
