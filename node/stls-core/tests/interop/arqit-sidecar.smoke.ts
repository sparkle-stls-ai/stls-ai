import assert from "node:assert/strict";
import {
  KeyProviderClientArqitSidecar,
  KeyProviderServerArqitSidecar,
} from "../../src/providers/arqit-sidecar.js";
import { InMemorySessionCache } from "../../src/session.js";
import { generateHandshakeRequest, performHandshake, verifyHandshakeProof } from "../../src/handshake.js";

const socketPath = process.env.ARQIT_SIDECAR_SOCKET ?? "/tmp/stls-arqit-sidecar-test.sock";

// STLS server: its session key is supplied by the Python sidecar (here
// with provider_name="base" to test only the RPC mechanics).
const serverProvider = new KeyProviderServerArqitSidecar({ socketPath, providerName: "base" });
await serverProvider.start();
const serverSession = serverProvider.createProviderSession();
console.log("OK: server connected to the Arqit sidecar, RPC session started");

const sessionCache = new InMemorySessionCache();
const req = generateHandshakeRequest("node-client-via-sidecar");
const response = await performHandshake(req, serverSession, sessionCache);
console.log("OK: performHandshake() completed using a key served by the sidecar, kid =", response.kid);

// STLS client: the same fixed "base" key served by the sidecar (no real
// negotiation — provider_name="base" is deterministic).
const clientProvider = new KeyProviderClientArqitSidecar({ socketPath, providerName: "base" });
await clientProvider.start();
const clientSession = clientProvider.createProviderSession();
const clientKid = await clientSession.getSessionKeyIdentifier();
const clientKey = await clientSession.getSessionKeyData(clientKid);

const verified = verifyHandshakeProof(clientKey, req.nonce, response);
assert.equal(verified, true);
console.log("OK: verifyHandshakeProof() succeeds — client and server share the same key via the sidecar");

// Real encrypted exchange over the session the server registered in the cache.
const serverTransport = await sessionCache.get(response.session_id);
assert.ok(serverTransport, "session not found in the cache");
const envelope = serverTransport!.encrypt(Buffer.from("secured-over-arqit-sidecar"));
const recovered = serverTransport!.decrypt(envelope);
assert.equal(recovered.toString("utf-8"), "secured-over-arqit-sidecar");
console.log("OK: real encrypt/decrypt over the STLS session served by the Arqit sidecar");

await serverProvider.stop();
await clientProvider.stop();
console.log("ALL ARQIT SIDECAR SMOKE CHECKS PASSED (provider_name=base — RPC mechanics, not real Arqit peering)");
