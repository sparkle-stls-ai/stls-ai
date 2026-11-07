# stls-mcp

STLS extension of the Model Context Protocol (MCP) — application-level
symmetric encryption (AES-256-GCM + HMAC-SHA256 + HKDF-Expand) on a second
Streamable HTTP endpoint, alongside a **100% standard** MCP endpoint,
generated and managed entirely by the official SDK, zero modifications.

`stls-mcp` shares cryptography, session cache, handshake and key provider
with [`stls-a2a`](../stls-a2a) through the common package
[`stls-core`](../stls-core).

## Why it exists

An AI agent that uses MCP to talk to external tools should be able to do so
over a quantum-safe channel, exactly as already happens for agent-to-agent
communication in `stls-a2a`. `stls-mcp` applies the same security model to
the MCP protocol, without requiring any change to the way tools, resources
and prompts are written and registered: the developer works with
`mcp.server.fastmcp.FastMCP` exactly as they normally would, and `stls-mcp`
adds an encrypted endpoint alongside the standard one.

## Design: "absolutely standard on plain"

Guiding requirement of this package: the plaintext MCP endpoint (`/mcp`)
must be **indistinguishable** from a "pure" MCP server to any spec-compliant
client — Claude Desktop, claude.ai, MCP Inspector, or any other host. For
this reason:

- `/mcp` is generated literally by `mcp.streamable_http_app()` (official SDK
  `mcp>=1.27,<2`), with no wrapping or modification.
- `stls-mcp` does not introduce a different protocol: it only adds a second
  endpoint, `/mcp-stls`, which speaks exactly the same JSON-RPC MCP, just
  encrypted in transit.
- **Same session manager, same tool/resource/prompt surface** on both
  endpoints — there are no two separate FastMCP instances to keep in sync.

This has been concretely validated: the example server included in this
package (`tests/vault_server.py`) has been tested as a real connector in
Claude Desktop on `/mcp`, with automatic discovery of all tools and calls
executed correctly, with Claude Desktop having no knowledge that
`/mcp-stls` even existed.

## Architecture

```mermaid
flowchart TB
    subgraph App["Application Layer"]
        FM["FastMCP (official SDK)\ntool / resource / prompt\nregistered by the developer"]
    end

    subgraph Server["stls-mcp Server (one Starlette app)"]
        SA["Starlette app (SDK)\nroute #quot;/mcp#quot; — untouched"]
        PLAIN["Mount(#quot;/#quot;)\nplain, unchanged"]
        HS["Route(#quot;/stls/handshake#quot;)\nHMAC challenge/response\n(base / arqit_py)"]
        PQC["Route(#quot;/stls/pqc/handshake#quot;)\nKEM X25519 + ML-KEM-768\n(KEY_PROVIDER_NAME=pqc)"]
        STLS["Mount(#quot;/mcp-stls#quot;)\nStlsMcpMiddleware"]
    end

    STD["Standard MCP client\n(Claude Desktop, etc.)"]
    SEC["STLS-aware client"]

    FM -->|"mcp.streamable_http_app()"| SA
    SA --> PLAIN
    SA --> HS
    SA --> PQC
    SA --> STLS
    STD --> PLAIN
    SEC --> HS
    SEC --> PQC
    SEC -->|"encrypted"| STLS
    STLS -->|"decrypt → synthetic scope\ninvoke SAME app\n→ encrypt"| SA
```

`StlsMcpMiddleware` does not duplicate application logic: it receives the
encrypted request, decrypts it, builds a synthetic ASGI scope pointing to
`/mcp`, invokes the standard app capturing its response, and re-encrypts
that response before returning it. The standard app has no idea it is being
invoked indirectly instead of directly by the client.

## Package Structure

Everything under [src/stls_mcp](./src/stls_mcp):

