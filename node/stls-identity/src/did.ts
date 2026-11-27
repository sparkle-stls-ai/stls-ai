/**
 * did.ts
 * ------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_did.py.
 *
 * Bootstraps a did:web identity for an STLS agent.
 *
 * Deliberately NOT reusing the agent's existing X25519 key (see the note
 * in models.ts): a fresh Ed25519 signing keypair is provisioned here
 * instead. X25519 (used everywhere else in stls-core for the PQC/Arqit
 * key exchange) is a Diffie-Hellman curve — it cannot sign anything.
 *
 * did:web resolution rule (https://w3c-ccg.github.io/did-method-web/):
 *     did:web:sofia.sparkle.example.com:agents:087
 *         -> https://sofia.sparkle.example.com/agents/087/did.json
 *     did:web:sofia.sparkle.example.com
 *         -> https://sofia.sparkle.example.com/.well-known/did.json
 */

import bs58 from "bs58";
import { ed25519 } from "@noble/curves/ed25519.js";

import {
  DID_DOCUMENT_CONTEXT,
  makeVerificationMethod,
  type DIDDocument,
  type VerificationMethod,
  toPydanticIsoString,
} from "./models.js";

// multicodec ed25519-pub, per did:key/multibase convention
const MULTIBASE_ED25519_PUB_PREFIX = new Uint8Array([0xed, 0x01]);

/**
 * base58btc, 'z' prefix — the multibase encoding W3C examples use for
 * Ed25519VerificationKey2020.
 */
export function multibaseEncodeEd25519Pub(rawPub: Uint8Array): string {
  const prefixed = new Uint8Array(MULTIBASE_ED25519_PUB_PREFIX.length + rawPub.length);
  prefixed.set(MULTIBASE_ED25519_PUB_PREFIX, 0);
  prefixed.set(rawPub, MULTIBASE_ED25519_PUB_PREFIX.length);
  return "z" + bs58.encode(prefixed);
}

/**
 * Inverse of multibaseEncodeEd25519Pub — reconstructs the raw 32-byte
 * Ed25519 public key from a DID Document's publicKeyMultibase string.
 */
export function decodeMultibaseEd25519PublicKey(multibaseStr: string): Uint8Array {
  if (!multibaseStr.startsWith("z")) {
    throw new Error(
      `expected multibase 'z' (base58btc) prefix, got: ${JSON.stringify(multibaseStr.slice(0, 1))}`,
    );
  }

  const raw = bs58.decode(multibaseStr.slice(1));
  const prefix = raw.slice(0, 2);
  const keyBytes = raw.slice(2);
  if (
    prefix.length !== MULTIBASE_ED25519_PUB_PREFIX.length ||
    prefix[0] !== MULTIBASE_ED25519_PUB_PREFIX[0] ||
    prefix[1] !== MULTIBASE_ED25519_PUB_PREFIX[1]
  ) {
    throw new Error(
      `unexpected multicodec prefix ${JSON.stringify(Array.from(prefix))}, expected ` +
        `${JSON.stringify(Array.from(MULTIBASE_ED25519_PUB_PREFIX))} (ed25519-pub) — ` +
        "this key may not be an Ed25519 public key",
    );
  }
  return keyBytes;
}

/** 'sofia.sparkle.example.com', 'agents', '087' -> 'did:web:sofia.sparkle.example.com:agents:087' */
export function domainAndPathToDid(host: string, ...pathSegments: string[]): string {
  let did = "did:web:" + host.replaceAll(":", "%3A");
  for (const seg of pathSegments) {
    did += ":" + seg;
  }
  return did;
}

/** Inverse of domainAndPathToDid — the URL a resolver must GET. */
export function didToResolutionUrl(did: string): string {
  if (!did.startsWith("did:web:")) {
    throw new Error(`expected a did:web DID, got: ${did}`);
  }
  const parts = did.slice("did:web:".length).split(":");
  const host = parts[0].replaceAll("%3A", ":");
  const pathSegments = parts.slice(1);
  if (pathSegments.length > 0) {
    return `https://${host}/${pathSegments.join("/")}/did.json`;
  }
  return `https://${host}/.well-known/did.json`;
}

/**
 * Narrow interface covering the one keystore method bootstrapSigningKey
 * needs — satisfied by IdentityKeystoreBase (keystore.ts, Phase 3),
 * declared here as a duck type so did.ts has no load-bearing import on
 * that module.
 */
export interface SigningKeystoreLike {
  getOrCreate(agentId: string): Promise<{ privateKey: Uint8Array; created: boolean }>;
}

/**
 * Load the agent's existing signing key from the keystore, or generate
 * and persist a new one on first bootstrap.
 *
 * Returns { privateKey, publicKey, created } — `created` tells the
 * caller whether this is a brand-new identity (should publish a fresh
 * DID Document) or a restart of an existing one (should keep serving
 * the DID Document already on record, unchanged).
 */
