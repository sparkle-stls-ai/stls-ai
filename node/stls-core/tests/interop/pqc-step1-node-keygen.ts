import fs from "node:fs";
import {
  mlkemGenerateKeypair,
  x25519GenerateKeypair,
} from "../../src/providers/pqc.js";

const x25519Kp = x25519GenerateKeypair();
const mlkemKp = mlkemGenerateKeypair();
const nonce = Buffer.from(Array.from({ length: 32 }, (_, i) => i)); // deterministic, for the test

fs.writeFileSync(
  new URL("./pqc-vector.json", import.meta.url),
  JSON.stringify({
    nonce: nonce.toString("base64"),
    client_x25519_private: x25519Kp.secretKey.toString("base64"),
    client_x25519_public: x25519Kp.publicKey.toString("base64"),
    client_mlkem_secret: mlkemKp.secretKey.toString("base64"),
    client_mlkem_public: mlkemKp.publicKey.toString("base64"),
  }),
);
console.log("Node: X25519 + ML-KEM-768 keypairs generated, public keys written to pqc-vector.json");