- `stls_mcp_constants.py` — paths (`/mcp`, `/mcp-stls`), MCP-specific headers, discovery keys
- `stls_mcp_server.py` — `create_secure_mcp_app()`, the server-side entry point
- `stls_mcp_middleware.py` — `StlsMcpMiddleware`, the encrypted ASGI proxy
- `stls_mcp_transport.py` — client side: `perform_client_handshake()` (base/arqit_py),
  `perform_pqc_client_handshake()` (PQC), `StlsHttpxTransport`, `stls_httpx_factory()`
- `stls_mcp_auth.py` — API key protection (`AuthConfig`, `wrap_with_auth()`),
  applied at the ASGI transport level in front of `/mcp` and `/mcp-stls`
- `stls_mcp_secure_toolset.py` — client-side integration with Google ADK
  (`STLSMCPToolset`, `create_stls_mcp_toolset()`, and `SecureToolsetManager`
  for multi-server/multi-session caching). Lazy `google-adk` import:
  `import stls_mcp` stays valid without ADK installed, required only
  if and when an encrypted toolset is actually built (extra `[adk]`)

Cryptography, session cache, handshake, key provider (`base`/`arqit_py`/`pqc`)
and the engine of the API key authentication system (`AuthConfig`,
`ApiKeyManager`, `KeyRecord`, the Redis/File/Postgres loaders) live in the
shared package [`stls-core`](../stls-core) — `stls_mcp_auth.py` is only the
local hook point (the ASGI wrapper that intercepts requests on `/mcp` and
`/mcp-stls`), not a reimplementation.

The package's public API is defined in [src/stls_mcp/\_\_init\_\_.py](./src/stls_mcp/__init__.py).

## Installation

`stls-mcp` expects `stls-core` as a sibling directory in the monorepo
(`python/stls-mcp` and `python/stls-core` at the same level). `uv` resolves
this automatically via `[tool.uv.sources]` in `pyproject.toml`.

```bash
cd python/stls-mcp
uv pip install -e ".[dev]"
```

### Available extras

| Extra | Contents | When to install it |
|---|---|---|
| *(base)* | stls-core, mcp, starlette, uvicorn, httpx | always |
| `redis` | `stls_core[redis]` | `SESSION_CACHE_BACKEND=redis` (multi-replica) |
| `postgres` | `stls_core[postgres]` → `asyncpg` | `AUTH_LOADER_BACKEND=postgres` (see "API key protection") |
| `arqit_py` | `stls_core[arqit_py]` | `KEY_PROVIDER_NAME=arqit_py` |
| `pqc` | `stls_core[pqc]` → `liboqs-python` | `KEY_PROVIDER_NAME=pqc` |
| `adk` | `google-adk>=1.25.0,<2.0.0` | using `STLSMCPToolset`/`create_stls_mcp_toolset()` — client-side only, not required for the server |

```bash
# With the PQC provider (X25519 + ML-KEM-768)
uv pip install -e ".[pqc]"

# With Redis cache (multi-replica)
uv pip install -e ".[redis]"

# With ApiKeyManager Postgres backend
uv pip install -e ".[postgres]"

# With Google ADK client integration (STLSMCPToolset)
uv pip install -e ".[adk]"

# With Arqit SKA-Platform
uv pip install -e ".[arqit_py]" \
  --find-links arqit_wheels/arqit-skaplatform-common/ \
  --find-links arqit_wheels/arqit-skaplatform-policy/ \
  --find-links arqit_wheels/arqit-skaplatform-registration/ \
  --find-links arqit_wheels/arqit-skaplatform-provisioning/ \
  --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \
  --find-links arqit_wheels/arqit-skaplatform-peering/ \
  --find-links arqit_wheels/arqit-skaplatform-monitoring/
```

## Configuration

Copy `.env.example` to `.env` and customize the values. Main variables:

