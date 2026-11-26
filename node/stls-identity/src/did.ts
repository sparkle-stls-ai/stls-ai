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
