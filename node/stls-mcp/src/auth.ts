/**
 * auth.ts
 * -------
 * API key protection for the MCP endpoints (plain and encrypted).
 *
 * Same two-level schema as stls-a2a / python/stls-mcp — genuinely shared
 * via stls-core (Node), not duplicated:
 *   1. ApiKeyManager (enterprise, Redis/File/Postgres, stls-core/auth) —
 *      if configured, takes priority.
 *   2. AuthConfig (static, in-memory, timing-safe, stls-core/auth) —
 *      backward-compatible fallback.
 *   3. Neither configured → no auth, open endpoints (unchanged default,
 *      backward-compatible with createSecureMcpApp() callers that pass
 *      no auth/apiKeyManager).
 *
 * Structural difference vs. stls-a2a: here the surface to protect
 * includes MCP_PLAIN_PATH — the app generated ENTIRELY by the official
 * SDK, whose internal handlers we don't own — in addition to
 * MCP_STLS_PATH (ours). The check can't live inside a handler body, so
 * wrapWithAuth() is a reusable higher-order function applied in front of
 * BOTH exposures (STLS or plain, indifferently), operating purely on the
 * (Request) => Promise<Response> shape every handler in this package
 * already has.
 *
 * Protects both MCP_PLAIN_PATH and MCP_STLS_PATH — not just the encrypted
 * channel, exactly like stls-a2a (where AuthConfig protects both /a2a and
 * /secure-a2a). The handshake endpoints (/stls/handshake,
 * /stls/pqc/handshake) stay ALWAYS open, by design — same as stls-a2a: a
 * client has no provable credentials yet before the handshake itself, and
 * the encrypted channel's security comes from the handshake's
 * proof-of-possession, not an additional API key on that endpoint.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_auth.py.
 */

import type { ApiKeyManager, AuthConfig, RequestLike } from "stls-core";

export type RequestHandler = (request: Request) => Promise<Response>;

export interface WrapWithAuthOptions {
  auth?: AuthConfig;
  apiKeyManager?: ApiKeyManager;
}

function toRequestLike(request: Request): RequestLike {
  const url = new URL(request.url);
  return {
    header: (name: string) => request.headers.get(name) ?? undefined,
    queryParam: (name: string) => url.searchParams.get(name) ?? undefined,
    path: url.pathname,
  };
}

function sendJsonError(statusCode: number, message: string, wwwAuthenticate?: string): Response {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    error: { code: statusCode < 500 ? -32600 : -32603, message },
    id: null,
  });
  const headers = new Headers({ "content-type": "application/json" });
  if (wwwAuthenticate) headers.set("www-authenticate", wwwAuthenticate);
  return new Response(body, { status: statusCode, headers });
}

/**
 * Wraps a handler with the auth gate if `auth` or `apiKeyManager` are
 * configured, otherwise returns it unchanged — no overhead, no behavior
 * change for callers who don't use either.
 */
export function wrapWithAuth(handler: RequestHandler, options: WrapWithAuthOptions = {}): RequestHandler {
  const { auth, apiKeyManager } = options;
  if (!apiKeyManager && !(auth && auth.enabled)) {
    return handler;
  }

  return async (request: Request): Promise<Response> => {
    // ── 1. Enterprise ApiKeyManager ──────────────────────────────────────
    if (apiKeyManager) {
      try {
        const result = await apiKeyManager.validateRequest(toRequestLike(request));
        if (result.ok) {
          return handler(request);
        }
        const url = new URL(request.url);
        console.warn(`stls-mcp: ApiKeyManager auth denied: ${request.method} ${url.pathname} — ${result.result}`);
        return sendJsonError(401, "Unauthorized: missing or invalid credentials", result.wwwAuthenticateHeader());
      } catch (err) {
        console.error("stls-mcp: ApiKeyManager.validateRequest() error:", err);
        return sendJsonError(503, "Auth service unavailable");
      }
    }

    // ── 2. Static AuthConfig (backward-compatible fallback) ─────────────
    if (auth && auth.enabled) {
      const authorization = request.headers.get("authorization");
      const url = new URL(request.url);
      const apiKey = request.headers.get("x-api-key") ?? url.searchParams.get("api_key");
      if (auth.check(authorization, apiKey)) {
        return handler(request);
      }
      return sendJsonError(401, "Unauthorized: missing or invalid credentials", `ApiKey realm="${auth.realm}"`);
    }

    return handler(request);
  };
}