```bash
MCP_SERVER_HOST=127.0.0.1
MCP_SERVER_PORT=8765
MCP_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:*,localhost,localhost:*

# Endpoint paths — default /mcp and /mcp-stls, customizable to expose
# the whole server under a prefix (e.g. /vault/mcp). If you change them,
# keep them in sync across EVERY process involved: server AND client — there
# is no automatic discovery of the effective path on the client side.
MCP_PLAIN_PATH=/mcp
MCP_STLS_PATH=/mcp-stls

# Key provider: "base" (dev/test) | "arqit_py" (QKD) | "pqc" (software quantum-safe)
KEY_PROVIDER_NAME=base

# Session cache: "memory" (default) | "redis" (multi-replica Cloud Run)
SESSION_CACHE_BACKEND=memory

# API key protection — optional, disabled by default (open endpoints).
# See "API key protection" below for the full list.
AUTH_ENABLED=false
```

`MCP_ALLOWED_HOSTS` feeds the DNS-rebinding protection built into the MCP
SDK (`TransportSecuritySettings.allowed_hosts`): without an explicit host
in this list, the SDK rejects even local requests with `421 Invalid Host
header`. When exposing the server publicly (development tunnel or
production domain), add the external host here. Cloudflare quick tunnels
generate a new domain on every restart — `MCP_ALLOWED_HOSTS` must be
updated accordingly.

See `.env.example` for the full list, partly shared with `stls-core` (Key
Provider, Arqit, Session Cache, Auth, Redis, PostgreSQL).

## API key protection

Optional, disabled by default. Same engine as `stls-a2a`
(`stls_core.auth`), applied uniformly on **both** endpoints — `/mcp` and
`/mcp-stls`, not just the encrypted channel. The handshake endpoints
(`/stls/handshake`, `/stls/pqc/handshake`) always stay open: a client has
no provable credentials yet before the handshake itself.

Two levels, cascaded:

1. **`ApiKeyManager`** (enterprise — Redis/File/Postgres, key rotation,
   per-key scope/endpoint, audit) — if configured, takes priority.
2. **`AuthConfig`** (static, in-memory, timing-safe) — simpler fallback,
   useful for quick tests without infrastructure.

### Quick (static) activation

```python
from stls_mcp import create_secure_mcp_app, AuthConfig

app = create_secure_mcp_app(
    mcp, provider,
    auth=AuthConfig(api_keys={"my-secret-key"}),
)
```

### Enterprise activation (`vault_server.py` as reference)

`vault_server.py` reads `AUTH_ENABLED` from the environment and exposes
`build_api_key_manager()`, which builds an `ApiKeyManager.from_env()` only
if `AUTH_ENABLED=true`:

```bash
AUTH_ENABLED=true
AUTH_LOADER_BACKEND=postgres        # static | file | postgres
AUTH_PG_DSN=postgresql://user:pass@host:5432/dbname
SF_API_KEYS_TABLE=sf_api_keys       # default
REDIS_URL=redis://localhost:6379
AUTH_KM_USE_REDIS=true
AUTH_KEY_TTL=3600
AUTH_REFRESH_INTERVAL=300
```

Why the explicit gate: `ApiKeyManager.from_env()` **always** builds a
working manager, but with zero configuration the default
(`AUTH_LOADER_BACKEND=static`, zero keys) means "deny everything", not
"auth disabled" — hence `AUTH_ENABLED` as an explicit switch, separate from
the backend configuration.

The lifecycle of `api_key_manager` is managed automatically by the lifespan
of `create_secure_mcp_app()` — it is passed an instance that is **not yet
started** (unlike `stls-a2a`, where the caller does `await manager.start()`
before passing it: here there is no external lifespan hook, the entire app
is built inside `create_secure_mcp_app()`).

Postgres table schema (`AUTH_LOADER_BACKEND=postgres`):

```sql
CREATE TABLE sf_api_keys (
    key_id      TEXT        PRIMARY KEY,   -- SHA-256 of the key, never the plaintext
    client_id   TEXT        NOT NULL,
    scopes      TEXT[]      DEFAULT '{}',
    endpoints   TEXT[]      DEFAULT '{}',  -- empty = wildcard, otherwise prefix-match
    expires_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now(),
    revoked     BOOLEAN     DEFAULT false,
    description TEXT        DEFAULT '',
    metadata    JSONB       DEFAULT '{}'
);
```

