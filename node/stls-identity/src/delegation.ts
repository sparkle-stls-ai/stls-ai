/**
 * delegation.ts
 * -------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_delegation.py.
 *
 * DelegationManager — lets an agent that already holds a valid
 * NetworkAuthorityCredential act as a Grantor, issuing scoped
 * OperationalCredentials to a Requestor agent.
 *
 * How the depth-1 limit is actually enforced: NOT by a counter (which
 * can be tampered with or omitted by a non-compliant implementation),
 * but structurally — issueOperationalCredential() requires the
 * Grantor's own, currently-valid NetworkAuthorityCredential. A
 * Requestor who has only ever received an OperationalCredential has no
 * NetworkAuthorityCredential of their own, so they structurally cannot
 * become a Grantor for further delegation using that credential.
 *
 * W185 alignment addition — role check, a SEPARATE gate from depth-1:
 * the Grantor's own identity credential must also declare the
 * "grantor" role before this will proceed. Holding *any* valid
 * identity credential is not, by itself, authorization to delegate.
 *
 * Revoking the Grantor's NetworkAuthorityCredential does NOT
 * automatically revoke OperationalCredentials it already issued —
 * those are tracked on their own status list; verifier.ts's
 * verifyDelegationChain() checks both independently.
 */

import { randomUUID } from "node:crypto";
import { AuditEntry, AuthResult, defaultAuditHook, type AuditHook } from "stls-core";
import { signCredentialPayload } from "./signing.js";
import { isNetworkAuthorityCredential, toPydanticIsoString } from "./models.js";
import type { CredentialProof, NetworkAuthorityCredential, OperationalCredential } from "./models.js";
import type { StatusListManagerLike } from "./issuer.js";
import { domainAndPathToDid } from "./did.js";
import { IdentityCredentialCacheFactory } from "./credential-cache.js";
import { IdentityKeystoreFactory } from "./keystore.js";

/**
 * Deliberately shorter than issuer.ts's DEFAULT_VALIDITY_MS (90 days)
 * — an operational grant for a specific task should not outlive the
 * task by months. Callers can override per-call.
 */
export const DEFAULT_OPERATIONAL_VALIDITY_MS = 30 * 24 * 60 * 60 * 1000;

export interface DelegationManagerOptions {
  grantorDid: string;
  grantorSigningKey: Uint8Array;
  grantorVerificationMethodId: string;
  statusListManager: StatusListManagerLike;
  statusListPublishUrl: string;
  auditHook?: AuditHook;
}

export interface IssueOperationalCredentialOptions {
  grantorIdentityCredential: NetworkAuthorityCredential;
  requestorDid: string;
  requestorClientId: string;
  scope: Record<string, unknown>;
  validForMs?: number;
}

/**
 * One instance per Grantor agent (not per Governing Authority — compare
 * with IdentityManager, which is authority-scoped). Constructed with
 * the Grantor's own signing key: the same Ed25519 key that key's DID
 * Document already advertises.
 */
export class DelegationManager {
  private readonly grantorDid: string;
  private readonly signingKey: Uint8Array;
  private readonly vmId: string;
  private readonly status: StatusListManagerLike;
  private readonly statusListUrl: string;
  private readonly auditHook: AuditHook;

  constructor(options: DelegationManagerOptions) {
    this.grantorDid = options.grantorDid;
    this.signingKey = options.grantorSigningKey;
    this.vmId = options.grantorVerificationMethodId;
    this.status = options.statusListManager;
    this.statusListUrl = options.statusListPublishUrl;
    this.auditHook = options.auditHook ?? defaultAuditHook();
  }

