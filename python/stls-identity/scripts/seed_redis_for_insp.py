"""
Not a test — a script to run by hand to populate Redis with realistic
data and LEAVE IT there (no flush), so you can inspect it with
redis-cli. Uses the same db 15 as the tests, to avoid dirtying db 0.

Usage:
    PYTHONPATH=src python3 scripts/seed_redis_for_inspection.py
"""

import asyncio
import os
import sys

sys.path.insert(0, "src")

import redis.asyncio as redis
from stls_identity import StatusListManager

REDIS_URL = os.getenv("STATUS_LIST_TEST_REDIS_URL", "redis://localhost:6379/15")


async def main() -> None:
    client = redis.Redis.from_url(REDIS_URL)
    await client.flushdb()  # initial cleanup only, not at the end

    manager = StatusListManager(client, authority_id="demo-authority")

    indices = [await manager.allocate_index() for _ in range(20)]
    print("Allocated indices:", indices)

    for idx in (2, 9, 15):
        await manager.revoke(idx)
    print("Revoked:", [2, 9, 15])

    encoded, generated_at = await manager.publish()
    print("Published document (gzip+base64url), generated_at =", generated_at)
    print(encoded)

    await client.aclose()
    print(f"\nDone. Data left in {REDIS_URL} — inspect it with redis-cli -n 15")


if __name__ == "__main__":
    asyncio.run(main())