Generate a key with the same algorithm used by the system:

```python
from stls_core.auth import generate_api_key
raw_key, key_id = generate_api_key(prefix="my-app")
# raw_key -> hand it to the client, never save it in plaintext
# key_id  -> goes in the key_id column of the table
```

### Client side — passing the key

Every framework exposes `headers` at the connection configuration level —
see the next section for full examples with
`STLSMCPToolset`/`MultiServerMCPClient`/`MCPStreamableHttpPlugin`.

## Quick usage

### Server

```python
from mcp.server.fastmcp import FastMCP
from stls_core import KeySessionProviderFactory, KeySessionProvider
from stls_mcp import create_secure_mcp_app

mcp = FastMCP("My Secure Server", stateless_http=True, json_response=True)

@mcp.tool()
def get_weather(city: str) -> str:
    return f"Weather in {city}: sunny"

provider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER,
    # Reads KEY_PROVIDER_NAME: "base" | "arqit_py" | "pqc"
)

app = create_secure_mcp_app(mcp, provider)
# uvicorn app:app --host 0.0.0.0 --port 8000
```

With `KEY_PROVIDER_NAME=pqc`, `create_secure_mcp_app()` automatically also
registers `POST /stls/pqc/handshake` — no other server code change needed.

### Client — standard (base / arqit_py)

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from stls_mcp import perform_client_handshake, stls_httpx_factory

session_id, transport = await perform_client_handshake(
    BASE_URL, session_key, client_id="my-client",
)
factory = stls_httpx_factory(session_id, transport)

async with streamablehttp_client(
    url=f"{BASE_URL}/mcp-stls",
    httpx_client_factory=factory,
) as (read_stream, write_stream, _):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        result = await session.call_tool("get_weather", {"city": "Rome"})
```

### Client — PQC (X25519 + ML-KEM-768)

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from stls_mcp import perform_pqc_client_handshake, stls_httpx_factory

# No pre-shared session_key: the KEM derives it on the fly
session_id, transport = await perform_pqc_client_handshake(
    BASE_URL, client_id="my-client",
)
factory = stls_httpx_factory(session_id, transport)

async with streamablehttp_client(
    url=f"{BASE_URL}/mcp-stls",
    httpx_client_factory=factory,
) as (read_stream, write_stream, _):
    async with ClientSession(read_stream, write_stream) as session:
        await session.initialize()
        result = await session.call_tool("get_weather", {"city": "Rome"})
```

The rest of the MCP stack (`ClientSession`, session management,
reconnection) remains entirely the official SDK's — encryption happens
underneath, at the `httpx.AsyncBaseTransport` level, transparently.

## Integration with agent frameworks (ADK / LangChain / Semantic Kernel)

The client layer (`stls_mcp_transport.py`: handshake + `stls_httpx_factory()`)
is **framework-agnostic** — no dependency on any agent framework. How to
hook it up depends on how much the framework's MCP client exposes natively:

