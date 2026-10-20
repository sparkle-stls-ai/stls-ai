# Sofia Client — Secure

React + TypeScript chat client for A2A agents that require **encrypted transport**.  
All outbound agent calls are routed through a Python microservice (`secure_transport.py`) that wraps `SecureA2ATransportAdapter` from `stls_a2a`.

> This project is a security-hardened fork of **sofia-client**.  
> Do **not** mix the two: sofia-client calls agents directly; sofia-client-secure never does.

---

## Architecture

```
Browser
  └── React SPA (port 5173 dev / served by Express in prod)
        └── POST /api/proxy/send|stream
              └── Express backend (Node.js, port 3000/8080)
                    ├── resolves agent URL from DB + agent card (prefers secureUrl)
                    └── POST /secure/send|stream
                          └── Python secure transport service (port 8090)
                                └── SecureA2ATransportAdapter
                                      └── encrypted A2A call → Agent
```

### Key differences from sofia-client

| | sofia-client | sofia-client-secure |
|---|---|---|
| Agent calls | Direct `fetch()` from Node.js | Via Python `SecureA2ATransportAdapter` |
| Agent card field used | `url` | `secureUrl` (falls back to `url`) |
| Streaming | Native SSE relay | Python wraps response as SSE events |
| Extra dependency | — | Python 3.11+, stls_a2a, FastAPI |

---

## Prerequisites

- **Node.js 20+**
- **Python 3.11+**
- **stls_a2a** package installed (see below)
- **PostgreSQL 14+** (production mode only)
- **Redis** — optional (`SESSION_CACHE_BACKEND=memory` is the default and needs none). Required when `SESSION_CACHE_BACKEND=redis`, so key sessions survive across container instances/restarts (Cloud Run) instead of living in process memory; also used by stls_a2a agent servers for task store / auth cache.
- The **key-exchange sidecar** running on `KEY_SESSION_HOST:KEY_SESSION_PORT`  
  (base/simple provider: pure Python; arqit: requires the JVM + JAR)

---

## Quick start — development

### 1. Install Python dependencies (includes stls_a2a)

The `stls_a2a` wheel is bundled in `python/wheels/`.  
Install everything with a single command from the project root:

```bash
pip install -r python/requirements.txt --find-links python/wheels
```

### 3. Install Node.js dependencies

```bash
npm install
```

### 4. Configure environment

```bash
cp .env.example .env
# edit .env — at minimum set DB_* and SECURE_TRANSPORT_* vars
```

### 5. Start the key-exchange sidecar

Follow the instructions in the `stls-a2a` project to start the key provider on `KEY_SESSION_HOST:KEY_SESSION_PORT`.

### 6. Start the Python secure transport service

```bash
# necessario almeno il 21
set JAVA_HOME="C:\Java\jdks\jdk-24"
set PATH=%JAVA_HOME%\bin;%PATH%
java -version
set JPYPE_JVM=C:\Java\jdks\jdk-24\bin\server\jvm.dll
# non funziona perche' legge direttamente dal registro
python -c "import jpype; print(jpype.getDefaultJVMPath())"
# sembra funzionare invece questa
notepad .venv\Lib\site-packages\sitecustomize.py
# contenuto
import jpype

_original_start = jpype.startJVM

def _patched_startJVM(*args, **kwargs):
    if not args or not str(args[0]).endswith(".dll"):
        kwargs.setdefault("jvmpath", r"C:\Java\jdks\jdk-24\bin\server\jvm.dll")
        return _original_start(**kwargs)
    return _original_start(*args, **kwargs)

jpype.startJVM = _patched_startJVM
# verifica
python -c "import os; print(os.environ.get('JPYPE_JVM'))"


python python/secure_transport.py
# → listening on http://localhost:8090
# → GET http://localhost:8090/health  → {"ok":true,"session_ready":true}
```

### 7. Start the Express backend

```bash
npm run dev:server
# → http://localhost:3000
```

### 8. Start the React dev server

```bash
npm run dev
# → http://localhost:5173
```

---

## Environment variables

### Node.js / Express

