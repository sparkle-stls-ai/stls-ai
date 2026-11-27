/**
 * identity-context.ts
 * ---------------------
 * Issue #74: authorization scope was never propagated from identity
 * verification to the tool/action executor.
 *
 * Port of python/stls-mcp/src/stls_mcp/stls_mcp_identity_context.py,
 * adapted to this package's architecture — see below for why the
 * mechanism differs even though the API (getVerifiedIdentity/
 * requireScope) is the same shape.
 *
 * Why this needs a VerifiedIdentityStore (Python didn't)
 * ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
 * Python's auth gate stashes the verified result on
 * `request.state` — a Starlette convenience backed by the ASGI
 * `scope["state"]` dict, which every later ASGI layer (including the
 * SDK's own) shares by reference even through a `dict(scope)` shallow
 * copy. Node's Fetch-API `Request` has no such mutable extension
 * point, and `encryptedHandler()` (middleware.ts) builds a genuinely
 * NEW `Request` object once it decrypts the body — any property
 * attached to the original request object would not survive onto
 * that new one.
 *
 * What DOES survive, mostly, across that boundary is the HTTP header
 * set: `encryptedHandler()` clones it (`new Headers(request.headers)`)
 * into the decrypted inner request — EXCEPT `X-STLS-Session-ID`
 * itself, which it deliberately deletes first ("STLS-only header,
 * meaningless to innerHandle"). So the identity gate (auth.ts's
 * checkIdentity()) mirrors the session id under a second, internal-
 * only header (HEADER_VERIFIED_SESSION_ID, constants.ts) on a
 * successful check — THAT one survives, and the official SDK threads
 * the inner request's headers into every tool call as
 * `extra.requestInfo.headers` (`@modelcontextprotocol/sdk`'s
 * `webStandardStreamableHttp.js`), which is what this module actually
 * reads. One `VerifiedIdentityStore` instance, created once by
 * whoever calls `createSecureMcpApp()`, passed to it as
 * `identityStore` AND closed over by the tool handlers that call
 * `getVerifiedIdentity`/`requireScope`. `consume()` is one-shot
 * (deletes on read): a tool call only ever sees a result from THIS
 * session's own, just-passed identity check, never a stale one left
 * over if some other call path skipped the check.
 */

import { HEADER_VERIFIED_SESSION_ID } from "./constants.js";

/**
 * Same duck-typed shape as python's PresentationVerificationResult,
 * minus `ok`/`error` (VerifiedIdentityStore only ever holds the
 * successful case — see auth.ts's checkIdentity()).
 */
export interface VerifiedIdentity {
  requestorDid?: string;
  grantorDid?: string;
  scope?: Record<string, unknown>;
}

/** Minimal shape of the MCP SDK's tool-callback `extra` this module
 * actually reads — avoids a hard dependency on the SDK's own type for
 * something this small and keeps this usable against a test double. */
export interface RequestInfoLike {
  requestInfo?: { headers?: Record<string, string> };
}

const SESSION_HEADER_KEY = HEADER_VERIFIED_SESSION_ID.toLowerCase();

/**
 * Session-id-keyed handoff between the identity gate (auth.ts's
 * checkIdentity()) and tool code. One instance per createSecureMcpApp()
 * call, shared by both sides — see this module's docstring.
 */
export class VerifiedIdentityStore {
  private readonly bySession = new Map<string, VerifiedIdentity>();

  /** Called by auth.ts's checkIdentity() on a successful verification. */
  record(sessionId: string, identity: VerifiedIdentity): void {
    this.bySession.set(sessionId, identity);
  }

  /** One-shot read: consumes the entry so it can't be reused by a
   * later, separate call that skipped its own identity check. */
  consume(sessionId: string): VerifiedIdentity | undefined {
    const identity = this.bySession.get(sessionId);
    this.bySession.delete(sessionId);
    return identity;
  }
}

function sessionIdFrom(extra: RequestInfoLike): string | undefined {
  return extra.requestInfo?.headers?.[SESSION_HEADER_KEY];
}

/**
 * Returns the identity_verifier's verified result for the request
 * backing this tool call (issue #74), or undefined when
 * `identityStore` was never given a result for this session — no
 * `identityVerifier` configured, the call didn't arrive over an
 * identity-checked session, or it was already consumed.
 */
export function getVerifiedIdentity(
  store: VerifiedIdentityStore,
  extra: RequestInfoLike,
): VerifiedIdentity | undefined {
  const sessionId = sessionIdFrom(extra);
  return sessionId ? store.consume(sessionId) : undefined;
}

/** Raised by requireScope() — a tool that calls it is explicitly
 * opting into "deny unless proven otherwise". A subclass of the
 * builtin Error, not a bespoke top-level type. */
export class ScopeMismatchError extends Error {}

/**
 * Asserts that this call's own parameters match the caller's
 * verified, delegated scope — the deterministic, in-code check the
 * issue asks for, meant to run as the FIRST line of a tool callback,
 * before any real effect:
 *
 *     mcp.tool("check_and_apply_reroute", schema, async (args, extra) => {
 *       requireScope(identityStore, extra, { action: "reroute_traffic", segment: args.segment });
 *       ...
 *     });
 *
 * Each key in `expected` is compared against the SAME key in the
 * verified OperationalCredential's scope — the model/caller never
 * gets a vote in what "the same key" means, since the tool author is
 * the one who wrote the key names being checked.
 *
 * Fails closed in every case: no verified identity at all, no
 * delegated scope (a plain identity presentation, or identityVerifier
 * not configured), or any mismatched key.
 *
 * Returns the full verified scope, for callers that need to read
 * further fields beyond the ones just checked.
 */
export function requireScope(
  store: VerifiedIdentityStore,
  extra: RequestInfoLike,
  expected: Record<string, unknown>,
): Record<string, unknown> {
  const identity = getVerifiedIdentity(store, extra);
  if (!identity) {
    throw new ScopeMismatchError(
      "no verified identity for this call — identityVerifier isn't configured on this " +
        "server, or this session never completed the identity challenge/response",
    );
  }

  const scope = identity.scope;
  if (scope === undefined) {
    throw new ScopeMismatchError(
      "caller presented a plain identity, not a delegated OperationalCredential — " +
        "there is no scope to authorize this call against",
    );
  }

  const mismatches: Record<string, { delegated: unknown; requested: unknown }> = {};
  for (const [key, requested] of Object.entries(expected)) {
    if (scope[key] !== requested) {
      mismatches[key] = { delegated: scope[key], requested };
    }
  }
  if (Object.keys(mismatches).length > 0) {
    throw new ScopeMismatchError(`call parameters do not match the delegated scope: ${JSON.stringify(mismatches)}`);
  }

  return scope;
}