export async function bootstrapSigningKey(
  agentId: string,
  keystore: SigningKeystoreLike,
): Promise<{ privateKey: Uint8Array; publicKey: Uint8Array; created: boolean }> {
  const { privateKey, created } = await keystore.getOrCreate(agentId);
  return { privateKey, publicKey: ed25519.getPublicKey(privateKey), created };
}

export interface BuildDidDocumentArgs {
  host: string;
  agentPathSegments: string[];
  governingAuthorityDid: string;
  signingPublicKey: Uint8Array;
  now?: Date;
}

/**
 * Build (but do not yet publish) the DID Document for one agent.
 *
 * Two different `controller` fields, two different meanings:
 *
 *     DIDDocument.controller        -> governance of the DID as a whole
 *                                       (the Governing Authority: can
 *                                       deactivate/rotate in an emergency)
 *     VerificationMethod.controller -> who operates THIS key day-to-day
 *                                       (the agent itself — it holds and
 *                                       uses its own signing key; the
 *                                       Authority does not sign on its
 *                                       behalf)
 *
 * "centralized trust, decentralized identity format" means the
 * Authority anchors *who counts as legitimate*, not that it operates
 * every agent's key.
 */
export function buildDidDocument(args: BuildDidDocumentArgs): DIDDocument {
  const now = args.now ?? new Date();
  const did = domainAndPathToDid(args.host, ...args.agentPathSegments);
  const keyId = `${did}#key-1`;

  const vm = makeVerificationMethod({
    id: keyId,
    controller: did, // the agent operates its own key — not the Authority
    publicKeyMultibase: multibaseEncodeEd25519Pub(args.signingPublicKey),
    validFrom: toPydanticIsoString(now),
  });

  return {
    "@context": DID_DOCUMENT_CONTEXT,
    id: did,
    controller: args.governingAuthorityDid, // governance of the DID stays with the Authority
    verificationMethod: [vm],
    authentication: [keyId],
    assertionMethod: [keyId],
    created: toPydanticIsoString(now),
  };
}

/**
 * The per-agent key rotation surface (issue #42) — 1:1 port of the
 * corresponding additions to python/stls-identity/src/stls_identity/stls_id_did.py.
 */

/**
 * The verification method an agent's own key rotation makes it unsafe
 * to find by position: after a rotation, document.verificationMethod
 * holds every key generation ever issued, oldest first, so
 * verificationMethod[0] is the OLDEST key, not the current one. This
 * is the replacement — resolves "current" the same way
 * PostgresTrustAnchorRegistryStore.get() does: validUntil is null (and
 * not revoked).
 *
 * Throws if zero or more than one currently-active entry is found — a
 * document that was hand-edited or corrupted outside
 * rotateAgentSigningKey(), which always leaves exactly one current
 * entry.
 */
export function currentVerificationMethod(document: DIDDocument): VerificationMethod {
  const current = document.verificationMethod.filter(
    (vm) => !vm.validUntil && !vm.revokedAt,
  );
  if (current.length !== 1) {
    throw new Error(
      `${document.id}: expected exactly one currently-active verification method, found ${current.length}`,
    );
  }
  return current[0];
}

/**
 * The per-agent analogue of PostgresTrustAnchorRegistryStore's
 * getKeyValidAt()/getKeyValidAtByVerificationMethod() — was this key
 * generation the one valid for this agent's DID Document at time
 * `at`? Unlike the Trust Anchor Registry, there's no separate store to
 * query: everything needed is already in the one DIDDocument fetched
 * via DIDDocumentStoreBase.get(), since an agent's key history lives
 * IN its own document (issue #42), not in a side table.
 *
 * Callers should pass the signature's OWN signing time
 * (CredentialProof.created / PresentationProof.created), not "now".
 */
export function resolveHolderKeyAt(
  document: DIDDocument,
  verificationMethodId: string,
  at: Date,
): Uint8Array | null {
  for (const vm of document.verificationMethod) {
    if (vm.id !== verificationMethodId) continue;
    if (vm.validFrom && at < new Date(vm.validFrom)) continue;
    if (vm.validUntil && at >= new Date(vm.validUntil)) continue;
    if (vm.revokedAt && at >= new Date(vm.revokedAt)) continue;
    return decodeMultibaseEd25519PublicKey(vm.publicKeyMultibase);
  }
  return null;
}

/**
 * Inverse of domainAndPathToDid(host, "agents", agentId) + "#key-N" —
 * extracts the agentId segment from one of ITS OWN verification
 * method ids, so a resolver that only has the id string can find
 * which agent's DIDDocument to fetch. Returns null if
 * verificationMethodId doesn't belong to an agent DID under this host
 * (e.g. it's the Governing Authority's own key instead).
 */
export function agentIdFromVerificationMethodId(
  verificationMethodId: string,
  host: string,
): string | null {
  const did = verificationMethodId.split("#")[0];
  const prefix = domainAndPathToDid(host, "agents") + ":";
  if (!did.startsWith(prefix) || did === prefix) {
    return null;
  }
  return did.slice(prefix.length);
}