| Variable | Default | Description |
|---|---|---|
| `VITE_MODE` | `production` | `debug` or `production` UI mode |
| `VITE_AUTH_MODE` | `none` | `none`, `user`, or `app` |
| `VITE_BASE` | `/` \| `/a/` \| … | Base path when served under a sub-path (e.g. `/a/`). Must end with `/`. **Build-time** — pass as `--build-arg VITE_BASE=/a/` in Docker. Must match `BASE_PATH`. |
| `PORT` | `3000` | Express listen port |
| `NODE_ENV` | `development` | `development` or `production` |
| `BASE_PATH` | `` \| `/a` | Runtime base path prefix. Set to the same path as `VITE_BASE` (without trailing slash). Required when behind a path-based load balancer. |
| `SESSION_SECRET` | *(required in prod)* | Express session secret |
| `SESSION_TIMEOUT_MINUTES` | `30` | Session inactivity timeout |
| `FRAME_ANCESTORS` | *(empty)* | Space-separated origins allowed to embed the app in an `<iframe>` (CSP `frame-ancestors`). Empty/unset = no restriction, any site can embed the app. Set to restrict to known integrator origins, e.g. a Salesforce Customer Portal domain — see `server/src/middleware/securityHeaders.ts`. |
| `DB_HOST` | `localhost` | PostgreSQL host |
| `DB_PORT` | `5432` | PostgreSQL port |
| `DB_NAME` | `sofia` | PostgreSQL database |
| `DB_USER` | `postgres` | PostgreSQL user |
| `DB_PASSWORD` | *(empty)* | PostgreSQL password |
| `SECURE_TRANSPORT_URL` | `http://localhost:8090` | URL of the Python service |

> `VITE_*` variables are **build-time** — they are compiled into the JavaScript bundle by Vite. Runtime variables
> (`BASE_PATH`, `DB_*`, `SESSION_SECRET`, `PORT`) are read by the Express server at startup.
>
> **Base path pairing**: `VITE_BASE` and `BASE_PATH` must always be set together.
> `VITE_BASE=/a/` → assets served at `/a/assets/…` and API calls go to `/a/api/…`.
> `BASE_PATH=/a` → Express mounts routes at `/a/api/…`.

### Python secure transport service

| Variable | Default | Description |
|---|---|---|
| `SECURE_TRANSPORT_PORT` | `8090` | Port the Python service binds to |
| `SECURE_TRANSPORT_HOST` | `0.0.0.0` | Bind address |
| `KEY_SESSION_PORT` | `8081` | Key-exchange sidecar port |
| `KEY_SESSION_HOST` | `localhost` | Key-exchange sidecar host |
| `KEY_PROVIDER_NAME` | `base` | `base`, `simple`, `arqit`, or `pqc` |
| `CLASSPATH` | *(unset)* | Path to `stls-ai-*.jar` (arqit only) |
| `ARQIT_CLIENT_CONFIG` | *(unset)* | Path to Arqit client config JSON |
| `ARQIT_SERVER_CONFIG` | *(unset)* | Path to Arqit server config JSON |
| `SESSION_CACHE_BACKEND` | `memory` | `memory` or `redis` — same switch as the server-side `stls_core.SessionCacheFactory` |
| `SESSION_CACHE_REDIS_URL` | `redis://localhost:6379` | Only used when `SESSION_CACHE_BACKEND=redis` |
| `SESSION_CACHE_REDIS_NAMESPACE` | `stls:sc` | Redis key prefix, distinct from the keystore's `stls:ks:*` |
| `SESSION_CACHE_TTL` | `3600` | Key-session idle TTL, seconds (sliding window) |

> Key sessions (kid + key material) are cached via a pluggable backend — same env var names as the server-side
> `stls_core.SessionCacheFactory`, so there's one naming convention across the whole stls stack. `memory` (default)
> lives in process and does not survive restarts or multi-instance deployments; switch to `redis` on Cloud Run, which
> does not guarantee that consecutive requests for the same `session_id` land on the same instance. This applies to
> `base`/`arqit` too, not just `pqc`. (Not a direct import of `stls_core`'s cache — see the note at the top of
> `secure_transport.py` for why.)

### Post-quantum key exchange (`KEY_PROVIDER_NAME=pqc`)

Hybrid X25519 + ML-KEM-768 handshake, performed lazily on the first `/secure/send` or `/secure/stream` call for a
session. Requires `liboqs-python`, pulled in via the `stls_a2a[pqc]` extra in `python/requirements.txt`, and the
liboqs C library baked into the image at build time (`ARG LIBOQS_VERSION` in the `Dockerfile`, currently `0.14.0`).
The C library version and the `liboqs-python` wrapper version must stay compatible — a mismatch surfaces as
`AttributeError` at runtime, not at install time.

---

## Agent card — secureUrl

For the secure transport to be used, an agent's `/.well-known/agent.json` should expose a `secureUrl` field:

```json
{
  "name": "my-agent",
  "url": "http://agent-host:8001/rpc",
  "secureUrl": "http://agent-host:8001/secure"
}
```

