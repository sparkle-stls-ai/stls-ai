/**
 * identity-context.smoke.ts
 * ----------------------------
 * Node mirror of python/stls-mcp/tests/test_stls_mcp_identity_context.py.
 * Issue #74: authorization scope propagation.
 *
 * Two things are proven here:
 *   1. getVerifiedIdentity()/requireScope() against a VerifiedIdentityStore
 *      directly — the actual authorization LOGIC (scope matching,
 *      fail-closed behavior), independent of a real MCP server.
 *   2. The full, real mechanism requireScope() depends on in
 *      production: wrapWithAuth()'s real checkIdentity(), against a
 *      real encryptedHandler() (middleware.ts) — not a hand-rolled
 *      stand-in — actually gets the verified result recorded AND the
 *      lookup key surviving into the decrypted inner request's
 *      headers, exactly what the official SDK reads into
 *      `extra.requestInfo.headers` for a tool callback. This matters
 *      here specifically because HEADER_SESSION_ID itself does NOT
 *      survive that boundary (encryptedHandler() deletes it) — see
 *      identity-context.ts's module docstring.
 *
 * Does NOT stand up a real McpServer/tool call — that full round trip
 * (real identityVerifier, real tool, real `extra` injection) has no
 * equivalent sample on the Node side yet (this package's role in
 * samples/peer/20_operator_identity_federation is Helios's own MCP
 * server, which doesn't configure identityVerifier at all — see that
 * sample's README).
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import {
  ScopeMismatchError,
  VerifiedIdentityStore,
  getVerifiedIdentity,
  requireScope,
  type RequestInfoLike,
} from "../src/identity-context.js";
import { wrapWithAuth, type IdentityVerifierLike } from "../src/auth.js";
import { encryptedHandler } from "../src/middleware.js";
import { HEADER_VERIFIED_SESSION_ID } from "../src/constants.js";
import { InMemorySessionCache, StlsTransport, HEADER_SESSION_ID } from "stls-core";

function extraWithSession(sessionId: string | undefined): RequestInfoLike {
  return sessionId ? { requestInfo: { headers: { [HEADER_VERIFIED_SESSION_ID.toLowerCase()]: sessionId } } } : {};
}

class FakeIdentityVerifier implements IdentityVerifierLike {
  constructor(
    private readonly ok: boolean,
    private readonly extra: { requestorDid?: string; grantorDid?: string; scope?: Record<string, unknown> } = {},
  ) {}
  async verifyRequest() {
    return this.ok ? { ok: true, ...this.extra } : { ok: false, error: "fake rejection" };
  }
}

async function main(): Promise<void> {
  // ── getVerifiedIdentity() against the store directly ───────────────

  {
    const store = new VerifiedIdentityStore();
    assert.equal(getVerifiedIdentity(store, extraWithSession(undefined)), undefined);
    console.log("OK: getVerifiedIdentity absent when extra carries no session marker");
  }

  {
    const store = new VerifiedIdentityStore();
    assert.equal(getVerifiedIdentity(store, extraWithSession("sess-unknown")), undefined);
    console.log("OK: getVerifiedIdentity absent when the session was never recorded");
  }

  {
    const store = new VerifiedIdentityStore();
    store.record("sess-A", { requestorDid: "did:web:b:agents:042", scope: { action: "x" } });
    const identity = getVerifiedIdentity(store, extraWithSession("sess-A"));
    assert.deepEqual(identity, { requestorDid: "did:web:b:agents:042", scope: { action: "x" } });
    console.log("OK: getVerifiedIdentity returns the recorded result");
  }

  {
    const store = new VerifiedIdentityStore();
    store.record("sess-B", { scope: {} });
    assert.ok(getVerifiedIdentity(store, extraWithSession("sess-B")));
    assert.equal(getVerifiedIdentity(store, extraWithSession("sess-B")), undefined);
    console.log("OK: getVerifiedIdentity is one-shot (consumes on read)");
  }

  // ── requireScope() ───────────────────────────────────────────────────

  {
    const store = new VerifiedIdentityStore();
    assert.throws(
      () => requireScope(store, extraWithSession("sess-none"), { action: "reroute_traffic" }),
      ScopeMismatchError,
    );
    console.log("OK: requireScope fails closed with no verified identity");
  }

  {
    const store = new VerifiedIdentityStore();
    store.record("sess-plain", {}); // plain identity presentation: no scope
    assert.throws(
      () => requireScope(store, extraWithSession("sess-plain"), { action: "reroute_traffic" }),
      ScopeMismatchError,
    );
    console.log("OK: requireScope fails closed on a plain identity with no delegation");
  }

  {
    const store = new VerifiedIdentityStore();
    store.record("sess-ok", { scope: { action: "reroute_traffic", segment: "MER-SEG-7" } });
    const scope = requireScope(store, extraWithSession("sess-ok"), {
      action: "reroute_traffic",
      segment: "MER-SEG-7",
    });
    assert.deepEqual(scope, { action: "reroute_traffic", segment: "MER-SEG-7" });
    console.log("OK: requireScope accepts matching parameters");
  }

  {
    // The exact failure the issue describes: a credential scoped to
    // MER-SEG-7 must not authorize a call for a different segment.
    const store = new VerifiedIdentityStore();
    store.record("sess-mismatch", { scope: { action: "reroute_traffic", segment: "MER-SEG-7" } });
    assert.throws(
      () => requireScope(store, extraWithSession("sess-mismatch"), { action: "reroute_traffic", segment: "MER-SEG-9" }),
      /MER-SEG-9/,
    );
    console.log("OK: requireScope rejects a mismatched segment");
  }

  {
    const store = new VerifiedIdentityStore();
    store.record("sess-extra", { scope: { action: "reroute_traffic", segment: "MER-SEG-7", extra: "whatever" } });
    const scope = requireScope(store, extraWithSession("sess-extra"), { action: "reroute_traffic", segment: "MER-SEG-7" });
    assert.equal(scope.extra, "whatever");
    console.log("OK: requireScope ignores scope keys the tool didn't assert on");
  }

  // ── Full real mechanism: wrapWithAuth()'s checkIdentity() against a
  //    real encryptedHandler(), through the whole challenge/response
  //    cycle — proving the recorded result's lookup key actually
  //    reaches the decrypted inner request's headers.

  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key = randomBytes(32);
    const sessionId = "sess-real-mechanism";
    await cache.put(sessionId, "kid-1", key);

    const store = new VerifiedIdentityStore();
    const verifier = new FakeIdentityVerifier(true, {
      requestorDid: "did:web:b.example.com:agents:042",
      grantorDid: "did:web:sofia.sparkle.example.com:agents:099",
      scope: { action: "reroute_traffic", segment: "MER-SEG-7" },
    });

    let observedExtra: RequestInfoLike = {};
    const innerHandle = async (request: Request): Promise<Response> => {
      observedExtra = { requestInfo: { headers: Object.fromEntries(request.headers.entries()) } };
      const payload = (await request.json()) as { id?: unknown };
      return new Response(JSON.stringify({ jsonrpc: "2.0", result: {}, id: payload.id }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    };
    const handler = wrapWithAuth(encryptedHandler(innerHandle, cache), { identityVerifier: verifier, identityStore: store });

    // First call: mint the challenge.
    const resp1 = await handler(
      new Request("http://testserver/mcp-stls", {
        method: "POST",
        headers: { [HEADER_SESSION_ID]: sessionId },
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
    );
    const challengeBody = (await resp1.json()) as { error: { challenge: string } };
    assert.ok(challengeBody.error.challenge);

    // Second call: present the (fake) VP header and a real encrypted body.
    const transport = new StlsTransport(key, sessionId);
    const envelope = transport.encryptJson({ jsonrpc: "2.0", method: "ping", id: 2 });
    const resp2 = await handler(
      new Request("http://testserver/mcp-stls", {
        method: "POST",
        headers: { [HEADER_SESSION_ID]: sessionId, "X-STLS-Identity-Presentation": "ZmFrZS12cA==" },
        body: JSON.stringify(envelope),
      }),
    );
    assert.equal(resp2.status, 200);

    // What the tool callback would have seen as `extra`, and what
    // getVerifiedIdentity()/requireScope() actually read from it.
    assert.equal(observedExtra.requestInfo?.headers?.[HEADER_SESSION_ID.toLowerCase()], undefined);
    const identity = getVerifiedIdentity(store, observedExtra);
    assert.deepEqual(identity, {
      requestorDid: "did:web:b.example.com:agents:042",
      grantorDid: "did:web:sofia.sparkle.example.com:agents:099",
      scope: { action: "reroute_traffic", segment: "MER-SEG-7" },
    });
    console.log("OK: the real checkIdentity()/encryptedHandler() mechanism carries the verified scope through");
  }

  console.log("\nAll identity-context.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exit(1);
});
