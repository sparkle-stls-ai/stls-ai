# Changelog

All notable changes to **stls_mcp** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.5] — 2026-11-13

Adds client-side retry-on-expiry (issue #6, second increment — the
first added the server-side absolute max-age check shared transparently
via `stls-core` 0.1.7, no code change needed here for that part).
Previously stls-mcp had no automatic recovery at all on session expiry
(idle-TTL or the new max-age), unlike stls-a2a's
`STLSHttpxTransport`/`SecureSessionManager`. Additive — every existing
caller (`stls_httpx_factory()` without the new `rehandshake` argument,
`SecureToolsetManager`) keeps today's exact behavior; retry only
activates where explicitly wired, as done below for
`create_stls_mcp_toolset()`.

### Added

- **`StlsHttpxTransport(..., rehandshake=...)` / `stls_httpx_factory(..., rehandshake=...)`**
  — optional zero-argument async callable that performs a brand new
  handshake and returns `(session_id, transport)`. On a `401` response
  whose body carries the new `ERR_SESSION_EXPIRED` code (see below) and
  no prior retry has happened yet, `StlsHttpxTransport` calls it, swaps
  its internal session state, and retries the original request exactly
  once — same retry-once, fail-fast contract as `stls-a2a`'s
  `STLSHttpxTransport` (a rehandshake failure, or the retried request
  also coming back expired, propagates/returns immediately, no loop).
  Kept provider-agnostic on purpose: the caller builds the closure,
  since only it knows which handshake function and credentials apply
  (`perform_client_handshake()` needs a pre-shared `session_key`;
  `perform_pqc_client_handshake()` needs none).
- **`create_stls_mcp_toolset()` now wires this automatically** — its
  `rehandshake` closure re-runs `establish_session()` with the exact
  same arguments as the initial call. `SecureToolsetManager`'s own
  `invalidate()`/rebuild-on-next-`get_toolset()` path is unchanged and
  stays reserved for failures this one in-transport retry doesn't cover.