If `secureUrl` is absent the proxy falls back to `url`.  
The `secureUrl` value is passed directly to `SecureA2ATransportAdapter(base_url=secureUrl)`.

---

## Database setup

Same schema as sofia-client.  Run the migration against your PostgreSQL instance:

```bash
psql -U <user> -d sofia -f server/migrations/001_init.sql
# or:
npm run db:migrate --db=postgresql://user:pass@localhost/sofia
```

Key tables:

| Table | Purpose |
|---|---|
| `sf_agents_config` | Agent registry (`agent_name`, `url`, `api_key`) |
| `sf_agents_access` | User ↔ agent access control + profile |
| `sf_users` | Login users (mode `user`) and app credentials (mode `app`) |
| `sf_app_tokens` | One-time tokens for app-mode browser login |
| `sf_sessions` | Express session store |

---

## Docker

### Build and run with docker-compose (recommended)

```bash
# Copy and edit environment
cp .env.example .env

docker compose up --build
# → app available at http://localhost:8080
```

The `app` container runs `docker-entrypoint.sh` which:
1. Starts the Python secure transport service (`python/secure_transport.py`)
2. Waits up to 30 s for `/health` to return OK
3. Starts the Express server

### stls_a2a in Docker

The `stls_a2a`/`stls_core` wheels under `python/wheels/` are already in the repo, and the `Dockerfile` installs them
automatically via `--find-links` (`stls_a2a[pqc]==0.1.3` in `python/requirements.txt`). No manual steps required.

### Arqit / JVM key provider

If `KEY_PROVIDER_NAME=arqit`, uncomment the JVM section in the Dockerfile:

```dockerfile
RUN apk add --no-cache openjdk17-jre
COPY path/to/stls-ai-0.0.1.jar /app/stls-ai.jar
ENV CLASSPATH=/app/stls-ai.jar
```

And set in `docker-compose.yml`:
```yaml
CLASSPATH: /app/stls-ai.jar
ARQIT_CLIENT_CONFIG: /path/to/a2a-client-config.json
```

### Deploy to Cloud Run

Two services, mirroring the `sofia-client` / `sofia-client-a` split:

| Service | Path | `VITE_AUTH_MODE` | Notes |
|---|---|---|---|
| `sofia-client-secure` | domain root | `user` | Username/password login |
| `sofia-client-secure-a` | `/a` (behind the internal LB) | `app` | App-to-app auth |

> `sofia-client-secure-a` serves from `/a`, same as `sofia-client-a`: `VITE_BASE=/a/` (build-time, baked into the
> asset paths and API calls via `src/api/apiBase.ts`) paired with `BASE_PATH=/a` (runtime, read by
> `server/src/config.ts` and used to mount the Express routers/static files in `server/src/index.ts`).

