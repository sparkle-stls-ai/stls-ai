import fs from "node:fs";
import assert from "node:assert/strict";
import { deriveHybridKey, mlkemDecap, x25519Exchange } from "../../src/providers/pqc.js";

const vector = JSON.parse(
  fs.readFileSync(new URL("./pqc-vector.json", import.meta.url), "utf-8"),
);
const response = JSON.parse(
  fs.readFileSync(new URL("./pqc-vector-response.json", import.meta.url), "utf-8"),
);

const nonce = Buffer.from(vector.nonce, "base64");
const clientX25519Priv = Buffer.from(vector.client_x25519_private, "base64");
const clientMlkemSecret = Buffer.from(vector.client_mlkem_secret, "base64");
const serverX25519Pub = Buffer.from(response.server_x25519_public, "base64");
const mlkemCiphertext = Buffer.from(response.mlkem_ciphertext, "base64");

const x25519Secret = x25519Exchange(clientX25519Priv, serverX25519Pub);
const mlkemSecret = mlkemDecap(clientMlkemSecret, mlkemCiphertext);
const sessionKeyNode = deriveHybridKey(x25519Secret, mlkemSecret, nonce);

const sessionKeyPython = Buffer.from(response.session_key_python, "base64");

assert.equal(sessionKeyNode.toString("base64"), sessionKeyPython.toString("base64"));
console.log("OK: PQC hybrid session_key Node == Python");
console.log("  session_key =", sessionKeyNode.toString("base64"));
