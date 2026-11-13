/**
 * transport-retry.smoke.ts
 * -------------------------
 * Node mirror of python/stls-mcp/tests/test_stls_mcp_transport_retry.py.
 * Covers the client-side retry-on-expiry added to createStlsFetch (issue
 * #6, second increment) — previously stls-mcp had no automatic recovery
 * at all on session expiry (idle-TTL or the new absolute max-age from
 * stls-core 0.1.7), unlike stls-a2a.
 *
 * Calls encryptedHandler()'s returned function DIRECTLY as the
 * "innerFetch" for createStlsFetch — no real HTTP server/port needed,
 * same "just a function call" architecture middleware.ts's own docstring
 * describes. This exercises the REAL server-side code (middleware.ts),
 * not a hand-crafted mock of its JSON shape, so these tests prove the
 * client and server sides actually agree on the wire format.
 *
 * Coverage (same IDs as the Python suite, for traceability):
 *   C01 — happy path: no expiry, no retry attempted, request succeeds
 *   C02 — session expired (cache evicted) -> ERR_SESSION_EXPIRED 401 ->
 *         rehandshake callback invoked once -> retried request succeeds
 *         under the NEW session_id/transport
 *   C03 — retry-once only: if the retried request ALSO comes back
 *         expired, the second 401 is returned to the caller, no infinite loop
 *   C04 — rehandshake callback itself throws -> propagates immediately,
 *         no retry attempted at all
 *   C05 — no rehandshake callback configured (default) -> unchanged
 *         behavior: the 401 is just returned
 *   C06 — looksLikeSessionExpired: a 401 NOT carrying ERR_SESSION_EXPIRED
 *         (e.g. an auth-layer rejection) must NOT be treated as a retry
 *         trigger
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { InMemorySessionCache, StlsTransport } from "stls-core";
import { encryptedHandler } from "../src/middleware.js";
import { createStlsFetch, looksLikeSessionExpired } from "../src/transport.js";
import { ERR_SESSION_EXPIRED, MCP_PLAIN_PATH } from "../src/constants.js";

function freshKey(): Buffer {
  return randomBytes(32);
}

const SID1 = "session-old-aaaa";
const SID2 = "session-new-bbbb";
const KID1 = "kid-aaa";
const KID2 = "kid-bbb";
const STLS_URL = "http://testserver/mcp-stls";

async function echoInnerHandle(request: Request): Promise<Response> {
  const url = new URL(request.url);
  assert.equal(url.pathname, MCP_PLAIN_PATH);
  const payload = (await request.json()) as { id?: unknown };
  return new Response(
    JSON.stringify({ jsonrpc: "2.0", result: payload, id: payload.id }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

async function main(): Promise<void> {
  // C01 — happy path
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const key1 = freshKey();
    await cache.put(SID1, KID1, key1);
    const transport = new StlsTransport(key1, SID1);

    const handler = encryptedHandler(echoInnerHandle, cache);
    let rehandshakeCalls = 0;
    const stlsFetch = createStlsFetch(SID1, transport, {
      fetch: (url, init) => handler(new Request(url, init)),
      rehandshake: async () => {
        rehandshakeCalls++;
        throw new Error("should not be called on the happy path");
      },
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 200);
    const decoded = (await response.json()) as { result: { method: string } };
    assert.equal(decoded.result.method, "ping");
    assert.equal(rehandshakeCalls, 0);
    console.log("OK: C01 happy path, no retry attempted");
  }

  // C02 — expired session, retried successfully under a new session
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    // Deliberately NOT put into the cache: simulates an already-expired
    // (idle-TTL or max-age) session the client doesn't know about yet.
    const oldTransport = new StlsTransport(freshKey(), SID1);

    const handler = encryptedHandler(echoInnerHandle, cache);
    let rehandshakeCalls = 0;
    const stlsFetch = createStlsFetch(SID1, oldTransport, {
      fetch: (url, init) => handler(new Request(url, init)),
      rehandshake: async () => {
        rehandshakeCalls++;
        const key2 = freshKey();
        const newTransport = new StlsTransport(key2, SID2);
        await cache.put(SID2, KID2, key2); // what a real handshake does server-side
        return { sessionId: SID2, transport: newTransport };
      },
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 200);
    assert.equal(rehandshakeCalls, 1);
    const decoded = (await response.json()) as { result: { method: string } };
    assert.equal(decoded.result.method, "ping");
    console.log("OK: C02 expired session retried successfully under a new session");
  }

  // C03 — retry-once only: a second expiry gives up, no infinite loop
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const oldTransport = new StlsTransport(freshKey(), SID1);

    const handler = encryptedHandler(echoInnerHandle, cache);
    let rehandshakeCalls = 0;
    const stlsFetch = createStlsFetch(SID1, oldTransport, {
      fetch: (url, init) => handler(new Request(url, init)),
      rehandshake: async () => {
        rehandshakeCalls++;
        // Returns a session that is STILL not in the cache -> the
        // retried request will ALSO come back as ERR_SESSION_EXPIRED.
        return { sessionId: SID2, transport: new StlsTransport(freshKey(), SID2) };
      },
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 401);
    assert.equal(rehandshakeCalls, 1);
    console.log("OK: C03 retry-once only, no infinite loop");
  }

  // C04 — rehandshake failure propagates without retry
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const oldTransport = new StlsTransport(freshKey(), SID1);

    const handler = encryptedHandler(echoInnerHandle, cache);
    const stlsFetch = createStlsFetch(SID1, oldTransport, {
      fetch: (url, init) => handler(new Request(url, init)),
      rehandshake: async () => {
        throw new Error("handshake endpoint unreachable");
      },
    });

    await assert.rejects(
      stlsFetch(STLS_URL, {
        method: "POST",
        body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
      }),
      /handshake endpoint unreachable/,
    );
    console.log("OK: C04 rehandshake failure propagates, no retry attempted");
  }

  // C05 — no rehandshake configured: unchanged behavior
  {
    const cache = new InMemorySessionCache({ maxSize: 10, ttlSeconds: 60 });
    const oldTransport = new StlsTransport(freshKey(), SID1);

    const handler = encryptedHandler(echoInnerHandle, cache);
    const stlsFetch = createStlsFetch(SID1, oldTransport, {
      fetch: (url, init) => handler(new Request(url, init)),
    });

    const response = await stlsFetch(STLS_URL, {
      method: "POST",
      body: JSON.stringify({ jsonrpc: "2.0", method: "ping", id: 1 }),
    });
    assert.equal(response.status, 401);
    console.log("OK: C05 no rehandshake configured, behaves like before");
  }

  // C06 — a 401 without ERR_SESSION_EXPIRED must not look like expiry
  {
    const expired = new Response(
      JSON.stringify({ jsonrpc: "2.0", error: { code: ERR_SESSION_EXPIRED, message: "x" }, id: null }),
      { status: 401 },
    );
    assert.equal(await looksLikeSessionExpired(expired), true);

    const authFailure = new Response(
      JSON.stringify({ jsonrpc: "2.0", error: { code: -32600, message: "invalid api key" }, id: null }),
      { status: 401 },
    );
    assert.equal(await looksLikeSessionExpired(authFailure), false);

    const notJson = new Response("plain text, not json", { status: 401 });
    assert.equal(await looksLikeSessionExpired(notJson), false);

    console.log("OK: C06 looksLikeSessionExpired distinguishes expiry from an auth 401");
  }

  console.log("\nAll transport-retry.smoke.ts checks passed.");
}

main().catch((err) => {
  console.error("FAILED:", err);
  process.exit(1);
});
