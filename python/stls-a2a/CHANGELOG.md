# Changelog

All notable changes to **stls_a2a** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.12] — 2026-11-27

Issue #65 audit: `google-adk`, `langchain`/`langchain-community`/
`langchain-openai`, `langgraph`, and `semantic-kernel` were declared
as MANDATORY dependencies while the code already treated each one as
optional in several places (lazy, guarded imports with
"pip install stls_a2a[google-adk]"-style messages pointing at extras
groups that didn't exist). Fixed both directions: the extras groups
now actually exist, and the remaining unconditional import chain
(`__init__.py` → `stls_a2a_integration.py` → `stls_a2a_adapters.py` →
`stls_a2a_google_adapter.py`/`stls_a2a_sk_adapter.py`) that WAS
genuinely requiring `google-genai`/`semantic-kernel` at `import stls_a2a`
time, contradicting that same intent, is now lazy too.

### Changed (breaking)

- `google-adk[a2a]`, `google-genai`, `opentelemetry-api`, `opentelemetry-sdk`,
  `langchain`, `langchain-community`, `langchain-openai`, `langgraph`,
  and `semantic-kernel` are no longer installed by `pip install stls_a2a`
  alone — moved to new optional extras: `stls_a2a[google-adk]`,
  `stls_a2a[langchain]`, `stls_a2a[langgraph]`, `stls_a2a[semantic-kernel]`.
  **Migration**: add the extra(s) your code actually uses, or
  `stls_a2a[all]` to keep the exact previous behavior (everything
  installed).
- `GoogleADKAdapter`/`SemanticKernelAdapter` are now imported lazily
  inside `create_adapter()` (`stls_a2a_adapters.py`), not at module
  level — `import stls_a2a` no longer requires either framework.
  `LangChainAdapter`/`CustomFunctionAdapter` were already safe (no
  hard dependency at their own module level) and needed no change.

### Fixed

- `stls_a2a_card_model.py`/`stls_a2a_utils.py` called bare
  `load_dotenv()` at module import time. `python-dotenv` searches
  upward from the *library's own* file location, so this resolved to
  this package's own dev `.env` (e.g. `PORT=8001`,
  `KEY_PROVIDER_NAME=pqc`) and silently injected those values into
  `os.environ` of ANY process that imports `stls_a2a` — overriding
  env vars the importing application never meant to share, with no
  indication anything happened. The root cause was already documented
  from a previous incident (`KEY_PROVIDER_NAME` override, found
  2026-11-07) but only patched in one consumer at the time, not fixed
  at the source. Both `load_dotenv()` calls are removed entirely —
  loading `.env` files is an application's own concern, not a
  library's. `stls_a2a_card_model.py`'s `KEY_PROVIDER_NAME` module
  constant was also dead code (assigned, never read anywhere) and
  removed with it. `stls_a2a_utils.py`'s copy (actually used, in
  `_build_stls_extension`/`_get_security_scheme`) is now read via
  `os.getenv("KEY_PROVIDER_NAME")` at call time instead of cached once
  at import time, matching the pattern already used elsewhere in this
  package (`stls_a2a_integration.py`, `stls_a2a_secure_remote.py`).
  Found while re-testing samples against this release's updated
  dependencies — `samples/server/19_identity_presentation`'s server
  silently relied on the leaked `KEY_PROVIDER_NAME=pqc` and never
  declared that dependency itself; fixed there too (now passes
  `name="pqc"` explicitly to `KeySessionProviderFactory` and sets
  `KEY_PROVIDER_NAME=pqc` via `os.environ.setdefault(...)` before
  anything PQC-dependent is imported).
- CI (`python-tests.yml`, `publish-python.yml`): `tests/google_adapter_error_surfacing_test.py`
  imports `google.adk.agents` directly (not through this package's own
  lazy-import wrapper, since it's constructing a real `Agent`/
  `InMemoryRunner` for the test itself) — once `google-adk` became an
  optional extra, CI's plain `pip install -e ".[dev]"` stopped
  installing it, and that one file's `ModuleNotFoundError` aborted the
  *entire* test session at collection time (pytest treats a collection
  error as fatal to the whole run, not just that file). Added
  `pytest.importorskip("google.adk")` so it degrades to a clean skip
  instead — the same fragility class this whole issue is about, just
  in a test file instead of library code. Both workflows now also
  install `stls_a2a[dev,google-adk]` specifically for this package
  (others stay `[dev]`-only), so the test actually runs in CI rather
  than permanently skipping there.

