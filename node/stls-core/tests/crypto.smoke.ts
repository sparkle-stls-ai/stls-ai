/**
 * crypto.smoke.ts
 * ----------------
 * Node mirror of python/stls-core/tests/test_stls_core_crypto.py (T01-T19).
 * Pure unit-level checks on StlsTransport/StlsEnvelope — no cross-language
 * fixture involved (see tests/interop/crypto.smoke.ts for that).
 *
 * Coverage (same IDs as the Python suite, for traceability):
 *   T01 — encrypt() produces an envelope with all required fields
 *   T02 — decrypt(encrypt(plaintext)) == plaintext (bytes + unicode)
 *   T03 — IV is random: two encrypt() calls on the same plaintext produce different IVs
 *   T04 — session_id mismatch -> throws
 *   T05 — tampered MAC -> throws
 *   T06 — tampered ciphertext -> throws (MAC covers ciphertext too)
 *   T07/T08 — encryptJson/decryptJson round-trip (object / array)
 *   T09 — enc key and MAC key differ; legacy mode shares enc/dec keys
 *   T10 — two transports with the same key+session_id derive identical keys
 *   T11 — StlsEnvelope.toDict/fromDict round-trip
 *   T12 — empty plaintext round-trips correctly
 *   T13 — large plaintext (1 MB) round-trips correctly
 *   T14 — tampered version field -> throws (version is now authenticated)
 *   T15 — tampered session_id field (via dict, not the local check) -> throws
 *   T16 — direction "c2s"/"s2c": bidirectional round-trip between two transports
 *   T17 — direction: the two directions' keys are independent (cross-checked
 *         between the two transports; a transport can't decrypt its own output)
 *   T18 — direction=undefined (legacy) and direction="c2s" derive different keys
 *   T19 — invalid direction value is rejected
 */

import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { StlsEnvelope, StlsTransport } from "../src/crypto.js";

function freshKey(): Buffer {
  return randomBytes(32);
}

const SESSION_ID = "test-session-id-0001";

// T01 — envelope fields
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("hello"));
  const d = envelope.toDict();
  assert.ok("version" in d);
  assert.ok("session_id" in d);
  assert.ok("iv" in d);
  assert.ok("ciphertext" in d);
  assert.ok("tag" in d);
  assert.ok("mac" in d);
  assert.equal(d.version, "1.0");
  assert.equal(d.session_id, transport.sessionId);
  console.log("OK: T01 envelope has all fields");
}

// T02 — round-trip
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const plaintext = Buffer.from("test message 1234", "utf-8");
  assert.deepEqual(transport.decrypt(transport.encrypt(plaintext)), plaintext);

  const unicode = Buffer.from("Hello world \u{1F30D}", "utf-8");
  assert.deepEqual(transport.decrypt(transport.encrypt(unicode)), unicode);
  console.log("OK: T02 round-trip (bytes + unicode)");
}

// T03 — IV randomness
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const e1 = transport.encrypt(Buffer.from("same payload"));
  const e2 = transport.encrypt(Buffer.from("same payload"));
  assert.notDeepEqual(e1.iv, e2.iv);
  assert.notDeepEqual(e1.ciphertext, e2.ciphertext);
  console.log("OK: T03 IV is random across encrypt() calls");
}

// T04 — session_id mismatch
{
  const key = freshKey();
  const t1 = new StlsTransport(key, "session-A");
  const t2 = new StlsTransport(key, "session-B");
  const envelope = t1.encrypt(Buffer.from("payload"));
  assert.throws(() => t2.decrypt(envelope), /Session ID mismatch/);
  console.log("OK: T04 session_id mismatch -> throws");
}

// T05 — MAC tampered
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("payload"));
  const badMac = Buffer.from(envelope.mac);
  badMac[0] ^= 0xff;
  const tampered = new StlsEnvelope(envelope.sessionId, envelope.iv, envelope.ciphertext, envelope.tag, badMac);
  assert.throws(() => transport.decrypt(tampered), /MAC verification failed/);
  console.log("OK: T05 tampered MAC -> throws");
}

// T06 — ciphertext tampered
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("original payload"));
  const badCt = Buffer.from(envelope.ciphertext);
  badCt[0] ^= 0x01;
  const tampered = new StlsEnvelope(envelope.sessionId, envelope.iv, badCt, envelope.tag, envelope.mac);
  assert.throws(() => transport.decrypt(tampered), /MAC verification failed/);
  console.log("OK: T06 tampered ciphertext -> throws (MAC covers ciphertext)");
}

// T07/T08 — encryptJson / decryptJson
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);

  const payload = { jsonrpc: "2.0", method: "tools/call", id: 1, params: { name: "get_weather", city: "Roma" } };
  const wire = transport.encryptJson(payload);
  assert.deepEqual(transport.decryptJson(wire), payload);
  console.log("OK: T07 encryptJson/decryptJson round-trip (object)");

  const list = [1, "due", { tre: 3 }, null, true];
  const wireList = transport.encryptJson(list);
  assert.deepEqual(transport.decryptJson(wireList), list);
  console.log("OK: T08 encryptJson/decryptJson round-trip (array)");
}

