"""
stls_identity.status
=====================

BitstringStatusList backing store.

Same shape as the existing stls:revoked pattern (RedisKeyCache /
auth_store.py) — a Redis SET flips to a bitstring only in its *published*
form. Internally it's still one Redis key per Governing Authority holding
a packed bit array, mutated with the same Lua-atomic discipline already
used elsewhere in stls-core, so:

    - allocate_index() / revoke() are O(1) atomic Redis operations
    - no read-modify-write race across replicas (same reasoning that led
      to the leader-election refresh pattern in ApiKeyManager)
    - the published document (publish()) is a cheap, cacheable snapshot —
      not recomputed per verification request

MRI (5-minute freshness guarantee): enforced by the caller (verifier.py),
which must treat a cached status document older than MRI_SECONDS as
untrustworthy and re-fetch — fail-closed, same posture as the rest of
stls-core's auth path.
"""

from __future__ import annotations

import base64
import gzip
import datetime as dt

import redis.asyncio as redis

MRI_SECONDS = 300  # 5 minutes — matches the revocation freshness requirement

_STATUS_LIST_KEY = "stls:identity:statuslist:{authority_id}"
_NEXT_INDEX_KEY = "stls:identity:statuslist:{authority_id}:next_index"

# Lua: atomically grow the bitstring (if needed) and set a single bit.
# Mirrors the atomic-script discipline already used for stls:revoked.
#
# NOTE: Redis embeds Lua 5.1, which has NO `//` floor-division operator
# (that's Lua 5.3+) — using it here fails at script-compile time with
# "unexpected symbol near '/'". math.floor(a / b) is the 5.1-compatible
# equivalent. Caught by test_status_list_redis.py against a real Redis
# instance; an in-memory Python stub can never catch this class of bug.
_SET_BIT_LUA = """
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
"""


class StatusListManager:
    """One instance per Governing Authority (i.e. per Sofia deployment)."""

    def __init__(self, redis_client: redis.Redis, authority_id: str):
        self._r = redis_client
        self._authority_id = authority_id
        self._list_key = _STATUS_LIST_KEY.format(authority_id=authority_id)
        self._next_index_key = _NEXT_INDEX_KEY.format(authority_id=authority_id)
        self._set_bit_script = self._r.register_script(_SET_BIT_LUA)

    async def allocate_index(self) -> int:
        """Called once per credential, at issuance time. INCR is atomic —
        no two credentials ever collide on the same statusListIndex, even
        across concurrent issuers (same guarantee UUID4 session_id gives
        the handshake today, just via Redis INCR instead of randomness)."""
        return await self._r.incr(self._next_index_key) - 1

    async def revoke(self, index: int) -> None:
        await self._set_bit_script(keys=[self._list_key], args=[index])

    async def is_revoked(self, index: int) -> bool:
        bit = await self._r.getbit(self._list_key, index)
        return bool(bit)

    async def publish(self) -> tuple[str, dt.datetime]:
        """Returns (base64url-encoded gzip bitstring, generated_at) ready
        to drop into StatusListCredential.encoded_list. Call this from a
        BackgroundRefresher-style periodic task (same pattern as the
        existing 300s refresh loop) and cache the result — verifiers must
        never trigger a fresh Redis read per-request."""
        raw = await self._r.get(self._list_key) or b""
        compressed = gzip.compress(raw)
        encoded = base64.urlsafe_b64encode(compressed).decode("ascii")
        return encoded, dt.datetime.now(dt.timezone.utc)