### Security

Vulnerability floors raised (checked via PyPI's vulnerability API
against both the previous floor and latest, as of 2026-11-27):

- `PyJWT`: `>=2.8.0` → `>=2.15.1` — the old floor was vulnerable to a
  long string of real CVEs across 2.8.0–2.14.x (header `crit` bypass,
  `PyJWKClient` SSRF/DoS, signature-verification issues). This package
  stays mandatory (used directly, not just via `google-adk`).
- `semantic-kernel` extra: `>=1.36.0` → `>=1.39.4` — **CVE-2026-26030
  is a remote code execution vulnerability**, plus CVE-2026-25592,
  both fixed by 1.39.4.
- `langchain` extra: `>=1.3.2` → `>=1.3.9` — fixes CVE-2026-55443.
- `google-adk` extra: `>=1.25.0` → `>=1.28.1` (ceiling `<2.0.0`
  unchanged) — fixes CVE-2026-4810.
- `opentelemetry-api`/`-sdk` ceiling (`<1.39.0`) **left unchanged,
  deliberately** — CVE-2026-114603 (fixed 1.45.1) and CVE-2026-126360
  (fixed 1.45.2) remain genuinely unpatched in this dependency set.
  Confirmed via `google-adk`'s own published metadata
  (`opentelemetry-api/-sdk>=1.36,<=1.41.1`) that even the latest 1.x
  `google-adk` release caps otel below the fixed versions — the actual
  fix requires `google-adk>=2.0`, a separate, higher-risk migration
  (ADK's 2.x async-runner rewrite is exactly what the `google-adk<2.0`
  ceiling already guards against — see e.g. still-open
  `google/adk-python#3708`/`#3692` for the same class of
  OpenTelemetry-context-across-asyncio-task bug). Documented here as a
  known, visible, residual exposure rather than left as a silent one.
- `google-genai`, `fastapi`, `psycopg2-binary`, `httpx`,
  `langchain-community`, `langchain-openai`, `langgraph`: already
  clean at their existing floors, no change.

### `uv.lock` regenerated

Found via a GitHub Dependabot scan after this branch merged:
`uv.lock` had never been regenerated since the original repo import —
it still pinned `pyjwt==2.11.0`, `semantic-kernel==1.36.0`,
`google-adk==1.25.0`, `langchain==1.2.10`, the exact pre-#65 vulnerable
versions, regardless of what the floors above now say in
`pyproject.toml`. Anyone installing via the lockfile (`uv sync`)
instead of a plain `pip install` got the old versions despite this
release's own floor raises. Regenerated with `uv lock --upgrade`,
which also resolved most of the ~180 Dependabot alerts that turned
out to be the same root cause, one call site removed (transitive
dependencies of `google-adk`/`langchain`/`semantic-kernel` that were
similarly stuck on year-old pins): `authlib`, `aiohttp`, `cryptography`,
`mcp`, `pyasn1`, `soupsieve`, `sqlparse`, `pyarrow`, `pyopenssl`,
`python-multipart`, `requests`, `urllib3`, `idna`, `python-dotenv`,
`pydantic-settings`, and more all moved to patched versions.

Along the way, `--upgrade` combined with `--prerelease=allow` (needed
for `azure-ai-agents`, see below) let the resolver drift two unrelated
packages onto stray pre-releases too (`authlib` → `1.10.0b1`, `redis`
→ `8.2.1b1`) — fixed by pinning `azure-ai-agents` to an exact version
instead of relying on the global flag (see the `semantic-kernel` extra
in `pyproject.toml`), which also means `uv pip install` no longer
needs `--prerelease=allow` for that extra at all — the "Known issue"
this release used to document above is resolved.

One residual: `werkzeug` stayed at `3.1.1` (patched: `3.1.6`) even
under `--upgrade-package werkzeug` with a cleared cache — pulled in
transitively via `semantic-kernel` → `openapi-core`, whose own
declared constraint (`werkzeug>=2.1.0`) doesn't explain the ceiling;
not chased further; MODERATE severity, three levels removed from any
code this package actually calls.

