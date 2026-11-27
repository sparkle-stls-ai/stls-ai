/**
 * presentation.ts
 * ---------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_presentation.py.
 *
 * Requestor-side construction of a VerifiablePresentation — the bundle
 * that actually travels with an A2A request. One VerifiablePresentation
 * can carry both an OperationalCredential and its Grantor's own
 * NetworkAuthorityCredential, signed once with the Requestor's own key.
 *
 * `IdentityPresenter`/`loadIdentityPresenter`/`loadDelegationPresenter`/
 * `identityChallengeHandlerFromEnv` (the keystore/credential-cache-backed
 * loaders) live in this same file, same as the Python module, but are
 * added once keystore.ts and credential-cache.ts exist — see this
 * file's later sections.
 */

import { signProofBoundPayload } from "./signing.js";
import {
  PRESENTATION_HEADER,
  type NetworkAuthorityCredential,
  type OperationalCredential,
  type PresentationProof,
  toPydanticIsoString,
  type VerifiablePresentation,
} from "./models.js";
import { domainAndPathToDid } from "./did.js";
import { IdentityKeystoreFactory } from "./keystore.js";
import { IdentityCredentialCacheFactory, OperationalCredentialCacheFactory } from "./credential-cache.js";

export interface BuildVerifiablePresentationArgs {
  holderDid: string;
  holderSigningKey: Uint8Array; // raw 32-byte Ed25519 private key / seed
  holderVerificationMethodId: string;
  credentials: (NetworkAuthorityCredential | OperationalCredential)[];
  challenge: string;
  now?: Date;
}

/**
 * Builds and signs a VerifiablePresentation.
 *
 * SECURITY: `challenge` (and the proof's other fields) MUST be part of
 * what gets signed, not appended afterward — otherwise anyone holding a
 * captured, validly-signed VP could overwrite `proof.challenge` with a
 * newly-issued one and replay it, no private key needed, since the
 * signature itself would never change. Mirrored exactly in
 * `Sentinel.verifyPresentationProof()` (verifier.ts) — both sides must
 * canonicalize the identical shape or every signature fails to verify.
 */
export function buildVerifiablePresentation(
  args: BuildVerifiablePresentationArgs,
): VerifiablePresentation {
  const now = args.now ?? new Date();

  const proof: PresentationProof = {
    type: "DataIntegrityProof",
    cryptosuite: "eddsa-jcs-2022",
    created: toPydanticIsoString(now),
    verificationMethod: args.holderVerificationMethodId,
    proofPurpose: "authentication",
    challenge: args.challenge,
    proofValue: "", // placeholder — cannot sign over its own value
  };

  const vp: VerifiablePresentation = {
    "@context": ["https://www.w3.org/ns/credentials/v2"],
    type: ["VerifiablePresentation"],
    holder: args.holderDid,
    verifiableCredential: args.credentials,
    proof,
  };

  proof.proofValue = signProofBoundPayload(vp, args.holderSigningKey);
  return vp;
}

/**
 * Encodes a signed VerifiablePresentation for the wire (issue #15
 * gap #1): base64 of its JSON serialization, under PRESENTATION_HEADER —
 * the exact inverse of what RequestPresentationVerifier.verifyRequest()
 * decodes server-side (request-verifier.ts).
 */
export function attachIdentityPresentationHeader(
  vp: VerifiablePresentation,
): Record<string, string> {
  const encoded = Buffer.from(JSON.stringify(vp), "utf-8").toString("base64");
  return { [PRESENTATION_HEADER]: encoded };
}

// ---------------------------------------------------------------------------
// Requestor-side convenience: IdentityPresenter + the generalized loaders
// (issue #45/#51/#54). These depend on keystore.ts/credential-cache.ts,
// unlike the pure functions above.
// ---------------------------------------------------------------------------

/**
 * Requestor-side convenience: holds one agent's holder identity (DID,
 * signing key, credential(s) to present) and turns a server-issued
 * challenge into ready-to-send headers.
 *
 * Built to be passed directly as stls-a2a's `identityChallengeHandler`
 * hook — `presenter.onChallenge` matches that hook's
 * `(challenge: string) => Record<string, string> | Promise<Record<string, string>>`
 * shape exactly, so stls-a2a never needs to import stls-identity itself.
 */
export class IdentityPresenter {
  constructor(
    private readonly holderDid: string,
    private readonly holderSigningKey: Uint8Array,
    private readonly holderVerificationMethodId: string,
    private readonly credentials: (NetworkAuthorityCredential | OperationalCredential)[],
  ) {}

  /**
   * Builds a fresh VerifiablePresentation signed over `challenge` (a
   * new one every call — buildVerifiablePresentation() never reuses a
   * signature across challenges) and returns the header dict ready to
   * merge into a request.
   */
  onChallenge(challenge: string): Record<string, string> {
    const vp = buildVerifiablePresentation({
      holderDid: this.holderDid,
      holderSigningKey: this.holderSigningKey,
      holderVerificationMethodId: this.holderVerificationMethodId,
      credentials: this.credentials,
      challenge,
    });
    return attachIdentityPresentationHeader(vp);
  }
}