// T09 — enc key and MAC key differ; legacy mode shares enc/dec keys
{
  const transport = new StlsTransport(freshKey(), SESSION_ID) as any;
  assert.notDeepEqual(transport.encAesKey, transport.encHmacKey);
  assert.deepEqual(transport.encAesKey, transport.decAesKey);
  assert.deepEqual(transport.encHmacKey, transport.decHmacKey);
  console.log("OK: T09 enc/MAC keys differ; legacy mode shares enc/dec keys");
}

// T10 — deterministic key derivation
{
  const key = freshKey();
  const t1 = new StlsTransport(key, SESSION_ID) as any;
  const t2 = new StlsTransport(key, SESSION_ID) as any;
  assert.deepEqual(t1.encAesKey, t2.encAesKey);
  assert.deepEqual(t1.encHmacKey, t2.encHmacKey);
  console.log("OK: T10 same key+session_id -> identical derived keys");
}

// T11 — StlsEnvelope toDict/fromDict round-trip
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("test"));
  const restored = StlsEnvelope.fromDict(envelope.toDict());
  assert.equal(restored.sessionId, envelope.sessionId);
  assert.deepEqual(restored.iv, envelope.iv);
  assert.deepEqual(restored.ciphertext, envelope.ciphertext);
  assert.deepEqual(restored.tag, envelope.tag);
  assert.deepEqual(restored.mac, envelope.mac);
  assert.deepEqual(transport.decrypt(restored), Buffer.from("test"));
  console.log("OK: T11 StlsEnvelope toDict/fromDict round-trip");
}

// T12 — empty plaintext
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.alloc(0));
  assert.deepEqual(transport.decrypt(envelope), Buffer.alloc(0));
  console.log("OK: T12 empty plaintext round-trips");
}

// T13 — large plaintext (1 MB)
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const plaintext = randomBytes(1024 * 1024);
  const envelope = transport.encrypt(plaintext);
  assert.deepEqual(transport.decrypt(envelope), plaintext);
  console.log("OK: T13 large plaintext (1 MB) round-trips");
}

// T14 — tampered version field
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("payload"));
  const wire = envelope.toDict();
  wire.version = "9.9"; // MAC/AAD were computed over version="1.0"
  assert.throws(() => transport.decrypt(StlsEnvelope.fromDict(wire)));
  console.log("OK: T14 tampered version field -> throws");
}

// T15 — tampered session_id field (via dict)
{
  const transport = new StlsTransport(freshKey(), SESSION_ID);
  const envelope = transport.encrypt(Buffer.from("payload"));
  const wire = envelope.toDict();
  wire.session_id = transport.sessionId + "-tampered";
  assert.throws(() => transport.decrypt(StlsEnvelope.fromDict(wire)), /Session ID mismatch/);
  console.log("OK: T15 tampered session_id field -> throws");
}

// T16 — direction c2s/s2c round-trip
{
  const key = freshKey();
  const client = new StlsTransport(key, SESSION_ID, "c2s");
  const server = new StlsTransport(key, SESSION_ID, "s2c");

  const request = client.encrypt(Buffer.from("request from the client"));
  assert.deepEqual(server.decrypt(request), Buffer.from("request from the client"));

  const response = server.encrypt(Buffer.from("response from the server"));
  assert.deepEqual(client.decrypt(response), Buffer.from("response from the server"));
  console.log("OK: T16 direction c2s/s2c bidirectional round-trip");
}

// T17 — direction keys are independent
{
  const key = freshKey();
  const client = new StlsTransport(key, SESSION_ID, "c2s") as any;
  const server = new StlsTransport(key, SESSION_ID, "s2c") as any;

  assert.deepEqual(client.encAesKey, server.decAesKey);
  assert.deepEqual(server.encAesKey, client.decAesKey);
  assert.deepEqual(client.encHmacKey, server.decHmacKey);
  assert.deepEqual(server.encHmacKey, client.decHmacKey);

  assert.notDeepEqual(client.encAesKey, client.decAesKey);
  assert.notDeepEqual(client.encHmacKey, client.decHmacKey);

  const envelope = (client as StlsTransport).encrypt(Buffer.from("for the server only"));
  assert.throws(() => (client as StlsTransport).decrypt(envelope), /MAC verification failed/);
  console.log("OK: T17 direction keys are independent; a transport can't decrypt its own output");
}

// T18 — legacy mode vs direction mode keys differ
{
  const key = freshKey();
  const legacy = new StlsTransport(key, SESSION_ID) as any;
  const labeled = new StlsTransport(key, SESSION_ID, "c2s") as any;

  assert.notDeepEqual(legacy.encAesKey, labeled.encAesKey);
  assert.notDeepEqual(legacy.encHmacKey, labeled.encHmacKey);

  const envelope = (legacy as StlsTransport).encrypt(Buffer.from("legacy payload"));
  assert.throws(() => (labeled as StlsTransport).decrypt(envelope), /MAC verification failed/);
  console.log("OK: T18 legacy and direction-mode keys differ, not interchangeable");
}

// T19 — invalid direction rejected
{
  const key = freshKey();
  assert.throws(() => new StlsTransport(key, SESSION_ID, "bogus" as any), /Invalid direction/);
  console.log("OK: T19 invalid direction value is rejected");
}

console.log("ALL CRYPTO UNIT CHECKS PASSED");