## [0.1.11] — 2026-11-20

Issue #15 gap #1 — the VP wire format — plus two real bugs found
while testing its follow-up (issue #45: sofia-admin's coordinator
calling a Google-ADK-based sub-agent), fixed in
`stls_a2a_google_adapter.py`. The adapter fixes are not
identity-specific. See [`python/stls-identity` 0.1.1](../stls-identity/CHANGELOG.md)
for the identity-engine side of the gap #1 change.

### Added

- `X-STLS-Identity-Presentation` request header, carrying a
  Verifiable Presentation signed over a server-minted challenge
  independent of the key-session provider (pqc/base/arqit_py).
- `identity_verifier` (server, `create_secure_a2a_endpoint()`) /
  `identity_challenge_handler` (client, `STLSHttpxTransport`) hooks —
  duck-typed, `stls-a2a` never imports `stls_identity`. Both default
  to `None`: zero behavior change for every existing caller.
- Retry-once client flow mirroring the existing `ERR_SESSION_EXPIRED`
  behavior: new `ERR_IDENTITY_CHALLENGE_REQUIRED` (`-32005`).

### Security

- Fixed an SSRF in `GoogleADKAdapter._to_genai_parts()`: a
  `FileReference.uri` that wasn't `gs://` or a real youtube.com host
  fell through to an unrestricted `httpx.get(uri)` — any caller of
  `messages/send`/`messages/sendStream` could make this server fetch
  an arbitrary URL on their behalf (cloud instance metadata endpoints,
  internal services) and have the response fed into the model. Added
  `_assert_safe_remote_file_url()`: restricts the scheme to http(s)
  and resolves the host, rejecting any address that is loopback/
  link-local/private/reserved/multicast/unspecified (catches DNS
  rebinding too, since the resolved IP is checked, not the hostname
  string); the download client also now sets `follow_redirects=False`
  explicitly so a safe URL can't redirect past the check.

### Fixed

- `create_secure_a2a_endpoint(enable_plain_endpoint=False, base_url=...)`
  crashed for every agent using that combination.
- `GoogleADKAdapter.invoke()`/`_invoke_stream()` checked
  `hasattr(event, 'is_error') and event.is_error()` to detect a failed
  sub-agent call — `google.adk.events.event.Event` has no
  `is_error()` method, so the check was permanently `False`, dead
  code. A sub-agent failure (e.g. a 401 from an identity-protected
  endpoint) now surfaces its real `error_message` instead of falling
  through to a generic "No response received from agent" (`invoke()`)
  or being dropped with zero trace at all (`_invoke_stream()`, the
  path a real chat UI like `sofia-client-secure` actually uses).

## [0.1.10] — 2026-11-19

