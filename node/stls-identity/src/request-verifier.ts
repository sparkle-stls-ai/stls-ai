/**
 * request-verifier.ts
 * ---------------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_request_verifier.py.
 *
 * Bridge between Sentinel (this package's verification engine) and
 * stls-a2a's optional identity_verifier hook (issue #15 gap #1).
 * stls-a2a defines that hook purely as a duck type and never imports
 * stls-identity — this module is the one-way dependency that makes the
 * hook concrete: it only reads a request-like object's `headers`
 * (duck-typed — works with any object exposing a `.headers` map/Headers
 * instance), never importing anything from stls-a2a, so stls-identity
 * stays fully usable without stls-a2a installed.
 */

import { PRESENTATION_HEADER, type VerifiableCredential } from "./models.js";
import { CredentialVerificationError, Sentinel } from "./verifier.js";

/**
 * Matches the minimal duck type stls-a2a's identity_verifier hook
 * expects back — `ok`/`error` is the part that hook has always read;
 * `requestorDid`/`grantorDid`/`scope` are additive (issue #74) so the
 * caller can carry the VERIFIED, unpacked credential past the gate to
 * wherever the real effect happens, instead of discarding it the
 * moment `ok` is true. 1:1 port of python's
 * PresentationVerificationResult (stls_id_request_verifier.py) —
 * same fields, same semantics for `scope` being `undefined` vs. an
 * intentionally empty `{}`.
 */
export interface PresentationVerificationResult {
  ok: boolean;
  error?: string;
  /** The presenting holder's own DID, set whenever ok=true — the
   * Grantor's DID for a plain identity presentation, the Requestor's
   * DID for a delegated one. */
  requestorDid?: string;
  /** Set only for a delegated presentation — undefined for a plain
   * identity presentation, since there's no delegation involved. */
  grantorDid?: string;
  /** The OperationalCredential's own `scope`, verbatim — undefined
   * for a plain identity presentation (no grant to carry), a dict
   * (possibly empty) for a delegated one. */
  scope?: Record<string, unknown>;
}

/**
 * Caller-supplied: given a credential, returns { fetchTime, isRevoked }
 * — exactly the two inputs Sentinel.verify()/verifyNotRevoked() already
 * require explicitly. No default is provided here on purpose, even
 * though a real one exists (fetchRemoteCredentialStatus, status.ts):
 * silently defaulting to it would let a caller end up doing a live HTTP
 * fetch per verification without ever having decided that's the right
 * tradeoff for their deployment. Pass fetchRemoteCredentialStatus
 * directly for the common case, or a trivial in-memory one for tests.
 */
export type StatusResolver = (
  credential: VerifiableCredential,
) => Promise<{ fetchTime: Date; isRevoked: boolean }>;

/** Duck type for anything exposing request headers — Fetch API Headers, Express, etc. */
export interface HeadersLike {
  get(name: string): string | null | undefined;
}

function headerValue(headers: HeadersLike | Record<string, string | undefined>, name: string): string {
  if (typeof (headers as HeadersLike).get === "function") {
    return (headers as HeadersLike).get(name) ?? "";
  }
  return (headers as Record<string, string | undefined>)[name] ?? "";
}

/**
 * Verifies the VerifiablePresentation attached to an incoming A2A
 * request's PRESENTATION_HEADER, end to end: holder-binding proof,
 * then the unpacked credential(s)' own signature/validity/revocation.
 *
 * One instance per Sentinel (i.e. per set of trusted issuer keys) is
 * meant to be constructed once and reused across requests, same as
 * Sentinel itself.
 */
export class RequestPresentationVerifier {
  constructor(
    private readonly sentinel: Sentinel,
    private readonly statusResolver: StatusResolver,
  ) {}

  async verifyRequest(
    request: { headers: HeadersLike | Record<string, string | undefined> },
    options: { challenge: string },
  ): Promise<PresentationVerificationResult> {
    const header = headerValue(request.headers, PRESENTATION_HEADER).trim();
    if (!header) {
      return { ok: false, error: "missing presentation header" };
    }

    let presentation;
    try {
      const raw = Buffer.from(header, "base64");
      presentation = JSON.parse(raw.toString("utf-8"));
    } catch (exc) {
      return { ok: false, error: `malformed presentation: ${(exc as Error).message}` };
    }

    try {
      await this.sentinel.verifyPresentationProof(presentation, { expectedChallenge: options.challenge });
      const shape = Sentinel.unpackPresentation(presentation);

      if (shape.length === 1) {
        const [identity] = shape;
        const { fetchTime, isRevoked } = await this.statusResolver(identity);
        await this.sentinel.verify(identity, { statusBitstringFetchTime: fetchTime, isRevoked });
        return { ok: true, requestorDid: identity.credentialSubject.id };
      } else {
        const [operational, grantorIdentity] = shape;
        const grantor = await this.statusResolver(grantorIdentity);
        const op = await this.statusResolver(operational);
        await this.sentinel.verifyDelegationChain(operational, {
          grantorIdentityCredential: grantorIdentity,
          grantorIdentityFetchTime: grantor.fetchTime,
          grantorIdentityIsRevoked: grantor.isRevoked,
          operationalFetchTime: op.fetchTime,
          operationalIsRevoked: op.isRevoked,
        });
        return {
          ok: true,
          requestorDid: operational.credentialSubject.id,
          grantorDid: grantorIdentity.credentialSubject.id,
          scope: operational.credentialSubject.scope,
        };
      }
    } catch (exc) {
      if (exc instanceof CredentialVerificationError) {
        return { ok: false, error: exc.message };
      }
      throw exc;
    }
  }
}
