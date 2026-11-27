/**
 * verifier.ts
 * -----------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_verifier.py.
 *
 * Sentinel — verifies a presented NetworkAuthorityCredential before an
 * agent-to-agent call is authorized. Meant to be called as an
 * ADDITIONAL check, alongside (not instead of) existing API-key
 * validation — verify first, only proceed if every check passes.
 */

import { verifyCredentialSignature, verifyProofBoundPayload } from "./signing.js";
import { MRI_SECONDS } from "./status.js";
import {
  isNetworkAuthorityCredential,
  isOperationalCredential,
  type NetworkAuthorityCredential,
  type OperationalCredential,
  type VerifiableCredential,
  type VerifiablePresentation,
} from "./models.js";

/**
 * Raised for any failure — expired, revoked, bad signature, stale
 * status list. Callers should treat every instance the same way:
 * reject the request. The message is for logs/audit, not for
 * branching logic.
 */
export class CredentialVerificationError extends Error {}

/**
 * Narrow interface satisfied by trust-registry-client.ts's
 * TrustAnchorRegistryClient (Phase 4) — declared here as a duck type
 * so verifier.ts has no load-bearing import on that module, matching
 * this codebase's existing duck-typed-hook convention.
 */
export interface TrustAnchorRegistryClientLike {
  resolveHistorical(verificationMethodId: string, at: Date): Promise<Uint8Array | null>;
}

export interface SentinelOptions {
  /**
   * Maps verification_method id -> raw 32-byte Ed25519 public key — the
   * fast path, checked first, no I/O. Populate from
   * TrustAnchorRegistryClient.asTrustedKeys() for the CURRENT set of
   * trusted Authority keys.
   */
  trustedIssuerKeys: Map<string, Uint8Array>;
  /**
   * Optional fallback for verifySignature(). When a verificationMethod
   * isn't in trustedIssuerKeys, Sentinel asks the registry "was this
   * key valid for its Authority AT THE TIME this credential was
   * signed?" instead of failing immediately — this is what makes a
   * credential signed before an Authority rotated its key still
   * verify correctly, without keeping every historical key loaded in
   * the hot-path cache.
   */
  trustRegistry?: TrustAnchorRegistryClientLike;
  /**
   * The per-agent analogue of trustRegistry, for
   * verifyPresentationProof()'s holder binding (issue #42).
   * Deliberately a DIFFERENT option, not a reuse of trustRegistry:
   * Governing Authority keys resolve via the Trust Anchor Registry
   * (one shared store, keyed by authorityId), while an agent's own key
   * history lives inside its OWN DIDDocument (one per agentId, see
   * did.ts's rotateAgentSigningKey()/resolveHolderKeyAt()) — a
   * structurally different lookup. Pass
   * makeDidDocumentHolderKeyResolver(didStore, governingAuthorityHost)
   * for the common case. undefined (default) = disabled: a holder key
   * not already in trustedIssuerKeys is simply rejected, same behavior
   * as before this option existed.
   */
  holderKeyResolver?: (verificationMethodId: string, at: Date) => Promise<Uint8Array | null>;
}

export class Sentinel {
  private readonly trustedKeys: Map<string, Uint8Array>;
  private readonly trustRegistry: TrustAnchorRegistryClientLike | undefined;
  private readonly holderKeyResolver:
    | ((verificationMethodId: string, at: Date) => Promise<Uint8Array | null>)
    | undefined;

  constructor(options: SentinelOptions) {
    this.trustedKeys = options.trustedIssuerKeys;
    this.trustRegistry = options.trustRegistry;
    this.holderKeyResolver = options.holderKeyResolver;
  }

  async verifySignature(credential: VerifiableCredential): Promise<void> {
    const proof = credential.proof;
    if (!proof) {
      throw new CredentialVerificationError("credential is unsigned");
    }

    let signer = this.trustedKeys.get(proof.verificationMethod);
    if (signer === undefined && this.trustRegistry !== undefined) {
      // Fallback path: not a currently-trusted key — but it might be
      // one the issuing Authority has since rotated away from, which
      // is not the same as "untrusted". Resolve it against the
      // credential's OWN signing time, not "now".
      const resolved = await this.trustRegistry.resolveHistorical(
        proof.verificationMethod,
        new Date(proof.created),
      );
      signer = resolved ?? undefined;
    }
    if (signer === undefined) {
      throw new CredentialVerificationError(
        `unknown or untrusted verification method: ${proof.verificationMethod}`,
      );
    }

    // Must match issuer.ts's signCredentialPayload() byte-for-byte —
    // same RFC 8785 canonicalization, not a local approximation. Any
    // divergence here makes every signature fail to verify.
    const ok = verifyCredentialSignature(credential, signer);
    if (!ok) {
      throw new CredentialVerificationError("signature verification failed");
    }
  }

  verifyValidityWindow(credential: VerifiableCredential): void {
    const now = new Date();
    if (now < new Date(credential.validFrom)) {
      throw new CredentialVerificationError("credential not yet valid");
    }
    if (credential.validUntil && now > new Date(credential.validUntil)) {
      throw new CredentialVerificationError("credential expired");
    }
  }

  /**
   * Caller supplies the already-fetched status bit + its fetch
   * timestamp, so Sentinel can enforce the MRI without owning the HTTP
   * fetch/cache itself.
   */
  verifyNotRevoked(
    _credential: VerifiableCredential,
    options: { statusBitstringFetchTime: Date; isRevoked: boolean },
  ): void {
    const ageSeconds = (Date.now() - options.statusBitstringFetchTime.getTime()) / 1000;
    if (ageSeconds > MRI_SECONDS) {
      throw new CredentialVerificationError(
        `status list stale (${Math.round(ageSeconds)}s > ${MRI_SECONDS}s MRI) — fail closed`,
      );
    }
    if (options.isRevoked) {
      throw new CredentialVerificationError("credential revoked");
    }
  }