export interface LoadIdentityPresenterOptions {
  governingAuthorityHost: string;
  /**
   * The CURRENT verification method id to present with, if the caller
   * already knows it (e.g. via currentVerificationMethod() on this
   * agent's own DIDDocument, resolved separately — this function
   * never touches DIDDocumentStoreBase itself). Defaults to
   * "{holderDid}#key-1", correct for an agent that has never rotated
   * its key (issue #42) but STALE — the wrong id — the moment it has.
   */
  verificationMethodId?: string;
}

/**
 * Loads an already-bootstrapped-and-issued agent's identity as an
 * IdentityPresenter — any Requestor that already has a signing key in
 * IdentityKeystoreFactory and a cached credential in
 * IdentityCredentialCacheFactory can load itself with this one call,
 * instead of hand-rolling key/credential/DID resolution per agent.
 *
 * Returns null if the agent has no signing key yet, or no cached
 * credential yet — the caller's own identityChallengeHandler should
 * then stay unconfigured, same off-by-default posture as not setting
 * one at all.
 */
export async function loadIdentityPresenter(
  agentId: string,
  options: LoadIdentityPresenterOptions,
): Promise<IdentityPresenter | null> {
  const keystore = IdentityKeystoreFactory.build();
  const holderKey = await keystore.get(agentId);
  if (holderKey === null) {
    return null;
  }

  const cache = IdentityCredentialCacheFactory.build();
  const credential = await cache.get(agentId);
  if (credential === null) {
    return null;
  }

  const holderDid = domainAndPathToDid(options.governingAuthorityHost, "agents", agentId);
  return new IdentityPresenter(
    holderDid,
    holderKey,
    options.verificationMethodId ?? `${holderDid}#key-1`,
    [credential],
  );
}

export interface LoadDelegationPresenterOptions {
  governingAuthorityHost: string;
  /** Same meaning and same STALE-after-rotation caveat as LoadIdentityPresenterOptions's own field (issue #42). */
  verificationMethodId?: string;
}

/**
 * Loads an already-received Operational Credential (a delegation) as
 * a ready-to-present IdentityPresenter — for a Requestor presenting a
 * delegated permission instead of its own plain identity.
 *
 * Deliberately does NOT resolve `grantorIdentityCredential` itself —
 * the Requestor must already have it (e.g. via a cached copy fetched
 * from whichever platform issued the delegation). This mirrors
 * node/stls-identity's delegation.ts validation posture: the Grantor's
 * identity is always a separately-supplied piece of data, never
 * auto-resolved by this package.
 *
 * Returns null if agentId has no signing key yet, or no cached
 * Operational Credential under operationalCredentialId.
 */
export async function loadDelegationPresenter(
  agentId: string,
  operationalCredentialId: string,
  grantorIdentityCredential: NetworkAuthorityCredential,
  options: LoadDelegationPresenterOptions,
): Promise<IdentityPresenter | null> {
  const keystore = IdentityKeystoreFactory.build();
  const holderKey = await keystore.get(agentId);
  if (holderKey === null) {
    return null;
  }

  const cache = OperationalCredentialCacheFactory.build();
  const operationalCredential = await cache.get(agentId, operationalCredentialId);
  if (operationalCredential === null) {
    return null;
  }

  const holderDid = domainAndPathToDid(options.governingAuthorityHost, "agents", agentId);
  return new IdentityPresenter(
    holderDid,
    holderKey,
    options.verificationMethodId ?? `${holderDid}#key-1`,
    [operationalCredential, grantorIdentityCredential],
  );
}

export interface IdentityChallengeHandlerFromEnvOptions {
  agentIdEnvVar?: string;
  hostEnvVar?: string;
}

/**
 * One-call wiring for stls-a2a's `identityChallengeHandler`, entirely
 * driven by environment variables — no per-agent boilerplate needed
 * in the caller.
 *
 * `agentIdEnvVar` unset (default: `IDENTITY_AGENT_ID`) means "this
 * process presents no identity" — returns undefined, same
 * off-by-default posture as every other piece of issue #15/#45:
 * setting ONE env var is what turns identity presentation on.
 *
 * stls-a2a itself never calls this and never imports stls-identity —
 * the caller imports stls-identity, calls this, and passes the result
 * into stls-a2a's already-generic, duck-typed parameter.
 *
 * Throws if `agentIdEnvVar` is set but `hostEnvVar` isn't — a
 * half-configured identity is worse than none, fail loudly rather
 * than silently presenting nothing.
 */
export async function identityChallengeHandlerFromEnv(
  options: IdentityChallengeHandlerFromEnvOptions = {},
): Promise<((challenge: string) => Record<string, string>) | undefined> {
  const agentIdEnvVar = options.agentIdEnvVar ?? "IDENTITY_AGENT_ID";
  const hostEnvVar = options.hostEnvVar ?? "GOVERNING_AUTHORITY_HOST";

  const agentId = (process.env[agentIdEnvVar] ?? "").trim();
  if (!agentId) {
    return undefined;
  }

  const host = (process.env[hostEnvVar] ?? "").trim();
  if (!host) {
    throw new Error(
      `${agentIdEnvVar}=${JSON.stringify(agentId)} is set but ${hostEnvVar} is not — ` +
        "both are required to resolve this agent's own DID.",
    );
  }

  const presenter = await loadIdentityPresenter(agentId, { governingAuthorityHost: host });
  return presenter ? presenter.onChallenge.bind(presenter) : undefined;
}
