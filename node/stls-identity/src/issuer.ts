/**
 * issuer.ts
 * ---------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_issuer.py.
 *
 * IdentityManager — the Grantor-side counterpart to node/stls-core's
 * ApiKeyManager. Design mirrors it deliberately:
 *   ApiKeyManager.validate()   <-> Sentinel.verify()      (verifier.ts)
 *   ApiKeyManager's KeyRecord  <-> NetworkAuthorityCredential (models.ts)
 *   ApiKeyManager audit log    <-> IdentityManager logs issuance the same way
 *   RedisKeyCache / revoked    <-> StatusListManager      (status.ts)
 *
 * Both systems run in parallel through the whole migration: issuing a
 * NetworkAuthorityCredential here does NOT touch KeyRecord,
 * RedisKeyCache, or the existing /a2a auth path in any way.
 */

import { randomUUID } from "node:crypto";
import { AuditEntry, AuthResult, defaultAuditHook, type AuditHook } from "stls-core";
import { signCredentialPayload } from "./signing.js";
import { KeyStorageTier, toPydanticIsoString, type CredentialProof, type NetworkAuthorityCredential } from "./models.js";

/** Independent of the API key's own expiresAt by design. */
export const DEFAULT_VALIDITY_MS = 90 * 24 * 60 * 60 * 1000;

/**
 * Narrow interface satisfied by status.ts's StatusListManager —
 * declared here as a duck type (same convention verifier.ts uses for
 * its trust-registry dependency) so issuer.ts has no load-bearing
 * import on status.ts, and tests can supply a trivial in-memory
 * implementation without a real Redis client.
 */
export interface StatusListManagerLike {
  allocateIndex(): Promise<number>;
  revoke(index: number): Promise<void>;
}

export interface IdentityManagerOptions {
  governingAuthorityDid: string;
  signingKey: Uint8Array;
  signingVerificationMethodId: string;
  statusListManager: StatusListManagerLike;
  statusListPublishUrl: string;
  auditHook?: AuditHook;
}

export interface IssueIdentityCredentialOptions {
  agentDid: string;
  clientId: string; // bridges to the existing KeyRecord.clientId
  validForMs?: number;
  roles?: string[];
  principalRef?: string;
  keyStorageTier?: KeyStorageTier;
}

/** One instance per Governing Authority. Holds the Ed25519 signing key used to sign every credential this authority issues. */
export class IdentityManager {
  private readonly issuerDid: string;
  private readonly signingKey: Uint8Array;
  private readonly vmId: string;
  private readonly status: StatusListManagerLike;
  private readonly statusListUrl: string;
  private readonly auditHook: AuditHook;

  constructor(options: IdentityManagerOptions) {
    this.issuerDid = options.governingAuthorityDid;
    this.signingKey = options.signingKey;
    this.vmId = options.signingVerificationMethodId;
    this.status = options.statusListManager;
    this.statusListUrl = options.statusListPublishUrl;
    this.auditHook = options.auditHook ?? defaultAuditHook();
  }

  /**
   * Issue a NetworkAuthorityCredential for one agent.
   *
   * Mirrors ApiKeyManager's posture: the credential id is server-
   * generated (urn:uuid), never client-controlled.
   *
   * `roles`: required (must include "grantor") if this agent will
   * ever call DelegationManager.issueOperationalCredential() — see
   * that method's role check. Empty by default.
   */
  async issueIdentityCredential(
    options: IssueIdentityCredentialOptions,
  ): Promise<NetworkAuthorityCredential> {
    const now = new Date();
    const statusIndex = await this.status.allocateIndex();
    const validUntil = new Date(now.getTime() + (options.validForMs ?? DEFAULT_VALIDITY_MS));

    const withoutProof = {
      "@context": ["https://www.w3.org/ns/credentials/v2"],
      id: `urn:uuid:${randomUUID()}`,
      type: ["VerifiableCredential", "NetworkAuthorityCredential"] as [
        "VerifiableCredential",
        "NetworkAuthorityCredential",
      ],
      schemaVersion: "1.0",
      issuer: this.issuerDid,
      validFrom: toPydanticIsoString(now),
      validUntil: toPydanticIsoString(validUntil),
      credentialSubject: {
        id: options.agentDid,
        clientId: options.clientId,
        roles: options.roles ?? [],
        keyStorageTier: options.keyStorageTier ?? KeyStorageTier.Tier1Software,
        ...(options.principalRef !== undefined ? { principalRef: options.principalRef } : {}),
      },
      credentialStatus: {
        id: `${this.statusListUrl}#${statusIndex}`,
        type: "BitstringStatusListEntry" as const,
        statusPurpose: "revocation" as const,
        statusListIndex: statusIndex,
        statusListCredential: this.statusListUrl,
      },
    };

    const signed = this.sign(withoutProof);

    await this.auditHook(
      new AuditEntry({
        timestamp: now,
        // Not a KeyRecord key_id, but the same field slot — the
        // credential id is this system's equivalent.
        keyId: signed.id,
        clientId: options.clientId,
        endpoint: "stls-identity:issue",
        result: AuthResult.OK,
      }),
    );
    return signed;
  }

  async revoke(credential: NetworkAuthorityCredential): Promise<void> {
    await this.status.revoke(credential.credentialStatus.statusListIndex);

    await this.auditHook(
      new AuditEntry({
        timestamp: new Date(),
        keyId: credential.id,
        clientId: credential.credentialSubject.clientId,
        endpoint: "stls-identity:revoke",
        result: AuthResult.REVOKED,
      }),
    );
  }

  /**
   * Detached Ed25519 signature over the JCS-canonicalized (RFC 8785)
   * JSON of the credential, with NO `proof` key present at all (not
   * even a placeholder) — must match verifier.ts's verifySignature()
   * byte-for-byte: same payload shape, same canonicalization.
   */
  private sign(
    credentialWithoutProof: Omit<NetworkAuthorityCredential, "proof">,
  ): NetworkAuthorityCredential {
    const proofValue = signCredentialPayload(credentialWithoutProof, this.signingKey);
    const proof: CredentialProof = {
      type: "DataIntegrityProof",
      cryptosuite: "eddsa-jcs-2022",
      created: toPydanticIsoString(new Date()),
      verificationMethod: this.vmId,
      proofPurpose: "assertionMethod",
      proofValue,
    };
    return { ...credentialWithoutProof, proof };
  }
}
