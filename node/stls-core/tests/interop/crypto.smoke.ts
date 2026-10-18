import assert from "node:assert/strict";
import { StlsEnvelope, StlsTransport } from "../../src/crypto.js";

const sessionKey = Buffer.from(Array.from({ length: 32 }, (_, i) => i));
const sessionId = "test-session-id";
const plaintext = Buffer.from("hello stls interop", "utf-8");

const pythonVector = {
  version: "1.0",
  session_id: "test-session-id",
  iv: "AAECAwQFBgcICQoL",
  ciphertext: "lUL1ha97qJ4xMH92IK1yk/CL",
  tag: "awWo24sN9qvfvi2TMBWF6g==",
  mac: "0Ew7WxI3Um9A2VCun27ZVeGkMYeHVPwL+DiXlZnBM4c=",
};

// 1) Node must correctly decrypt the envelope produced by Python.
const transport = new StlsTransport(sessionKey, sessionId);
const envelopeFromPython = StlsEnvelope.fromDict(pythonVector);
const recovered = transport.decrypt(envelopeFromPython);
assert.equal(recovered.toString("utf-8"), "hello stls interop");
console.log("OK: Node decrypts Python-produced envelope");

// 2) Node encrypts with a random IV -> writes the envelope to a file.
//    Cross-language decryption (Python reads this file) is verified by
//    the bash script that runs this test (see the verification command).
const envelope = transport.encrypt(plaintext);
const fs = await import("node:fs");
fs.writeFileSync(
  new URL("./node-produced-envelope.json", import.meta.url),
  JSON.stringify(envelope.toDict()),
);
console.log("OK: Node-produced envelope written for Python round-trip check");

console.log("ALL INTEROP SMOKE CHECKS PASSED");
