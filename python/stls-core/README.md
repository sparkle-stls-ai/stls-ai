# stls-core

Protocol-agnostic cryptographic layer for the STLS ecosystem (Symmetric
Transport Layer Security).

stls-core implements symmetric cryptography, session management, the
proof-of-key-possession handshake, quantum-safe key providers (Arqit
SKA-Platform™ and software-based PQC), and API key authentication — with
no dependency on any specific application protocol. It is the common
foundation shared by stls-a2a (Agent-to-Agent protocol extension) and
stls-mcp (Model Context Protocol extension).

## Why it exists

stls-a2a and stls-mcp encrypt different payloads (A2A JSON-RPC vs. MCP
JSON-RPC) but use identical cryptography, identical session management,
and identical authentication schema. stls-core extracts this common
logic into a package independent of FastAPI or any application
framework — the crypto/session/auth layers work only on bytes and
JSON-serializable dicts. The one exception is the client-side STLS
handshake (`stls_core_client_handshake.py`, behind
`establish_session()`): since issue #5 ("Restrict stls-core's public
API surface") moved it here from stls-mcp, `httpx` is a real runtime
dependency of this package now — needed to actually perform that
handshake's HTTP POST, not just for tests.

## Features

- **Symmetric cryptography**: AES-256-GCM for confidentiality/authentication,
  an additional HMAC-SHA256 over the whole envelope, HKDF-Expand to derive
  distinct AES and HMAC keys from the session key (domain separation).
- **Proof-of-possession handshake**: the session key never travels over
  the wire. Client and server mutually prove they hold the same key via
  an HMAC challenge on a random nonce, with no cryptographic material
  ever transmitted. Dedicated endpoints `/stls/handshake` (standard) and
  `/stls/pqc/handshake` (PQC).
- **LRU+TTL session cache**: in-memory backend (default) or Redis-backed
  for multi-instance deployments. Configurable via `SESSION_CACHE_BACKEND`.
- **Interchangeable key providers**: three options with a common
  interface — `base` (fixed key, for development/test/CI), `arqit_py`
  (Arqit SKA-Platform™ via the native Python SDK, quantum-safe key
  exchange via QKD), and `pqc` (hybrid X25519 + ML-KEM-768, software-based
  quantum-safe, redistributable, no dependency on external services).
