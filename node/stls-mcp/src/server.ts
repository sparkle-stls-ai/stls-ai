/**
 * server.ts
 * ---------
 * Server-side entry point: createSecureMcpApp().
 *
 * Mounts, side by side, on one Web-standard `(Request) => Promise<Response>`
 * handler:
 *
 *   plainPath  ("/mcp")      → the SAME request logic as the encrypted
 *                              path, called directly. Any spec-compliant
 *                              MCP client uses it without knowing anything
 *                              about STLS.
 *   stlsPath   ("/mcp-stls") → encryptedHandler(), which decrypts inbound,
 *                              calls the SAME request logic, and encrypts
 *                              the response.
 *   HANDSHAKE_PATH           → stls-core's HMAC challenge/response
 *                              handshake, shared with stls-a2a.
 *   HANDSHAKE_PQC_PATH       → registered only when KEY_PROVIDER_NAME=pqc.
 *
 * No framework router is involved: routing is a plain pathname comparison.
 * python/stls-mcp needed `_ExactMountPathNormalizer`/path-rewriting to work
 * around a Starlette `Mount` quirk (it doesn't match the exact path
 * without a trailing slash) and to re-target an ASGI scope at the right
 * internal route — neither applies here, because there's no second
 * dispatch through a router: the plain and STLS handlers both call the
 * same function directly (see middleware.ts).
 *
 * Request lifecycle, following the official SDK's own stateless example
 * (examples/server/simpleStatelessStreamableHttp.ts): a fresh `McpServer`
 * (from the `getServer` factory) and a fresh
 * `WebStandardStreamableHTTPServerTransport` are created and connected for
 * EVERY request, then closed once the response is ready — mirroring
 * Python's `stateless_http=True, json_response=True` recommended
 * production config. This is a deliberate port-time adaptation, not a
 * literal copy: python/stls-mcp reuses one long-lived FastMCP instance for
 * the whole process because that's how the Python SDK's session manager
 * is designed; the TypeScript SDK's own idiom for the stateless case is
 * "fresh server+transport per request", confirmed by reading the SDK's
 * own examples (both the stateless AND the stateful one create a fresh
 * McpServer per logical session via the same getServer() factory pattern
 * — there's no supported way to `connect()` one McpServer to many
 * transports sequentially).
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_server.py.
 */

