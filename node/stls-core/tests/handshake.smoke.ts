/**
 * handshake.smoke.ts
 * -------------------
 * Node mirror of python/stls-core/tests/test_stls_core_handshake.py (H01-H18)
 * and the PQC proof-tampering coverage added to test_pqc.py
 * (TestPQCProofTampering) — both for issue #36: the widened handshake
 * proof binds session_id, kid, protocol and the declared algorithm
 * fields via a dedicated HKDF-derived proof key, not just the nonce.
 *
 * No dedicated unit test file for handshake.ts/handshake-pqc.ts existed
 * before this — only PQC cross-language interop scripts and sidecar
 * smoke tests exercised these functions indirectly.
 *
 * Coverage:
 *   H01 — handshake with the "base" provider: session_id/kid/proof present
 *   H02 — verifyHandshakeProof() accepts the real client key
 *   H03 — verifyHandshakeProof() rejects a wrong key
 *   H04 — session_id is a valid UUID
 *   H05 — session registered in the InMemorySessionCache after handshake
 *   H06 — generateHandshakeRequest() produces a 32-byte nonce
 *   H07 — nonce too short -> HandshakeError(-32600)
 *   H08 — nonce not valid base64 -> HandshakeError(-32600)
 *   H09 — provider returning an empty key -> HandshakeError
 *   H10 — two handshakes produce different session_ids
 *   H11 — transport in cache encrypts/decrypts correctly
 *   H12-H17 — tampering session_id/kid/protocol/encryption/hmac/
 *             key_derivation in the response invalidates the proof
 *   H18 — the proof is not HMAC-SHA256(raw session_key, nonce)
 *   P01 — PQC: proof valid before tampering (manually built response,
 *         no liboqs/native KEM dependency needed for this check)
 *   P02 — PQC: tampering the same six fields invalidates the proof
 *   P03 — PQC: proof does not use the raw session_key directly either
 *   P04 — PQC: server_x25519_public_key/mlkem_ciphertext are NOT part
 *         of the proof input — tampering them doesn't affect proof
 *         validity (a KEM mismatch they'd cause is caught elsewhere,
 *         via the session_key the client itself would derive)
 */

import assert from "node:assert/strict";
import { createHmac, randomBytes, randomUUID } from "node:crypto";
import {
  KeySessionProviderFactory,
  KeySessionProviderType,
  InMemorySessionCache,
  generateHandshakeRequest,
  performHandshake,
  verifyHandshakeProof,
  HandshakeError,
  HandshakeResponse,
  verifyPqcHandshakeProof,
  PQCHandshakeResponse,
  computeHandshakeProof,
} from "../src/index.js";
import type { KeySessionProvider } from "../src/providers/base.js";

function freshCache(): InMemorySessionCache {
  return new InMemorySessionCache();
}

// H01 — base fields present
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const req = generateHandshakeRequest("test-client");
  const resp = await performHandshake(req, serverProvider, freshCache());

  assert.ok(resp.session_id);
  assert.ok(resp.kid);
  assert.ok(resp.proof);
  assert.equal(resp.encryption, "AES-256-GCM");
  assert.equal(resp.hmac, "HMAC-SHA256");
  assert.equal(resp.key_derivation, "HKDF-Expand-SHA256");
  console.log("OK: H01 handshake base fields present");
}

async function validHandshake(): Promise<{
  req: ReturnType<typeof generateHandshakeRequest>;
  resp: HandshakeResponse;
  sessionKey: Buffer;
}> {
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const clientProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.CLIENT,
    name: "base",
  });
  const req = generateHandshakeRequest("test-client");
  const resp = await performHandshake(req, serverProvider, freshCache());
  const kid = await clientProvider.getSessionKeyIdentifier();
  const sessionKey = await clientProvider.getSessionKeyData(kid);
  return { req, resp, sessionKey };
}

// H02 — proof valid
{
  const { req, resp, sessionKey } = await validHandshake();
  assert.equal(verifyHandshakeProof(sessionKey, req.nonce, resp), true);
  console.log("OK: H02 proof valid with the real client key");
}

// H03 — proof with wrong key -> false
{
  const { req, resp } = await validHandshake();
  const wrongKey = randomBytes(32);
  assert.equal(verifyHandshakeProof(wrongKey, req.nonce, resp), false);
  console.log("OK: H03 proof invalid with a wrong key");
}

