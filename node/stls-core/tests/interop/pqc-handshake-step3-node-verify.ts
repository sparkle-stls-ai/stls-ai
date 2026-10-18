import fs from "node:fs";
import assert from "node:assert/strict";
import {
  StlsEnvelope,
  StlsTransport,
  deriveSessionKeyClient,
  verifyPqcHandshakeProof,
  type PQCHandshakeResponse,
} from "../../src/index.js";

const request = JSON.parse(
  fs.readFileSync(new URL("./pqc-handshake-request.json", import.meta.url), "utf-8"),
);
const clientSecrets = JSON.parse(
  fs.readFileSync(new URL("./pqc-handshake-client-secrets.json", import.meta.url), "utf-8"),
);
const responseRaw = JSON.parse(
  fs.readFileSync(new URL("./pqc-handshake-response.json", import.meta.url), "utf-8"),
);
const response = responseRaw as PQCHandshakeResponse;
const envelopeFromPython = responseRaw.envelope_from_python;

const clientX25519Priv = Buffer.from(clientSecrets.x25519_private_key, "base64");
const clientMlkemSecret = Buffer.from(clientSecrets.mlkem_secret_key, "base64");

const sessionKey = deriveSessionKeyClient(clientX25519Priv, clientMlkemSecret, response, request.nonce);

const proofOk = verifyPqcHandshakeProof(sessionKey, request.nonce, response);
assert.equal(proofOk, true, "verifyPqcHandshakeProof() failed — derived key does not match the server's proof");
console.log("OK: verifyPqcHandshakeProof() succeeds — Node derived the same session_key as Python");

const transport = new StlsTransport(sessionKey, response.session_id);

const recovered = transport.decrypt(StlsEnvelope.fromDict(envelopeFromPython));
assert.equal(recovered.toString("utf-8"), "hello-from-python-pqc-handshake");
console.log("OK: Node decrypts Python's envelope using the session established via the real PQC handshake");

const nodeEnvelope = transport.encrypt(Buffer.from("hello-from-node-pqc-handshake", "utf-8"));
fs.writeFileSync(
  new URL("./pqc-handshake-node-envelope.json", import.meta.url),
  JSON.stringify(nodeEnvelope.toDict()),
);
console.log("OK: Node's envelope written for Python to decrypt");
