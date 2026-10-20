# Changelog

All notable changes to **stls_a2a** are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed

- Translated remaining Italian docstrings/comments/log messages in
  `src/stls_a2a/` to English. No logic changes, string literals only.

---

## [0.1.4] — 2026-07-30

### Added

- **SSE heartbeat** (`STLS_SSE_HEARTBEAT_SECONDS`, default 15s; `0` = disabilitato)
  in `process_plain_stream()` / `process_secure_stream()`, sia sul path
  streaming (`adapter.stream()`) sia sul fallback sincrono
  (`adapter.handle_request()`). Emette periodicamente un commento SSE
  (`: keep-alive`) mentre l'adapter è bloccato in una delega long-running
  (es. sub-agent non-streaming), senza mai perdere o interrompere il chunk
  in attesa. Risolve la disconnessione silenziosa dei client (default httpx
  120s, proxy/LB con read-timeout stretti) mentre il lavoro server-side
  prosegue fino al completamento.

- **`TaskStoreBase.touch(task_id)`** — non-abstract, default no-op per
  retrocompatibilità con implementazioni esterne. Chiamato ad ogni battito
  del heartbeat SSE: rinnova `updatedAt` (in-memory) o solo il TTL via
  `EXPIRE` (Redis — volutamente **non** un get-modify-save, per non
  sovrascrivere update concorrenti applicati via script Lua). Evita che un
  job più lungo del TTL venga rimosso dal cleanup mentre è ancora in corso.

- **`ERR_SESSION_EXPIRED` (`-32004`)** e **`SessionExpiredError`** —
  codice/eccezione dedicati a una sessione STLS sconosciuta o scaduta e
  **non rigenerabile**. Per il provider PQC è l'unico caso possibile: la
  `session_key` nasce da un KEM effimero al momento dell'handshake, non è
  derivabile dal solo `session_id` come invece accade (lazy) per
  `base`/`arqit_py`. Propagato con shape JSON-RPC standard e HTTP 401 su
  tutti gli handler (sync `/secure-a2a`, SSE, plain) al posto del blob
  generico `{'error': str(e)}` precedente — permette a un client
  STLS-aware di distinguere "ri-fai l'handshake e ritenta" da un errore
  applicativo permanente.

- **`stls_a2a_secure_remote.py`** — nuovo modulo per la delega **intra-agent
  cifrata**, analogo di `ContextualRemoteA2aAgent` ma con traffico su
  `/secure-a2a` invece che sul plain `/a2a`:
  - **`SecureSessionManager`** — cache di sessioni PQC per `base_url`, con
    handshake lazy e `asyncio.Lock` anti-doppio-handshake concorrente.
    L'handshake viene sempre eseguito alla **radice** dell'host
    (`/stls/pqc/handshake` è un endpoint globale per app, non prefissato
    per-agente); gli endpoint dati (`_secure_url`/`_stream_url`) vengono
    ricostruiti sul `base_url` effettivamente richiesto — che può includere
    un prefisso multi-agente (es. `/agents/agent-b`) — senza rifare la
    negoziazione crittografica già completata.
  - **`STLSHttpxTransport`** (`httpx.AsyncBaseTransport`) — intercetta in
    trasparenza le richieste JSON-RPC dirette al plain endpoint di un
    agente STLS, le cifra e le rigira su `/secure-a2a` (o
    `/secure-a2a/stream`), decifra la risposta. Passthrough per discovery e
    body non-JSON-RPC. Path SSE ricostruito byte-per-byte compatibile con
    l'endpoint plain (stesso envelope JSON-RPC 2.0, `artifactId`,
    `timestamp`). Retry-once su `ERR_SESSION_EXPIRED` sul path sincrono
    (sempre sicuro, nessun byte ancora emesso) e sul path SSE **solo** se
    l'errore è il primissimo evento del generatore ("priming" pre-Response:
    consumato prima di costruire la `httpx.Response`, quindi ancora zero
    byte verso il chiamante) — mai a metà stream, dove potrebbero già
    essere stati inoltrati eventi reali a valle.
  - **`ContextualSecureRemoteA2aAgent`** — drop-in replacement di
    `RemoteA2aAgent` che compone propagazione del contesto (identica a
    `ContextualRemoteA2aAgent`) e trasporto cifrato via
    `STLSHttpxTransport`. Rifiuta esplicitamente `httpx_client=` passato a
    mano (verrebbe sovrascritto silenziosamente).

