# stls-core (Node.js)

TypeScript port of [`python/stls-core`](../../python/stls-core/README.md) —
the same encrypted envelope, the same handshake, a bit-compatible wire
format. It lets you use STLS (Symmetric Transport Layer Security)
cryptography to secure Node applications that need to interoperate with
existing Python `stls-core` servers/clients (e.g. `stls-a2a`,
`stls-mcp`) — or with each other over pure Node.

Every compatibility claim in this README has been verified with a real
cross-language test (see [Testing](#testing)), not just written on
faith: Node and Python exchange vectors produced by each other and
check that they match byte-for-byte.

## Porting status

| Python module                               | Node                              | Status | Verified with |
|----------------------------------------------|------------------------------------|-------|-----------------|
| `stls_core_constants.py`                     | `src/constants.ts`                 | ✅ | — |
| `stls_core_crypto.py`                        | `src/crypto.ts`                    | ✅ | real cross-language vectors |
| `stls_core_session.py` (memory)              | `src/session.ts`                   | ✅ | — |
| `stls_core_session.py` (redis)               | `src/session-redis.ts`             | ✅ | real redis-server, bidirectional read/write |
| `stls_core_handshake.py`                     | `src/handshake.ts`                 | ✅ | — |
| `stls_core_handshake_pqc.py`                 | `src/handshake-pqc.ts`             | ✅ | — |
| `providers/..._base.py`                      | `src/providers/base.ts`            | ✅ | — |
| `providers/..._pqc.py`                       | `src/providers/pqc.ts`             | ✅ | real liboqs-python + pyca/cryptography |
| `providers/..._arqit_py.py`                  | `src/providers/arqit-sidecar.ts`   | ✅ | RPC mechanics verified with the `base` provider; real Arqit SKA-Platform™ peering verified through two separate sidecar processes (one per role — see [below](#6-the-arqit-provider-via-the-sidecar)), including the role guard and 3 sequential clients against one long-running SERVER |
| `providers/..._factory.py`                   | `src/providers/factory.ts`         | ✅ | real handshake through a factory-built provider — `base`, `pqc`, and `arqit_py` against the real Arqit cloud (key agreement + handshake + AES-256-GCM round trip) |
| `providers/stls_core_providers_session_resolver.py` | `src/providers/session-resolver.ts` | ✅ | real Arqit cloud — one process resolving two different, already-provisioned `arqit_py` device identities via per-`targetKey` `configPath` |
| `auth/stls_core_auth_models.py`              | `src/auth/models.ts`               | ✅ | — |
| `auth/stls_core_auth_loaders.py` (static, file) | `src/auth/loaders.ts`           | ✅ | JSON and YAML |
| `auth/stls_core_auth_loaders.py` (postgres)  | `src/auth/loaders-postgres.ts`      | ✅ | real PostgreSQL instance — `loadAll()`/`loadOne()`, `TEXT[]`/`JSONB`/`TIMESTAMPTZ` conversion, and the full `ApiKeyManager` stack through it |
| `auth/stls_core_auth_store.py`               | `src/auth/store.ts`                | ✅ | real redis-server |
| `auth/stls_core_auth_manager.py`             | `src/auth/manager.ts`              | ✅ | real redis-server |

Legend: ✅ ported and verified with a real test in this environment · ⚠️ ported but with a part that could not be verified here · ⬜ not ported.

## Installation

```bash
cd node/stls-core
npm install
```

Runtime dependencies installed automatically:

| Package | Used for |
|---|---|
| `@noble/post-quantum` | ML-KEM-768 (the `pqc` provider) |
| `@noble/curves` | X25519 (the `pqc` provider) — same author as `@noble/post-quantum`, raw-bytes API |
| `redis` | Redis backend for `SessionCache` and `RedisKeyCache` (auth) |
| `yaml` (optional) | `FileLoader` reads `.yaml`/`.yml` files. If not installed, `FileLoader` falls back to JSON — the same behavior as the Python side when PyYAML is missing. |

None of these are needed for the `base` transport alone (crypto +
handshake + in-memory session cache): if you don't use PQC, Redis or
YAML, you never touch them.

## Build

```bash
npm run build       # compiles src/ -> dist/ (required before publishing or importing from other Node packages)
```

## Usage

### 1. Basic cryptography (the `base` provider, no external dependencies)

```ts
import { StlsTransport } from "stls-core";

const sessionKey = Buffer.from("0123456789abcdef"); // in production: from a handshake
const transport = new StlsTransport(sessionKey, "session-id-123");

const envelope = transport.encrypt(Buffer.from("secret payload"));
const wireJson = envelope.toDict(); // ready for HTTP/JSON-RPC

// on the receiving side
const recovered = transport.decrypt(StlsEnvelope.fromDict(wireJson));
```

### 2. Full handshake (Node client ↔ Node or Python server)

```ts
import {
  generateHandshakeRequest,
  performHandshake,
  verifyHandshakeProof,
  InMemorySessionCache,
  KeyProviderServerBase,
} from "stls-core";

// --- server side ---
const provider = new KeyProviderServerBase();
await provider.start();
const serverSession = provider.createProviderSession();
const sessionCache = new InMemorySessionCache();

// (received over HTTP) request = the client's generateHandshakeRequest(...)
const response = await performHandshake(request, serverSession, sessionCache);
// -> send `response` back to the client

// --- client side ---
const request = generateHandshakeRequest("my-node-client");
// -> send `request` to the server, receive `response`
const sessionKey = Buffer.from("0123456789abcdef"); // base provider: key known to both sides
const ok = verifyHandshakeProof(sessionKey, request.nonce, response);
```

### 3. PQC handshake (hybrid X25519 + ML-KEM-768)

```ts
import {
  KeyProviderClientPQC,
  KeyProviderServerPQC,
  generatePqcHandshakeRequest,
  performPqcHandshake,
  deriveSessionKeyClient,
  verifyPqcHandshakeProof,
  InMemorySessionCache,
} from "stls-core";

// --- client ---
const clientProvider = new KeyProviderClientPQC();
clientProvider.start(); // generates the X25519 + ML-KEM-768 keypairs
const request = generatePqcHandshakeRequest(
  "my-node-client",
  clientProvider.x25519PublicKey,
  clientProvider.mlkemPublicKey,
);

// --- server ---
const sessionCache = new InMemorySessionCache();
const response = await performPqcHandshake(request, sessionCache);

// --- client: derive the same key and verify the proof ---
const clientSession = clientProvider.createProviderSession();
const sessionKey = deriveSessionKeyClient(
  clientSession.x25519PrivateKeyBytes!,
  clientSession.mlkemSecretKeyBytes!,
  response,
  request.nonce,
);
const ok = verifyPqcHandshakeProof(sessionKey, request.nonce, response);
```

### 3b. Selecting a provider by name (`KeySessionProviderFactory`)

Sections 1–3 above instantiate a concrete provider class directly. For an
env-var-driven selection (`KEY_PROVIDER_NAME=base|pqc|arqit_py`, the same
convention Python reads), use `KeySessionProviderFactory` instead — it
creates, starts, and returns a ready `KeySessionProvider` in one call:

```ts
import { KeySessionProviderFactory, KeySessionProviderType } from "stls-core";

const provider = await KeySessionProviderFactory.getSessionProvider({
  sessionType: KeySessionProviderType.SERVER,
  // name: "pqc",   // optional override — defaults to process.env.KEY_PROVIDER_NAME
});
```

Simpler than Python's factory here, not just a smaller port: Python's
`get_session_provider()` resolves a possibly-async `start()` with its own
internal `asyncio.run()`, which only works from synchronous top-level
code — calling it from inside a running event loop raises. Node has no
such restriction (`getSessionProvider()` is just an `async function` that
`await`s `start()` directly), so it's safe to call from anywhere.

For `arqit_py`, the SERVER and CLIENT roles must each resolve to a
**different** sidecar socket — read from `ARQIT_SIDECAR_SOCKET_SERVER` /
`ARQIT_SIDECAR_SOCKET_CLIENT` respectively (or pass `socketPath`
explicitly), never the single role-agnostic `ARQIT_SIDECAR_SOCKET` that
`KeyProviderArqitSidecar` itself falls back to — using one socket for
both roles would silently put them in the same sidecar process, the one
arqit_py scenario that reliably fails against the real Arqit cloud (see
[`ARQIT_PEERING_NOTES.md`](../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md)
and the [Arqit section](#6-the-arqit-provider-via-the-sidecar) below).

This `arqit_py` path is verified against the **real** Arqit cloud, not
just the wiring: `tests/interop/provider-factory-arqit-real.live.ts`
builds both roles through this exact factory call and completes a real
bilocation key agreement, a real handshake, and a real AES-256-GCM
round trip.

### 4. Redis session cache (multi-instance deployment)

```ts
import { SessionCacheFactory } from "stls-core";

process.env.SESSION_CACHE_BACKEND = "redis";
process.env.SESSION_CACHE_REDIS_URL = "redis://localhost:6379";

const cache = await SessionCacheFactory.build(); // Promise: the redis backend must connect
```

Redis layout identical to the Python side (`{prefix}:{session_id}` → JSON
`{session_key, kid, created_at}`, sliding TTL) — a Redis instance shared
between Node and Python clients/servers works with no changes needed.

### 5. API key authentication

```ts
import { ApiKeyManager, AuthResult } from "stls-core";

// Quick, for test/dev (no Redis needed):
const manager = ApiKeyManager.fromStatic(
  { "my-dev-key": { clientId: "dev", scopes: ["secure-a2a"] } },
  /* useRedis */ false,
);
await manager.start();

const result = await manager.validate("my-dev-key", "/secure-a2a");
if (result.result !== AuthResult.OK) {
  // 401 — result.record is null, result.audit has the details
}
```

With an HTTP framework, implement the small `RequestLike` adapter to use
`validateRequest()` (Node has no single canonical `Request` type the way
Python has Starlette's):

```ts
import type { RequestLike } from "stls-core";

function fromExpress(req: express.Request): RequestLike {
  return {
    header: (name) => req.get(name) ?? undefined,
    queryParam: (name) => (req.query[name] as string) ?? undefined,
    path: req.path,
    remoteAddr: req.ip,
  };
}

const result = await manager.validateRequest(fromExpress(req));
```

For production, with key rotation, scope/endpoint checks, audit logging
and a Redis cache shared across instances:

```ts
import { ApiKeyManager, FileLoader } from "stls-core";

const manager = ApiKeyManager.fromLoader(new FileLoader("api_keys.yaml"), {
  redisUrl: "redis://localhost:6379",
});
await manager.start(); // also starts the BackgroundRefresher
```

### 6. The Arqit provider via the sidecar

Arqit SKA-Platform™ does not publish a Node SDK. To use it from Node,
peering stays on the Python side
(`python/stls-core/sidecar/arqit_sidecar.py`) and Node talks to that
process via JSON-RPC over a Unix domain socket:

```bash
# Terminal 1 — start the sidecar (in production: KEY_PROVIDER_NAME=arqit_py
# with the Arqit wheels installed and ARQIT_PY_CLIENT_CONFIG/ARQIT_PY_SERVER_CONFIG
# configured — see python/stls-core/README.md)
cd python/stls-core
KEY_PROVIDER_NAME=base .venv/bin/python sidecar/arqit_sidecar.py --socket /tmp/stls-arqit-sidecar.sock
```

```ts
// Terminal 2 — Node
import { KeyProviderClientArqitSidecar } from "stls-core";

const client = new KeyProviderClientArqitSidecar({
  socketPath: "/tmp/stls-arqit-sidecar.sock",
});
await client.start();
const session = client.createProviderSession();
const kid = await session.getSessionKeyIdentifier();
const key = await session.getSessionKeyData(kid);
```

**Important — one role per sidecar process for `provider_name=arqit_py`.**
A SERVER and a CLIENT session for the same Arqit device pair must run
in two SEPARATE sidecar processes, never the same one — mixing both in
one OS process makes the real bilocation key exchange fail with a
genuine `401 NotAuthorizedException` from Arqit's own backend, not a
local bug (full investigation in
[`python/stls-core/sidecar/ARQIT_PEERING_NOTES.md`](../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md)).
The sidecar enforces this itself: a second, different role on the same
process is rejected immediately with an explicit error instead of
failing mysteriously 30+ seconds later.

```bash
# Two separate sidecar processes, one per role — not one process for both
cd python/stls-core
KEY_PROVIDER_NAME=arqit_py .venv/bin/python sidecar/arqit_sidecar.py --socket /tmp/arqit-server.sock &
KEY_PROVIDER_NAME=arqit_py .venv/bin/python sidecar/arqit_sidecar.py --socket /tmp/arqit-client.sock &
```

```ts
const server = new KeyProviderServerArqitSidecar({ socketPath: "/tmp/arqit-server.sock", providerName: "arqit_py" });
const client = new KeyProviderClientArqitSidecar({ socketPath: "/tmp/arqit-client.sock", providerName: "arqit_py" });
```

Verified against the real Arqit cloud this way — not just
`provider_name=base` RPC mechanics — in
[`tests/interop/arqit-sidecar-real.live.ts`](./tests/interop/arqit-sidecar-real.live.ts):
a real key agreement, a real STLS handshake (proof that the
independently-obtained client/server keys are byte-identical), and a
real AES-256-GCM encrypt/decrypt round trip, both for a single client
and for 3 sequential clients against one long-running SERVER session.
The role guard itself is verified separately, with no real credentials
needed, in [`tests/interop/arqit-sidecar-guard.smoke.ts`](./tests/interop/arqit-sidecar-guard.smoke.ts).

## Environment variables

Same variables as the Python side (`python/stls-core/env.examples`), where applicable:

```bash
# Session cache: "memory" (default) | "redis"
SESSION_CACHE_BACKEND=memory
SESSION_CACHE_REDIS_URL=redis://localhost:6379
SESSION_CACHE_REDIS_NAMESPACE=stls:sc
SESSION_CACHE_TTL=3600
STLS_SESSION_CACHE_MAX=256

# Auth
AUTH_LOADER_BACKEND=static        # "static" | "file" | "postgres"
AUTH_KEYS_FILE=api_keys.yaml
AUTH_KEY_TTL=3600
AUTH_REFRESH_INTERVAL=300
AUTH_KM_USE_REDIS=true
AUTH_REFRESH_DISTRIBUTED=true
AUTH_REDIS_NS=stls
REDIS_URL=redis://localhost:6379
AUTH_PG_DSN=postgresql://user:pass@host/db   # PostgresLoader — if set, run-interop-tests.sh also runs the live Postgres tests
SF_API_KEYS_TABLE=sf_api_keys

# Arqit sidecar (Node only)
ARQIT_SIDECAR_SOCKET=/tmp/stls-arqit-sidecar.sock             # single-process smoke tests (base provider, guard check)
ARQIT_SIDECAR_SOCKET_SERVER=/tmp/arqit-server.sock            # real peering — SERVER role's own sidecar process
ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-client.sock            # real peering — CLIENT role's own sidecar process (must differ from SERVER's)
ARQIT_N_CLIENTS=1                 # arqit-sidecar-real.live.ts: how many sequential clients to run against the one SERVER session
KEY_PROVIDER_NAME=base            # also read by the Python sidecar: "base" | "arqit_py"
```

## Testing

### Automated CI

[`.github/workflows/stls-core-node-interop.yml`](../../.github/workflows/stls-core-node-interop.yml)
runs the full interoperability test suite on every push/PR that touches
`node/stls-core/**` or `python/stls-core/**`: it builds liboqs from
source, installs both packages, starts Redis and a disposable Postgres
as service containers, and runs `tests/run-interop-tests.sh` (which
auto-detects `AUTH_PG_DSN` and runs the live Postgres tests against
that throwaway database).

### One command, locally

```bash
cd node/stls-core
START_LOCAL_REDIS=1 ./tests/run-interop-tests.sh
```

Runs in sequence: `PostgresLoader` error paths, bidirectional crypto
interop, PQC interop, Redis session cache interop, the auth module
(`ApiKeyManager` against a real Redis), the `BackgroundRefresher`
distributed lock (real Redis, a scheduled refresh that actually
elapses), the live Postgres tests (only if `AUTH_PG_DSN` is set), the
Arqit sidecar RPC mechanics + role guard (`provider_name=base`, no real
credentials needed), and real Arqit peering through two separate
sidecar processes (only if `ARQIT_PY_CLIENT_CONFIG`/
`ARQIT_PY_SERVER_CONFIG` point at a real, already-provisioned device
pair) — starting and stopping a local `redis-server` and the Python
sidecar process(es) on its own. Useful variables: `SKIP_PQC=1` and
`SKIP_ARQIT_SIDECAR=1` to skip the parts that require `liboqs` or the
sidecar; `TEST_REDIS_URL` to point at a Redis that is already running
instead of starting one (in that case `START_LOCAL_REDIS=0`, the
default); `AUTH_PG_DSN` to also exercise `PostgresLoader` against a
real database (auto-detected, no flag needed);
`ARQIT_PY_CLIENT_CONFIG`/`ARQIT_PY_SERVER_CONFIG` to also exercise real
Arqit peering (auto-detected too — see the
[Usage](#6-the-arqit-provider-via-the-sidecar) section for the cwd
caveat about relative paths inside those device JSON configs). Still
requires `python/stls-core/.venv` with `pip install -e ".[dev,pqc]"` —
see [python/stls-core/README.md](../../python/stls-core/README.md) —
and `npm install && npm run build` already run in this directory.

### Node-only functional tests

```bash
npx tsx tests/auth.smoke.ts
npx tsx tests/postgres-loader.smoke.ts        # error paths only, no real DB

# requires a reachable Redis (TEST_REDIS_URL, default :6399) — exercises
# RedisKeyCache.tryAcquireLock()/releaseLock() and a real scheduled refresh:
npx tsx tests/background-refresher.live.ts

# with a real AUTH_PG_DSN (creates its own table/fixture row, cleans up after itself):
AUTH_PG_DSN=postgresql://user:pass@host/db npx tsx tests/postgres-loader.live.ts
AUTH_PG_DSN=postgresql://user:pass@host/db TEST_REDIS_URL=redis://localhost:6399 \
  npx tsx tests/apikeymanager-postgres.live.ts
```

### Running the individual steps manually

Useful for isolating a failure. Each Python script in the pairs below
coordinates with the Node script of the same name through a transient
JSON file in `tests/interop/` (already in `.gitignore`):

```bash
# Crypto: Node decrypts a Python envelope and vice versa
npx tsx tests/interop/crypto.smoke.ts
../../python/stls-core/.venv/bin/python tests/interop/python_crypto_verify_node_envelope.py

# PQC: Node and Python derive the same hybrid X25519+ML-KEM-768 session_key
npx tsx tests/interop/pqc-step1-node-keygen.ts
../../python/stls-core/.venv/bin/python tests/interop/python_pqc_server_step.py
npx tsx tests/interop/pqc-step2-node-verify.ts

# PQC handshake orchestration: the actual performPqcHandshake()/perform_pqc_handshake()
# wire flow (request/response, HMAC proof, real encrypt/decrypt), not just the raw
# KEM/DH primitives above. Python stays alive as the "server" and waits for Node's
# envelope — run it in the background and give it a moment before step 3.
npx tsx tests/interop/pqc-handshake-step1-node-request.ts
../../python/stls-core/.venv/bin/python tests/interop/python_pqc_handshake_step2_server.py &
npx tsx tests/interop/pqc-handshake-step3-node-verify.ts
wait

# Redis session cache: requires a reachable redis-server (TEST_REDIS_URL, default :6399)
redis-server --port 6399 --daemonize yes --save ""
npx tsx tests/interop/redis-step1-node-put.ts
../../python/stls-core/.venv/bin/python tests/interop/python_redis_step2_read_and_put.py
npx tsx tests/interop/redis-step3-node-get.ts
redis-cli -p 6399 shutdown nosave

# Arqit sidecar — RPC mechanics (provider_name=base) + the role guard,
# no real Arqit credentials needed for either
python/stls-core/.venv/bin/python python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/stls-arqit-sidecar-test.sock &
ARQIT_SIDECAR_SOCKET=/tmp/stls-arqit-sidecar-test.sock npx tsx tests/interop/arqit-sidecar.smoke.ts
ARQIT_SIDECAR_SOCKET=/tmp/stls-arqit-sidecar-test.sock npx tsx tests/interop/arqit-sidecar-guard.smoke.ts

# Arqit real peering — requires a real, already-provisioned device pair
# and TWO separate sidecar processes (see the Usage section above for why)
python/stls-core/.venv/bin/python python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/arqit-server.sock &
python/stls-core/.venv/bin/python python/stls-core/sidecar/arqit_sidecar.py --socket /tmp/arqit-client.sock &
ARQIT_SIDECAR_SOCKET_SERVER=/tmp/arqit-server.sock \
ARQIT_SIDECAR_SOCKET_CLIENT=/tmp/arqit-client.sock \
ARQIT_N_CLIENTS=3 \
  npx tsx tests/interop/arqit-sidecar-real.live.ts
```

## Critical cryptographic compatibility notes

### HKDF-Expand vs. full HKDF — the trickiest part of this port

`StlsTransport` (in `crypto.ts`) derives the AES and HMAC keys from the
session key via **pure HKDF-Expand** (RFC 5869 §2.3, expand only, **no
extract**) — identical to
`cryptography.hazmat.primitives.kdf.hkdf.HKDFExpand` used on the Python
side, where the session key is passed in directly as the PRK.

`node:crypto` **has no built-in equivalent**: `crypto.hkdfSync()`
implements the **full** HKDF (extract-then-expand) defined by RFC 5869,
and with the same input it produces a **different** derived key than
`HKDFExpand`. Using it here would have silently broken compatibility
with every envelope produced by a Python peer — no error, no exception,
just a MAC/GCM tag that no longer verifies (and it could initially have
looked like it "worked" in Node-only tests, because Node would have been
internally consistent with itself).

The `hkdfExpand()` function in `crypto.ts` therefore manually
reimplements just the expand step (RFC 5869 §2.3): for L=32=HashLen(SHA-256)
it reduces to a single `HMAC-SHA256(PRK, info ‖ 0x01)`.

This was verified with a real vector generated by the Python
implementation (`stls-core` with `os.urandom` pinned), not just by code
inspection: Node decrypts the envelope produced by Python byte-for-byte,
and vice versa (see `tests/interop/crypto.smoke.ts`).

**Where Node's native `hkdfSync` IS the right choice instead**: the
hybrid PQC derivation in `providers/pqc.ts::deriveHybridKey()` mirrors
`cryptography.hazmat.primitives.kdf.hkdf.HKDF` (extract+expand) on the
Python side — there, the nonce acts as the salt in the extract step.
Using the same transport-level `hkdfExpand()` at that point would have
been the opposite mistake. The two functions derive keys for different
purposes and are **not interchangeable** — see `CHANGELOG.md` for details.

### SessionCache is async in Node, sync in Python

The Redis client used on the Python side (`redis-py`) is
synchronous/blocking, so `SessionCacheBase` can have sync methods even
for the Redis backend. There is no mature synchronous Redis client for
Node — every backend is Promise-based. To have a single `SessionCache`
interface valid for both `InMemorySessionCache` and `RedisSessionCache`,
**all methods are async** in Node, including `InMemorySessionCache`
(where the implementation stays synchronous internally and the Promise
resolves immediately).

### PQC provider: different libraries, same standard

ML-KEM-768 uses `@noble/post-quantum` on the Node side against
`liboqs-python` on the Python side — different implementations of the
same NIST FIPS 203 standard, not internally bit-identical but
interoperable at the protocol level (pubkey/ciphertext/shared-secret).
Verified with a real vector: a keypair generated by Node, encapsulation
performed by liboqs-python, decapsulation back in Node — the same final
session_key on both sides (see `tests/interop/pqc-step2-node-verify.ts`).

This covers the raw KEM/DH primitives; the actual handshake orchestration
(`generatePqcHandshakeRequest()` → `performPqcHandshake()` →
`deriveSessionKeyClient()` → `verifyPqcHandshakeProof()`, i.e. the real
wire-format request/response objects and the HMAC proof, not just the
math) is verified separately and bidirectionally in
`tests/interop/pqc-handshake-step*.ts` +
`python_pqc_handshake_step2_server.py`: Node as the STLS client, Python
as the STLS server with a real `InMemorySessionCache`, with an actual
encrypted message decrypted correctly on both sides over the session
the handshake established.

### `validateRequest()` requires an adapter, not a concrete Request type

The Python side accepts a Starlette/FastAPI `Request` directly. Node has
no single canonical HTTP framework (Express, Fastify, native `http` all
have different shapes) — the `RequestLike` adapter (`header()`,
`queryParam()`, `path`, `remoteAddr`) is the deliberate choice to stay
framework-agnostic. Any framework can implement it in a few lines
(Express example in the Usage section).

### Postgres loader: verified against a real database

`PostgresLoader` (`auth/loaders-postgres.ts`) uses the optional `pg`
package and the same table schema as the Python side (`sf_api_keys`,
see `tests/postgres-schema.sql` or `stls_core_auth_loaders.py` for the
full SQL schema). Initially this could not be verified in the
development sandbox (no Postgres server, no root/Docker access), so it
shipped with only the "soft-fail" error path tested. It was later
verified against a real PostgreSQL instance (`tests/postgres-loader.live.ts`,
`tests/apikeymanager-postgres.live.ts`): `loadAll()`/`loadOne()` against
a real row, `TEXT[]`/`JSONB`/`TIMESTAMPTZ` conversion, and the full
`PostgresLoader -> RedisKeyCache -> ApiKeyManager` chain, all against
live data. These live tests manage their own fixture row (insert at the
start, delete in a `finally`) and run automatically whenever
`AUTH_PG_DSN` is set — including in CI, against a disposable Postgres
service container (see `.github/workflows/stls-core-node-interop.yml`).

### BackgroundRefresher's distributed lock: verified against real Redis

`auth.smoke.ts` and the live Postgres tests exercise `ApiKeyManager.start()`/
`stop()`, but their `BackgroundRefresher` refresh interval (default 300s)
never actually elapsed during those short-lived tests — so
`RedisKeyCache.tryAcquireLock()`/`releaseLock()`, and the scheduled
`doRefresh()` that calls them, had never really run.
`tests/background-refresher.live.ts` closes that gap: it tests the lock
primitives directly with two distinct simulated node ids (mutual
exclusion on `SET NX`, and that `releaseLock()` from a non-holder is a
no-op — the Lua check-and-delete script), then runs a real
`BackgroundRefresher` with a 1-second interval against a fake loader and
confirms the scheduled refresh actually calls `loadAll()`, writes the
record to Redis via `bulkReload()`, and releases the lock afterwards.

### Arqit provider: verified against real peering (and a same-process pitfall fixed along the way)

Initially only the RPC protocol itself (newline-delimited JSON over a
Unix domain socket) was verified, with `provider_name=base` — real
`arqit_py` peering needed a real Arqit deployment, not available at the
time. It later was: `tests/interop/arqit-sidecar-real.live.ts` drives a
real bilocation key exchange, a real STLS handshake, and a real
AES-256-GCM round trip through the sidecar, both for one client and for
3 sequential clients against one long-running SERVER session.

Getting there surfaced a real pitfall, not a Node-specific one: a
SERVER and a CLIENT session for the same Arqit device pair must live in
two separate OS processes — one sidecar process serving both (as the
original `provider_name=base`-only smoke test structurally allowed)
makes the real bilocation exchange fail with a genuine `401` from
Arqit's own backend. Full investigation in
[`python/stls-core/sidecar/ARQIT_PEERING_NOTES.md`](../../python/stls-core/sidecar/ARQIT_PEERING_NOTES.md).
The sidecar now guards against it directly — a second, different role
on the same process is rejected immediately — verified with no real
credentials needed in `tests/interop/arqit-sidecar-guard.smoke.ts`. See
the [Usage](#6-the-arqit-provider-via-the-sidecar) section for the
correct two-processes pattern.

## Keeping in sync with the Python version

The version number in `package.json` follows
`python/stls-core/pyproject.toml` (currently `0.1.1`). Changes to the
wire format or the cryptography on the Python side must be mirrored
here — the cross-language interoperability tests in `tests/interop/`,
automated in CI (see above), are the main verification mechanism. Not
yet implemented: shared KAT vectors generated from a single source (so
a cryptographic change fails before a live test even runs), and
generating the shared constants (`ENVELOPE_*`, the `X-STLS-*` headers)
from a neutral spec instead of duplicating them by hand in
`constants.py` and `constants.ts`.

## License

Dual-licensed under the [GNU AGPLv3](LICENSE) and a
[commercial license](LICENSE-COMMERCIAL.md), consistent with
`python/stls-core`, `stls-a2a` and `stls-mcp`.
