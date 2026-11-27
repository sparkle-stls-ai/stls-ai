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
 *   3. DID/VC identity presentation (issue #15 gap #1, brought here in
 *      #46) — optional, ADDITIONAL check, strictly after 1/2 already
 *      passed. See checkIdentity() below.
 *   4. None of the above configured → no auth, open endpoints (unchanged
 *      default, backward-compatible with createSecureMcpApp() callers
 *      that pass no auth/apiKeyManager/identityVerifier).
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
 * /secure-a2a). The identity check specifically only ever engages on
 * MCP_STLS_PATH in practice: it keys off the STLS session header, which
 * the plain path never carries — see checkIdentity()'s own docstring,
 * same posture as python/stls-mcp's identical check. The handshake
 * endpoints (/stls/handshake, /stls/pqc/handshake) stay ALWAYS open, by
 * design — same as stls-a2a: a client has no provable credentials yet
 * before the handshake itself, and the encrypted channel's security
 * comes from the handshake's proof-of-possession, not an additional API
 * key on that endpoint.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_auth.py.
 */

import { randomBytes } from "node:crypto";
import { HEADER_SESSION_ID } from "stls-core";
import type { ApiKeyManager, AuthConfig, RequestLike } from "stls-core";
import { ERR_IDENTITY_CHALLENGE_REQUIRED, HEADER_IDENTITY_PRESENTATION, HEADER_VERIFIED_SESSION_ID } from "./constants.js";
import type { VerifiedIdentityStore } from "./identity-context.js";

export type RequestHandler = (request: Request) => Promise<Response>;

/**
 * Duck-typed stand-in for stls-identity's RequestPresentationVerifier
 * (or an equivalent object) — this package never imports stls-identity,
 * it only calls `identityVerifier.verifyRequest(request, { challenge })`
 * expecting `{ ok, error?, requestorDid?, grantorDid?, scope? }`.
 * `requestorDid`/`grantorDid`/`scope` are additive (issue #74) — same
 * posture as ApiKeyManager above, and same shape as python's
 * PresentationVerificationResult.
 */
export interface IdentityVerifierLike {
  verifyRequest(
    request: Request,
    options: { challenge: string },
  ): Promise<{
    ok: boolean;
    error?: string;
    requestorDid?: string;
    grantorDid?: string;
    scope?: Record<string, unknown>;
  }>;
}

export interface WrapWithAuthOptions {
  auth?: AuthConfig;
  apiKeyManager?: ApiKeyManager;
  /**
   * Optional DID/VC identity check (issue #15 gap #1, brought here in
   * #46). Runs as an ADDITIONAL check, strictly after apiKeyManager/
   * auth have already passed — never a substitute for them. undefined
   * (default) = disabled: zero header parsing, zero challenge-cache
   * use, zero behavior change for every existing caller.
   */
  identityVerifier?: IdentityVerifierLike;
  /**
   * Issue #74: where a successful identityVerifier result's
   * requestorDid/grantorDid/scope get recorded, keyed by sessionId, so
   * a tool handler can read it back via getVerifiedIdentity()/
   * requireScope() (identity-context.ts). undefined (default) = the
   * verified result is still checked for ok/error as before, just not
   * retained past this request — no behavior change for callers that
   * don't pass one.
   */
  identityStore?: VerifiedIdentityStore;
}

// Same value and reasoning as python/stls-mcp's own
// _IDENTITY_CHALLENGE_TTL_SECONDS (mirrors stls-a2a's
// _IDENTITY_CHALLENGE_TTL_SECONDS): long enough for a client to sign
// and retry within a normal request round trip, short enough that a
// captured, unused challenge doesn't stay exploitable indefinitely.
const IDENTITY_CHALLENGE_TTL_MS = 120_000;

function toRequestLike(request: Request): RequestLike {
  const url = new URL(request.url);
  return {
    header: (name: string) => request.headers.get(name) ?? undefined,
    queryParam: (name: string) => url.searchParams.get(name) ?? undefined,
    path: url.pathname,
  };
}

function sendJsonError(
  statusCode: number,
  message: string,
  wwwAuthenticate?: string,
  extra?: { code?: number; challenge?: string },
): Response {
  const body = JSON.stringify({
    jsonrpc: "2.0",
    error: {
      code: extra?.code ?? (statusCode < 500 ? -32600 : -32603),
      message,
      ...(extra?.challenge !== undefined ? { challenge: extra.challenge } : {}),
    },
    id: null,
  });
  const headers = new Headers({ "content-type": "application/json" });
  if (wwwAuthenticate) headers.set("www-authenticate", wwwAuthenticate);
  return new Response(body, { status: statusCode, headers });
}

/**
 * DID/VC identity check (issue #15 gap #1, brought here in #46) — runs
 * only when identityVerifier is configured, strictly AFTER
 * apiKeyManager/auth have already passed. Mirrors python/stls-mcp's
 * _AuthGateASGI._check_identity_async() exactly.
 *
 * With identityVerifier undefined (the default), this is a no-op:
 * returns null immediately, no header parsing, no challenge-cache use
 * — zero behavior change for every existing caller.
 *
 * Challenge/response shape: the first request on a sessionId without a
 * (still-fresh) cached challenge gets back
 * ERR_IDENTITY_CHALLENGE_REQUIRED plus a freshly minted challenge; the
 * client is expected to sign a VerifiablePresentation over it and
 * retry exactly once, same shape as ERR_SESSION_EXPIRED.
 *
 * Keys off stls-core's own HEADER_SESSION_ID (X-STLS-Session-ID), NOT a
 * custom stls-mcp header — same as python/stls-mcp. Scoped to the
 * encrypted transport by design: MCP_PLAIN_PATH requests never carry
 * this header, so this check is naturally a no-op there even when
 * identityVerifier is configured.
 *
 * Returns: null (authorized / not configured) or a Response (denied).
 */