// H04 — session_id is a UUID
{
  const { resp } = await validHandshake();
  assert.match(
    resp.session_id,
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
  );
  console.log("OK: H04 session_id is a valid UUID");
}

// H05 — session registered in cache
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const cache = freshCache();
  const req = generateHandshakeRequest("test-client");
  const resp = await performHandshake(req, serverProvider, cache);

  const transport = await cache.get(resp.session_id);
  assert.ok(transport);
  assert.equal(transport!.sessionId, resp.session_id);
  console.log("OK: H05 session registered in InMemorySessionCache");
}

// H06 — generateHandshakeRequest nonce is 32 bytes
{
  const req = generateHandshakeRequest("client-1");
  assert.equal(Buffer.from(req.nonce, "base64").length, 32);
  console.log("OK: H06 generated nonce is 32 bytes");
}

// H07 — nonce too short -> HandshakeError(-32600)
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const req = { client_id: "c", nonce: Buffer.from("short").toString("base64"), protocol: "1.0" };
  await assert.rejects(
    performHandshake(req, serverProvider, freshCache()),
    (err: unknown) => err instanceof HandshakeError && err.code === -32600,
  );
  console.log("OK: H07 nonce too short raises HandshakeError(-32600)");
}

// H08 — nonce not valid base64 -> HandshakeError(-32600)
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const req = { client_id: "c", nonce: "!!!not-base64!!!", protocol: "1.0" };
  await assert.rejects(
    performHandshake(req, serverProvider, freshCache()),
    (err: unknown) => err instanceof HandshakeError && err.code === -32600,
  );
  console.log("OK: H08 non-base64 nonce raises HandshakeError(-32600)");
}

// H09 — provider returning an empty key -> HandshakeError
{
  const nullProvider: KeySessionProvider = {
    getSessionType: () => KeySessionProviderType.SERVER,
    getSessionKeyIdentifier: async () => "kid-null",
    setSessionKeyIdentifier: () => {},
    getSessionKeyData: async () => Buffer.alloc(0),
    getPeerIdentity: () => null,
  };
  const req = generateHandshakeRequest("test-client");
  await assert.rejects(performHandshake(req, nullProvider, freshCache()), HandshakeError);
  console.log("OK: H09 empty session key raises HandshakeError");
}

// H10 — two handshakes -> different session_ids
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const cache = freshCache();
  const resp1 = await performHandshake(generateHandshakeRequest("A"), serverProvider, cache);
  const resp2 = await performHandshake(generateHandshakeRequest("B"), serverProvider, cache);
  assert.notEqual(resp1.session_id, resp2.session_id);
  console.log("OK: H10 two handshakes produce different session_ids");
}

// H11 — transport in cache encrypts/decrypts correctly
{
  const serverProvider = await KeySessionProviderFactory.getSessionProvider({
    sessionType: KeySessionProviderType.SERVER,
    name: "base",
  });
  const cache = freshCache();
  const resp = await performHandshake(generateHandshakeRequest("c"), serverProvider, cache);
  const transport = await cache.get(resp.session_id);
  const plaintext = Buffer.from("payload di test post-handshake", "utf-8");
  const recovered = transport!.decrypt(transport!.encrypt(plaintext));
  assert.deepEqual(recovered, plaintext);
  console.log("OK: H11 transport from cache round-trips correctly");
}

// H12-H17 — tampering each bound field invalidates the proof
{
  const tamperCases: Array<[keyof HandshakeResponse, string]> = [
    ["session_id", randomUUID()],
    ["kid", "attacker-chosen-kid"],
    ["protocol", "9.9"],
    ["encryption", "AES-128-CBC"],
    ["hmac", "HMAC-SHA1"],
    ["key_derivation", "none"],
  ];
  for (const [field, tamperedValue] of tamperCases) {
    const { req, resp, sessionKey } = await validHandshake();
    assert.equal(verifyHandshakeProof(sessionKey, req.nonce, resp), true);
    (resp as unknown as Record<string, string>)[field] = tamperedValue;
    assert.equal(
      verifyHandshakeProof(sessionKey, req.nonce, resp),
      false,
      `tampering ${field} should invalidate the proof`,
    );
  }
  console.log("OK: H12-H17 tampering session_id/kid/protocol/encryption/hmac/key_derivation invalidates the proof");
}