```bash

# ── sofia-client-secure (root path, user auth) ─────────────────────────────
# test -da fare senza VPN
docker build --build-arg VITE_MODE=production --build-arg VITE_AUTH_MODE=user --build-arg LIBOQS_VERSION=0.14.0 \
  -t europe-west8-docker.pkg.dev/spk-dev-dt-prc-0/sofia/sofia-client-secure:latest .
# produzione
docker build --build-arg VITE_MODE=production --build-arg VITE_AUTH_MODE=user --build-arg LIBOQS_VERSION=0.14.0 \
  -t europe-west8-docker.pkg.dev/spk-pro-dt-prc-0/sofia/sofia-client-secure:latest .

# ── sofia-client-secure-a (served at /a behind the internal LB, app auth) ──
# test
docker build --build-arg VITE_MODE=production --build-arg VITE_AUTH_MODE=app --build-arg VITE_BASE=/a/ --build-arg LIBOQS_VERSION=0.14.0 \
  -t europe-west8-docker.pkg.dev/spk-dev-dt-prc-0/sofia/sofia-client-secure-a:latest .
# produzione
docker build --build-arg VITE_MODE=production --build-arg VITE_AUTH_MODE=app --build-arg VITE_BASE=/a/ --build-arg LIBOQS_VERSION=0.14.0 \
  -t europe-west8-docker.pkg.dev/spk-pro-dt-prc-0/sofia/sofia-client-secure-a:latest .

# servono solo per le push - con i comandi di sopra la build e' comunque effettuata su docker locale
gcloud auth login
gcloud auth configure-docker europe-west8-docker.pkg.dev

# dev
docker push europe-west8-docker.pkg.dev/spk-dev-dt-prc-0/sofia/sofia-client-secure:latest
docker push europe-west8-docker.pkg.dev/spk-dev-dt-prc-0/sofia/sofia-client-secure-a:latest
# produzione
docker push europe-west8-docker.pkg.dev/spk-pro-dt-prc-0/sofia/sofia-client-secure:latest
docker push europe-west8-docker.pkg.dev/spk-pro-dt-prc-0/sofia/sofia-client-secure-a:latest

# Deploy — sofia-client-secure (served at domain root, no BASE_PATH needed)
gcloud run deploy sofia-client-secure \
  --image europe-west8-docker.pkg.dev/<project>/sofia/sofia-client-secure:latest \
  --region europe-west8 \
  --platform managed \
  --allow-unauthenticated \
  --vpc-connector <redis-vpc-connector> \
  --set-env-vars "SESSION_SECRET=<secret>,DB_HOST=<host>,DB_NAME=<db>,DB_USER=<user>,DB_PASSWORD=<pw>,KEY_PROVIDER_NAME=<base|arqit|pqc>,SESSION_CACHE_BACKEND=redis,SESSION_CACHE_REDIS_URL=redis://<memorystore-ip>:6379"

# Deploy — sofia-client-secure-a (served at /a behind the internal LB)
gcloud run deploy sofia-client-secure-a \
  --image europe-west8-docker.pkg.dev/<project>/sofia/sofia-client-secure-a:latest \
  --region europe-west8 \
  --platform managed \
  --allow-unauthenticated \
  --vpc-connector <redis-vpc-connector> \
  --set-env-vars "SESSION_SECRET=<secret>,DB_HOST=<host>,DB_NAME=<db>,DB_USER=<user>,DB_PASSWORD=<pw>,KEY_PROVIDER_NAME=<base|arqit|pqc>,SESSION_CACHE_BACKEND=redis,SESSION_CACHE_REDIS_URL=redis://<memorystore-ip>:6379,BASE_PATH=/a"
```

