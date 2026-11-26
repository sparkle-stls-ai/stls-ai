/**
 * stls-identity (Node.js)
 * ------------------------
 * TypeScript port of python/stls-identity. See README.md for the
 * porting status and what remains out of scope (bridge.ts/
 * trust-registry-bridge.ts's FastAPI route wiring, and actually wiring
 * this package into node/stls-a2a or node/stls-mcp — see issue #47).
 */

export * from "./models.js";
export * from "./did.js";
export * from "./signing.js";
export * from "./presentation.js";
export * from "./verifier.js";
export * from "./request-verifier.js";
export * from "./status.js";
export * from "./keystore.js";
export * from "./credential-cache.js";
export * from "./postgres-store.js";
export * from "./trust-registry-store.js";
export * from "./trust-registry-client.js";
export * from "./issuer.js";
export * from "./delegation.js";

export const STLS_IDENTITY_NODE_VERSION = "0.1.4";