| Framework | Native `httpx_client_factory` support | Needs an adapter? |
|---|---|---|
| **LangChain / LangGraph** (`langchain-mcp-adapters`) | Yes — `StreamableHttpConnection.httpx_client_factory` | No |
| **Semantic Kernel** (`MCPStreamableHttpPlugin`) | Yes — `**kwargs` forwarded to `streamablehttp_client()` | No |
| **Google ADK** (`MCPToolset`) | No — [open issue](https://github.com/google/adk-python/issues/3005) | Yes — `stls_mcp_secure_toolset.py` |

Only ADK requires a dedicated adapter, due to a gap in the SDK itself (not
in STLS): `StreamableHTTPConnectionParams` does not expose a way to inject
a custom httpx client, so `STLSMCPToolset` subclasses
`MCPSessionManager`/`MCPToolset` to do so. The `google-adk` import is
lazy — installing it (extra `[adk]`) is only necessary if you actually
build an encrypted toolset, not to use the rest of the package.

### Google ADK

```python
from stls_mcp import create_stls_mcp_toolset

toolset = await create_stls_mcp_toolset(
    BASE_URL,
    client_id="my-agent",
    provider="pqc",                              # or "base"/"arqit_py" with session_key=
    headers={"X-API-Key": API_KEY} if API_KEY else None,
)

agent = Agent(model="gemini-2.5-flash", name="my_agent", tools=[toolset])
```

### LangChain / LangGraph

```python
from langchain_mcp_adapters.client import MultiServerMCPClient
from stls_mcp import perform_pqc_client_handshake, stls_httpx_factory

session_id, transport = await perform_pqc_client_handshake(BASE_URL, client_id="my-agent")
factory = stls_httpx_factory(session_id, transport)

client = MultiServerMCPClient({
    "my-server": {
        "transport": "http",
        "url": f"{BASE_URL}/mcp-stls",
        "httpx_client_factory": factory,
        "headers": {"X-API-Key": API_KEY} if API_KEY else {},
    }
})
tools = await client.get_tools()
```

### Semantic Kernel

```python
from semantic_kernel.connectors.mcp import MCPStreamableHttpPlugin
from stls_mcp import perform_pqc_client_handshake, stls_httpx_factory

session_id, transport = await perform_pqc_client_handshake(BASE_URL, client_id="my-agent")
factory = stls_httpx_factory(session_id, transport)

async with MCPStreamableHttpPlugin(
    name="MyServer",
    url=f"{BASE_URL}/mcp-stls",
    httpx_client_factory=factory,
    headers={"X-API-Key": API_KEY} if API_KEY else None,
) as plugin:
    agent = ChatCompletionAgent(service=..., plugins=[plugin])
```

Three complete test scripts, same server (`vault_server.py`), same tool
set, `--secure` flag to compare plain and encrypted:
[tests/vault_agent_test_adk.py](./tests/vault_agent_test_adk.py) (ADK),
[tests/vault_agent_test_lg.py](./tests/vault_agent_test_lg.py) (LangChain/LangGraph),
[tests/vault_agent_test_sk.py](./tests/vault_agent_test_sk.py) (Semantic Kernel).

## Complete example: "Vault" server

[tests/vault_server.py](./tests/vault_server.py) and
[tests/vault_client.py](./tests/vault_client.py) are a complete example
covering all three MCP primitives — tools, resources, prompts — over a
small themed domain (an archive of quantum-safe space stations).

```bash
# With base provider (default)
python tests/vault_server.py
python tests/vault_client.py

# With PQC provider
KEY_PROVIDER_NAME=pqc python tests/vault_server.py
KEY_PROVIDER_NAME=pqc python tests/vault_client.py
```

The client performs an STLS handshake (standard or PQC depending on
`KEY_PROVIDER_NAME`), then discovers and uses all tools, resources and
prompts, entirely through `/mcp-stls`. It is also the most direct reference
reading for the complete client-side pattern (plain `ClientSession`, no
agent framework on top).

For the same server seen through a real LLM agent, with real tool-calling
instead of direct calls, see
[tests/vault_agent_test_adk.py](./tests/vault_agent_test_adk.py) (ADK),
[tests/vault_agent_test_lg.py](./tests/vault_agent_test_lg.py) (LangChain/LangGraph)
and [tests/vault_agent_test_sk.py](./tests/vault_agent_test_sk.py) (Semantic Kernel)
— see the "Integration with agent frameworks" section above.

## Testing with a real MCP client (Claude Desktop)

Claude Desktop connects to remote MCP servers via *Settings →
Connectors → Add custom connector* with a publicly reachable HTTP(S) URL.
For local development/testing a tunnel is required:

```bash
# terminal 1 — server
python tests/vault_server.py

# terminal 2 — public tunnel
cloudflared tunnel --url http://127.0.0.1:8765
```

`cloudflared` prints a URL like `https://<random-name>.trycloudflare.com`.
Add it to `MCP_ALLOWED_HOSTS` **before** starting the server:

```bash
MCP_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:*,localhost,localhost:*,<random-name>.trycloudflare.com
```

Cloudflare quick tunnels generate a new domain on every restart —
`MCP_ALLOWED_HOSTS` must be updated every time the tunnel is recreated.
Verify first with `curl`:

```bash
curl -s -X POST https://<random-name>.trycloudflare.com/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl-test","version":"1"}},"id":1}'
```

A response with `serverInfo` and `capabilities` confirms that the tunnel
and the DNS-rebinding protection are configured correctly. In Claude
Desktop, enter as the connector URL:

```
https://<random-name>.trycloudflare.com/mcp
```

## Known limitations (v0.1)

- **Streamable HTTP only** — the stdio transport is not covered by this
  package (for local stdio servers there is nothing to encrypt at the
  network level).
- **`/mcp-stls` does not yet support chunk-by-chunk encrypted SSE
  streaming** — `StlsMcpMiddleware` buffers the entire response before
  re-encrypting it. This is not an issue with the recommended production
  configuration (`stateless_http=True, json_response=True`), where every
  response is a single JSON block. Chunk-by-chunk encrypted streaming is
  planned for a later version.
- **Multi-replica (Cloud Run scale-out)** — as with any provider, set
  `SESSION_CACHE_BACKEND=redis` to share sessions across instances.
- **Custom `MCP_PLAIN_PATH`/`MCP_STLS_PATH` must be synced by hand** — if
  customized, every process involved (server and each client) must read
  the same configuration; there is no automatic discovery of the effective
  path on the client side in this version.
- **E2E tests with a real `arqit_py` provider** not included in this
  package — requires the real Arqit SKA-Platform™ environment.
- **Concurrent `arqit_py` handshakes against one long-running server
  — fixed (2026-10-23).** `perform_handshake()` used to resolve the
  server's kid via plain FIFO ("next available, not yet claimed"),
  with no correlation to which specific incoming request it was for —
  safe only when handshakes never overlap in time. Concurrent
  sessions (`asyncio.gather`/`Promise.all` from several clients, or
  several devices, against the same server) could get cross-wired:
  one client's proof-of-possession check validated against a
  *different* client's key. Fixed by having the client send its own
  already-resolved kid as a hint (`HandshakeRequest.kid`, optional,
  `stls_core_handshake.py`) so `arqit_py` can resolve that exact key
  directly instead of guessing by arrival order — `base`/`pqc` ignore
  the hint (nothing to disambiguate for `base`; `pqc` doesn't use this
  endpoint). See `python/stls-core/sidecar/ARQIT_PEERING_NOTES.md`'s
  "DeviceMetadata singleton race" follow-up for the full chain of
  fixes this was found alongside.

## Testing

```bash
# Manual E2E example — base (default)
python tests/vault_server.py     # terminal 1
python tests/vault_client.py     # terminal 2

# Manual E2E example — PQC
KEY_PROVIDER_NAME=pqc python tests/vault_server.py
KEY_PROVIDER_NAME=pqc python tests/vault_client.py
```

The client runs a complete sequence: handshake, `initialize()`, discovery
of tools/resources/prompts, and execution of each at least once, all
through `/mcp-stls`.

## Roadmap

- Chunk-by-chunk encrypted SSE streaming on `/mcp-stls`
- stdio support (if a real use case emerges)
- Automated pytest test suite for `stls-mcp` (currently none — only
  manual `vault_*.py` scripts), including E2E coverage with `arqit_py`
  against real Arqit infrastructure
- Publishing `stls-core` to a private index, to remove the dependency on
  the local monorepo path in production

## License

Dual-licensed under the [GNU AGPLv3](LICENSE) and a
[commercial license](LICENSE-COMMERCIAL.md), consistent with `stls-a2a`
and `stls-core`. The Arqit SKA-Platform™ wheels remain subject to the
proprietary Arqit license.