  /**
   * Full check, in fail-fast order: cheapest/least-sensitive first,
   * signature before trusting any other field.
   */
  async verify(
    credential: VerifiableCredential,
    options: { statusBitstringFetchTime: Date; isRevoked: boolean },
  ): Promise<void> {
    this.verifyValidityWindow(credential);
    await this.verifySignature(credential);
    this.verifyNotRevoked(credential, options);
  }

  /**
   * Full Phase 3 check: an OperationalCredential is only as good as the
   * Grantor's own standing at verification time — not just at the
   * moment it was issued.
   *
   *   1. The Grantor's NetworkAuthorityCredential must itself verify.
   *   2. The OperationalCredential's `issuer` must be exactly the DID
   *      the Grantor's identity credential asserts.
   *   3. The OperationalCredential must itself verify, on its own,
   *      separate status list.
   */
  async verifyDelegationChain(
    operationalCredential: OperationalCredential,
    options: {
      grantorIdentityCredential: NetworkAuthorityCredential;
      grantorIdentityFetchTime: Date;
      grantorIdentityIsRevoked: boolean;
      operationalFetchTime: Date;
      operationalIsRevoked: boolean;
    },
  ): Promise<void> {
    await this.verify(options.grantorIdentityCredential, {
      statusBitstringFetchTime: options.grantorIdentityFetchTime,
      isRevoked: options.grantorIdentityIsRevoked,
    });

    if (operationalCredential.issuer !== options.grantorIdentityCredential.credentialSubject.id) {
      throw new CredentialVerificationError(
        "operational credential issuer does not match the Grantor's own identity " +
          "credential subject — possible identity substitution",
      );
    }

    await this.verify(operationalCredential, {
      statusBitstringFetchTime: options.operationalFetchTime,
      isRevoked: options.operationalIsRevoked,
    });
  }

  /**
   * Verifies the PRESENTATION's own proof — holder binding, not
   * credential validity. Credential validity is verified separately,
   * via verify()/verifyDelegationChain() on the unpacked credentials —
   * this method does not touch those at all.
   *
   * SECURITY: `challenge` MUST be covered by the signature, not
   * checked as a separate plaintext comparison — a signature that
   * covers only {holder, verifiableCredential} would let anyone
   * holding a captured, validly-signed VP overwrite proof.challenge
   * with a newly-issued one and replay it, no private key needed. The
   * canonical payload therefore includes proof-minus-proofValue (so
   * challenge, created and verificationMethod are all signed over),
   * mirroring presentation.ts's buildVerifiablePresentation() exactly.
   *
   * Async since the holderKeyResolver fallback (unlike the
   * trustedIssuerKeys Map lookup) does I/O — same reason
   * verifySignature() is already async.
   */
  async verifyPresentationProof(
    presentation: VerifiablePresentation,
    options: { expectedChallenge: string },
  ): Promise<void> {
    const proof = presentation.proof;
    if (!proof) {
      throw new CredentialVerificationError("presentation is unsigned");
    }

    if (proof.proofPurpose !== "authentication") {
      throw new CredentialVerificationError(
        `presentation proof has wrong purpose: ${JSON.stringify(proof.proofPurpose)} ` +
          "(expected 'authentication') — possible credential/presentation confusion",
      );
    }

    if (proof.challenge !== options.expectedChallenge) {
      throw new CredentialVerificationError(
        "presentation challenge does not match the one issued for this request — " +
          "possible replay of a captured presentation",
      );
    }

    let signer = this.trustedKeys.get(proof.verificationMethod);
    if (signer === undefined && this.holderKeyResolver !== undefined) {
      const resolved = await this.holderKeyResolver(proof.verificationMethod, new Date(proof.created));
      signer = resolved ?? undefined;
    }
    if (signer === undefined) {
      throw new CredentialVerificationError(
        `unknown or untrusted holder verification method: ${proof.verificationMethod}`,
      );
    }

    const ok = verifyProofBoundPayload(presentation, proof.proofValue, signer);
    if (!ok) {
      throw new CredentialVerificationError("presentation signature verification failed");
    }
  }

  /**
   * Splits a verified presentation's credentials into the shape
   * verify()/verifyDelegationChain() expect. Call this only AFTER
   * verifyPresentationProof() has passed — this method does no
   * cryptographic verification of its own, it only interprets
   * structure.
   *
   * Returns a single-element tuple [NetworkAuthorityCredential] for a
   * plain identity call, or [OperationalCredential,
   * NetworkAuthorityCredential] for a delegated call, regardless of
   * how the Requestor ordered them in verifiableCredential.
   */
  static unpackPresentation(
    presentation: VerifiablePresentation,
  ): [NetworkAuthorityCredential] | [OperationalCredential, NetworkAuthorityCredential] {
    const creds = presentation.verifiableCredential;

    if (creds.length === 1 && isNetworkAuthorityCredential(creds[0])) {
      return [creds[0]];
    }

    if (creds.length === 2) {
      const operational = creds.find(isOperationalCredential);
      const identity = creds.find(isNetworkAuthorityCredential);
      if (operational !== undefined && identity !== undefined) {
        return [operational, identity];
      }
    }

    throw new CredentialVerificationError(
      `presentation does not match a known shape: ${creds.length} credential(s) of types ` +
        `${JSON.stringify(creds.map((c) => c.type))} — expected either one ` +
        "NetworkAuthorityCredential, or one OperationalCredential plus its Grantor's " +
        "NetworkAuthorityCredential",
    );
  }
}
