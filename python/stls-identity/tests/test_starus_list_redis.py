"""
status.py against a REAL Redis instance — no mocks.

Covers what test_e2e.py's InMemoryStatusListManager stub cannot:
    - the actual Lua script (bit growth across byte boundaries)
    - allocate_index() concurrency safety (real INCR, not a Python counter)
    - publish() producing a real gzip+base64 document, and that decoding
      it reproduces the exact bit pattern a verifier would read

Requires a Redis instance reachable at STATUS_LIST_TEST_REDIS_URL
(default redis://localhost:6379/15 — db 15 to stay out of the way of
anything else running locally). Flushes only that db, before and after.
"""

import asyncio
import base64
import gzip
import os

import pytest
import redis.asyncio as redis

from stls_identity import StatusListManager, MRI_SECONDS

REDIS_URL = os.getenv("STATUS_LIST_TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def redis_client():
    client = redis.Redis.from_url(REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def manager(redis_client):
    return StatusListManager(redis_client, authority_id="test-authority")


async def test_allocate_index_is_sequential_and_unique(manager):
    indices = [await manager.allocate_index() for _ in range(5)]
    assert indices == [0, 1, 2, 3, 4]


async def test_allocate_index_concurrent_no_collisions(manager):
    """Real INCR under concurrency — the property InMemoryStatusListManager
    can't actually exercise, since Python's GIL makes its counter safe by
    accident, not by design."""
    results = await asyncio.gather(*[manager.allocate_index() for _ in range(50)])
    assert sorted(results) == list(range(50)), "every credential must get a unique index"


async def test_revoke_and_is_revoked_single_bit(manager):
    index = await manager.allocate_index()
    assert await manager.is_revoked(index) is False

    await manager.revoke(index)
    assert await manager.is_revoked(index) is True


async def test_revoke_does_not_affect_other_bits(manager):
    """The Lua SETRANGE pre-growth must zero-pad, not leave garbage —
    otherwise unrelated indices could read back as 'revoked' by accident."""
    indices = [await manager.allocate_index() for _ in range(10)]
    target = indices[7]

    await manager.revoke(target)

    for i in indices:
        expected = (i == target)
        assert await manager.is_revoked(i) is expected, f"index {i} unexpectedly {'not ' if expected else ''}revoked"


async def test_revoke_across_byte_boundary(manager):
    """Indices 0-7 fit in byte 0; index 8+ forces the Lua script's
    SETRANGE growth path. This is the case InMemoryStatusListManager
    (a Python set) can never catch, because it has no byte layout at all."""
    for _ in range(20):
        await manager.allocate_index()

    for idx in (0, 7, 8, 15, 16, 19):
        await manager.revoke(idx)

    for idx in range(20):
        expected = idx in (0, 7, 8, 15, 16, 19)
        assert await manager.is_revoked(idx) is expected, f"index {idx} mismatch"


async def test_publish_roundtrip_matches_raw_bits(manager, redis_client):
    """The actual artifact a verifier downloads. Decode it exactly the way
    a third-party Sentinel would (gzip -> raw bytes -> read bit N) and
    confirm it matches what revoke()/is_revoked() see internally."""
    for _ in range(16):
        await manager.allocate_index()
    for idx in (2, 9, 15):
        await manager.revoke(idx)

    encoded, generated_at = await manager.publish()
    assert generated_at.tzinfo is not None, "publish() must return a tz-aware timestamp"

    # Decode exactly as an external verifier would, using nothing but the
    # published document — no access to Redis.
    raw = gzip.decompress(base64.urlsafe_b64decode(encoded))

    def bit_at(data: bytes, index: int) -> bool:
        byte_i, bit_i = divmod(index, 8)
        return bool(data[byte_i] & (0x80 >> bit_i))

    for idx in range(16):
        expected = idx in (2, 9, 15)
        assert bit_at(raw, idx) is expected, f"published bit {idx} mismatch"


async def test_publish_on_empty_list_does_not_error(manager):
    """No credentials issued yet — publish() must return a valid (empty)
    document, not raise, since a Governing Authority with zero credentials
    is a legitimate state (e.g. right after bootstrap)."""
    encoded, _ = await manager.publish()
    raw = gzip.decompress(base64.urlsafe_b64decode(encoded))
    assert raw == b""


def test_mri_constant_matches_documented_five_minutes():
    """Guards against MRI_SECONDS silently drifting from the 5-minute
    guarantee documented everywhere else (verifier.py, the design review)."""
    assert MRI_SECONDS == 300