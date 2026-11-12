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

// x25519PrivateKeyBytes/mlkemSecretKeyBytes are private on
// KeySessionProviderPQC (issue #5, "Restrict stls-core's public API
// surface") — this interop fixture generator is white-box test code
// within the same package, deliberately reaching past that to persist
// the raw KEM secrets for step3's cross-language derivation check
// (the only legitimate reason to ever do this outside the class).
const clientSessionInternal = clientSession as unknown as {
  x25519PrivateKeyBytes: Buffer;
  mlkemSecretKeyBytes: Buffer;
};

fs.writeFileSync(
  new URL("./pqc-handshake-request.json", import.meta.url),
  JSON.stringify(request),
);
fs.writeFileSync(
  new URL("./pqc-handshake-client-secrets.json", import.meta.url),
  JSON.stringify({
    x25519_private_key: clientSessionInternal.x25519PrivateKeyBytes.toString("base64"),
    mlkem_secret_key: clientSessionInternal.mlkemSecretKeyBytes.toString("base64"),
  }),
);

console.log("Node: PQCHandshakeRequest generated and written (client_id =", request.client_id, ")");