- **`server_context_test.py`**: flag `--secure` per switchare la delega di
  test A→B tra `ContextualRemoteA2aAgent` (plain, default) e
  `ContextualSecureRemoteA2aAgent` (cifrata, handshake PQC lazy verso B).
  **`agent_context_test_a_secure.py`** — gemello cifrato di
  `agent_context_test_a.py`, stesso scenario di propagazione contesto, per
  confronto diretto del traffico osservato.

### Changed

- **`stream_secure_message()`** (client SSE cifrato) e **`A2AClient.stream()`**
  (client SSE plain) — timeout di default `120.0s → 300.0s`, con
  `httpx.Timeout` strutturato (`connect=10, write=30, pool=10`, solo il
  `read` esteso). Con il server ≥0.1.4 che emette keep-alive periodico, il
  read timeout scatta ormai solo se il server è realmente morto, non più
  durante un job lungo ma vivo.

- **`TASK_STORE_TTL_SECONDS`** — default `180s → 900s` (in-memory, Redis,
  factory) — margine più realistico per job long-running, specie ora che il
  `touch()` da heartbeat lo rinnova comunque durante l'esecuzione.

- **Timeout esplicito** (`connect=10, read=120`) sul fetch di `FileReference`
  URI in `stls_a2a_google_adapter.py` — era il default httpx di 5s,
  insufficiente per il download di file remoti anche solo moderatamente
  grandi.

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

- **`SecureSessionManager`: stesso base_url (con prefisso) riusato anche per
  l'handshake PQC** — che invece vive sempre alla radice dell'app
  (endpoint globale, registrato una sola volta per processo, mai
  prefissato per-agente). Causava 404 sull'handshake stesso in ogni
  deployment multi-agente, anche dopo il fix del punto precedente.

- **`_card_from_adk()`: tool con nome `<function foo at 0x7f...>` invece
  del nome reale** — `getattr(tool, "name", str(tool))` presuppone che
  `agent.tools` contenga sempre oggetti già wrappati (`FunctionTool`).
  Con `Agent(tools=[my_function])`, ADK a seconda della versione non
  ri-avvolge la funzione grezza prima di esporla su `agent.tools`:
  `getattr` non trova `.name`, cade su `str(tool)` — un repr che cambia
  ad ogni processo (contiene l'indirizzo di memoria) e non serve a
  nulla nella card pubblica. Nuovo helper `_tool_identity()`: fallback
  `.name` → `.__name__` (funzione grezza) → `str(tool)`; per la
  description, `.description` → prima riga del `.__doc__` → stringa
  vuota.

### Security

- **Card pubbliche non menzionano più il branding "Sparkle SKA"** —
  `_build_stls_extension()`/`_get_security_scheme()` includevano la
  stringa `"Sparkle SKA"` nelle description e nel nome della classe
  `SparkleSKASecurityScheme`. Il provider attivo (`keyProvider` /
  `key_provider`) **resta visibile** in chiaro nella card, invariato —
  è informazione legittima per l'interoperabilità (un client può volersi
  accordare su quale meccanismo di scambio chiave supportare); il
  problema era solo il nome prodotto Arqit, non il fatto di indicare
  quale provider è in uso.

- **`SparkleSKASecurityScheme` → `StlsKeyExchangeSecurityScheme`,
  `type: "sparkle_ska"` → `"stls_key_exchange"`** — non solo un problema
  di branding: il nome "Sparkle SKA" (prodotto Arqit) era semanticamente
  sbagliato anche per il caso `pqc`, che con Arqit non ha alcuna
  relazione (X25519 + ML-KEM-768 è un meccanismo indipendente).
  **Breaking change deliberato**: chi già parsava
  `securityScheme.type == "sparkle_ska"` deve aggiornarsi al nuovo
  valore.

- **URI dell'estensione STLS**: `https://stls-ai.tisparkle.com/...` →
  `https://stls-ai.com/...` — rimosso il sottodominio aziendale
  dall'identificatore pubblico dell'estensione. Stesso cambiamento
  applicato anche a `stls-mcp` (`STLS_EXTENSION_URI`), per coerenza tra
  i due protocolli.

### Note operative