export interface RotateAgentSigningKeyArgs {
  newSigningPublicKey: Uint8Array;
  now?: Date;
}

/**
 * Rotates an agent's own signing key WITHIN its existing DIDDocument —
 * the per-agent analogue of PostgresTrustAnchorRegistryStore.put()'s
 * "close the previous key generation, open a new one" rotation path,
 * scoped to a single agent instead of the shared Trust Anchor
 * Registry. Pure/in-memory: the caller still has to persist the
 * result via DIDDocumentStoreBase.put() (see rotateSigningKey() below
 * for the full orchestration, including the private-key half).
 *
 * Keeps every prior key generation in verificationMethod (audit trail
 * + historical resolution via resolveHolderKeyAt()) but repoints
 * authentication/assertionMethod at ONLY the new key.
 */
export function rotateAgentSigningKey(
  document: DIDDocument,
  args: RotateAgentSigningKeyArgs,
): DIDDocument {
  const now = args.now ?? new Date();
  const oldVm = currentVerificationMethod(document);

  const nextN = document.verificationMethod.length + 1;
  const newKeyId = `${document.id}#key-${nextN}`;
  const newVm = makeVerificationMethod({
    id: newKeyId,
    controller: document.id,
    publicKeyMultibase: multibaseEncodeEd25519Pub(args.newSigningPublicKey),
    validFrom: toPydanticIsoString(now),
  });

  const updatedMethods = document.verificationMethod.map((vm) =>
    vm.id === oldVm.id ? { ...vm, validUntil: toPydanticIsoString(now) } : vm,
  );
  updatedMethods.push(newVm);

  return {
    ...document,
    verificationMethod: updatedMethods,
    authentication: [newKeyId],
    assertionMethod: [newKeyId],
  };
}

/**
 * Narrow interface covering the one keystore method rotateSigningKey
 * needs beyond SigningKeystoreLike — satisfied by IdentityKeystoreBase
 * (keystore.ts), declared here as a duck type so did.ts has no
 * load-bearing import on that module.
 */
export interface RotatingKeystoreLike extends SigningKeystoreLike {
  rotate(agentId: string): Promise<Uint8Array>;
}

/**
 * Narrow interface covering the one DID store method rotateSigningKey/
 * makeDidDocumentHolderKeyResolver need, declared here as a duck type
 * so did.ts has no load-bearing import on postgres-store.ts.
 */
export interface DidDocumentStoreLike {
  get(agentId: string): Promise<DIDDocument | null>;
  put(agentId: string, document: DIDDocument): Promise<void>;
}

/**
 * Full key rotation for an already-bootstrapped agent (issue #42):
 * generates a new private key (keystore.rotate(), which unconditionally
 * overwrites — the old private key is never needed again, only its
 * PUBLIC half, which stays resolvable via the updated DIDDocument),
 * then updates and persists the agent's DIDDocument so the OLD key
 * remains resolvable for signatures made before this call
 * (resolveHolderKeyAt()).
 *
 * Throws if this agent has no DID Document yet — this rotates an
 * EXISTING identity, it does not bootstrap a new one (see
 * bootstrapSigningKey()/buildDidDocument() for that).
 */
export async function rotateSigningKey(
  agentId: string,
  keystore: RotatingKeystoreLike,
  didStore: DidDocumentStoreLike,
  options: { now?: Date } = {},
): Promise<{ newPrivateKey: Uint8Array; document: DIDDocument }> {
  const document = await didStore.get(agentId);
  if (document === null) {
    throw new Error(
      `${agentId}: no DID Document on record — bootstrap this agent's identity before rotating its key`,
    );
  }

  const newPrivateKey = await keystore.rotate(agentId);
  const newPublicKey = ed25519.getPublicKey(newPrivateKey);
  const rotatedDocument = rotateAgentSigningKey(document, {
    newSigningPublicKey: newPublicKey,
    now: options.now,
  });
  await didStore.put(agentId, rotatedDocument);
  return { newPrivateKey, document: rotatedDocument };
}

/**
 * Builds the async resolver Sentinel's holderKeyResolver option
 * expects (verifier.ts) — the holder-key equivalent of
 * TrustAnchorRegistryClient.resolveHistorical(), but backed by each
 * agent's own DIDDocument instead of a shared registry (issue #42).
 *
 * Deliberately a closure over didStore rather than a new class: no
 * caching is needed here — this is already the slow, rare fallback
 * path, same posture as resolveHistorical() itself.
 */
export function makeDidDocumentHolderKeyResolver(
  didStore: DidDocumentStoreLike,
  governingAuthorityHost: string,
): (verificationMethodId: string, at: Date) => Promise<Uint8Array | null> {
  return async (verificationMethodId: string, at: Date): Promise<Uint8Array | null> => {
    const agentId = agentIdFromVerificationMethodId(verificationMethodId, governingAuthorityHost);
    if (agentId === null) return null;
    const document = await didStore.get(agentId);
    if (document === null) return null;
    return resolveHolderKeyAt(document, verificationMethodId, at);
  };
}
