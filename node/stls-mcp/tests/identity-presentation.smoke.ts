/**
 * identity-presentation.smoke.ts
 * ---------------------------------
 * Node mirror of python/stls-mcp/tests/test_stls_mcp_identity_presentation.py.
 * Covers issue #46: the DID/VC identity-presentation hook (issue #15
 * gap #1) brought to stls-mcp's wrapWithAuth()/createSecureMcpApp()
 * (`identityVerifier`) and createStlsFetch() (`identityChallengeHandler`).
 *
 * Calls encryptedHandler()'s returned function (wrapped with
 * wrapWithAuth()) DIRECTLY — same "real server-side code, not a
 * hand-crafted mock" architecture as transport-retry.smoke.ts — so
 * these tests prove the client and server sides actually agree on the
 * wire format, not just that each half does what this file assumes
 * the other half does.
 *
 * This suite does NOT exercise real stls-identity crypto (Sentinel,
 * VerifiablePresentation) — identityVerifier/identityChallengeHandler
 * are minimal fake duck-typed objects here, proving the hook's wiring
 * and off-by-default guarantee independent of any identity backend.
 *
 * Coverage (same structure as the Python suite):
 *   1. off-by-default — no identityVerifier -> never
 *      ERR_IDENTITY_CHALLENGE_REQUIRED
 *   2. challenge required -> 401 + ERR_IDENTITY_CHALLENGE_REQUIRED + a
 *      challenge
 *   3. accepted presentation passes the check, request reaches the
 *      inner handler
 *   4. rejected presentation is denied with a distinct error code
 *   5. identityVerifier configured ALONE (no apiKeyManager/auth) still
 *      engages the gate — regression for the same early-return fix
 *      python/stls-mcp needed
 *   6. apiKeyManager success still falls through to the identity check
 *      cleanly when identityVerifier is undefined
 *   7. createStlsFetch()'s client-side retry-once on an identity
 *      challenge
 *   8. identityChallengeHandler=undefined (default): unchanged
 *      behavior, the 401 just propagates
 *   9. looksLikeIdentityChallengeRequired distinguishes the identity
 *      error from a session-expiry 401
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { InMemorySessionCache, StlsTransport } from "stls-core";
import { encryptedHandler } from "../src/middleware.js";
import { wrapWithAuth, type IdentityVerifierLike } from "../src/auth.js";
import { createStlsFetch, looksLikeIdentityChallengeRequired } from "../src/transport.js";
import { ERR_IDENTITY_CHALLENGE_REQUIRED, ERR_SESSION_EXPIRED, MCP_PLAIN_PATH } from "../src/constants.js";

function freshKey(): Buffer {
  return randomBytes(32);
}

const SID1 = "session-identity-aaaa";
const KID1 = "kid-identity-aaa";
const STLS_URL = "http://testserver/mcp-stls";

class FakeIdentityVerifier implements IdentityVerifierLike {
  calls: string[] = [];
  constructor(private readonly ok: boolean) {}
  async verifyRequest(_request: Request, options: { challenge: string }) {
    this.calls.push(options.challenge);
    return this.ok ? { ok: true } : { ok: false, error: "fake rejection" };
  }
}

const fakeApiKeyManager = {
  async validateRequest() {
    return { ok: true, result: "ok" };
  },
} as never;

async function echoInnerHandle(request: Request): Promise<Response> {
  const url = new URL(request.url);
  assert.equal(url.pathname, MCP_PLAIN_PATH);
  const payload = (await request.json()) as { id?: unknown };
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", result: payload, id: payload.id }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

async function errorInfo(response: Response): Promise<{ code: unknown; message: string }> {
  const body = (await response.json()) as { error?: { code?: unknown; message?: string; challenge?: string } };
  return { code: body.error?.code, message: body.error?.message ?? "" };
}

async function main(): Promise<void> {
  // 1 — off by default
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: undefined });

    const response = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": "unknown-session" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    const { code } = await errorInfo(response);
    assert.notEqual(code, ERR_IDENTITY_CHALLENGE_REQUIRED);
    console.log("OK: 1 off-by-default, identity check never fires");
  }

  // 2 — challenge required
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(true);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: verifier });

    const response = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1 },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    assert.equal(response.status, 401);
    const { code } = await errorInfo(response);
    assert.equal(code, ERR_IDENTITY_CHALLENGE_REQUIRED);
    assert.deepEqual(verifier.calls, []); // challenge issuance doesn't need the verifier at all
    console.log("OK: 2 challenge issued, verifier not called yet");
  }

  // 3 — accepted presentation passes the identity check
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(true);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: verifier });

    const resp1 = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1 },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    const body1 = (await resp1.json()) as { error: { challenge: string } };
    const challenge = body1.error.challenge;
    assert.ok(challenge);

    const transport = new StlsTransport(key1, SID1);
    const envelope = transport.encryptJson({ jsonrpc: "2.0", method: "ping", id: 1 });
    const resp2 = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1, "X-STLS-Identity-Presentation": "ZmFrZS12cA==" },
        body: JSON.stringify(envelope),
      }),
    );
    assert.equal(resp2.status, 200);
    assert.deepEqual(verifier.calls, [challenge]);
    console.log("OK: 3 accepted presentation passes the identity check");
  }

  // 4 — rejected presentation is denied
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(false);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: verifier });

    const resp1 = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1 },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    assert.ok(((await resp1.json()) as { error: { challenge: string } }).error.challenge);

    const resp2 = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1, "X-STLS-Identity-Presentation": "dGFtcGVyZWQ=" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    const { code, message } = await errorInfo(resp2);
    assert.equal(resp2.status, 401);
    assert.notEqual(code, ERR_IDENTITY_CHALLENGE_REQUIRED);
    assert.match(message, /identity/);
    console.log("OK: 4 rejected presentation denied with a distinct error code");
  }

  // 5 — identityVerifier alone engages the gate (the early-return fix)
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(true);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), {
      auth: undefined,
      apiKeyManager: undefined,
      identityVerifier: verifier,
    });

    const response = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": SID1 },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    assert.equal(response.status, 401);
    const { code } = await errorInfo(response);
    assert.equal(code, ERR_IDENTITY_CHALLENGE_REQUIRED);
    console.log("OK: 5 identityVerifier alone engages the auth gate");
  }

  // 6 — apiKeyManager success still falls through cleanly
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), {
      apiKeyManager: fakeApiKeyManager,
      identityVerifier: undefined,
    });

    const response = await handler(
      new Request(STLS_URL, {
        method: "POST",
        headers: { "X-STLS-Session-ID": "sess-km" },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    const { code } = await errorInfo(response);
    assert.notEqual(code, ERR_IDENTITY_CHALLENGE_REQUIRED);
    console.log("OK: 6 apiKeyManager success falls through to the identity check cleanly");
  }

  // 7 — client-side retry-once on an identity challenge
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(true);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: verifier });
    const transport = new StlsTransport(key1, SID1);

    const seenChallenges: string[] = [];
    const stlsFetch = createStlsFetch(SID1, transport, {
      fetch: (url, init) => handler(new Request(url, init)),
      identityChallengeHandler: (challenge) => {
        seenChallenges.push(challenge);
        return { "X-STLS-Identity-Presentation": "ZmFrZS12cA==" };
      },
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 200);
    assert.equal(seenChallenges.length, 1);
    assert.deepEqual(verifier.calls, seenChallenges);
    const decoded = (await response.json()) as { result: { method: string } };
    assert.equal(decoded.result.method, "ping");
    console.log("OK: 7 client retries once on an identity challenge and succeeds");
  }

  // 8 — no handler configured: unchanged behavior
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const verifier = new FakeIdentityVerifier(true);
    const handler = wrapWithAuth(encryptedHandler(echoInnerHandle, cache), { identityVerifier: verifier });
    const transport = new StlsTransport(key1, SID1);

    const stlsFetch = createStlsFetch(SID1, transport, {
      fetch: (url, init) => handler(new Request(url, init)),
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 401);
    assert.deepEqual(verifier.calls, []);
    console.log("OK: 8 identityChallengeHandler undefined, behaves like before");
  }

  // 9 — looksLikeIdentityChallengeRequired distinguishes session expiry
  {
    const challengeRequired = new Response(
      JSON.stringify({
        jsonrpc: "2.0",
        error: { code: ERR_IDENTITY_CHALLENGE_REQUIRED, message: "x", challenge: "chal-1" },
        id: null,
      }),
      { status: 401 },
    );
    assert.equal(await looksLikeIdentityChallengeRequired(challengeRequired), "chal-1");

    const sessionExpired = new Response(
      JSON.stringify({ jsonrpc: "2.0", error: { code: ERR_SESSION_EXPIRED, message: "x" }, id: null }),
      { status: 401 },
    );
    assert.equal(await looksLikeIdentityChallengeRequired(sessionExpired), undefined);

    const notJson = new Response("plain text, not json", { status: 401 });
    assert.equal(await looksLikeIdentityChallengeRequired(notJson), undefined);

    console.log("OK: 9 looksLikeIdentityChallengeRequired distinguishes session expiry");
  }

  console.log("\nAll identity-presentation.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exit(1);
});
