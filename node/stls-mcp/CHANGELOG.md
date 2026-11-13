# Changelog

All notable changes to **stls-mcp (Node.js)** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.5] — 2026-11-13

Adds client-side retry-on-expiry (issue #6, second increment), mirroring
[`python/stls-mcp` 0.1.5](../../python/stls-mcp/CHANGELOG.md). The first
increment (server-side absolute max-age) needed no change here — it's
shared transparently via `stls-core` 0.1.7's `SessionCacheFactory`.
Additive — every existing caller of `createStlsFetch()` without the new
`rehandshake` option keeps today's exact behavior.

### Added

- **`createStlsFetch(..., { rehandshake })`** — optional zero-argument
  async callback that performs a brand new handshake and returns
  `{sessionId, transport}`. On a `401` response whose body carries the
  new `ERR_SESSION_EXPIRED` code and no prior retry has happened yet,
  the returned `fetch` calls it, swaps its internal session state for
  the lifetime of that closure, and retries the original request
  exactly once — same retry-once, fail-fast contract as the Python
  side (a rehandshake failure, or the retried request also coming back
  expired, propagates/returns immediately, no loop).
- **`ERR_SESSION_EXPIRED = -32004`** (`constants.ts`, same numeric value
  and meaning as the Python side's constant). `encryptedHandler` now
  raises it explicitly (still `401`) when the session cache has no
  entry for the given session_id, instead of the generic `-32600`
  every other `4xx` `StlsMcpError` still uses. `StlsMcpError` gained an
  optional `code` constructor parameter to carry this through.
- **`looksLikeSessionExpired()`** (`transport.ts`, exported) —
  distinguishes the above from an unrelated `401` (e.g. an auth-layer
  rejection), so only the former triggers the automatic retry.
- **`tests/transport-retry.smoke.ts`** (`npm run test:transport-retry-smoke`)
  — calls `encryptedHandler`'s returned function directly as
  `createStlsFetch`'s `fetch` option (no real HTTP server/port needed,
  same "just a function call" architecture `middleware.ts` already
  uses), proving the client and server sides actually agree on the
  wire format, not a hand-crafted mock of it.

### Note

- `node/stls-mcp` has no `SecureToolsetManager`/`create_stls_mcp_toolset()`
  equivalent (no ADK-toolset convenience layer exists on this side) —
  `rehandshake` must be passed explicitly by whoever calls
  `createStlsFetch()` directly (see `tests/vault-client.ts` for the
  pattern). `node/stls-a2a` still doesn't exist as a package.

## [0.1.4] — 2026-11-12

Tracks [`stls-core`](../../node/stls-core/CHANGELOG.md) 0.1.5 (issue #5,
"Restrict stls-core's public API surface"). No change to this package's
own public API.

### Changed

- `transport.ts`'s `performClientHandshake()`/`performPqcClientHandshake()`/
  `StlsHandshakeFailed` moved to `stls-core` (generic STLS-protocol
  handshake, no MCP-specific knowledge) and are now re-exported here
  unchanged — same names, same signatures, same behavior.

---

## [0.1.3] — 2026-11-10

Packaging-only release: no functional changes. Version bump to align
with [`python/stls-mcp`](../../python/stls-mcp/CHANGELOG.md) 0.1.3 and
to enable the first publish to npm.

---

## [0.1.2] — 2026-10-19

### Fixed (follow-up, 2026-10-23 — concurrent `arqit_py` sessions)

Concurrent `arqit_py` MCP sessions against one long-running server
could get cross-wired (a client's key ending up checked against a
*different* session's handshake) once `node/stls-core`'s
`_ARQIT_PY_START_LOCK`/`hintKid` fixes stopped them from deadlocking
first (see `node/stls-core/CHANGELOG.md`). Fixed by threading a `kid`
hint through `performClientHandshake()`:

- `transport.ts`: `PerformClientHandshakeOptions` gained an optional
  `kid?: string`, threaded into `generateHandshakeRequest()`.
- `samples/client/12_client_mcp_ms/client_multi_session.ts` and
  `client_multi_server.ts`, plus `tests/vault-client.ts` (for
  consistency): all now pass their already-resolved `kid` through
  instead of discarding it.

Confirmed fixed: re-ran `client_multi_session.ts` with genuinely
concurrent sessions — 3/3 then 6/6 clean passes, 0 errors.

### Fixed

- **`encryptedHandler()` double-encrypted HTTP-level error responses** —
  when the inner MCP SDK handler returned a raw HTTP error (400/406/415,
  etc.) not raised as `StlsMcpError`, the middleware still encrypted it
  before returning it on `/mcp-stls`. The client (`transport.ts`)
  already assumed any status ≥ 400 arrives in plaintext and skipped
  decryption, so the caller received an undecryptable ciphertext
  envelope instead of the real JSON-RPC error. Fixed by returning the
  inner response as-is, in plaintext, whenever its status is ≥ 400 —
  matching the assumption the client already encoded.
- **`STLS_MCP_NODE_VERSION`** constant was stale at `"0.1.0"`; now tracks
  the package version.

### Security

- Root `.gitignore` now excludes `aq-*.json` (Arqit device/identity files,
  containing realm credentials and MQTT broker secrets in plaintext) and
  `arqit_utility/` (proprietary Arqit SKA-Platform example code) so they
  are never committed alongside the package.

### Changed

- `wrapWithAuth()`'s `ApiKeyManager` path now logs auth denials
  (`console.warn`) and backend exceptions (`console.error`) instead of
  failing silently — matching the logging already present on the Python
  side (`stls_mcp_auth.py`).

### Added

- **`.env.example`** — this package had none; added, documenting every
  environment variable actually read by `src/` and `tests/` (MCP server
  host/port/paths, Key Provider, Arqit sidecar, Session Cache, Auth,
  Redis/PostgreSQL, test-script target URLs).

### Fixed (found running the suite, 2026-10-19)

- **`tests/run-interop-tests.sh` defaulted `PYTHON_VENV` to
  `python/stls-mcp/.venv/bin/python`**, a venv the script's own CI
  (`.github/workflows/stls-mcp-node-interop.yml`) never creates — CI
  always installs both `stls_core` and `stls_mcp` into the single shared
  `python/stls-core/.venv` and passes that path explicitly. Running the
  script locally with no `PYTHON_VENV` override (exactly as its own
  "Usage" section documents) silently used whatever stale
  `python/stls-mcp/.venv` happened to exist on disk instead — in this
  environment, an old editable install left over from before this
  project was migrated from a differently-named checkout, pointing
  `import stls_mcp` at a **different, unrelated repository** entirely
  (same package name, stale `0.1.1` source, no error raised). Fixed the
  default to `python/stls-core/.venv/bin/python`, matching CI, and
  updated the header comment accordingly. Re-ran the full suite relying
  only on the new default (no `PYTHON_VENV` set): all 6 applicable steps
  passed cleanly end-to-end (step 7, real Arqit peering, auto-skipped —
  no credentials in this environment, same as CI).
- **Step `[7/7]` (real Arqit peering) could never actually pass once
  `stls_mcp` is installed in the same venv as the `arqit_py` wheels** —
  the Arqit SDK wheels declare `jwt==1.3.1` (`python-jwt`, providing
  `jwt.JWT`), while `mcp` (a `stls_mcp` dependency) pulls in `PyJWT`.
  Both packages install to the same top-level `jwt/` import path in
  `site-packages`, so whichever is installed last silently overwrites
  the other's files; with `stls_mcp` installed after `stls_core[arqit_py]`
  (as this suite's own setup does), the two `arqit_sidecar.py` processes
  in step 7 failed with `ImportError: cannot import name 'JWT' from
  'jwt'` — a dependency conflict masquerading as a peering failure,
  with the real Arqit cloud never even reached. Fixed by giving the
  sidecar processes their own interpreter, `python/stls-core/.venv-arqit-sidecar`
  (`stls_core[dev,arqit_py]` only, never `mcp`/`stls_mcp`/`PyJWT`), via
  the new `ARQIT_SIDECAR_PYTHON` variable (documented in the script's
  header, with the exact one-time setup commands). Verified against the
  real Arqit SKA-Platform™ cloud with this repo's own provisioned device
  pair: both devices authenticated, bilocation + P2P key agreement
  succeeded, full MCP session (discovery + all 5 tools + the resource +
  both prompts) over the real `arqit_py`-derived STLS session — all 7
  steps passing end-to-end in one run, not just 6.

## [0.1.0] — 2026-10-18

First release: a TypeScript port of `python/stls-mcp`, built on
`node/stls-core` (already ported and CI-verified). Secures Node MCP
servers/clients while interoperating byte-for-byte, and protocol-for-
protocol, with existing Python `stls-mcp` servers/clients.

### Investigated first: what does the TypeScript MCP SDK actually offer?

Before writing any code, installed `@modelcontextprotocol/sdk` (v1.32.1)
in an isolated sandbox and read its `.d.ts` files directly, rather than
assuming the Python architecture would transfer as-is:

- **Server**: `WebStandardStreamableHTTPServerTransport.handleRequest(req: Request): Promise<Response>`
  is pure Web Standard — no ASGI scope to synthesize. The SDK's own
  stateless example creates a fresh `McpServer` + transport per request;
  there is no supported way to `connect()` one `McpServer` to many
  transports sequentially (confirmed by reading both the stateless and
  the stateful official examples — the stateful one also calls a
  `getServer()` factory fresh per logical session, just keeps the result
  around for that session's lifetime instead of per-request).
- **Client**: `StreamableHTTPClientTransport` accepts `fetch?: FetchLike`
  (`(url, init?) => Promise<Response>`) in its constructor — the exact
  Node equivalent of Python's `httpx_client_factory` extension point.
- **No Node equivalent of Google ADK** exists, so
  `stls_mcp_secure_toolset.py` (288 lines, ADK-specific) has no Node
  port — LangChain.js/Vercel AI SDK already support a custom `fetch`
  natively.

These findings drove two deliberate architectural decisions, not literal
1:1 ports:

1. `createSecureMcpApp()` returns a Web-standard
   `(Request) => Promise<Response>` handler (portable to Deno/Bun/
   Cloudflare Workers), with `toNodeRequestListener()` as a thin
   `node:http` convenience wrapper — mirroring the SDK's own split
   between `WebStandardStreamableHTTPServerTransport` and
   `StreamableHTTPServerTransport`. Confirmed with the user before
   implementation (the alternatives considered — a required Express app,
   or a Node-http-only listener with no portable core — were rejected in
   favor of this one).
2. Both the plain and STLS paths call ONE shared `innerHandle` function
   directly, never through a router. This makes `_ExactMountPathNormalizer`
   and ASGI-scope path-rewriting (needed in Python to work around a
   Starlette `Mount` quirk) entirely unnecessary here — there is no
   second dispatch step that could ever send a decrypted request to the
   wrong place.

### Added

- **`constants.ts`** — direct port of `stls_mcp_constants.py`: `MCP_PLAIN_PATH`,
  `MCP_STLS_PATH`, MCP header names, STLS extension discovery keys.
- **`server.ts`** — `createSecureMcpApp(getServer, keySessionProvider, options)`:
  assembles the full handler (plain + STLS + handshake + optional PQC
  handshake), dispatching on a plain pathname comparison (`normalizePath()`
  strips a trailing slash) — no framework router involved. Also
  `stlsCapabilitiesFragment()`, a direct port of the Python function.
- **`middleware.ts`** — `encryptedHandler(innerHandle, sessionCache, options)`:
  decrypts the inbound body, builds a synthetic `Request` pointing at the
  logical plain path (cosmetic only — `innerHandle` doesn't route on
  path), calls `innerHandle` directly, encrypts the response. `StlsMcpError`
  for status-coded application errors, same pattern as Python's.
- **`auth.ts`** — `wrapWithAuth(handler, { auth, apiKeyManager })`: a
  higher-order function wrapping any `(Request) => Promise<Response>`
  handler, reusing `ApiKeyManager`/`AuthConfig`/`RequestLike` already
  ported and verified in `stls-core` (Node). `toRequestLike()` adapts a
  Web-standard `Request` to the `RequestLike` interface
  (`header()`/`queryParam()`/`path`) in a few lines — no new design
  needed, the interface was already built framework-agnostic.
- **`transport.ts`** — client side, direct port of `stls_mcp_transport.py`:
  - `performClientHandshake(baseUrl, sessionKey, { clientId })` — standard
    handshake (base/arqit_py), verifies the server's proof with
    `verifyHandshakeProof()` (stls-core) before trusting the session.
  - `performPqcClientHandshake(baseUrl, { clientId })` — PQC handshake,
    generates X25519 + ML-KEM-768 keypairs via `KeyProviderClientPQC`
    (stls-core), derives the session key via `deriveSessionKeyClient()`,
    zeroes private keys in a `finally` (best-effort, same as Python).
  - `createStlsFetch(sessionId, transport, options)` — the `FetchLike`
    passed to `StreamableHTTPClientTransport`. Ported as a plain function
    (not a class implementing an abstract transport interface like
    Python's `StlsHttpxTransport`), because the SDK's own extension
    point is itself a plain function.
- **`http-adapter.ts`** — `toNodeRequestListener(handler)`: converts the
  portable Web-standard handler into a `node:http` `RequestListener`.
- **`tests/vault-server.ts` / `tests/vault-client.ts`** — the same "Deep
  Sky Vault" themed example as Python (5 tools, 2 resources — one
  templated, 2 prompts), same names/behavior, for direct cross-language
  comparison. Select the provider via `KEY_PROVIDER_NAME` using
  `stls-core`'s `KeySessionProviderFactory.getSessionProvider()`.
- **`tests/interop/plain-endpoint.smoke.ts`** — connects to `/mcp` with
  the **official, unmodified** `StreamableHTTPClientTransport` + `Client`,
  zero stls-mcp code on the client side: `initialize()`, full tool/
  resource/prompt discovery, and exercising each — proving the plain
  path is indistinguishable from a standard MCP server, the same design
  claim Python validates against a real Claude Desktop connection.

### Added (follow-up, same day)

- **`tests/run-interop-tests.sh`** — automates the full manual sequence
  documented in "Validated" below into one re-runnable script, exactly like
  `node/stls-core`'s own script: `[1/7]` plain-endpoint check, `[2/7]`
  Node↔Node same-language, `[3/7]`–`[6/7]` cross-language in both
  directions for both `base` and `pqc` (`SKIP_PQC=1` to skip), `[7/7]` real
  `arqit_py` peering (auto-detected via `ARQIT_PY_CLIENT_CONFIG`/
  `ARQIT_PY_SERVER_CONFIG`, like `stls-core`'s `AUTH_PG_DSN` pattern) —
  starts two separate sidecar processes itself, one per role, matching the
  validated two-process pattern. Added after the user explicitly pointed
  out this automation was missing: the cross-language checks above had only
  ever been run by hand across multiple terminals, never released as a
  script, despite that being what the original approved plan called for.
  Full run (all 7 steps, `arqit_py` auto-skipped without credentials
  exported) passes clean end-to-end.

  **Real bug found and fixed while building it**: the first draft's
  `stop_server()` used `kill "$CURRENT_SERVER_PID"` against the PID
  captured via `$!` right after backgrounding `npx tsx tests/vault-server.ts
  &`. That only kills the immediate `npx` wrapper process, not the real
  `node` process several levels down the tree (`npx` → `tsx`'s loader →
  `node`) that's actually bound to the port — confirmed with
  `ss -ltnp | grep 8765` showing a zombie `node` process still listening
  after "cleanup", and `ps -p <pid> -o cmd` showing its full cmdline
  contains `tsx/dist/loader.mjs tests/vault-server.ts`, i.e. the literal
  substring `"tsx tests/vault-server.ts"` is NOT contiguous in it (there's
  the loader path in between), so even a `pkill -f` on that literal substring
  silently fails to match. This caused real, observed corruption: step 2
  crashed with `EADDRINUSE` on its own server-start attempt but the test
  still printed "successful"-looking handshake output, because the Node
  client was actually talking to the still-alive zombie server left over
  from step 1 (wrong provider configuration, right-looking output) — a
  genuinely dangerous failure mode, since it looks like a pass. Fixed by
  switching `stop_server()`/`cleanup()` to `pkill -f "tests/vault-server.ts"`
  / `pkill -f "tests/vault_server.py"` — the literal script path, which
  *is* a contiguous trailing argument in every process in the tree (the
  `npx` wrapper, the `tsx` loader, and the final `node`/`python` process
  alike) — plus a real `socket.bind()`-based `wait_port_free()` check
  (not a `connect()`-based one) before the next step starts, mirroring the
  same lesson already learned on the Python side in
  `python/stls-core/tests/test_arqit_py_real_peering.py`'s
  `_wait_port_free()`. Re-ran the full suite after the fix: clean, no
  `EADDRINUSE`, no cross-contamination between steps.

### Added (CI, same day)

- **`.github/workflows/stls-mcp-node-interop.yml`** — runs
  `tests/run-interop-tests.sh` on every push/PR touching `node/stls-mcp/**`,
  `python/stls-mcp/**`, `node/stls-core/**` or `python/stls-core/**`
  (the latter two because stls-mcp is built directly on top of them).
  Mirrors `stls-core-node-interop.yml`'s structure (same liboqs build step,
  same pinned `liboqs-python==0.16.0.1`), but needs no Redis/Postgres
  service containers — every Python invocation in the interop script runs
  with `SESSION_CACHE_BACKEND=memory`/`KEY_STORE_BACKEND=memory`/
  `AUTH_ENABLED=false`, unlike stls-core's own suite. Installs `stls_core`
  (Python) and `stls_mcp` (Python) into one shared venv with plain `pip`
  (not `uv`): `stls_mcp`'s `pyproject.toml` declares `stls_core>=0.1.1` as
  a dependency resolved from a local path only via `[tool.uv.sources]`
  (uv-specific); under plain pip that requirement is satisfied by whatever
  stls_core build is already installed in the venv instead of hitting PyPI
  for a package never published there. Validated by simulating the exact
  install sequence locally in a throwaway venv (reusing the liboqs C
  library already built on this machine instead of rebuilding it) and
  running the full interop script against it end-to-end — all 7 steps
  passed, including both PQC cross-language steps.

### Validated

All of the following were run for real in this environment, not assumed
from code review:

- **Plain endpoint**: the official unmodified MCP client against `/mcp`
  — `initialize()`, discovery of all 5 tools/2 resources/2 prompts, and
  calling `list_stations`/`calculate_orbit`, reading `vault://manifest`,
  getting `fleet_summary` — all correct on the first run.
- **STLS encrypted path, same language**: Node client ↔ Node server over
  `/mcp-stls` — handshake, proof verification, all 5 tools/2 resources/2
  prompts exercised, correct results, first run.
- **Cross-language, both directions, both providers** — the real
  compatibility proof:
  - Node server + Python client (`base`): full session succeeded.
  - Python server + Node client (`base`): full session succeeded.
  - Node server + Python client (`pqc`, real `liboqs-python` ↔
    `@noble/post-quantum` KEM exchange): full session succeeded.
  - Python server + Node client (`pqc`): full session succeeded.
  - All four runs: identical tool outputs, correct decrypted resource
    JSON, correct prompt text — the same wire format already verified
    byte-compatible at the `stls-core` crypto layer now proven correct
    through the full MCP protocol stack (initialize, discovery, tool/
    resource/prompt calls), not just raw envelope encrypt/decrypt.
- **`arqit_py`, against the real Arqit cloud**: `vault-server.ts` +
  `vault-client.ts`, two separate sidecar processes, real bilocation key
  agreement through `KeySessionProviderFactory`, real handshake, full
  MCP session over `/mcp-stls` — passed, completing in ~15 seconds. Took
  three attempts to pin down, not because the underlying work ever
  failed: attempt 1's output was lost to a pipe across an abrupt
  `timeout` kill (the process doesn't exit on its own with `arqit_py` —
  see the fix below — so the external kill always fires); attempt 2 hit
  sidecar processes that had separately expired from their own `timeout`
  wrapper while attempt 1 was being investigated. Attempt 3, with direct
  file output and a patient wait, showed all the real work completing
  cleanly in well under the timeout, start to finish.
- `npm run build` (tsc, strict mode) clean throughout development.

### Not ported

- **`stls_mcp_secure_toolset.py`** (Google ADK integration) — no Node
  equivalent of ADK exists. Not a gap: LangChain.js/Vercel AI SDK already
  expose a native `fetch` override, so `createStlsFetch()` works with
  them directly, no adapter needed (mirrors the favorable situation
  Python already has with LangChain/Semantic Kernel).

### Known limitations

- No chunk-by-chunk encrypted SSE streaming on `/mcp-stls` (same as
  Python; not an issue with `enableJsonResponse: true`, the configuration
  this package targets).
- **`vault-client.ts` calls `process.exit(0)` after a successful run**
  — with `KEY_PROVIDER_NAME=arqit_py`, `KeySessionProviderFactory.getSessionProvider()`
  returns only the `KeySessionProvider`, not a handle to the underlying
  sidecar RPC connection, so there's no way to close it from the caller
  and the process never exits on its own (`base`/`pqc` open no such
  handle and exit cleanly). Fine for a one-shot demo script; a future
  API that also returns a `stop()`/`close()` handle would remove the
  need for this.
