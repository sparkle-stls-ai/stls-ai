# Changelog

All notable changes to **stls_a2a** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.7] — 2026-11-12

Tracks [`stls_core`](../stls-core/CHANGELOG.md) 0.1.5 (issue #5,
"Restrict stls-core's public API surface").

### Added

- **`SecureA2ATransport.export_session_key()`** (`stls_a2a_transport.py`)
  — explicit, documented accessor for callers with a legitimate need to
  persist the raw session key themselves (e.g. `sofia-client-secure`,
  across process restarts). Replaces the `session_key` public attribute,
  which was kept only for "log/debug backward compatibility" — a
  leftover, not load-bearing. Raises if the instance was built via the
  new `from_transport()` classmethod (no raw key exists on that path).
- **`SecureA2ATransport.from_transport()`/`SecureA2ATransportAdapter.from_transport()`**
  — build either from an already-established `StlsTransport` (e.g. from
  `stls_core.resolve_transport()`), with no raw key material involved.

### Changed

- `SecureSessionManager`'s base/arqit_py path
  (`_resolve_via_key_session_provider()`) now uses
  `stls_core.resolve_transport()` instead of the removed
  `KeySessionResolver` directly — same no-handshake wire behavior
  (this path never did one), but the raw key no longer passes through
  this method at all, even internally.