async function checkIdentity(
  request: Request,
  identityVerifier: IdentityVerifierLike | undefined,
  challenges: Map<string, { challenge: string; mintedAt: number }>,
  identityStore: VerifiedIdentityStore | undefined,
): Promise<Response | null> {
  if (!identityVerifier) {
    return null;
  }

  const sessionId = request.headers.get(HEADER_SESSION_ID)?.trim();
  if (!sessionId) {
    return null;
  }

  const presentationHeader = request.headers.get(HEADER_IDENTITY_PRESENTATION)?.trim();
  const cached = challenges.get(sessionId);

  if (!presentationHeader || !cached || Date.now() - cached.mintedAt > IDENTITY_CHALLENGE_TTL_MS) {
    // Same entropy as python/stls-mcp's secrets.token_urlsafe(32).
    const challenge = randomBytes(32).toString("base64url");
    challenges.set(sessionId, { challenge, mintedAt: Date.now() });
    console.log(`stls-mcp: identity challenge issued for sessionId=${sessionId}`);
    return sendJsonError(401, "Identity presentation required", undefined, {
      code: ERR_IDENTITY_CHALLENGE_REQUIRED,
      challenge,
    });
  }

  const { challenge } = cached;
  let result: { ok: boolean; error?: string; requestorDid?: string; grantorDid?: string; scope?: Record<string, unknown> };
  try {
    result = await identityVerifier.verifyRequest(request, { challenge });
  } catch (err) {
    console.error("stls-mcp: identityVerifier.verifyRequest() error:", err);
    return sendJsonError(503, "Identity verification service unavailable");
  }

  if (result.ok) {
    challenges.delete(sessionId);
    if (identityStore) {
      // Issue #74: record the verified, unpacked grant for this
      // session so a tool handler can read it back via
      // getVerifiedIdentity()/requireScope().
      identityStore.record(sessionId, {
        requestorDid: result.requestorDid,
        grantorDid: result.grantorDid,
        scope: result.scope,
      });
      // HEADER_SESSION_ID itself gets deleted by encryptedHandler()
      // before the decrypted inner request is built (middleware.ts) —
      // mirror it under an internal-only header (constants.ts) so the
      // lookup key survives into extra.requestInfo.headers for the
      // tool call. Request.headers is a mutable Headers instance here,
      // same request object `handler(request)` is about to be called
      // with below — this never touches the actual wire.
      request.headers.set(HEADER_VERIFIED_SESSION_ID, sessionId);
    }
    return null;
  }

  console.warn(`stls-mcp: identity presentation rejected for sessionId=${sessionId}: ${result.error}`);
  return sendJsonError(401, "Unauthorized: invalid identity presentation");
}

/**
 * Wraps a handler with the auth gate if `auth`, `apiKeyManager`, or
 * `identityVerifier` are configured, otherwise returns it unchanged —
 * no overhead, no behavior change for callers who use none of them.
 */
export function wrapWithAuth(handler: RequestHandler, options: WrapWithAuthOptions = {}): RequestHandler {
  const { auth, apiKeyManager, identityVerifier, identityStore } = options;
  if (!apiKeyManager && !(auth && auth.enabled) && !identityVerifier) {
    return handler;
  }

  // {sessionId: {challenge, mintedAt}} — only ever populated when
  // identityVerifier is configured; see checkIdentity(). Captured per
  // wrapWithAuth() call, same lifetime as the app it guards (mirrors
  // python/stls-mcp's _AuthGateASGI instance-level dict).
  const identityChallenges = new Map<string, { challenge: string; mintedAt: number }>();

  return async (request: Request): Promise<Response> => {
    // ── 1. Enterprise ApiKeyManager ──────────────────────────────────────
    if (apiKeyManager) {
      try {
        const result = await apiKeyManager.validateRequest(toRequestLike(request));
        if (!result.ok) {
          const url = new URL(request.url);
          console.warn(`stls-mcp: ApiKeyManager auth denied: ${request.method} ${url.pathname} — ${result.result}`);
          return sendJsonError(401, "Unauthorized: missing or invalid credentials", result.wwwAuthenticateHeader());
        }
      } catch (err) {
        console.error("stls-mcp: ApiKeyManager.validateRequest() error:", err);
        return sendJsonError(503, "Auth service unavailable");
      }
    } else if (auth && auth.enabled) {
      // ── 2. Static AuthConfig (backward-compatible fallback) ─────────
      const authorization = request.headers.get("authorization");
      const url = new URL(request.url);
      const apiKey = request.headers.get("x-api-key") ?? url.searchParams.get("api_key");
      if (!auth.check(authorization, apiKey)) {
        return sendJsonError(401, "Unauthorized: missing or invalid credentials", `ApiKey realm="${auth.realm}"`);
      }
    }

    // ── 3. Identity presentation (issue #15 gap #1, #46) ────────────────
    const identityDenial = await checkIdentity(request, identityVerifier, identityChallenges, identityStore);
    if (identityDenial) {
      return identityDenial;
    }

    return handler(request);
  };
}