- **`SessionCache` persistente (Redis) + provider deterministico** (`base`:
  `session_id` sempre fisso `"base-session-key-identifier"`) può mascherare
  un mismatch di `KEY_PROVIDER_NAME` tra due lanci diversi dello stesso
  agente: una sessione `base` cachata prima del cambio provider resta
  valida per cache-hit anche dopo un riavvio con `KEY_PROVIDER_NAME=pqc`,
  perché il cache-hit bypassa `key_session_provider` interamente. Non un
  bug applicativo — ma da tenere presente: **flushare la `SessionCache`
  quando si cambia provider tra un giro di test e l'altro**
  (`redis-cli --scan --pattern 'stls:sc:*' | xargs redis-cli DEL`).

- **Handshake PQC condiviso in scenari multi-agent-per-app** (fixme, non
  ancora risolto): `/stls/pqc/handshake` è registrato una sola volta per
  app (guard su `app.routes`), e il closure cattura `middleware.sessions`
  del **primo** agente registrato. Se più agenti PQC condividono lo stesso
  processo FastAPI, solo il primo vede le sessioni popolate
  dall'handshake. Non impatta i deployment a un agente per processo (es.
  `server_context_test.py`).

---

## [0.1.3] — 2026-06-26

### Added

- **`create_pqc_a2a_transport(base_url, *, client_id, handshake_path, httpx_client)`**
  (`stls_a2a_integration.py`) — helper client-side per il provider PQC.
  Sostituisce la sequenza manuale (provider → kid → key →
  `SecureA2ATransportAdapter`) con una chiamata singola che esegue
  internamente il KEM handshake X25519 + ML-KEM-768: genera i keypair
  client-side, invia `PQCHandshakeRequest` all'endpoint
  `/stls/pqc/handshake`, deriva la `session_key` via HKDF-SHA256,
  verifica il proof di possesso e azzera le chiavi private. Restituisce
  un `SecureA2ATransportAdapter` pronto per `send_secure_message()` /
  `stream_secure_message()`. Non richiede una chiave pre-condivisa.
  Re-esportato dal package pubblico (`from stls_a2a import
  create_pqc_a2a_transport`). Richiede `liboqs-python` (`[pqc]` extra).

- **Endpoint `/stls/pqc/handshake`** in `create_secure_a2a_endpoint()` —
  quando `KEY_PROVIDER_NAME=pqc`, la funzione registra automaticamente il
  route `POST /stls/pqc/handshake` che chiama
  `stls_core.perform_pqc_handshake()`. L'endpoint usa la `SessionCache`
  interna del middleware (`middleware.sessions`) così le sessioni PQC sono
  immediatamente riconoscibili da `get_transport()` senza passare per la
  risoluzione lazy del provider. Il route è registrato **una sola volta**
  per app (guard su `app.routes`): più agenti che chiamano
  `create_secure_a2a_endpoint()` sullo stesso server non duplicano il
  handler. **Nota multi-agent con memory backend**: con
  `SESSION_CACHE_BACKEND=memory`, l'endpoint è tied alla `SessionCache`
  del primo agente registrato. Per multi-agent PQC (client che chiamano
  agenti diversi con la stessa sessione) usare
  `SESSION_CACHE_BACKEND=redis` — che è già il requisito standard per
  qualunque deployment Cloud Run multi-replica.

- **Supporto PQC in `stls_a2a_utils.py`**:
  - `_get_security_scheme()` — aggiunto case `"pqc"`: restituisce
    `SparkleSKASecurityScheme(key_provider="pqc", description="Quantum-safe
    key exchange via X25519 + ML-KEM-768 (NIST FIPS 203)")`.
  - `_build_stls_extension()` — per `KEY_PROVIDER_NAME=pqc` aggiunge
    `keyDerivation: "HKDF-SHA256+X25519+ML-KEM-768"` e
    `handshakeEndpoint: "/stls/pqc/handshake"` nei parametri
    dell'estensione STLS nell'`AgentCard`.

- **Extra `[pqc]`** in `pyproject.toml` — pass-through verso
  `stls_core[pqc]` (`liboqs-python`). Installazione:
  `uv pip install -e ".[pqc]"`.

- **Supporto PQC nei test**:
  - `task_test.py` — rilevamento `KEY_PROVIDER_NAME=pqc` a module level:
    per `base`/`arqit_py` la session key è risolta al lancio come prima;
    per `pqc` viene impostato un placeholder e il KEM handshake avviene
    all'inizio di `task_test()` via `create_pqc_a2a_transport()`, poi
    `session_key_identifier` e `session_key_data` vengono popolati per
    riutilizzo in tutti i `call_agent()` successivi (invariati).
  - `key_providers_test.py` / `key_providers_test_en.py` — `open_session()`
    ora è PQC-aware: se `KEY_PROVIDER_NAME=pqc` chiama
    `create_pqc_a2a_transport()` ed estrae `(session_id, transport.session_key)`
    invece di usare `KeySessionProviderFactory`. Signature di ritorno
    `(kid, kdata)` invariata — `stream_call()` e tutto il resto del test
    rimangono immutati.

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