  /**
   * `grantorIdentityCredential`: the Grantor's OWN, currently-valid
   * NetworkAuthorityCredential. The caller is expected to have already
   * run Sentinel.verify() on it before calling this — DelegationManager
   * checks the validity *window* here (cheap, no I/O) as a sanity
   * guard, but does NOT re-check revocation status (that needs the
   * Governing Authority's status list, which this Grantor-scoped
   * manager has no access to by design).
   *
   * `scope`: domain-specific grant, opaque to this module.
   *
   * Throws if grantorIdentityCredential is not actually a
   * NetworkAuthorityCredential (the runtime half of the depth-1
   * enforcement — without this check, nothing would stop a caller
   * from passing an OperationalCredential whose subject happens to
   * match, defeating the structural argument by duck typing), does not
   * belong to this Grantor, does not declare the "grantor" role, or is
   * outside its validity window.
   */
  async issueOperationalCredential(
    options: IssueOperationalCredentialOptions,
  ): Promise<OperationalCredential> {
    const grantor = options.grantorIdentityCredential;

    if (!isNetworkAuthorityCredential(grantor)) {
      throw new TypeError(
        "grantorIdentityCredential must be a NetworkAuthorityCredential — an " +
          "OperationalCredential cannot itself justify issuing another " +
          "OperationalCredential, that is precisely the depth-1 limit",
      );
    }

    if (grantor.credentialSubject.id !== this.grantorDid) {
      throw new Error(
        "grantorIdentityCredential does not belong to this Grantor " +
          `(credential subject=${JSON.stringify(grantor.credentialSubject.id)}, ` +
          `expected=${JSON.stringify(this.grantorDid)})`,
      );
    }

    if (!grantor.credentialSubject.roles.includes("grantor")) {
      throw new Error(
        "grantorIdentityCredential does not declare the 'grantor' role " +
          `(roles=${JSON.stringify(grantor.credentialSubject.roles)}) — holding a valid ` +
          "identity is not, by itself, authorization to delegate. This is the W185 " +
          "alignment fix: knowing who signed is not the same as being authorized to " +
          "issue THIS kind of grant.",
      );
    }

    const now = new Date();
    if (
      now < new Date(grantor.validFrom) ||
      (grantor.validUntil && now > new Date(grantor.validUntil))
    ) {
      throw new Error(
        "grantorIdentityCredential is outside its validity window — a Grantor " +
          "cannot delegate on an expired identity credential",
      );
    }

    const statusIndex = await this.status.allocateIndex();
    const validUntil = new Date(now.getTime() + (options.validForMs ?? DEFAULT_OPERATIONAL_VALIDITY_MS));

    const withoutProof = {
      "@context": ["https://www.w3.org/ns/credentials/v2"],
      id: `urn:uuid:${randomUUID()}`,
      type: ["VerifiableCredential", "OperationalCredential"] as ["VerifiableCredential", "OperationalCredential"],
      schemaVersion: "1.0",
      issuer: this.grantorDid,
      validFrom: toPydanticIsoString(now),
      validUntil: toPydanticIsoString(validUntil),
      credentialSubject: {
        id: options.requestorDid,
        clientId: options.requestorClientId,
        scope: options.scope,
      },
      credentialStatus: {
        id: `${this.statusListUrl}#${statusIndex}`,
        type: "BitstringStatusListEntry" as const,
        statusPurpose: "revocation" as const,
        statusListIndex: statusIndex,
        statusListCredential: this.statusListUrl,
      },
      delegationDepth: 1 as const,
    };

    const signed = this.sign(withoutProof);

    await this.auditHook(
      new AuditEntry({
        timestamp: now,
        keyId: signed.id,
        clientId: options.requestorClientId,
        endpoint: "stls-identity:delegate",
        result: AuthResult.OK,
      }),
    );
    return signed;
  }

  async revoke(credential: OperationalCredential): Promise<void> {
    await this.status.revoke(credential.credentialStatus.statusListIndex);

    await this.auditHook(
      new AuditEntry({
        timestamp: new Date(),
        keyId: credential.id,
        clientId: credential.credentialSubject.clientId,
        endpoint: "stls-identity:delegate-revoke",
        result: AuthResult.REVOKED,
      }),
    );
  }

  /**
   * Same pattern as issuer.ts's IdentityManager's private sign() —
   * kept as a deliberate, small duplication rather than a premature
   * shared abstraction (mirrors the Python port's own choice).
   */
  private sign(
    credentialWithoutProof: Omit<OperationalCredential, "proof">,
  ): OperationalCredential {
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

export interface LoadDelegationManagerOptions {
  governingAuthorityHost: string;
  statusListManager: StatusListManagerLike;
  statusListPublishUrl: string;
  auditHook?: AuditHook;
  /**
   * The CURRENT verification method id to sign delegations with, if
   * the caller already knows it (e.g. via currentVerificationMethod()
   * on this agent's own DIDDocument, resolved separately). Defaults
   * to "{holderDid}#key-1", correct for a Grantor that has never
   * rotated its key (issue #42) but WRONG the moment it has: a
   * DelegationManager built with a stale id signs real Operational
   * Credentials with a verification method a verifier will find
   * CLOSED at the signature's own timestamp, failing verification
   * outright. Matters most for a long-running Grantor agent process
   * (built once at startup, cached) that might outlive a rotation of
   * its own key without restarting.
   */
  verificationMethodId?: string;
}

/**
 * Loads an already-bootstrapped-and-issued agent's own identity as a
 * ready-to-use DelegationManager — mirrors presentation.ts's
 * loadIdentityPresenter() exactly (same two stores, same resolution),
 * except it returns something that can ACT as a Grantor rather than
 * something that can PRESENT an identity.
 *
 * statusListManager/statusListPublishUrl are NOT resolved here —
 * they're shared platform infrastructure, not part of "my own
 * identity", so the caller passes them in exactly as it would to
 * DelegationManager's own constructor.
 *
 * Returns null if the agent has no signing key yet, no cached
 * credential yet, or its cached credential doesn't declare the
 * "grantor" role — the caller should then refuse to start up as a
 * Grantor rather than silently accept delegation requests it has no
 * standing to issue.
 */
export async function loadDelegationManager(
  agentId: string,
  options: LoadDelegationManagerOptions,
): Promise<DelegationManager | null> {
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
  if (!credential.credentialSubject.roles.includes("grantor")) {
    return null;
  }

  const holderDid = domainAndPathToDid(options.governingAuthorityHost, "agents", agentId);
  return new DelegationManager({
    grantorDid: holderDid,
    grantorSigningKey: holderKey,
    grantorVerificationMethodId: options.verificationMethodId ?? `${holderDid}#key-1`,
    statusListManager: options.statusListManager,
    statusListPublishUrl: options.statusListPublishUrl,
    auditHook: options.auditHook,
  });
}
