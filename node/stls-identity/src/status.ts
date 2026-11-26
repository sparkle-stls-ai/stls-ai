/**
 * status.ts
 * ---------
 * 1:1 port of python/stls-identity/src/stls_identity/stls_id_status.py.
 *
 * BitstringStatusList backing store. One Redis key per Governing
 * Authority holding a packed bit array, mutated with the same
 * Lua-atomic discipline node/stls-core already uses elsewhere
 * (auth/store.ts's lock script):
 *
 *   - allocateIndex() / revoke() are O(1) atomic Redis operations
 *   - no read-modify-write race across replicas
 *   - the published document (publish()) is a cheap, cacheable
 *     snapshot — not recomputed per verification request
 *
 * MRI (5-minute freshness guarantee): enforced by the CALLER
 * (verifier.ts), which must treat a cached status document older than
 * MRI_SECONDS as untrustworthy and re-fetch — fail-closed.
 */

import { createClient, RESP_TYPES, type RedisClientType } from "redis";
import { gunzipSync, gzipSync } from "node:zlib";
import type { VerifiableCredential } from "./models.js";

export const MRI_SECONDS = 300; // 5 minutes — matches the revocation freshness requirement

function statusListKey(authorityId: string): string {
  return `stls:identity:statuslist:${authorityId}`;
}

function nextIndexKey(authorityId: string): string {
  return `stls:identity:statuslist:${authorityId}:next_index`;
}

// Lua: atomically grow the bitstring (if needed) and set a single bit.
//
// NOTE: Redis embeds Lua 5.1, which has NO `//` floor-division operator
// (that's Lua 5.3+) — using it fails at script-compile time with
// "unexpected symbol near '/'". math.floor(a / b) is the 5.1-compatible
// equivalent. This string is copied byte-for-byte from the Python
// port's _SET_BIT_LUA: Redis runs the SAME embedded Lua regardless of
// which language's client submitted the EVAL, so there is nothing
// language-specific to port here beyond the client call convention.
const SET_BIT_LUA = `
local key = KEYS[1]
local index = tonumber(ARGV[1])
local byte_index = math.floor(index / 8)
local needed_len = byte_index + 1
local cur_len = redis.call('STRLEN', key)
if cur_len < needed_len then
    redis.call('SETRANGE', key, needed_len - 1, '\\0')
end
redis.call('SETBIT', key, index, 1)
return 1
`;

/** One instance per Governing Authority (i.e. per deployment). */
export class StatusListManager {
  private readonly listKey: string;
  private readonly nextIndexKey: string;

  constructor(
    private readonly client: RedisClientType,
    authorityId: string,
  ) {
    this.listKey = statusListKey(authorityId);
    this.nextIndexKey = nextIndexKey(authorityId);
  }

  /**
   * Called once per credential, at issuance time. INCR is atomic — no
   * two credentials ever collide on the same statusListIndex, even
   * across concurrent issuers.
   */
  async allocateIndex(): Promise<number> {
    const next = await this.client.incr(this.nextIndexKey);
    return next - 1;
  }

  async revoke(index: number): Promise<void> {
    await this.client.eval(SET_BIT_LUA, { keys: [this.listKey], arguments: [String(index)] });
  }

  async isRevoked(index: number): Promise<boolean> {
    const bit = await this.client.getBit(this.listKey, index);
    return bit === 1;
  }

  /**
   * Returns [base64url-encoded gzip bitstring, generatedAt] ready to
   * drop into StatusListCredential.encodedList. Call this from a
   * periodic background task and cache the result — verifiers must
   * never trigger a fresh Redis read per-request.
   */
  async publish(): Promise<{ encodedList: string; generatedAt: Date }> {
    // The bitstring is arbitrary binary data (SETBIT/SETRANGE operate on
    // its raw bytes directly, same as the Python port's
    // decode_responses=False client) — a plain `.get()` would decode it
    // as UTF-8 and corrupt any byte that isn't valid UTF-8. `withTypeMapping`
    // requests the raw Buffer reply instead, just for this one call.
    const raw =
      (await this.client.withTypeMapping({ [RESP_TYPES.BLOB_STRING]: Buffer }).get(this.listKey)) ??
      Buffer.alloc(0);
    const compressed = gzipSync(raw);
    return { encodedList: compressed.toString("base64url"), generatedAt: new Date() };
  }
}

/**
 * Verifier-side inverse of publish(): fetches a published
 * StatusListCredential over HTTP and checks a single bit. Only works
 * against a document actually produced by publish() (or wire-compatible
 * with it).
 *
 * Throws on fetch failure or a missing/empty encodedList — fails
 * loudly rather than silently treating "couldn't check" as "not
 * revoked", same fail-closed posture as verifier.ts's MRI staleness
 * check.
 */
export async function fetchStatusBit(statusListUrl: string, index: number): Promise<boolean> {
  const resp = await fetch(statusListUrl, { signal: AbortSignal.timeout(10_000) });
  if (!resp.ok) {
    throw new Error(`fetchStatusBit: GET ${statusListUrl} failed with status ${resp.status}`);
  }
  const payload = (await resp.json()) as { encodedList?: string };

  const encodedList = payload.encodedList ?? "";
  if (!encodedList) {
    throw new Error(`empty or missing encodedList at ${statusListUrl}`);
  }

  const decompressed = gunzipSync(Buffer.from(encodedList, "base64url"));
  const byteIdx = Math.floor(index / 8);
  const bitIdx = index % 8;
  if (decompressed.length <= byteIdx) {
    return false;
  }
  return ((decompressed[byteIdx] >> (7 - bitIdx)) & 1) === 1;
}

/**
 * Ready-to-use status resolver for request-verifier.ts — fetches the
 * credential's OWN issuer-published status list
 * (credentialStatus.statusListCredential) and checks its bit. Works
 * for any credential whose issuer publishes a StatusListCredential in
 * this shape, not just this deployment's own.
 */
export async function fetchRemoteCredentialStatus(
  credential: VerifiableCredential,
): Promise<{ fetchTime: Date; isRevoked: boolean }> {
  const isRevoked = await fetchStatusBit(
    credential.credentialStatus.statusListCredential,
    credential.credentialStatus.statusListIndex,
  );
  return { fetchTime: new Date(), isRevoked };
}

/** Convenience matching stls-core's own createClient + connect pattern. */
export async function connectStatusListRedis(url: string): Promise<RedisClientType> {
  const client: RedisClientType = createClient({ url });
  await client.connect();
  return client;
}