import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { WebStandardStreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js";

import {
  HANDSHAKE_PATH,
  HANDSHAKE_PQC_PATH,
  HandshakeError,
  HandshakeRequest,
  PQCHandshakeError,
  PQCHandshakeRequest,
  SessionCache,
  SessionCacheFactory,
  performHandshake,
  performPqcHandshake,
} from "stls-core";
import type { ApiKeyManager, AuthConfig, KeySessionProvider } from "stls-core";

import { MCP_PLAIN_PATH, MCP_STLS_PATH } from "./constants.js";
import { encryptedHandler } from "./middleware.js";
import { wrapWithAuth, type RequestHandler } from "./auth.js";

export interface CreateSecureMcpAppOptions {
  /** Path of the encrypted endpoint. Default MCP_STLS_PATH ("/mcp-stls"). */
  stlsPath?: string;
  /** Path of the standard endpoint. Default MCP_PLAIN_PATH ("/mcp"). */
  plainPath?: string;
  /** SessionCache shared with the handshake endpoint and the STLS handler. Defaults to SessionCacheFactory.build() (memory, unless SESSION_CACHE_BACKEND=redis). */
  sessionCache?: SessionCache;
  /** Static AuthConfig — optional, disabled by default (open endpoints). */
  auth?: AuthConfig;
  /** Enterprise ApiKeyManager — takes priority over `auth` if both are set. Pass an instance NOT yet started; its lifecycle is the caller's responsibility (there is no app-level lifespan hook here, unlike create_secure_mcp_app() in Python — see README). */
  apiKeyManager?: ApiKeyManager;
}

function normalizePath(pathname: string): string {
  return pathname.length > 1 && pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
}

function jsonRpcResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function jsonRpcError(status: number, code: number, message: string): Response {
  return jsonRpcResponse({ jsonrpc: "2.0", error: { code, message }, id: null }, status);
}

/**
 * Builds the complete request handler: {plainPath} standard MCP,
 * {stlsPath} STLS-encrypted MCP, plus the handshake endpoint(s) — ready to
 * be served directly (any runtime with a `fetch`-style request/response
 * model) or adapted to node:http via toNodeRequestListener() (http-adapter.ts).
 */
export async function createSecureMcpApp(
  getServer: () => McpServer,
  keySessionProvider: KeySessionProvider,
  options: CreateSecureMcpAppOptions = {},
): Promise<RequestHandler> {
  const stlsPath = normalizePath(options.stlsPath ?? MCP_STLS_PATH);
  const plainPath = normalizePath(options.plainPath ?? MCP_PLAIN_PATH);
  const cache = options.sessionCache ?? (await SessionCacheFactory.build());

  // ── Shared request logic: fresh McpServer + transport per request ──────
  const innerHandle: RequestHandler = async (request: Request): Promise<Response> => {
    const server = getServer();
    const transport = new WebStandardStreamableHTTPServerTransport({
      sessionIdGenerator: undefined, // stateless
      enableJsonResponse: true, // single JSON response, no SSE stream
    });
    try {
      await server.connect(transport);
      return await transport.handleRequest(request);
    } finally {
      await transport.close();
      await server.close();
    }
  };

  const stlsHandler = encryptedHandler(innerHandle, cache, { plainPath });

  // ── Auth gate — applied ONLY to the two external exposures ─────────────
  // innerHandle itself stays unguarded: encryptedHandler() calls it
  // internally for every decrypted request, and gating it too would
  // authenticate every encrypted request twice (once on /mcp-stls, once
  // on the internal call with the same headers) — redundant, not a
  // security issue, just wasted work. Mirrors python/stls-mcp exactly.
  const guardedPlain = wrapWithAuth(innerHandle, { auth: options.auth, apiKeyManager: options.apiKeyManager });
  const guardedStls = wrapWithAuth(stlsHandler, { auth: options.auth, apiKeyManager: options.apiKeyManager });

  // ── Handshake endpoints — always open, by design ────────────────────────
  const handshakeHandler: RequestHandler = async (request: Request): Promise<Response> => {
    let handshakeRequest: HandshakeRequest;
    try {
      handshakeRequest = (await request.json()) as HandshakeRequest;
    } catch (exc) {
      return jsonRpcError(400, -32600, `Invalid request: ${(exc as Error).message}`);
    }
    try {
      const response = await performHandshake(handshakeRequest, keySessionProvider, cache);
      return jsonRpcResponse(response);
    } catch (exc) {
      if (exc instanceof HandshakeError) {
        return jsonRpcError(exc.code === -32600 ? 400 : 503, exc.code, exc.message);
      }
      throw exc;
    }
  };

  const providerName = (process.env.KEY_PROVIDER_NAME ?? "base").trim().toLowerCase();
  const pqcHandshakeHandler: RequestHandler | null =
    providerName === "pqc"
      ? async (request: Request): Promise<Response> => {
          let pqcRequest: PQCHandshakeRequest;
          try {
            pqcRequest = (await request.json()) as PQCHandshakeRequest;
          } catch (exc) {
            return jsonRpcError(400, -32600, `Invalid request: ${(exc as Error).message}`);
          }
          try {
            const response = await performPqcHandshake(pqcRequest, cache);
            return jsonRpcResponse(response);
          } catch (exc) {
            if (exc instanceof PQCHandshakeError) {
              return jsonRpcError(exc.code === -32600 ? 400 : 503, exc.code, exc.message);
            }
            throw exc;
          }
        }
      : null;

  if (pqcHandshakeHandler) {
    // eslint-disable-next-line no-console
    console.log(`stls-mcp: PQC endpoint registered on ${HANDSHAKE_PQC_PATH}`);
  }
  // eslint-disable-next-line no-console
  console.log(`stls-mcp started: plain=${plainPath} stls=${stlsPath} handshake=${HANDSHAKE_PATH}`);

  // ── Dispatcher — plain pathname comparison, no router involved ─────────
  return async (request: Request): Promise<Response> => {
    const pathname = normalizePath(new URL(request.url).pathname);

    if (pathname === HANDSHAKE_PATH && request.method === "POST") {
      return handshakeHandler(request);
    }
    if (pqcHandshakeHandler && pathname === HANDSHAKE_PQC_PATH && request.method === "POST") {
      return pqcHandshakeHandler(request);
    }
    if (pathname === stlsPath) {
      return guardedStls(request);
    }
    if (pathname === plainPath) {
      return guardedPlain(request);
    }
    return jsonRpcError(404, -32601, "Not found");
  };
}

/**
 * Builds the fragment to insert into capabilities.experimental.stls in
 * the initialize() response — used by the STLS extension discovery.
 */
export function stlsCapabilitiesFragment(options: { keyProviderName?: string; endpoint?: string } = {}): {
  stls: Record<string, string>;
} {
  const keyProviderName = options.keyProviderName ?? "base";
  const endpoint = options.endpoint ?? MCP_STLS_PATH;

  const [keyDerivation, handshakeEndpoint] =
    keyProviderName === "pqc"
      ? ["HKDF-SHA256+X25519+ML-KEM-768", HANDSHAKE_PQC_PATH]
      : ["HKDF-Expand-SHA256", HANDSHAKE_PATH];

  return {
    stls: {
      version: "1.0",
      encryption: "AES-256-GCM",
      hmac: "HMAC-SHA256",
      keyDerivation,
      keyProvider: keyProviderName,
      endpoint,
      handshakeEndpoint,
    },
  };
}