- **`ERR_SESSION_EXPIRED = -32004`** (`stls_mcp_constants.py`, same
  numeric value and meaning as `stls_a2a`'s constant of the same name).
  `StlsMcpMiddleware` now raises it explicitly (still `401`) when the
  session cache has no entry for the given `session_id`, instead of the
  generic `-32600` every other `4xx` `StlsMcpError` still uses — lets a
  client distinguish "repeat the handshake" from an unrelated `401`
  (e.g. an auth-layer API-key rejection), so only the former triggers
  the automatic retry above. `StlsMcpError` gained an optional `code`
  parameter to carry this through.
- **`tests/test_stls_mcp_transport_retry.py`** — previously no pytest
  unit coverage existed for `stls_mcp_transport.py`/`stls_mcp_middleware.py`
  at all (only manual integration scripts in `tests/`). Runs the REAL
  middleware against the REAL client transport via `httpx.ASGITransport`
  (not a hand-crafted mock of the wire format), proving the two sides
  actually agree on it.

## [0.1.4] — 2026-11-12

Tracks [`stls_core`](../stls-core/CHANGELOG.md) 0.1.5 (issue #5,
"Restrict stls-core's public API surface").

### Changed

- `create_stls_mcp_toolset()` no longer takes `session_key`/
  `session_key_identifier` — the session key is resolved internally via
  `stls_core.establish_session()` for every provider (`pqc` included,
  collapsing what used to be two separate code paths into one). New
  `config_path` (arqit_py only) and `host`/`port` passthrough params
  replace them.
- `SecureToolsetManager` no longer holds a `KeySessionResolver`; it
  stores its own `{base_url: config_path}` map and forwards it to
  `create_stls_mcp_toolset()`.
- `stls_mcp_transport.py`'s `perform_client_handshake()`/
  `perform_pqc_client_handshake()`/`StlsHandshakeFailed` moved to
  `stls_core` (generic STLS-protocol handshake, no MCP-specific
  knowledge) and are now re-exported here unchanged — same names, same
  signatures, same behavior.

---

## [0.1.3] — 2026-11-10

Packaging-only release: no functional changes. Version bump to enable
the first publish to PyPI via Trusted Publishing.

---

## [0.1.2] — 2026-10-19

### Added (follow-up, 2026-10-23)

- **`SecureToolsetManager`** (`stls_mcp_secure_toolset.py`) — cache of
  encrypted STLS MCP toolsets by `base_url`, with a lazy build and a
  lock against duplicate concurrent handshakes, the `stls-mcp` twin of
  `stls_a2a.SecureSessionManager`. Resolves `base`/`arqit_py` session
  keys via the new shared `stls_core.KeySessionResolver` (`pqc` still
  does its own handshake via `perform_pqc_client_handshake`, same as
  before). Accepts an optional `arqit_client_configs={base_url:
  config_path}` constructor argument, letting one process hold several
  `arqit_py` device identities for different MCP servers — same shape
  and env-var fallback as `stls_a2a.SecureSessionManager`'s own option.
  Validated end-to-end against the real Arqit cloud for the sequential
  case (see `samples/client/11_client_mcp_ms/` and its README).

### Fixed (follow-up, 2026-10-23, continued — concurrent `arqit_py` sessions)

Concurrent `arqit_py` MCP sessions against one long-running server used
to fail with "Invalid proof of possession" once `stls-core`'s
`_ARQIT_PY_START_LOCK` fix (see `python/stls-core/CHANGELOG.md`) stopped
them from deadlocking first: the server resolved its kid via plain
FIFO with no correlation to which incoming handshake it was for, so
concurrent handshakes could get cross-wired. Fixed by threading a
`kid` hint through end to end, mirroring `stls_core_handshake.py`'s
new `HandshakeRequest.kid`:

- `create_stls_mcp_toolset()` gained an optional
  `session_key_identifier: Optional[str] = None` parameter, passed
  through to `perform_client_handshake(kid=...)`.
- `SecureToolsetManager`'s own cache-miss path no longer discards the
  kid half of `self._resolver.resolve(base_url)`'s `(kid, kdata)`
  tuple — passes it through as `session_key_identifier=`.
- `stls_mcp_transport.py`'s `perform_client_handshake()` gained a
  matching optional `kid: str | None = None` parameter, threaded into
  `HandshakeRequest.generate()`.

Confirmed fixed: the same 3-concurrent-sessions reproduction that
previously failed with cross-wired keys now passes cleanly (3/3 and
6/6, 0 errors, each handshake resolving in ~0.1-0.2s) — see
`samples/client/11_client_mcp_ms/README.md` and
`samples/server/12_server_mcp_ms/README.md` (Node side, same fix
ported, see `node/stls-core/CHANGELOG.md`).

### Fixed

- **`StlsMcpMiddleware._handle_http()` double-encrypted HTTP-level error
  responses** — when the inner Starlette app returned a raw HTTP error
  (400/406/415, etc.) not raised as `StlsMcpError`, the middleware still
  encrypted it before returning it on `/mcp-stls`. The client
  (`StlsHttpxTransport`) already assumed any status ≥ 400 arrives in
  plaintext and skipped decryption, so the caller received an
  undecryptable ciphertext envelope instead of the real JSON-RPC error.
  Fixed by returning the captured response as-is, in plaintext, whenever
  its status is ≥ 400 — matching the assumption the client already
  encoded. Identical bug and fix on the Node.js port
  (`node/stls-mcp/src/middleware.ts`).

### Security

- Root `.gitignore` now excludes `aq-*.json` (Arqit device/identity files,
  containing realm credentials and MQTT broker secrets in plaintext) and
  `arqit_utility/` (proprietary Arqit SKA-Platform example code) so they
  are never committed alongside the package.

### Changed

- Translated all remaining Italian docstrings and comments to English,
  across every `src/stls_mcp/*.py` module and every `tests/*.py` script
  (including the three `vault_agent_test_*.py` ADK/LangChain/Semantic
  Kernel variants and the FastMCP-facing `instructions=` text in
  `vault_server.py`).

## [0.1.1] — 2026-07-31

### Added

- **`stls_mcp_secure_toolset.py`** — client-side integration with Google
  ADK: `STLSMCPToolset` / `create_stls_mcp_toolset()`, a workaround for the
  known ADK gap (`StreamableHTTPConnectionParams` does not expose
  `httpx_client_factory` — issue
  [google/adk-python#3005](https://github.com/google/adk-python/issues/3005)),
  subclassing `MCPSessionManager`/`MCPToolset` to inject the STLS encrypted
  transport in place of the stock httpx one. Lazy `google-adk` import,
  inside `STLSMCPToolset.__new__` (same pattern as
  `ContextualRemoteA2aAgent` in stls-a2a): `import stls_mcp` stays valid
  even in purely server-side processes (e.g. `vault_server.py`) without
  google-adk installed. New `[adk]` extra
  (`google-adk>=1.25.0,<2.0.0`, same version constraint as stls-a2a).

- **Three end-to-end test scripts**, same server (`vault_server.py`),
  same tool set, a single `--secure` flag to compare plain vs. encrypted,
  three different frameworks:
  - `tests/vault_agent_test.py` — Google ADK (requires `[adk]`).
  - `tests/vault_agent_test_lg.py` — LangChain/LangGraph
    (`langchain-mcp-adapters`). **No dependency on google-adk**:
    `MultiServerMCPClient`/`StreamableHttpConnection` exposes
    `httpx_client_factory` natively — unlike ADK, no subclassing is needed.
  - `tests/vault_agent_test_sk.py` — Semantic Kernel
    (`MCPStreamableHttpPlugin`). Same story as LangChain:
    `httpx_client_factory` passed to the plugin constructor reaches
    `streamablehttp_client()` intact via `**kwargs`, verified against the
    official source (`semantic_kernel/connectors/mcp.py`,
    `get_mcp_client()`). Uses `OpenAIChatCompletion` instead of
    `GoogleAIChatCompletion`: Semantic Kernel's Google connector relies on
    the **deprecated** `google.generativeai` package, whose protobuf
    `Schema` does not recognize the `"title"` field that MCP tool JSON
    schemas always include (a known, widespread bug, also reproduced on
    `langchain_google_genai` — not specific to this project, see
    [langchain-ai/langchain-google#1121](https://github.com/langchain-ai/langchain-google/issues/1121)).

- **API key protection** (`stls_mcp_auth.py`, new module) — the same
  two-level scheme already in use on stls-a2a, actually shared (not just
  mirrored) via `stls_core.auth`:
  1. `ApiKeyManager` (enterprise: Redis/File/Postgres, rotation,
     per-key scope/endpoint, audit) — if configured, takes priority.
  2. `AuthConfig` (static, in-memory, timing-safe) — backward-compatible
     fallback.
  3. Neither configured → no auth, open endpoints (unchanged default
     behavior).

  `_AuthGateASGI` / `wrap_with_auth()` are a reusable ASGI wrapper applied
  in front of **both** apps (`/mcp` AND `/mcp-stls`) — not just the
  encrypted channel, an exact mirror of stls-a2a's behavior. Necessary
  because, unlike stls-a2a (where every endpoint is a hand-written handler
  with an explicit check at the top), here `MCP_PLAIN_PATH` is generated
  entirely by the official MCP SDK — we don't own its internal handlers,
  so the check must live upstream, at the ASGI transport level. The
  handshake endpoints (`/stls/handshake`, `/stls/pqc/handshake`) remain
  **always open**, by design: a client has no provable credentials yet
  before the handshake itself.

  `create_secure_mcp_app()` now accepts `auth=` / `api_key_manager=`.
  **`api_key_manager` lifecycle reversed compared to stls-a2a**: here
  there is no external lifespan hook (the entire app, lifespan included,
  is built inside `create_secure_mcp_app()`), so an instance that is
  **not yet started** is passed in — start/stop are managed automatically
  by the internal lifespan (`try/finally`, verified order:
  `plain_app_lifespan_enter → api_key_manager.start → api_key_manager.stop
  → plain_app_lifespan_exit`).

- **`AuthConfig` moved to `stls_core.auth`** (`stls_core_auth_models.py`)
  — previously it only existed as a local class in
  `stls_a2a_integration.py` (pydantic `BaseModel`); now it is truly shared
  between stls-a2a and stls-mcp, no longer duplicated. Converted to a
  stdlib `@dataclass` (consistent with `KeyRecord`/`AuditEntry` in the
  same module, which explicitly declares "no external dependency") —
  identical behavior, zero observed breaking change on existing call
  sites (none constructed `AuthConfig()` relying on the previous default
  `realm`, `"A2A Agent"` → now generic `"STLS"`). `stls-a2a` and
  `stls-mcp` re-import it with a single line, same `try/except ImportError`
  pattern already used for `ApiKeyManager`.

- **`vault_server.py`**:
  - `MCP_PLAIN_PATH` / `MCP_STLS_PATH` configurable via `.env`
    (defaults unchanged: `/mcp`, `/mcp-stls`), useful for exposing the
    whole server under a prefix (e.g. `/vault/mcp`).
  - `AUTH_ENABLED` — explicit gate, ours (not a `stls_core.auth`
    environment variable): `ApiKeyManager.from_env()` **always** builds a
    working manager, but with zero configuration the default
    (`AUTH_LOADER_BACKEND=static`, zero keys) means "deny everything",
    not "auth disabled". Without explicit `AUTH_ENABLED=true`,
    `api_key_manager=None` — unchanged historical behavior.

- **`pyproject.toml`** — new `[postgres]` extra (pass-through to
  `stls_core[postgres]`) for the `AUTH_LOADER_BACKEND=postgres` backend of
  `ApiKeyManager`.

### Changed

- **`create_stls_mcp_toolset()`** — new `headers` parameter (dict),
  forwarded to `StreamableHTTPConnectionParams` for every encrypted MCP
  request (never on the handshake). Needed to pass an API key on the
  encrypted ADK path, since `StreamableHTTPConnectionParams` previously had
  no entry point for application headers in this module.

- **`StlsMcpMiddleware.__init__`** — now accepts `stls_path=` / `plain_path=`
  (defaulting to the module constants, so backward-compatible).

### Fixed

- **`StlsMcpMiddleware._rewrite_path()` ignored custom paths** — it always
  compared against the module constants `MCP_STLS_PATH`/
  `MCP_PLAIN_PATH` (`/mcp-stls`, `/mcp`), never against the `stls_path`/
  `plain_path` actually passed to `create_secure_mcp_app()`. With a custom
  path (e.g. `/vault/mcp-stls`), the condition never matched: the
  decrypted body was forwarded **unchanged** to the inner app, which
  expected a different path (`/vault/mcp`) — an internal mismatch, a
  non-JSON response, `json.loads()` failing in the response-encryption
  block, the exception caught generically →
  **500 to the client**, a symptom that didn't suggest a routing issue at
  all. The `plain_path` parameter of `create_secure_mcp_app()`, previously
  purely cosmetic ("only for the startup log"), is now
  **functionally required**: it must match exactly the
  `streamable_http_path` of the `FastMCP` constructor.

- **The three client test scripts read the encrypted path from a fixed
  constant** (`from stls_mcp import MCP_STLS_PATH`), not from `.env` — even
  though the server already supported configuring it via env. Client and
  server could silently diverge if the path was customized only on the
  server side. Same bug class already seen with stls-a2a's PQC handshake
  (paths derived from different sources that must be kept in sync by
  hand).

- **`postgres` extra missing at the `stls_core` level** — `asyncpg` lived
  (undocumented) inside the `[redis]` extra; the `stls-mcp` `[postgres]`
  extra (pass-through) therefore pointed at a nonexistent extra on the
  `stls_core` side. Fixed in `stls_core`: `[redis]` reduced to just the
  `redis` package, new `[postgres]` with only `asyncpg` — two independent
  backends, no reason installing one should pull in the other.

### Security

- `ApiKeyManager`/`AuthConfig` now protect **both** `/mcp` **and**
  `/mcp-stls` uniformly — not just the encrypted channel, mirroring the
  behavior already in use on stls-a2a (`/a2a` and `/secure-a2a` protected
  the same way). The handshake endpoints remain always open, unchanged.

### Validated

- Per-key restriction on `endpoints` (`KeyRecord.allows_endpoint()`,
  prefix-based): key valid on only one path, denied on the other —
  tested by hand against a real deployment with a Postgres backend.
- Key revocation: behavior verified by hand against a real deployment.
- Plain (`base`) handshake + encrypted traffic with `AUTH_ENABLED=true`
  and `X-API-Key` propagated on all three clients (ADK, LangChain,
  Semantic Kernel): end-to-end success on all three after the path
  rewrite fix.

### Operational notes

- **Editable install not rebuilt after changes to `stls-core`**: observed
  that, without a version bump in `stls-core`'s `pyproject.toml`,
  `uv pip install -e` may not regenerate the installed package — the
  files in `site-packages` remain the old ones even after modifying the
  sources. Immediate fix:
  `uv pip install -e ".[extra]" --reinstall-package stls_core`.
  Structural recommendation: a real version bump on every substantial
  change to `stls-core`, not just a cosmetic one — that's the signal that
  triggers reinstallation without special flags.
- **`plain_path`/`stls_path` must be kept in sync across every process
  involved** (server AND each client) when customized via `.env` — there
  is no automatic discovery of the effective path on the client side in
  this version.

---

## [0.1.0] — 2026-06-26

Initial release.

### Added

- **Dual endpoint design**: `/mcp` (standard MCP, generated and managed
  entirely by `mcp.server.fastmcp.FastMCP.streamable_http_app()`, zero
  STLS modifications) alongside `/mcp-stls` (same JSON-RPC MCP protocol,
  encrypted in transit with AES-256-GCM + HMAC-SHA256 + HKDF-Expand). Same
  session manager, same tool/resource/prompt surface on both — no
  duplication of application logic.

- **`create_secure_mcp_app(mcp, key_session_provider, ...)`**
  (`stls_mcp_server.py`) — assembles the complete Starlette app: mounts
  the standard FastMCP app untouched, registers the `/stls/handshake`
  endpoint (reusing `stls_core.perform_handshake()` 1:1 with `stls-a2a`),
  and mounts `StlsMcpMiddleware` on `/mcp-stls`. Also automatically
  registers `/stls/pqc/handshake` when `KEY_PROVIDER_NAME=pqc` (see the
  PQC section below). Correctly propagates the MCP session manager's
  lifespan (`app.router.lifespan_context`).

- **`StlsMcpMiddleware`** (`stls_mcp_middleware.py`) — a byte-level ASGI
  proxy: decrypts the inbound body on `/mcp-stls`, builds a synthetic
  scope pointing to `/mcp`, invokes the standard app capturing its
  response, re-encrypts and returns it. Errors always returned in
  plaintext (same design choice already adopted in `stls-a2a` for SSE
  errors). The middleware's internal `SessionCache` is shared with all
  handshake endpoints (standard and PQC), guaranteeing that sessions
  created by one are recognized by the other.

- **Client-side transport** (`stls_mcp_transport.py`):
  - `perform_client_handshake()` — STLS handshake against
    `/stls/handshake`, with explicit client-side verification of the
    proof-of-key-possession (`StlsHandshakeFailed` if the server doesn't
    hold the expected key). Used with the `base` and `arqit_py`
    providers.
  - `perform_pqc_client_handshake()` — PQC handshake against
    `/stls/pqc/handshake`. Requires no pre-shared session key: generates
    X25519 + ML-KEM-768 keypairs client-side, performs the KEM exchange
    with the server, derives the session key locally via HKDF, verifies
    the proof and zeroes the private keys. Requires `liboqs-python`
    (`[pqc]` extra). Same return signature as
    `perform_client_handshake()` — `(session_id, transport)` — for
    interchangeability in client code.
  - `StlsHttpxTransport(httpx.AsyncBaseTransport)` — transparently
    encrypts/decrypts only requests to the STLS path, leaving everything
    else unchanged (including any GET for SSE).
  - `stls_httpx_factory()` — factory compatible with
    `mcp.client.streamable_http.streamablehttp_client(httpx_client_factory=...)`,
    for drop-in integration with the official MCP client stack
    (`ClientSession` has no idea it's talking to an encrypted endpoint).

- **PQC provider on `/stls/pqc/handshake`** — when
  `KEY_PROVIDER_NAME=pqc`, `create_secure_mcp_app()` automatically
  registers the `POST /stls/pqc/handshake` route that calls
  `stls_core.perform_pqc_handshake()`. The KEM exchange happens on every
  handshake (X25519 DH + ML-KEM-768 encapsulation + HKDF-SHA256); the
  derived session key is inserted directly into the middleware's
  `SessionCache`. For multi-replica deployments (Cloud Run scale-out),
  set `SESSION_CACHE_BACKEND=redis` — a requirement common to all
  providers in these architectures, not specific to PQC.

- **`stls_capabilities_fragment()`** — fragment for
  `capabilities.experimental.stls` in the `initialize()` response. For
  `KEY_PROVIDER_NAME=pqc`, `keyDerivation` reports
  `"HKDF-SHA256+X25519+ML-KEM-768"` and `handshakeEndpoint` reports
  `"/stls/pqc/handshake"` — so clients aware of the active provider can
  select the correct handshake path with no extra configuration.

- **STLS extension discovery** — under `experimental`, by MCP design: a
  client unaware of the extension ignores it and keeps using `/mcp`
  normally.

- **Complete "Vault" example** (`tests/vault_server.py`,
  `tests/vault_client.py`) — reference server and client covering all
  three MCP primitives (5 tools, 2 resources including a parameterized
  resource template, 2 prompts) over a small themed domain (an archive of
  quantum-safe space stations). Supports all three providers (`base`,
  `arqit_py`, `pqc`) via `KEY_PROVIDER_NAME` in the environment — no code
  change needed to switch providers.

- **`pyproject.toml`** — dependency on `stls-core` via
  `[tool.uv.sources]` (local monorepo path), `mcp>=1.27,<2`, `starlette`,
  `uvicorn`, `httpx`. `[redis]`, `[arqit_py]` and `[pqc]` extras as
  pass-through to the identically-named `stls-core` extras.

- **`.env.example`** — variables for host/port/path of the MCP server,
  `MCP_ALLOWED_HOSTS` for the SDK's DNS-rebinding protection, plus the
  sections shared with `stls-core` (Key Provider, Arqit, Session Cache,
  Auth, Redis, PostgreSQL).

### Fixed during development (root causes noted, for future reference)

- **`app.lifespan` doesn't exist on `streamable_http_app()` in
  `mcp>=1.27`** — older documentation and examples (the pattern
  `FastAPI(lifespan=lambda app: mcp.session_manager.run())`) don't apply
  to the Starlette app returned by the current SDK. The session
  manager's context manager lives on `app.router.lifespan_context`.
  Without correctly propagating it into the outer app, the MCP session
  manager never starts and all requests (plain and encrypted, because
  they share the same session manager) fail silently or with cryptic
  client-side errors.

- **Starlette `Mount(prefix, app=...)` doesn't match the exact path
  without a trailing slash** — the regex generated by `Mount` is
  `^{prefix}/(?P<path>.*)$`, which requires a slash *after* the prefix. A
  `POST /mcp-stls` request (with nothing after it, typical behavior of
  MCP clients including Claude Desktop) doesn't match that `Mount` and is
  silently routed to the next, generic `Mount("/", ...)`, producing a
  completely wrong response with no visible error. Fix:
  `_ExactMountPathNormalizer`, an ASGI wrapper that wraps the entire app
  and normalizes the scope (adding the missing slash) *before* it reaches
  the Starlette router — it must act upstream of routing, because the
  match failure already happens before any middleware nested inside that
  `Mount` is invoked.

- **`StlsMcpMiddleware._rewrite_path()` always received the path with a
  trailing slash** — a direct consequence of the Starlette behavior
  above: the scope the middleware receives when mounted via `Mount`
  always has `path` with a trailing slash added, even for requests to the
  exact path without a slash. The rewrite `/mcp-stls/` → `/mcp/` (empty
  suffix → `/`) didn't match the exact `/mcp` route registered by
  FastMCP, causing an internal 307 redirect that the middleware's
  `capturing_send` intercepted as if it were the tool's real response.
  Fix: normalize the `/` suffix to an empty string before concatenation.

- **The SDK's DNS-rebinding protection blocks `127.0.0.1`/`localhost` by
  default** — not specific to `stls-mcp`, but worth documenting:
  `TransportSecuritySettings.enable_dns_rebinding_protection` is `True` by
  default and requires an explicit `allowed_hosts` even for local
  development, otherwise every request (including those from
  `127.0.0.1`) gets `421 Invalid Host header`. When the server is exposed
  via a tunnel (e.g. `cloudflared`), the tunnel's domain must be added
  explicitly — the Cloudflare quick tunnel generates a new domain on
  every restart, so `MCP_ALLOWED_HOSTS` must be updated every time the
  tunnel is recreated.

### Validated

- Complete manual E2E suite (`tests/vault_server.py` +
  `tests/vault_client.py`) with the official client stack
  (`streamablehttp_client` + `ClientSession`) over a real HTTP connection
  (uvicorn, not `TestClient`): handshake, `initialize()`, discovery and
  calling of every tool/resource/prompt through `/mcp-stls`. Plain `/mcp`
  verified untouched both before and after the encrypted traffic.
  Validated with all three providers: `base`, `arqit_py` and `pqc`.

- Real test with **Claude Desktop** on plain `/mcp` (via `cloudflared`
  tunnel): connector recognized and connected, automatic discovery of
  all 5 tools, calls executed correctly
  (`list_stations`, `get_station_status`, `calculate_orbit`) with exact
  results. Confirms the design requirement "absolutely standard plain
  endpoint, indistinguishable from a pure MCP server".

- **PQC provider E2E**: `vault_server.py` + `vault_client.py` with
  `KEY_PROVIDER_NAME=pqc`. KEM handshake completed (X25519 + ML-KEM-768 +
  HKDF), proof verified, session used correctly for the entire MCP flow
  (discovery + tool/resource/prompt calls). Validated with
  `liboqs-python 0.15.0` + `liboqs C 0.15.0` built from source.

### Notes

- E2E tests with a real `arqit_py` provider not included in this
  package — requires the real Arqit SKA-Platform™ environment, not
  reproducible in CI. Same decision already made for `stls-a2a`: the test
  must run on infrastructure that has Arqit peering configured.
