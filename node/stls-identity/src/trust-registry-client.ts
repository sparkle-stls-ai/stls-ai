/**
 * trust-registry-client.ts
 * ---------------------------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_trust_registry_client.py.
 *
 * TrustAnchorRegistryClient — what Sentinel actually calls (indirectly)
 * to answer "who are the recognized Governing Authorities, and what
 * are their current public keys".
 *
 * This cache is touched rarely and is small (tens of entries), NOT
 * per-request — revocation state (large, per-issuer, changes often)
 * never lives here, only Authority identity (small, shared, changes
 * rarely).
 *
 * Deliberately does NOT change Sentinel's constructor shape — this
 * client's job is to PRODUCE the trustedIssuerKeys map
 * (asTrustedKeys()), refreshed periodically, not to make Sentinel
 * aware a registry exists at all.
 */

import { decodeMultibaseEd25519PublicKey } from "./did.js";
import type { TrustAnchorEntry } from "./models.js";
import type { TrustAnchorRegistryStoreBase } from "./trust-registry-store.js";

/**
 * How long a cached registry snapshot may be served before a refresh
 * is required. Deliberately much longer than the 5-minute MRI used
 * for credential revocation (status.ts's MRI_SECONDS) — recognizing a
 * NEW Authority or offboarding one is a rare, deliberate governance
 * action, not a per-credential event.
 */
export const TRUST_ANCHOR_CACHE_TTL_SECONDS = 3600;

/**
 * Raised when resolve()/asTrustedKeys() is asked to serve a cache
 * older than TRUST_ANCHOR_CACHE_TTL_SECONDS and no refresh has
 * happened. Same fail-closed posture as verifier.ts's MRI check —
 * stale trust data is worse than no data, because it can silently
 * keep trusting an Authority that was just offboarded.
 */
export class TrustAnchorRegistryStaleError extends Error {}

function decodePublicKey(entry: TrustAnchorEntry): Uint8Array {
  return decodeMultibaseEd25519PublicKey(entry.publicKeyMultibase);
}

/**
 * One instance per verifying platform. Wraps a
 * TrustAnchorRegistryStoreBase — direct Postgres access for now, since
 * the Option A anchor operator hosts its own store. When an external
 * operator (B, C) needs to resolve the registry over HTTP instead,
 * swap the store for an HTTP-fetching implementation of the same
 * TrustAnchorRegistryStoreBase interface — this class does not change.
 */
export class TrustAnchorRegistryClient {
  private cache = new Map<string, Uint8Array>();
  private lastRefresh: Date | null = null;

  constructor(private readonly store: TrustAnchorRegistryStoreBase) {}

  /**
   * Pulls every active (non-revoked) entry from the store and rebuilds
   * the cache from scratch — a revoked Authority silently disappears
   * from the next refresh, no separate "remove" step needed. Returns
   * the number of entries now cached.
   *
   * Call this from a periodic task, not per-request.
   */
  async refresh(): Promise<number> {
    const entries = await this.store.listActive();
    const newCache = new Map<string, Uint8Array>();
    for (const entry of entries) {
      newCache.set(entry.verificationMethodId, decodePublicKey(entry));
    }
    this.cache = newCache;
    this.lastRefresh = new Date();
    return newCache.size;
  }

  /**
   * Returns the cache in exactly the shape Sentinel's constructor
   * expects: `new Sentinel({ trustedIssuerKeys: client.asTrustedKeys() })`.
   *
   * Throws TrustAnchorRegistryStaleError if refresh() has never run,
   * or the cache is older than TRUST_ANCHOR_CACHE_TTL_SECONDS.
   */
  asTrustedKeys(): Map<string, Uint8Array> {
    this.assertFresh();
    return new Map(this.cache); // copy — caller must not mutate our cache
  }

  /** Single-key lookup, same freshness guarantee as asTrustedKeys(). */
  resolve(verificationMethodId: string): Uint8Array | null {
    this.assertFresh();
    return this.cache.get(verificationMethodId) ?? null;
  }

  private assertFresh(): void {
    if (this.lastRefresh === null) {
      throw new TrustAnchorRegistryStaleError(
        "refresh() has never been called — no trust anchor data available",
      );
    }
    const ageSeconds = (Date.now() - this.lastRefresh.getTime()) / 1000;
    if (ageSeconds > TRUST_ANCHOR_CACHE_TTL_SECONDS) {
      throw new TrustAnchorRegistryStaleError(
        `trust anchor cache stale (${Math.round(ageSeconds)}s > ` +
          `${TRUST_ANCHOR_CACHE_TTL_SECONDS}s) — fail closed, call refresh()`,
      );
    }
  }

  /**
   * Resolves a key that may no longer be current — was this
   * verificationMethodId the trusted key for this Authority at time
   * `at`? Goes straight to the store (NOT the cache: this is for the
   * rare/investigative case of verifying an old credential against a
   * since-rotated key, not the per-request hot path asTrustedKeys()/
   * resolve() serve).
   *
   * Prefer resolveHistorical() if you don't already have the
   * authorityId on hand — Sentinel's fallback path uses that one.
   */
  async resolveAt(authorityId: string, verificationMethodId: string, at: Date): Promise<Uint8Array | null> {
    const entry = await this.store.getKeyValidAt(authorityId, verificationMethodId, at);
    return entry === null ? null : decodePublicKey(entry);
  }

  /**
   * Same as resolveAt(), but keyed by verificationMethodId alone —
   * what Sentinel.verifySignature() actually calls when a key isn't
   * in the live cache and might simply be a since-rotated Authority
   * key rather than genuinely unknown. Also bypasses the cache and its
   * freshness guarantee, for the same reason as resolveAt().
   */
  async resolveHistorical(verificationMethodId: string, at: Date): Promise<Uint8Array | null> {
    const entry = await this.store.getKeyValidAtByVerificationMethod(verificationMethodId, at);
    return entry === null ? null : decodePublicKey(entry);
  }
}
