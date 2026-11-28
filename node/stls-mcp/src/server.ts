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
import { wrapWithAuth, type IdentityVerifierLike, type RequestHandler } from "./auth.js";
import type { VerifiedIdentityStore } from "./identity-context.js";

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
  /**
   * Optional DID/VC identity check (issue #15 gap #1, brought here in
   * #46), duck-typed — this package never imports stls-identity, it
   * only calls `identityVerifier.verifyRequest(request, { challenge })`
   * expecting `{ ok, error? }`. Runs as an ADDITIONAL check, strictly
   * after apiKeyManager/auth have already passed. undefined (default)
   * = disabled, zero behavior change. Scoped to {stlsPath} in practice:
   * it keys off the STLS session header, which {plainPath} requests
   * never carry — see auth.ts's checkIdentity().
   */
  identityVerifier?: IdentityVerifierLike;
  /**
   * Issue #74: shared with `identityVerifier` — pass the SAME
   * VerifiedIdentityStore instance here and to whatever tool handlers
   * (registered inside `getServer`) call getVerifiedIdentity()/
   * requireScope() (identity-context.ts), so the verified, delegated
   * scope reaches the tool call itself, not just this gate's ok/error
   * check. undefined (default) = the identity check still runs when
   * `identityVerifier` is set, its result just isn't retained for any
   * tool to read back.
   */
  identityStore?: VerifiedIdentityStore;
  /**
   * If true (default), mounts `plainPath` as an external exposure. Set
   * false to expose this server over the encrypted channel only.
   */
  enablePlainEndpoint?: boolean;
  /**
   * If true (default), mounts `stlsPath` (and the STLS/PQC handshake
   * endpoint(s)) as an external exposure. Set false for a pure
   * standard-MCP deployment with no STLS session ever negotiated. Issue
   * #80, mirrors python/stls-mcp's create_secure_mcp_app() and
   * stls-a2a's create_secure_a2a_endpoint() enablePlainEndpoint/
   * enableSecureEndpoint. Throws at setup time if both this and
   * enablePlainEndpoint end up false — no reachable endpoint is a
   * misconfiguration, not a valid "disable everything" state.
   */
  enableSecureEndpoint?: boolean;
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
  const enablePlainEndpoint = options.enablePlainEndpoint ?? true;
  const enableSecureEndpoint = options.enableSecureEndpoint ?? true;
  if (!enablePlainEndpoint && !enableSecureEndpoint) {
    throw new Error(
      "createSecureMcpApp: enablePlainEndpoint and enableSecureEndpoint " +
        "can't both be false — that would register no reachable endpoint at all.",
    );
  }

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

  // ── Auth gate — applied ONLY to the external exposures that are enabled ──
  // innerHandle itself stays unguarded: encryptedHandler() calls it
  // internally for every decrypted request, and gating it too would
  // authenticate every encrypted request twice (once on /mcp-stls, once
  // on the internal call with the same headers) — redundant, not a
  // security issue, just wasted work. Mirrors python/stls-mcp exactly.
  const guardedPlain: RequestHandler | null = enablePlainEndpoint
    ? wrapWithAuth(innerHandle, {
        auth: options.auth,
        apiKeyManager: options.apiKeyManager,
        identityVerifier: options.identityVerifier,
        identityStore: options.identityStore,
      })
    : null;

  // ── Encrypted endpoints (issue #80: enableSecureEndpoint) ───────────────
  let guardedStls: RequestHandler | null = null;
  let handshakeHandler: RequestHandler | null = null;
  let pqcHandshakeHandler: RequestHandler | null = null;

  if (enableSecureEndpoint) {
    const stlsHandler = encryptedHandler(innerHandle, cache, { plainPath });
    guardedStls = wrapWithAuth(stlsHandler, {
      auth: options.auth,
      apiKeyManager: options.apiKeyManager,
      identityVerifier: options.identityVerifier,
      identityStore: options.identityStore,
    });

    // ── Handshake endpoints — always open, by design ──────────────────────
    handshakeHandler = async (request: Request): Promise<Response> => {
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
    if (providerName === "pqc") {
      pqcHandshakeHandler = async (request: Request): Promise<Response> => {
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
      };
      // eslint-disable-next-line no-console
      console.log(`stls-mcp: PQC endpoint registered on ${HANDSHAKE_PQC_PATH}`);
    }
  } else {
    // eslint-disable-next-line no-console
    console.log(`stls-mcp: secure endpoint disabled (enableSecureEndpoint=false): ${stlsPath}`);
  }

  if (!enablePlainEndpoint) {
    // eslint-disable-next-line no-console
    console.log(`stls-mcp: plain endpoint disabled (enablePlainEndpoint=false): ${plainPath}`);
  }

  // eslint-disable-next-line no-console
  console.log(`stls-mcp started: plain=${enablePlainEndpoint ? plainPath : "disabled"} stls=${enableSecureEndpoint ? stlsPath : "disabled"} handshake=${enableSecureEndpoint ? HANDSHAKE_PATH : "disabled"}`);

  // ── Dispatcher — plain pathname comparison, no router involved ─────────
  return async (request: Request): Promise<Response> => {
    const pathname = normalizePath(new URL(request.url).pathname);

    if (handshakeHandler && pathname === HANDSHAKE_PATH && request.method === "POST") {
      return handshakeHandler(request);
    }
    if (pqcHandshakeHandler && pathname === HANDSHAKE_PQC_PATH && request.method === "POST") {
      return pqcHandshakeHandler(request);
    }
    if (guardedStls && pathname === stlsPath) {
      return guardedStls(request);
    }
    if (guardedPlain && pathname === plainPath) {
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