- **`establish_session()`/`resolve_transport()`** (`stls_core_session_establish.py`):
  the public, non-leaking way to get a ready `StlsTransport` for an
  arbitrary caller-defined `base_url`/`target_key` — `establish_session()`
  resolves the key AND performs the STLS handshake in one call (used by
  stls-mcp, whose wire protocol always does a handshake round trip);
  `resolve_transport()` resolves + builds the transport with NO
  handshake (used by stls-a2a's base/arqit_py path, which has none). The
  raw key never leaves either function. Both share one internal
  per-`target_key` async lock plus a per-call `config_path` override for
  `arqit_py` — lets one process hold distinct `arqit_py` device
  identities for different remote counterparts instead of being limited
  to a single device/config per process. (`KeySessionResolver` itself is
  the internal implementation detail behind both — not part of the
  public API since issue #5, "Restrict stls-core's public API surface".)
- **API key authentication**: two composable levels. `AuthConfig`
  (static, in-memory, timing-safe) for quick tests and small deployments
  with no infrastructure — shared by stls-a2a and stls-mcp, not
  duplicated. `ApiKeyManager` (enterprise) with `KeyRecord`,
  interchangeable loaders (static/file/postgres), an optional Redis
  cache with background refresh and a revocation set — takes priority
  over `AuthConfig` when both are configured.

## Installation

```bash
cd python/stls-core
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Available extras

| Extra | Contents | When to install it |
|---|---|---|
| (base) | `cryptography`, `pydantic`, `python-dotenv`, `httpx` | always — the minimum needed to use the package (`httpx` since issue #5, for the client-side handshake) |
| `redis` | `redis>=7.4.0` | if `SESSION_CACHE_BACKEND=redis` |
| `postgres` | `asyncpg>=0.31.0` | if `AUTH_LOADER_BACKEND=postgres` |
| `arqit_py` | Arqit SKA-Platform™ wheels (proprietary, not redistributable) | only if `KEY_PROVIDER_NAME=arqit_py` |
| `pqc` | `liboqs-python` | only if `KEY_PROVIDER_NAME=pqc` |
| `dev` | `pytest`, `pytest-asyncio`, `pytest-cov`, `fastapi`, `uvicorn`, `fakeredis` | development and testing (`httpx` moved to the base dependencies) |

```bash
# Crypto/auth only, no Redis or Arqit dependency
uv pip install -e "."

# With Redis cache
uv pip install -e ".[redis]"

# With ApiKeyManager Postgres backend
uv pip install -e ".[postgres]"

# With the PQC provider (X25519 + ML-KEM-768, requires liboqs-python)
uv pip install -e ".[pqc]"

# With Arqit SKA-Platform (proprietary wheels, not on PyPI)
uv pip install -e ".[arqit_py]" \
  --find-links arqit_wheels/arqit-skaplatform-common/ \
  --find-links arqit_wheels/arqit-skaplatform-policy/ \
  --find-links arqit_wheels/arqit-skaplatform-registration/ \
  --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
  --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
  --find-links arqit_wheels/arqit-skaplatform-peering/ \
  --find-links arqit_wheels/arqit-skaplatform-monitoring/
```

The Arqit SKA-Platform™ wheels are proprietary and not redistributable.
They must be obtained directly from Arqit and are not included in this
repository (excluded via `.gitignore`).

**liboqs version alignment**: the `liboqs-python` version (the Python
wrapper) must match the `liboqs` C library installed on the system. A
mismatch causes an `AttributeError` at import time. PyPI only hosts the
most recent versions; if the system has an older C library, build
`liboqs` from source at the matching tag with `BUILD_SHARED_LIBS=ON`.
Verify with:

```bash
python -c "import oqs; kem = oqs.KeyEncapsulation('ML-KEM-768'); print('OK')"
```

**Arqit `arqit_py` — SERVER and CLIENT must run in separate OS
processes.** This is a hard requirement, not a style preference: running
both roles of the same device pair inside one Python process makes the
real bilocation key exchange fail with `401 NotAuthorizedException` from
Arqit's own backend (confirmed with a controlled A/B — identical config,
identical device pair, only variable being same-process vs
two-processes; full write-up in
[`sidecar/ARQIT_PEERING_NOTES.md`](./sidecar/ARQIT_PEERING_NOTES.md)).
It doesn't matter whether that single process is your own code calling
`KeyProviderClientArqitPy`/`KeyProviderServerArqitPy` directly, or a
sidecar process serving both an RPC "SERVER" and "CLIENT" session (as
`sidecar/arqit_sidecar.py` currently can, for provider-agnostic sessions —
do not point that configuration at `arqit_py` with both roles active
until its process model is adjusted for this constraint).

The correct pattern is simply: two independent `python` processes, one
per role — exactly what `stls-mcp`'s `tests/vault_server.py` +
`tests/vault_client.py` already do. The runnable, executable
documentation of this constraint lives in
[`tests/test_arqit_py_real_peering.py`](./tests/test_arqit_py_real_peering.py),
which covers three scenarios against the real Arqit cloud:

- the correct pattern (SERVER + CLIENT as two real subprocesses) —
  and not just "the key exchange succeeds": it runs the full round trip
  with real `stls_core` primitives — `perform_handshake()` /
  `verify_handshake_proof()` (proving the two independently-obtained
  keys are byte-identical, not just "both present") and a real
  `StlsTransport` AES-256-GCM encrypt/decrypt round trip;
- the same full round trip repeated against **one long-running SERVER
  serving multiple sequential CLIENTs** — the real production shape
  (a long-lived service, many separate handshakes over its lifetime),
  asserting every `kid`/`session_id` comes out distinct;
- the anti-pattern (both roles in the test's own process), which must
  fail.

Like other `arqit_py` E2E tests in this project, it requires a real,
already provisioned Arqit device pair (`ARQIT_PY_CLIENT_CONFIG`/
`ARQIT_PY_SERVER_CONFIG` pointing at existing config files) and skips
automatically otherwise — it does not run in CI.

**`KEY_SESSION_PORT`/`KEY_SESSION_HOST` are ignored by `arqit_py`.**
`KeyProviderArqitPy.create_provider_client(host, port, options)` and
`create_provider_server(port, options)` accept these arguments for API
uniformity with the other providers (`base`, `pqc`) but never read them —
see `providers/stls_core_providers_arqit_py.py`. The real peer
address/port always comes from the JSON pointed at by
`ARQIT_PY_CLIENT_CONFIG`/`ARQIT_PY_SERVER_CONFIG`
(`SOCKET_RECEIVER_IP_ADDRESS`/`PORT` on the client side,
`SOCKET_LISTENING_PORT` on the server side). `ARQIT_PY_TRANSPORT`
(`"socket"` default | `"mqtt"`) is symmetric — read the same way on both
sides via `_get_transport()` — and must agree between client and server
and with the matching `SOCKET_*`/`MQTT_*` fields in their configs.

One `ARQIT_PY_SERVER_CONFIG` is enough for a server that peers with many
different clients (one device identity, many incoming peerings). The
client side used to be the opposite by construction: `ARQIT_PY_CLIENT_CONFIG`
is one device/peer config per process, with no way to override it per
call. `create_provider_client()`/`create_provider_server()` now also
accept `options["config_path"]`, taking priority over the env var — a
caller holding several device configs (e.g. `stls-a2a`'s
`SecureSessionManager(arqit_client_configs={...})`) can pass a different
one per call instead of being limited to the single process-wide env var.
Validated end-to-end against the real Arqit SDK for N *different*
client identities in one process, both sequentially and under genuine
concurrency (`asyncio.gather`/real OS threads) — see the `arqit_py`
section of
[`stls-a2a/README.md`](../stls-a2a/README.md#current-limitations-and-notes)
for the full write-up, including the process-wide serialization lock
this required inside the provider itself to make concurrent use safe.

## Configuration

Copy `.env.example` to `.env` and adjust the values. Main variables:

```bash
# Key provider: "base" (dev/test) | "arqit_py" (QKD, production) | "pqc" (software quantum-safe)
KEY_PROVIDER_NAME=base

# Session cache: "memory" (default, single instance) | "redis" (multi-instance)
SESSION_CACHE_BACKEND=memory

# API key authentication
AUTH_LOADER_BACKEND=static      # "static" | "file" | "postgres"

# Session limits
STLS_SESSION_CACHE_MAX=256      # max SessionCache size (memory backend only)
STLS_SESSION_TTL=3600           # session idle TTL (seconds)
```

See `.env.example` for the full list, organized into 6 sections (Key
Provider, Arqit, Session Cache, Auth, Redis, PostgreSQL).

## License

Dual-licensed under the [GNU AGPLv3](LICENSE) and a
[commercial license](LICENSE-COMMERCIAL.md), consistent with `stls-a2a`
and `stls-mcp`. The Arqit SKA-Platform™ wheels remain subject to the
proprietary Arqit license.