- **Handshake con proof-of-key-possession ora disponibile per il provider
  PQC** — `create_secure_a2a_endpoint()` registra `/stls/pqc/handshake`
  quando `KEY_PROVIDER_NAME=pqc`. Il flusso usa lo stesso HMAC
  challenge/response di `stls-core` / `stls-mcp`: session_id generato
  server-side (UUID, non enumerabile), proof = HMAC-SHA256(session_key,
  nonce), nessun key material in transito. Per `base` e `arqit_py`, il
  flusso lazy esistente (session_id embedded nel primo envelope cifrato,
  risoluzione del provider su cache miss) rimane invariato — nessun
  breaking change per i deployment esistenti. La convergenza sull'handshake
  esplicito anche per `base`/`arqit_py` è tracciata come lavoro futuro.

---

## [0.1.2] — 2026-06-17

### Added

- **`FileInline.name`** — nuovo campo opzionale per propagare il nome file
  originale attraverso il protocollo A2A.

- **Propagazione `display_name` nel `Blob`** (`stls_a2a_google_adapter.py`,
  `_to_genai_parts`) — legge `FileInline.name` e lo imposta come
  `display_name` del `Blob` genai.

- **`SecureA2ATransportAdapter._maybe_process_csv_key()`** — post-processing
  dei chunk di risposta in `process_secure_stream` per il tool
  `export_sim_csv`.

---

## [0.1.1] — 2026-05-30

### Added

- **`stls_a2a_remote.ContextualRemoteA2aAgent`** — drop-in replacement di
  `RemoteA2aAgent` con propagazione automatica di `user_id` e `profile` da
  `session.state` tramite `a2a_request_meta_provider`.

- **`SecureA2ATransportAdapter.stream_secure_message()`** — vero streaming
  incrementale, chunk per chunk.

- **Header HTTP `X-STLS-User-Id` / `X-STLS-Profile`** — canale di
  propagazione del contesto utente a priorità 3.

- **`pyproject.toml` extra `[examples]`** — `yfinance` spostata a extra
  opzionale.

- **`KeyProviderArqitPy`** — provider quantum-safe via SDK Python nativo
  Arqit SKA-Platform™, senza JVM/JPype.

- **`pyproject.toml` extra `[arqit_py]`** — 7 wheel proprietari Arqit.

### Fixed

- **`pyproject.toml` TOML malformato** — mancava `[build-system]`.
- **Conflitto `langgraph-prebuilt`** — stack LangChain allineato.
- **`_build_parts()` mutazione dict in-place** — sostituita con lettura
  non-distruttiva.
- **`_run_in_background()` crash su `result["parts"]`** — aggiunto
  `_normalize_result()` con fallback.
- **`_card_from_llm()` campi errati nell'AgentCard** — `default_input_modes`
  / `default_output_modes` al posto di `input_modes` / `output_modes`.
- **`_fetch_cards()` sincrono in contesto async** — tutti i metodi discovery
  di `A2AClient` resi async.
- **`self.sessions` memory leak su Cloud Run** — sostituito con LRU+TTL.
- **`session.state` non visibile a `tool_context.state`** — aggiornamento
  diretto prima di `run_async()`.
- **`is_final_response()` break prematuro con `FunctionTool`** — break solo
  quando le parts contengono testo effettivo.
- **Propagazione `user_id`/`profile` da `params.metadata`** — aggiunta come
  sorgente a priorità 2.
- **Conflitto `jwt` / `PyJWT`** — pin espliciti nel `pyproject.toml`.
- **Incompatibilità `google-adk>=2.0` con `opentelemetry>=1.39`** — vincoli
  `google-adk>=1.25.0,<2.0.0` e `opentelemetry<1.39.0`.
- **`KeySessionProviderArqitPy.getSessionKeyData()` lookup errato** —
  lookup primario su `_session_key_identifier` (key_id Arqit).

### Changed

- **Priorità catena context propagation** — documentata: message.metadata
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