No code changes in this package. Documents the cross-process
`arqit_py` device-identity conflict found and fixed in
[`python/stls-core` 0.1.10](../stls-core/CHANGELOG.md) (issue #38) in
this README's own "Current Limitations and Notes" section —
`SecureSessionManager` picks up `ARQIT_PY_DEVICE_ROLE=consumer` and
the new device-level owner lock automatically, since both live inside
`KeySessionProviderFactory.get_session_provider()`.

## [0.1.9] — 2026-11-17

No code changes in this package. Raises the `stls_core` dependency
floor for the widened handshake proof fix (issue #36, see
[`python/stls-core` 0.1.9](../stls-core/CHANGELOG.md)) — a breaking
wire change affecting the PQC handshake
(`PQCHandshakeRequest`/`PQCHandshakeResponse`/`perform_pqc_handshake`)
this package imports from `stls_core`.

### Security

- Raised the `stls_core` dependency floor to `>=0.1.9` (was `>=0.1.5`).

---

## [0.1.8] — 2026-11-14

Part of the A2A protocol conformance assessment (issue #14) — closes
all four gaps it identified (JSON-RPC method naming, the AgentCard
schema, the streaming capability-flag bug, the cosmetic
generic-card-name fallback), plus nine more found by running real
`a2a-sdk` clients (both 0.3.26 and 1.3.0) against this server while
building the issue's second half (testing against more complex
agents): a non-oneof-wrapped `SendMessage` result, discriminator-less
message parts being silently dropped, `securitySchemes` breaking the
0.3 SDK's card resolution entirely, SSE streaming events not being
oneof-wrapped either (plus a related task-state enum mismatch), a
matching `Message.role` enum mismatch, `ListTasks`'s result not being
wrapped at all, `GetTask`/`CancelTask`/`SubscribeToTask` rejecting
every real client outright over a request field-name mismatch, and
file parts (`FilePart`) being unparseable in both directions — dropped
on the way in, empty on the way out.

### Added

- **A2A 1.0 JSON-RPC method names** (spec §9.4) — `SendMessage`,
  `SendStreamingMessage`, `GetTask`, `ListTasks`, `CancelTask`,
  `SubscribeToTask`, `Create/Get/List/DeleteTaskPushNotificationConfig`.
  1.0 moved the JSON-RPC binding to PascalCase names shared with
  gRPC/REST, breaking from 0.3's slash-style names
  (`messages/send`, `tasks/get`, ...) — which every dispatch point in
  this package still matches on internally. The 1.0 names are
  normalized to their 0.3 equivalent in a single place
  (`_A2A_1_0_METHOD_ALIASES` in `stls_a2a_adapters.py`); SSE methods
  are recognized directly in `SSE_STREAM_METHODS`/`SSE_SEND_METHODS`/
  `SSE_SUBSCRIBE_METHODS` (`stls_a2a_integration.py`). Purely additive:
  0.3 clients are unaffected.
- `tasks/pushNotificationConfig/list` and `.../delete` (and their 1.0
  names) now resolve to the same `ERR_PUSH_NOTIFICATION_NOT_SUPPORTED`
  response as `set`/`get`, instead of falling through to "method not
  found" — push notifications aren't implemented yet, but the method
  names are now all recognized consistently.
- **A2A 1.0 AgentCard fields** (spec §4.4.1) — `AgentCard.supportedInterfaces`
  (`stls_a2a_card_model.py`'s new `AgentInterface` model: `url`,
  `protocolBinding`, `protocolVersion`) and `securitySchemes` (a map,
  vs. the existing singular `securityScheme`). Both are auto-derived
  from the pre-existing `url` and `securityScheme` fields via a
  `model_validator`, so every `_card_from_*()` builder in
  `stls_a2a_utils.py` produces spec-conformant output with no changes
  of its own.
- `tests/a2a_protocol_conformance_test.py` — regression suite asserting
  0.3 names/pre-existing card fields are unchanged and 1.0 names/card
  fields produce the spec-conformant shape, in-process via
  `httpx.ASGITransport` (no server process needed). Extended with a 6th
  group that re-exercises the SSE path and checks the result matches
  what `capabilities.streaming` claims, and a 7th asserting the card's
  generic-agent name isn't the useless `"function"` (see Fixed, below).
- `samples/client/sofia-client-secure`: the debug `AgentCard` viewer
  now has its own "Security Schemes (A2A 1.0)" section for the new
  `securitySchemes` field (previously it would've landed in the
  generic "Extra fields" catch-all). Purely additive — nothing removed
  or renamed; `tsc --noEmit` clean.
- `samples/client/a2a_sdk_1_0` — a real `a2a-sdk` 1.x client (the major
  version implementing A2A 1.0's protobuf-based wire format) against
  `samples/server/4_custom_financial_agent`, in its own isolated venv
  (1.x can't coexist with the 0.3.26 install `samples/client/a2a_sdk`
  depends on in the shared root venv). Running it is what found gaps
  #5 and #6 below. Real SSE streaming (`ClientConfig(streaming=True)`)
  is now also fully verified against it — see the gap #7 fix below.
- Extended `tests/a2a_protocol_conformance_test.py` to 14 groups: an
  8th (checks `messages/send`'s result is task-wrapped), a 9th (checks
  a part with neither `"type"` nor `"kind"` isn't dropped), a 10th
  (checks SSE events are oneof-wrapped with correct `TASK_STATE_*`
  state names), an 11th (checks history `role` uses the proto enum
  name inside the `"task"` wrapper), a 12th (checks `ListTasks`'s
  result is the wrapped object, not a bare array), a 13th (checks
  `GetTask`/`CancelTask`/`SubscribeToTask` accept `"id"`), and a 14th
  (checks a flat `FilePart` round-trips in both directions) — all as
  plain httpx/JSON assertions, so the regressions are caught without
  needing the real `a2a-sdk` installed in this venv.

### Fixed

- **`capabilities.streaming` now reflects actual behavior.**
  `_card_from_adk`, `_card_from_langgraph`, and `_card_generic`
  (`stls_a2a_utils.py`) hardcoded `streaming=False` regardless of the
  framework, even though `GoogleADKAdapter` streams natively and every
  other adapter inherits a genuinely-working SSE fallback from the
  base `AgentAdapter.stream()` (`stls_a2a_adapters_model.py`). Verified
  live before and after: `messages/sendStream`/`SendStreamingMessage`
  produced an identical, correct SSE lifecycle
  (`submitted`→`working`→`completed`) in both cases — only the card's
  claim about it was wrong. Clients that honored the (incorrect) flag
  by skipping streaming now get to use it; nothing that worked before
  stops working.
- **Generic agent cards no longer report the name `"function"`.**
  `_card_generic()`'s name fallback was `getattr(agent, "name",
  type(agent).__name__)` — for a plain Python function with no `.name`
  attribute (the `'custom'` framework's typical shape, e.g. the
  `calculator_handler` wrapper in `multi_agent_server.py`), `type(x)`
  of any function is literally the class `function`, so every such
  agent's card reported the same useless, non-distinguishing name.
  Now tries the function's own `__name__` first — same fallback chain
  `_tool_identity()` already used for tools, applied here for the same
  reason. `samples/server/4_custom_financial_agent` already worked
  around this independently (by passing a class instance with `.name`
  set instead of a bare function) — unaffected by this fix, since
  `.name` was already set there and takes priority either way.
- **`messages/send`'s result is now wrapped in a `"task"` key, matching
  A2A 1.0's oneof.** Spec §9.4.1 defines `SendMessageResponse` as a
  `oneof {task, message}` — a real `a2a-sdk` 1.3.0 `Client` raised
  `"Response has neither task nor message"` against this server,
  because the result had every `Task` field spread flat at the top
  level with no `"task"` key at all. `handle_request()`'s
  `messages/send` branch (`stls_a2a_adapters_model.py`) now adds a
  `"task"` key nesting the same fields, alongside the flat shape kept
  for existing consumers (`stls_a2a_client.py`, ADK's
  `RemoteA2aAgent`) — additive only, verified both ways still work.
- **Message parts with neither `"type"` nor `"kind"` are no longer
  silently dropped.** A real A2A 1.0 client's proto-JSON mapping sends
  a text part as the bare `{"text": "..."}` — no discriminator key at
  all, since the `oneof`'s selected field name is itself the only
  discriminator. `_build_parts()` treated this as an empty part and
  skipped it; even where kept, `_handle_messages_send`/
  `_handle_messages_send_stream` passed adapters `request.params`
  verbatim, so an adapter's own ad-hoc parsing (e.g.
  `financial_agent.py`'s `(p.get("kind") or p.get("type")) == "text"`)
  still missed it — **with no error of any kind**: the agent just saw
  an empty message and replied with its generic fallback. Fixed in
  two places: `_build_parts()` now infers the part's type from which
  known key (`text`/`data`/`file`) is actually present, and both
  `_handle_messages_send` and `_handle_messages_send_stream` now
  substitute the already-normalized parts (which always carry both
  `"type"` and `"kind"`, from the `TextPart`/`FilePart`/`DataPart`
  models) into what the adapter actually receives, instead of the
  raw, as-received ones. Verified against the real SDK: a "What is
  the price of AAPL?" query that previously got the generic fallback
  now returns the real looked-up price, through the same server code
  path real agents use.

- **`securitySchemes` no longer breaks the official 0.3 SDK's card
  resolution.** `SecurityScheme` is a CLOSED union in both the 0.3 and
  1.0 spec (`apiKey`/`http`/`oauth2`/`openIdConnect`/`mutualTLS` — no
  custom/extension variant in either). Our own
  `StlsKeyExchangeSecurityScheme` ("stls_key_exchange") isn't one of
  them, and the initial version of `securitySchemes` auto-population
  (above) included it anyway — which is every agent's *default* scheme
  in this project. Found by running a real `a2a-sdk` 0.3.26
  `A2ACardResolver` against a live agent: it didn't just warn, it
  **hard-failed card resolution entirely**, breaking every 0.3 client
  against every agent in this repo, including the already-existing
  `samples/client/a2a_sdk` sample. `AgentCard._populate_spec_fields()`
  now only populates `securitySchemes` when `security_scheme.type` is
  one of the 5 official values; for our own extension type the field
  is correctly omitted (spec-legal — it's optional in both versions)
  rather than populated with something no real client can parse.
  Verified against the real 0.3 SDK before and after: card resolution
  now succeeds again, full real tool-calling confirmed through
  Semantic Kernel and LangChain/LangGraph agents (not just the
  deterministic custom-framework fixture used for gaps #5/#6).
- **SSE streaming events are now oneof-wrapped too, with the correct
  task-state enum names.** Same root cause as the `SendMessage` fix
  above, for the streaming path: A2A 1.0's `StreamResponse` is a
  `oneof {task, message, statusUpdate, artifactUpdate}` (spec §4.2),
  but our SSE events were a flat dict with `"kind":
  "status-update"`/`"artifact-update"` — a real `a2a-sdk` 1.3.0
  `Client` got back events with **none** of the oneof's fields set.
  Reproduced identically across every framework (custom,
  LangChain/LangGraph, Semantic Kernel) — confirmed general-purpose,
  not agent-specific. `_make_status_payload`/`_make_artifact_payload`
  (`stls_a2a_integration.py`) now also nest the same data under
  `"statusUpdate"`/`"artifactUpdate"`, additive alongside the flat
  shape kept for existing consumers (`sofia-client-secure`'s SSE
  parsing reads only the flat `kind`/`final`/`status.state` fields,
  confirmed unaffected).

  Fixing this surfaced a second, related issue: A2A 1.0's `TaskState`
  proto enum uses `TASK_STATE_*` names (e.g. `"TASK_STATE_COMPLETED"`),
  not our lowercase values — a real client silently defaults
  `status.state` to `TASK_STATE_UNSPECIFIED` otherwise (protobuf's
  JSON unmarshal ignores unrecognized enum strings rather than
  erroring). This matters more than it sounds: the 1.0
  `TaskStatusUpdateEvent` proto has **no separate `"final"` field at
  all** — `status.state` is the *only* signal a strict client has for
  detecting task completion in a stream. New
  `to_task_state_proto_name()` (`stls_a2a_task.py`) maps our lowercase
  `TaskState` values to their proto name, used only inside the new
  nested structures (here, and in the `SendMessage` `"task"` wrapper
  above) — the pre-existing flat lowercase fields are untouched.
  Verified against the real SDK: a full streaming exchange now reports
  `TASK_STATE_SUBMITTED` → `TASK_STATE_WORKING` → (artifact) →
  `TASK_STATE_COMPLETED`, matching the proto enum exactly.
- **`Message.role` now also uses the proto enum name inside the new
  nested structures.** Same root cause as the `TaskState` issue above,
  for A2A 1.0's `Role` enum (`ROLE_USER`/`ROLE_AGENT`, spec §4.1.3,
  not our lowercase `"user"`/`"agent"`) — a real client otherwise sees
  `ROLE_UNSPECIFIED` for every history message and can't tell which
  came from the user vs. the agent. New `to_role_proto_name()`
  (`stls_a2a_task.py`), used only inside the `SendMessage` `"task"`
  wrapper (same scope restriction as `to_task_state_proto_name()`).
  `GetTask`/`CancelTask` return a bare `Task` with no wrapper to add
  this into without risking 0.3 compat (both read the exact same
  object as 0.3 clients), so their `status.state`/`history[].role`
  stay lowercase — a known, deliberate asymmetry, not an oversight.
- **`GetTask`/`CancelTask`/`SubscribeToTask` no longer reject every
  real client.** Their proto request field for the task identifier is
  `"id"` (spec §9.4.3 example: `{"id": "task-uuid", "historyLength":
  10}`), not `"taskId"` — a real `a2a-sdk` 1.3.0 `Client` sends exactly
  `"id"` and got a flat `"taskId param is required"` error for all
  three methods, regardless of the enum-naming issues above. Not a
  shape nuance — a complete functional break, the most severe of this
  batch. All three now accept either name.
- **`ListTasks`'s result is now the spec-shaped wrapped object, not a
  bare array.** A2A 1.0's `ListTasksResponse` is `{"tasks": [...],
  "pageSize": ..., "nextPageToken": ...}` (spec §9.4.4); our result was
  a bare array, which a real `a2a-sdk` 1.3.0 `Client` couldn't parse at
  all (`ParseError ... unhashable type: 'dict'`) — a hard failure, not
  a soft degradation. Also accepts the proto's `pageSize`/`pageToken`
  request fields now, alongside the existing `limit`/`cursor`. This is
  the **one fix in this release that isn't purely additive**: JSON
  can't be an array and an object with a `"tasks"` key at the same
  time, so the old bare-array shape is genuinely gone. Grep-verified
  the only in-repo consumer (`tests/task_test.py`) before changing it,
  and updated it to read `result.tasks` — no other consumer exists
  (`samples/client/sofia-client-secure` never calls this method;
  `sofia-admin`'s own `list_tasks` tool reads Redis directly, unrelated
  to this JSON-RPC method entirely).
- **File parts (`FilePart`) are now parseable in both directions.**
  A2A 1.0's `Part` proto has no `"file"` wrapper at all — `FileInline`/
  `FileReference`'s fields (`raw`/`url`/`filename`/`mediaType`) sit
  directly on `Part`, part of its `content` oneof (`text`/`raw`/`url`/
  `data`). Two separate bugs, same root cause, found and fixed
  together:
  - **Incoming:** a file part sent this way was silently dropped by
    `_build_parts()` — the exact same symptom as the text-part fix
    above, just never checked for files until now. Fixed by extending
    the same `text`/`data`/`file` key-presence inference to also
    recognize `raw`/`url`.
  - **Outgoing:** even once accepted, a `FilePart` echoed back (e.g.
    in `Task.history` after the client calls `GetTask`) was still
    serialized with *only* the old nested `{"file": {...}}` shape — a
    real client's `Part.WhichOneof('content')` came back `None`,
    effectively empty. This is the direction the text/data fixes above
    never had to address (`TextPart`'s `text` field already matches
    both conventions). Fixed via a new `model_serializer` on
    `FilePart` (`stls_a2a_task.py`) that adds the flat fields alongside
    the nested `"file"` shape — one change point, since every
    `Task.history`/artifact dump goes through this model's
    serialization. `DataPart` needed no fix — its shape already
    matched on both sides.
  Verified against the real SDK: an inline file (`raw`) and a
  URL-referenced one (`url`) both round-trip correctly now, in a
  message sent to the server and echoed back in the task's history.

### Changed

- Replaced the old `supportedInterfaces` shape — a `{jsonRpc, grpc,
  rest}` boolean-flag object that happened to reuse the A2A 1.0 field
  name (`supportedInterfaces`) for an unrelated, non-conformant type.
  Any strict 1.0 AgentCard validator rejected that shape outright, and
  nothing in this codebase read those booleans (grep-verified before
  removing the `SupportedInterfaces` class), so there was no real
  consumer to preserve.
- Kept, unchanged, everything that *is* actually read today: the
  singular `securityScheme` field (read by `stls_a2a_client.py` and
  rendered by the `sofia-client-secure` debug UI) and the `url`/
  `secureUrl` fields (read by `stls_a2a_client.py`'s `_resolve_paths()`).
  `securitySchemes`/`supportedInterfaces` are additive siblings, not
  replacements.

### Known partial gap

- For the officially-recognized scheme types that DO populate
  `securitySchemes` (see Fixed, above), the values stay in the flatter
  `{"type": "apiKey", ...}` shape already used by `security_scheme`,
  rather than the A2A 1.0 proto's nested oneof wrapper (e.g.
  `{"apiKeySecurityScheme": {...}}`). The map container itself — the
  larger structural defect — is now correct and keyed by scheme name;
  revisit the inner shape if a strict 1.0 validator flags it. Moot for
  our own default scheme, which is omitted from the field entirely.

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