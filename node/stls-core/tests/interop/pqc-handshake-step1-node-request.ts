import fs from "node:fs";
import { KeyProviderClientPQC, generatePqcHandshakeRequest } from "../../src/index.js";

// Full PQC handshake orchestration test (not just raw KEM/DH primitives,
// see pqc-step1/step2): Node acts as the STLS client, Python as the STLS
// server. Node generates the request via the actual KeyProviderClientPQC
// + generatePqcHandshakeRequest() wire-format helper used in real usage.

const clientProvider = new KeyProviderClientPQC();
clientProvider.start();

const request = generatePqcHandshakeRequest(
  "node-pqc-handshake-test",
  clientProvider.x25519PublicKey,
  clientProvider.mlkemPublicKey,
);

const clientSession = clientProvider.createProviderSession();

fs.writeFileSync(
  new URL("./pqc-handshake-request.json", import.meta.url),
  JSON.stringify(request),
);
fs.writeFileSync(
  new URL("./pqc-handshake-client-secrets.json", import.meta.url),
  JSON.stringify({
    x25519_private_key: clientSession.x25519PrivateKeyBytes!.toString("base64"),
    mlkem_secret_key: clientSession.mlkemSecretKeyBytes!.toString("base64"),
  }),
);

console.log("Node: PQCHandshakeRequest generated and written (client_id =", request.client_id, ")");