// H18 — proof is not HMAC-SHA256(raw session_key, nonce)
{
  const { req, resp, sessionKey } = await validHandshake();
  const nonceBytes = Buffer.from(req.nonce, "base64");
  const naiveProof = createHmac("sha256", sessionKey).update(nonceBytes).digest();
  const receivedProof = Buffer.from(resp.proof, "base64");
  assert.notDeepEqual(naiveProof, receivedProof);
  console.log("OK: H18 proof uses a dedicated derived key, not the raw session_key");
}

// ---------------------------------------------------------------------------
// PQC proof tampering (P01-P04) — no liboqs/native KEM dependency needed:
// computeHandshakeProof()/verifyPqcHandshakeProof() never touch the KEM
// machinery, so a manually-built PQCHandshakeResponse is enough.
// ---------------------------------------------------------------------------

function validPqcResponseAndKey(): {
  sessionKey: Buffer;
  nonce: string;
  resp: PQCHandshakeResponse;
} {
  const sessionKey = randomBytes(32);
  const nonce = randomBytes(32).toString("base64");
  const resp: PQCHandshakeResponse = {
    session_id: randomUUID(),
    kid: randomUUID(),
    proof: "",
    server_x25519_public_key: randomBytes(32).toString("base64"),
    mlkem_ciphertext: randomBytes(100).toString("base64"),
    protocol: "1.0",
    encryption: "AES-256-GCM",
    hmac: "HMAC-SHA256",
    key_derivation: "HKDF-SHA256+X25519+ML-KEM-768",
  };
  const proofBytes = computeHandshakeProof(
    sessionKey,
    resp.session_id, resp.kid, resp.protocol,
    resp.encryption, resp.hmac, resp.key_derivation,
    Buffer.from(nonce, "base64"),
  );
  resp.proof = proofBytes.toString("base64");
  return { sessionKey, nonce, resp };
}

// P01 — PQC proof valid before tampering
{
  const { sessionKey, nonce, resp } = validPqcResponseAndKey();
  assert.equal(verifyPqcHandshakeProof(sessionKey, nonce, resp), true);
  console.log("OK: P01 PQC proof valid before tampering");
}

// P02 — tampering each bound field invalidates the PQC proof
{
  const tamperCases: Array<[keyof PQCHandshakeResponse, string]> = [
    ["session_id", "attacker-chosen-session-id"],
    ["kid", "attacker-chosen-kid"],
    ["protocol", "9.9"],
    ["encryption", "AES-128-CBC"],
    ["hmac", "HMAC-SHA1"],
    ["key_derivation", "none"],
  ];
  for (const [field, tamperedValue] of tamperCases) {
    const { sessionKey, nonce, resp } = validPqcResponseAndKey();
    (resp as unknown as Record<string, string>)[field] = tamperedValue;
    assert.equal(
      verifyPqcHandshakeProof(sessionKey, nonce, resp),
      false,
      `tampering PQC ${field} should invalidate the proof`,
    );
  }
  console.log("OK: P02 tampering PQC session_id/kid/protocol/encryption/hmac/key_derivation invalidates the proof");
}

// P03 — PQC proof does not use the raw session_key directly
{
  const { sessionKey, nonce, resp } = validPqcResponseAndKey();
  const naiveProof = createHmac("sha256", sessionKey).update(Buffer.from(nonce, "base64")).digest();
  assert.notDeepEqual(naiveProof, Buffer.from(resp.proof, "base64"));
  console.log("OK: P03 PQC proof uses a dedicated derived key, not the raw session_key");
}

// P04 — KEM fields are not required for PQC proof validity
{
  const { sessionKey, nonce, resp } = validPqcResponseAndKey();
  resp.server_x25519_public_key = randomBytes(32).toString("base64");
  resp.mlkem_ciphertext = randomBytes(100).toString("base64");
  assert.equal(verifyPqcHandshakeProof(sessionKey, nonce, resp), true);
  console.log("OK: P04 KEM fields are not part of the PQC proof input");
}

console.log("All handshake.smoke.ts checks passed.");