> **Redis / Memorystore**: `SESSION_CACHE_BACKEND` defaults to `memory`, which does **not** survive Cloud Run's
> multi-instance/scale-to-zero behavior — set `SESSION_CACHE_BACKEND=redis` on both services for a real deployment,
> for every `KEY_PROVIDER_NAME` value (not just `pqc`). Cloud Run has no direct route to a private Memorystore
> instance, so both services also need a
> [Serverless VPC Access connector](https://cloud.google.com/run/docs/configuring/connecting-vpc) (`--vpc-connector`)
> reaching the Redis instance's VPC, with `SESSION_CACHE_REDIS_URL` pointed at its private IP. If you're running
> Redis as a public/reachable endpoint instead (e.g. local `docker-compose` testing), `--vpc-connector` is not
> needed.

> **`LIBOQS_VERSION`** is a build-time arg baked into the image (compiles liboqs from source — see the Dockerfile).
> Only relevant when `KEY_PROVIDER_NAME=pqc` will be used at runtime, but harmless to always build with it since the
> import is lazy. Bump it together with the `liboqs-python` version pinned in `python/requirements.txt`.

> **Cloud Run request timeout**: as with `sofia-client`/`sofia-client-a`, `--timeout` is intentionally not passed
> above — omitting it preserves whatever value the service already has, and only applies Cloud Run's own default the
> *first* time a service is created. Check the live value before deploying if in doubt:
> `gcloud run services describe <service> --region europe-west8 --format="value(spec.template.spec.timeoutSeconds)"`.

> **Note**: `VITE_MODE`, `VITE_AUTH_MODE`, `VITE_BASE`, and `LIBOQS_VERSION` are **build-time** arguments baked into
> the image. All other variables (`BASE_PATH`, `SESSION_SECRET`, `DB_*`, `KEY_PROVIDER_NAME`, `SESSION_CACHE_*`) are
> runtime env vars set on the Cloud Run service — never put them in the image.

---

## Auth modes

Identical to sofia-client:

| Mode | Description |
|---|---|
| `none` | No login. All agents in `sf_agents_config` are accessible to everyone. |
| `user` | Username + password login form. Access controlled via `sf_agents_access`. |
| `app` | App-to-app. External app authenticates with `POST /api/auth/app-login`, browser exchanges token via `GET /api/auth/exchange`. |

### Cross-site iframe embeds (`app` mode)

When the app is loaded inside an `<iframe>` from a third-party portal (e.g. a Salesforce Customer Portal), browsers refuse to persist/send the `sofia.sid` session cookie in that context. To keep working there, `GET /api/auth/exchange` also returns `sessionToken` (the session id); the SPA keeps it in memory/`sessionStorage` and sends it back as `Authorization: Bearer <sessionToken>` on every subsequent `/api/*` call — including the key-session calls (`/api/proxy/connect`, `/disconnect`, `/heartbeat`) — instead of relying on the cookie (see `server/src/middleware/bearerAuth.ts` and `src/api/apiFetch.ts`). Same-site/standalone use is unaffected — it keeps working via the cookie exactly as before. Only `app` mode is covered; `user` mode remains cookie-only.

Who is allowed to embed the app is controlled by `FRAME_ANCESTORS` (see [Environment variables](#environment-variables)).

**Simulating an iframe embed locally** — add `--iframe` to the login simulation script:

```bash
node scripts/simulate-app-login.js ^
  --browser-url http://localhost:5173 ^
  --caller-user-id mario.rossi ^
  --iframe
```

It starts a tiny local server on `http://127.0.0.1:5555/` (change with `--harness-port`) serving a page that embeds the app in a centered `<iframe>`, and opens it. `127.0.0.1` and `localhost` are different "sites" for the browser, so the same cross-site cookie rules apply as with a real third-party domain. In DevTools check **Application → Cookies** (was `sofia.sid` accepted?) and **Network → `/api/agents`** (should be `200`, via cookie or via `Authorization: Bearer`). To test the `FRAME_ANCESTORS` restriction, start the server with `FRAME_ANCESTORS=http://127.0.0.1:5555` and confirm the iframe loads; change `--harness-port` without updating it and confirm the browser blocks the frame.

---

## Python service API

The service exposes three endpoints consumed exclusively by the Express proxy:

### `GET /health`
Returns `{"ok": true, "session_ready": true}` once the key session is initialised.

### `POST /secure/send`
Non-streaming call.

```json
// Request
{ "url": "<secureUrl>", "payload": { "jsonrpc": "2.0", "method": "message/send", "params": {...}, "id": "1" } }

// Response — full jsonrpc envelope
{ "jsonrpc": "2.0", "id": "1", "result": { ... } }
```

### `POST /secure/stream`
Streaming call — returns `text/event-stream`.

Internally calls `message/send` via `SecureA2ATransportAdapter` (request/response semantics) and re-emits the result as two A2A SSE events:

1. `data: {"result": {"kind": "message", "parts": [...], "final": false}}`
2. `data: {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": true}}`

---

## Timeout reference

All timeout values are aligned to **180 s** to match the Python test baseline. The chain is:

```
Browser (useChatProd watchdog)
  → Express proxy (CONNECT + NO_DATA timers)
    → Python secure transport (stream_timeout)
      → Agent
```

### Client-side watchdog — `src/hooks/useChatProd.ts`
| Constant | Value | Purpose |
|---|---|---|
| `CLIENT_STREAM_TIMEOUT_MS` | 180 000 ms | Aborts if no SSE chunk is **yielded** within this window. Resets only on yielded chunks (status-update events that carry no parts do NOT reset it). |

### Express proxy — `server/src/routes/proxy.ts`
Applies to both `/stream` (production) and `/direct/stream` (debug) routes.

| Constant | Value | Purpose |
|---|---|---|
| `CONNECT_TIMEOUT_MS` | 180 000 ms | Max time to wait for the Python secure transport to open the SSE connection. |
| `NO_DATA_TIMEOUT_MS` | 180 000 ms | Max silence between consecutive SSE bytes from the Python service. |
| `AbortSignal.timeout` on `/send` | 180 000 ms | Max time for a non-streaming call to the Python secure transport. |

### Python secure transport — `python/secure_transport.py`
| Parameter | Value | Purpose |
|---|---|---|
| `stream_timeout` | 180.0 s | Passed to `adapter.stream_secure_message()` — max time the adapter waits for the upstream agent to complete the secure streaming call. |
| `SESSION_CACHE_TTL` | 3600 s (env-configurable) | Key-session idle TTL, sliding window. Same name/default as the server-side `stls_core.SessionCacheFactory`. Enforced natively (Redis `EXPIRE`/`GETEX`, or an in-process check on the `memory` backend) — no separate cleanup sweep/interval. |

### Agent calls (frontend — debug mode) — `src/api/client.ts`
| Call | Value | Purpose |
|---|---|---|
| `sendChat` fetch | 180 000 ms | `AbortSignal.timeout` on the non-streaming direct fetch to the agent. |
| `streamChat` fetch | 180 000 ms | `AbortSignal.timeout` on the SSE fetch to the agent. |

This keeps the Sofia frontend's SSE parser (`parseLine`) happy without requiring native streaming support in the adapter.
