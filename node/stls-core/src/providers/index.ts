export {
  KeySessionProviderType,
  KeySessionProviderBase,
  KeyProviderClientBase,
  KeyProviderServerBase,
  KeyProviderBase,
} from "./base.js";
export type {
  KeySessionProvider,
  KeyProviderClient,
  KeyProviderServer,
  KeyProvider,
} from "./base.js";

export {
  KeySessionProviderPQC,
  KeyProviderClientPQC,
  KeyProviderServerPQC,
  KeyProviderPQC,
  PQCKeyStore,
  mlkemGenerateKeypair,
  mlkemEncap,
  mlkemDecap,
  x25519GenerateKeypair,
  x25519Exchange,
  deriveHybridKey,
} from "./pqc.js";

export {
  KeyProviderArqitSidecar,
  KeyProviderClientArqitSidecar,
  KeyProviderServerArqitSidecar,
  ArqitSidecarKeySessionProvider,
} from "./arqit-sidecar.js";
export type { ArqitSidecarOptions } from "./arqit-sidecar.js";

export { KeyProviderFactory, KeySessionProviderFactory } from "./factory.js";
export type { GetSessionProviderOptions } from "./factory.js";

// KeySessionResolver itself is intentionally NOT exported — resolve()
// hands back raw key material, exactly what issue #5 ("Restrict
// stls-core's public API surface") closes off. getTransport() below is
// the public replacement; it uses KeySessionResolver internally.
export { getTransport } from "./session-establish.js";
export type { GetTransportOptions, GetTransportResult } from "./session-establish.js";