- `create_pqc_a2a_transport()` now derives its session key via
  `KeySessionProviderPQC.derive_client_key()` instead of reading
  `x25519_private_key_bytes`/`mlkem_secret_key_bytes` directly (those
  are private now — see `stls_core`'s own CHANGELOG entry).

### Fixed

- Removed a debug log line in `stls_a2a_integration.py` that printed
  the raw session key at `DEBUG` level.

---

## [0.1.6] — 2026-11-10

Packaging-only release: no functional changes. Version bump to enable
the first publish to PyPI via Trusted Publishing.

---

## [0.1.5] — 2026-10-23

### Added

- `ContextualSecureRemoteA2aAgent`/`SecureSessionManager`
  (`stls_a2a_secure_remote.py`) now support `base` and `arqit_py` in
  addition to `pqc`, via an opt-in `provider_name=` argument. `base`/
  `arqit_py` resolve the session through the generic
  `KeySessionProviderFactory` (awaited directly — no
  `resolve_if_awaitable()`, which would break inside the already-running
  event loop this class operates in) instead of the PQC-specific
  `/stls/pqc/handshake` round trip. Default stays `pqc`; no behavior
  change for existing callers that don't pass `provider_name=`.

- **`SecureSessionManager(arqit_client_configs={base_url: config_path})`**
  — lets one client process hold distinct `arqit_py` device identities
  for different remote counterparts, instead of being limited to the
  single device/config the whole process's `ARQIT_PY_CLIENT_CONFIG` env
  var pointed at. Resolved per `base_url` via `stls_core`'s new shared
  `KeySessionResolver` (see `python/stls-core/CHANGELOG.md` — also used
  by `stls-mcp`'s `SecureToolsetManager`); falls back to the env var
  when no entry matches a given `base_url` (or the argument is
  omitted), so existing callers are unaffected. Works identically with
  `ContextualSecureRemoteA2aAgent`
  by sharing one `SecureSessionManager` across its sub-agents (no
  `stls_a2a` code change was needed for that case). Validated
  end-to-end against the real Arqit cloud for the sequential-use case
  (one device's session fully set up before the next starts — see
  `samples/client/10_client_ms/` and its README for the reproducible
  test matrix).

### Fixed (follow-up, 2026-10-23, continued)

- **Concurrent/parallel use of several `arqit_py` configs in one
  process — now safe, not just validated sequentially.** Two real bugs
  were found in `stls_core`'s `arqit_py` provider (a process-wide Arqit
  SDK singleton deadlock, and a plain-FIFO key resolution that broke
  under genuine concurrency) and fixed at the source — this package
  needed no code change of its own, since `SecureSessionManager` calls
  into the now-fixed `stls_core` provider/resolver. See the `arqit_py`
  section of `README.md`'s "Current Limitations and Notes" and
  `python/stls-core/CHANGELOG.md` for the full write-up.

### Changed

- Translated remaining Italian docstrings/comments/log messages in
  `src/stls_a2a/` to English. No logic changes, string literals only.

---

## [0.1.4] — 2026-07-30

### Added

- **SSE heartbeat** (`STLS_SSE_HEARTBEAT_SECONDS`, default 15s; `0` = disabled)
  in `process_plain_stream()` / `process_secure_stream()`, both on the
  streaming path (`adapter.stream()`) and on the synchronous fallback
  (`adapter.handle_request()`). Periodically emits an SSE comment
  (`: keep-alive`) while the adapter is blocked in a long-running
  delegation (e.g. a non-streaming sub-agent), without ever losing or
  interrupting the pending chunk. Fixes silent client disconnection
  (default httpx 120s, proxies/LBs with tight read-timeouts) while
  server-side work keeps running to completion.

- **`TaskStoreBase.touch(task_id)`** — non-abstract, default no-op for
  backward compatibility with external implementations. Called on every
  SSE heartbeat tick: refreshes `updatedAt` (in-memory) or just the TTL via
  `EXPIRE` (Redis — deliberately **not** a get-modify-save, so as not to
  overwrite concurrent updates applied via Lua script). Prevents a job that
  runs longer than the TTL from being removed by cleanup while still in
  progress.

- **`ERR_SESSION_EXPIRED` (`-32004`)** and **`SessionExpiredError`** —
  dedicated code/exception for an unknown or expired STLS session that is
  **not regenerable**. For the PQC provider this is the only possible case:
  the `session_key` is born from an ephemeral KEM at handshake time, it
  can't be derived from the `session_id` alone the way it is (lazily) for
  `base`/`arqit_py`. Propagated with the standard JSON-RPC shape and HTTP
  401 on all handlers (sync `/secure-a2a`, SSE, plain) instead of the
  previous generic `{'error': str(e)}` blob — lets an STLS-aware client
  distinguish "redo the handshake and retry" from a permanent application
  error.

- **`stls_a2a_secure_remote.py`** — new module for **encrypted intra-agent**
  delegation, analogous to `ContextualRemoteA2aAgent` but with traffic over
  `/secure-a2a` instead of plain `/a2a`:
  - **`SecureSessionManager`** — per-`base_url` cache of PQC sessions, with
    lazy handshake and an `asyncio.Lock` guarding against a concurrent
    double handshake. The handshake is always performed at the host's
    **root** (`/stls/pqc/handshake` is a global, app-wide endpoint, not
    per-agent-prefixed); the data endpoints (`_secure_url`/`_stream_url`)
    are rebuilt on the actually requested `base_url` — which may include a
    multi-agent prefix (e.g. `/agents/agent-b`) — without redoing the
    crypto negotiation that's already been completed.
  - **`STLSHttpxTransport`** (`httpx.AsyncBaseTransport`) — transparently
    intercepts JSON-RPC requests aimed at an STLS agent's plain endpoint,
    encrypts them and redirects them to `/secure-a2a` (or
    `/secure-a2a/stream`), decrypts the response. Passthrough for discovery
    and non-JSON-RPC bodies. SSE path rebuilt byte-for-byte compatible with
    the plain endpoint (same JSON-RPC 2.0 envelope, `artifactId`,
    `timestamp`). Retry-once on `ERR_SESSION_EXPIRED` on the synchronous
    path (always safe, no bytes emitted yet) and on the SSE path **only**
    if the error is the very first event from the generator ("priming"
    before the Response: consumed before building the `httpx.Response`, so
    still zero bytes sent to the caller) — never mid-stream, where real
    downstream events might already have been forwarded.
  - **`ContextualSecureRemoteA2aAgent`** — drop-in replacement for
    `RemoteA2aAgent` that composes context propagation (identical to
    `ContextualRemoteA2aAgent`) with encrypted transport via
    `STLSHttpxTransport`. Explicitly rejects a manually passed
    `httpx_client=` (it would be silently overwritten).

- **`server_context_test.py`**: `--secure` flag to switch the A→B test
  delegation between `ContextualRemoteA2aAgent` (plain, default) and
  `ContextualSecureRemoteA2aAgent` (encrypted, lazy PQC handshake towards
  B). **`agent_context_test_a_secure.py`** — encrypted twin of
  `agent_context_test_a.py`, same context-propagation scenario, for direct
  comparison of the observed traffic.

### Changed

- **`stream_secure_message()`** (encrypted SSE client) and
  **`A2AClient.stream()`** (plain SSE client) — default timeout
  `120.0s → 300.0s`, with a structured `httpx.Timeout`
  (`connect=10, write=30, pool=10`, only `read` extended). With the
  server ≥0.1.4 emitting periodic keep-alives, the read timeout now only
  triggers if the server is actually dead, not during a long but live job.

- **`TASK_STORE_TTL_SECONDS`** — default `180s → 900s` (in-memory, Redis,
  factory) — a more realistic margin for long-running jobs, especially now
  that heartbeat `touch()` refreshes it during execution anyway.

- **Explicit timeout** (`connect=10, read=120`) on the `FileReference` URI
  fetch in `stls_a2a_google_adapter.py` — it used to be httpx's 5s default,
  insufficient for downloading even moderately large remote files.

### Fixed

- **`A2AMessage.id` tipizzato `Optional[int]`** — troppo stretto rispetto a
  JSON-RPC 2.0 (§4.1), che ammette `id` come stringa, numero o null.
  a2a-sdk/ADK generano `id` come UUID stringa (es.
  `"b202b8e2-37e1-43de-8e00-374884280289"`), causando un
  `ValidationError` pydantic su **ogni** richiesta cifrata proveniente da
  un client ADK reale. Ora `Optional[Union[int, str]]`. Bug preesistente
  nella classe base condivisa, non limitato alla nuova delega cifrata —
  colpiva chiunque chiamasse `/secure-a2a` con un id non numerico.

- **`SecureSessionManager`: base_url troncato a `scheme://netloc`** —
  scartava qualunque prefisso dell'agente, rompendo ogni deployment
  multi-agente dove `/secure-a2a` vive sotto un path dedicato (es.
  `/agents/agent-b/secure-a2a`, convenzione standard di
  `create_secure_a2a_endpoint`). Produceva un `secure_url` errato
  (`.../secure-a2a` invece di `.../agents/agent-b/secure-a2a`) → 404
  silenzioso lato ADK, senza un'eccezione riconoscibile.

- **`SecureSessionManager`: same base_url (with prefix) also reused for
  the PQC handshake** — which actually always lives at the app's root
  (global endpoint, registered once per process, never per-agent
  prefixed). This caused a 404 on the handshake itself in every
  multi-agent deployment, even after the fix to the previous point.

- **`_card_from_adk()`: tool shown with the name `<function foo at
  0x7f...>` instead of the real name** — `getattr(tool, "name",
  str(tool))` assumes `agent.tools` always contains already-wrapped
  objects (`FunctionTool`). With `Agent(tools=[my_function])`, depending
  on the ADK version, the raw function isn't re-wrapped before being
  exposed on `agent.tools`: `getattr` doesn't find `.name`, falls back to
  `str(tool)` — a repr that changes on every process (it contains the
  memory address) and is useless in the public card. New helper
  `_tool_identity()`: fallback `.name` → `.__name__` (raw function) →
  `str(tool)`; for the description, `.description` → first line of
  `.__doc__` → empty string.

### Security

- **Public cards no longer mention the "Sparkle SKA" branding** —
  `_build_stls_extension()`/`_get_security_scheme()` included the string
  `"Sparkle SKA"` in descriptions and in the `SparkleSKASecurityScheme`
  class name. The active provider (`keyProvider` / `key_provider`)
  **remains visible** in plaintext in the card, unchanged — that is
  legitimate information for interoperability (a client may want to agree
  on which key-exchange mechanism to support); the problem was only the
  Arqit product name, not the fact of indicating which provider is in use.

- **`SparkleSKASecurityScheme` → `StlsKeyExchangeSecurityScheme`,
  `type: "sparkle_ska"` → `"stls_key_exchange"`** — not just a branding
  issue: the name "Sparkle SKA" (an Arqit product) was also semantically
  wrong for the `pqc` case, which has no relationship with Arqit at all
  (X25519 + ML-KEM-768 is an independent mechanism). **Deliberate breaking
  change**: anyone already parsing `securityScheme.type == "sparkle_ska"`
  must update to the new value.

- **STLS extension URI**: `https://stls-ai.tisparkle.com/...` →
  `https://stls-ai.com/...` — removed the company subdomain from the
  extension's public identifier. The same change was also applied to
  `stls-mcp` (`STLS_EXTENSION_URI`), for consistency between the two
  protocols.

### Operational notes

- **Persistent `SessionCache` (Redis) + deterministic provider** (`base`:
  `session_id` always fixed to `"base-session-key-identifier"`) can mask a
  `KEY_PROVIDER_NAME` mismatch between two different runs of the same
  agent: a `base` session cached before the provider change stays valid
  for a cache hit even after a restart with `KEY_PROVIDER_NAME=pqc`,
  because the cache hit bypasses `key_session_provider` entirely. Not an
  application bug — but worth keeping in mind: **flush the
  `SessionCache` when switching provider between test runs**
  (`redis-cli --scan --pattern 'stls:sc:*' | xargs redis-cli DEL`).

- **Shared PQC handshake in multi-agent-per-app scenarios** (known issue,
  not yet fixed): `/stls/pqc/handshake` is registered only once per app
  (guarded on `app.routes`), and the closure captures the
  `middleware.sessions` of the **first** agent registered. If multiple PQC
  agents share the same FastAPI process, only the first one sees the
  sessions populated by the handshake. Does not affect one-agent-per-process
  deployments (e.g. `server_context_test.py`).

---

## [0.1.3] — 2026-06-26

### Added

- **`create_pqc_a2a_transport(base_url, *, client_id, handshake_path, httpx_client)`**
  (`stls_a2a_integration.py`) — client-side helper for the PQC provider.
  Replaces the manual sequence (provider → kid → key →
  `SecureA2ATransportAdapter`) with a single call that internally performs
  the X25519 + ML-KEM-768 KEM handshake: generates the keypair client-side,
  sends a `PQCHandshakeRequest` to the `/stls/pqc/handshake` endpoint,
  derives the `session_key` via HKDF-SHA256, verifies the proof of
  possession and zeroes out the private keys. Returns a
  `SecureA2ATransportAdapter` ready for `send_secure_message()` /
  `stream_secure_message()`. Doesn't require a pre-shared key. Re-exported
  from the public package (`from stls_a2a import
  create_pqc_a2a_transport`). Requires `liboqs-python` (the `[pqc]` extra).

- **`/stls/pqc/handshake` endpoint** in `create_secure_a2a_endpoint()` —
  when `KEY_PROVIDER_NAME=pqc`, the function automatically registers the
  `POST /stls/pqc/handshake` route, which calls
  `stls_core.perform_pqc_handshake()`. The endpoint uses the middleware's
  internal `SessionCache` (`middleware.sessions`) so PQC sessions are
  immediately recognized by `get_transport()` without going through the
  provider's lazy resolution. The route is registered **only once** per
  app (guarded on `app.routes`): multiple agents calling
  `create_secure_a2a_endpoint()` on the same server don't duplicate the
  handler. **Multi-agent note with the memory backend**: with
  `SESSION_CACHE_BACKEND=memory`, the endpoint is tied to the
  `SessionCache` of the first agent registered. For multi-agent PQC
  (clients calling different agents with the same session) use
  `SESSION_CACHE_BACKEND=redis` — which is already the standard
  requirement for any multi-replica Cloud Run deployment.

- **PQC support in `stls_a2a_utils.py`**:
  - `_get_security_scheme()` — added a `"pqc"` case: returns
    `SparkleSKASecurityScheme(key_provider="pqc", description="Quantum-safe
    key exchange via X25519 + ML-KEM-768 (NIST FIPS 203)")`.
  - `_build_stls_extension()` — for `KEY_PROVIDER_NAME=pqc`, adds
    `keyDerivation: "HKDF-SHA256+X25519+ML-KEM-768"` and
    `handshakeEndpoint: "/stls/pqc/handshake"` to the STLS extension
    parameters in the `AgentCard`.

- **`[pqc]` extra** in `pyproject.toml` — pass-through to
  `stls_core[pqc]` (`liboqs-python`). Install with:
  `uv pip install -e ".[pqc]"`.

- **PQC support in tests**:
  - `task_test.py` — module-level `KEY_PROVIDER_NAME=pqc` detection: for
    `base`/`arqit_py` the session key is resolved at launch as before; for
    `pqc` a placeholder is set and the KEM handshake happens at the start
    of `task_test()` via `create_pqc_a2a_transport()`, then
    `session_key_identifier` and `session_key_data` are populated for
    reuse in all subsequent `call_agent()` calls (unchanged).
  - `key_providers_test.py` / `key_providers_test_en.py` — `open_session()`
    is now PQC-aware: if `KEY_PROVIDER_NAME=pqc` it calls
    `create_pqc_a2a_transport()` and extracts
    `(session_id, transport.session_key)` instead of using
    `KeySessionProviderFactory`. The return signature `(kid, kdata)` is
    unchanged — `stream_call()` and the rest of the test remain untouched.

### Changed

- **Extraction of `stls-core`** — the protocol-agnostic crypto/session/handshake/
  provider/auth layer has been extracted into a new, independent package,
  `stls-core`, shared between `stls-a2a` and `stls-mcp`
  (Model Context Protocol extension). `stls-a2a` now depends on `stls-core`
  instead of duplicating this code. The package is resolved as a local
  monorepo dependency via `[tool.uv.sources]` (`stls_core = { path =
  "../stls-core" }`); in production this should be replaced with a published
  package on a private index.

- **`stls_a2a_transport.SecureA2ATransport`** — no longer implements AES-256-GCM
  / HMAC-SHA256 / HKDF-Expand directly. It is now a thin wrapper around
  `stls_core.StlsTransport`, adding only the `A2AMessage` (JSON-RPC)
  serialization layer. The wire format produced by `encrypt_message()` /
  consumed by `decrypt_message()` is byte-for-byte unchanged — no breaking
  change for existing clients or servers. `A2AMessage`, `A2ASecureClient`, and
  `A2ASecureServer` remain in this module since they are A2A-specific.

- **`SecureA2AHTTPMiddleware.sessions`** — the in-memory `OrderedDict`-based
  LRU+TTL session cache has been replaced with `stls_core.SessionCache`,
  identical to the one used internally by `stls-mcp`. `STLS_SESSION_CACHE_MAX`
  and `STLS_SESSION_TTL` environment variables keep working exactly as before.
  External behavior of `get_transport()` (lazy provider resolution on cache
  miss, cache hit reuse, TTL-based eviction) is unchanged for `base` and
  `arqit_py`; for `pqc` the session is always pre-populated by the
  `/stls/pqc/handshake` endpoint before any A2A call.

- **Key providers (`base`, `arqit_py`, `pqc`) now live in `stls_core.providers`** —
  re-exported from `stls_core` and imported transparently through `stls_a2a`'s
  public API (`from stls_a2a import KeyProviderFactory` still works unchanged).

- **Auth system (`ApiKeyManager`, loaders, `RedisKeyCache`, ...) now lives in
  `stls_core.auth`** — re-exported through `stls_a2a`'s public API with the
  same import paths as before.

- **`pyproject.toml`** — `cryptography`, `asyncpg`, and `redis` removed from
  base dependencies (now transitive through `stls_core` / `stls_core[redis]`).
  New `[redis]` and `[pqc]` extras added as pass-through to `stls_core[redis]`
  and `stls_core[pqc]`. The `[arqit_py]` extra pulls Arqit wheels via
  `stls_core[arqit_py]`.

### Removed

- **`arqit_jvm` extra and `jpype1` dependency** — the JVM/JPype bridge to the
  Java STLS-AI SDK has been removed entirely, now that the Arqit SKA-Platform™
  Python native SDK (`arqit_py` provider) covers the same functionality
  without a JVM dependency.

- **`simple` and `arqit` (JVM-based) key providers** — removed along with the
  JPype bridge. Only `base` (fixed key, dev/test), `arqit_py` (Arqit
  SKA-Platform™ native Python SDK), and `pqc` (X25519 + ML-KEM-768,
  software-based, no external service) remain. `KEY_PROVIDER_NAME` now
  accepts these three values; deployments still configured with
  `KEY_PROVIDER_NAME=simple` or `KEY_PROVIDER_NAME=arqit` must switch to
  `arqit_py`.

- **`stls_a2a_provider.py`** — deleted. Its content now lives in
  `stls_core.providers`.

- **`stls_a2a_auth_models.py`, `stls_a2a_auth_loaders.py`,
  `stls_a2a_auth_store.py`, `stls_a2a_auth_manager.py`** — deleted. Their
  content now lives in `stls_core.auth`.

### Fixed

- **`KeySessionProviderFactory.resolve_if_awaitable()` missing after
  migration** — the public static method used by client code to normalize
  a provider result was inadvertently renamed to a private `_resolve()`.
  Restored as a public method; `_resolve` is kept as an internal alias.

- **Leftover import of the removed `stls_a2a_auth_manager` /
  `stls_a2a_auth_models` modules** — the optional `try/except ImportError`
  guard in `stls_a2a_integration.py` still pointed at the deleted files.
  Updated to import from `stls_core` and `stls_core.auth`.

### Security

- **Handshake with proof-of-key-possession now available for the PQC
  provider** — `create_secure_a2a_endpoint()` registers
  `/stls/pqc/handshake` when `KEY_PROVIDER_NAME=pqc`. The flow uses the
  same HMAC challenge/response as `stls-core` / `stls-mcp`: server-side
  generated session_id (UUID, non-enumerable), proof =
  HMAC-SHA256(session_key, nonce), no key material in transit. For `base`
  and `arqit_py`, the existing lazy flow (session_id embedded in the
  first encrypted envelope, provider resolution on cache miss) remains
  unchanged — no breaking change for existing deployments. Converging on
  an explicit handshake for `base`/`arqit_py` too is tracked as future
  work.

---

## [0.1.2] — 2026-06-17

### Added

- **`FileInline.name`** — new optional field to propagate the original
  file name through the A2A protocol.

- **`display_name` propagation in `Blob`** (`stls_a2a_google_adapter.py`,
  `_to_genai_parts`) — reads `FileInline.name` and sets it as the genai
  `Blob`'s `display_name`.

- **`SecureA2ATransportAdapter._maybe_process_csv_key()`** — post-processing
  of response chunks in `process_secure_stream` for the `export_sim_csv`
  tool.

---

## [0.1.1] — 2026-05-30

### Added

- **`stls_a2a_remote.ContextualRemoteA2aAgent`** — drop-in replacement for
  `RemoteA2aAgent` with automatic propagation of `user_id` and `profile`
  from `session.state` via `a2a_request_meta_provider`.

- **`SecureA2ATransportAdapter.stream_secure_message()`** — true
  incremental, chunk-by-chunk streaming.

- **`X-STLS-User-Id` / `X-STLS-Profile` HTTP headers** — priority-3 user
  context propagation channel.

- **`pyproject.toml` `[examples]` extra** — `yfinance` moved to an
  optional extra.

- **`KeyProviderArqitPy`** — quantum-safe provider via the Arqit
  SKA-Platform™ native Python SDK, no JVM/JPype.

- **`pyproject.toml` `[arqit_py]` extra** — 7 proprietary Arqit wheels.

### Fixed

- **Malformed TOML in `pyproject.toml`** — missing `[build-system]`.
- **`langgraph-prebuilt` conflict** — LangChain stack aligned.
- **`_build_parts()` in-place dict mutation** — replaced with a
  non-destructive read.
- **`_run_in_background()` crash on `result["parts"]`** — added
  `_normalize_result()` with a fallback.
- **`_card_from_llm()` wrong fields in the AgentCard** —
  `default_input_modes` / `default_output_modes` instead of
  `input_modes` / `output_modes`.
- **`_fetch_cards()` synchronous in an async context** — all of
  `A2AClient`'s discovery methods made async.
- **`self.sessions` memory leak on Cloud Run** — replaced with LRU+TTL.
- **`session.state` not visible to `tool_context.state`** — direct update
  before `run_async()`.
- **`is_final_response()` premature break with `FunctionTool`** — break
  only when the parts contain actual text.
- **`user_id`/`profile` propagation from `params.metadata`** — added as a
  priority-2 source.
- **`jwt` / `PyJWT` conflict** — explicit pins in `pyproject.toml`.
- **`google-adk>=2.0` incompatible with `opentelemetry>=1.39`** —
  constrained to `google-adk>=1.25.0,<2.0.0` and `opentelemetry<1.39.0`.
- **`KeySessionProviderArqitPy.getSessionKeyData()` wrong lookup** —
  primary lookup on `_session_key_identifier` (Arqit key_id).

### Changed

- **Context propagation priority chain** — documented: message.metadata
  (1) → params.metadata (2) → HTTP headers (3) → fallback (4).

---

## [0.1.0] — 2026-04-01

Initial release.

- A2A v1.0 protocol implementation (86% compliance, Rev.7)
- Application-layer encryption: AES-256-GCM + HMAC-SHA256 + HKDF
- Quantum-safe key exchange via Arqit Sparkle SKA
- Multi-framework adapter: Google ADK, LangChain/LangGraph, Semantic Kernel, Custom
- FastAPI transport layer with plain and secure endpoints
- API Key authentication system (5 modules: models, loaders, store, manager)
- Redis-backed TaskStore with LRU index and distributed lock
- AgentRegistry with heartbeat and liveness tracking
- Document generation via ADK `tool_context.save_artifact()` pattern