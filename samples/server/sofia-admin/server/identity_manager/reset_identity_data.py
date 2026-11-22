"""
reset_identity_data.py
--------------------------
Full reset of Sparkle's identity data, to start #49 (keystore
encryption at rest) from a clean slate after #48's testing left a lot
of demo agents/credentials around. Standalone — does NOT import
tools_identity.py, deliberately: importing it would trigger _deps(),
which re-bootstraps the Governing Authority's key and re-publishes its
DID Document as a side effect, which is the opposite of "start clean"
if run before you're ready for that.

Scoped precisely to identity data, nothing else:
- Postgres: TRUNCATEs sf_identity_did_documents, sf_identity_credential_log,
  AND sf_identity_trust_anchors (the Trust Anchor Registry — shared with
  operator-b, Option A centralized design, so this is the ONLY script
  that touches it; operator-b/identity_manager/reset_identity_data.py
  does not).
- Redis (DB 0 — shared with the task store/API-key cache on this
  platform, so this uses pattern-scoped DELETE, never FLUSHDB):
  stls:identity:signingkey:*, stls:identity:credential:*,
  stls:identity:opcred:*, stls:identity:opcred_index:*,
  stls:identity:statuslist:{IDENTITY_AUTHORITY_ID}*.

Run from this directory (same env as server.py):

    python reset_identity_data.py

Asks for confirmation before doing anything (prints what it's about to
truncate/delete, requires typing "yes").
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

PG_DSN = os.environ["IDENTITY_PG_DSN"]
DID_TABLE = os.environ.get("IDENTITY_PG_DID_TABLE", "sf_identity_did_documents")
CREDENTIAL_LOG_TABLE = os.environ.get("IDENTITY_PG_CREDENTIAL_LOG_TABLE", "sf_identity_credential_log")
TRUST_REGISTRY_TABLE = os.environ.get("TRUST_REGISTRY_PG_TABLE", "sf_identity_trust_anchors")

REDIS_URL = os.environ.get("IDENTITY_KEYSTORE_REDIS_URL", "redis://localhost:6379")
KEYSTORE_NAMESPACE = os.environ.get("IDENTITY_KEYSTORE_REDIS_NAMESPACE", "stls:identity:signingkey")
CREDENTIAL_CACHE_NAMESPACE = os.environ.get("IDENTITY_CREDENTIAL_CACHE_REDIS_NAMESPACE", "stls:identity:credential")
AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "sofia-authority")

REDIS_KEY_PATTERNS = [
    f"{KEYSTORE_NAMESPACE}:*",
    f"{CREDENTIAL_CACHE_NAMESPACE}:*",
    "stls:identity:opcred:*",
    "stls:identity:opcred_index:*",
    f"stls:identity:statuslist:{AUTHORITY_ID}*",
]


async def _delete_by_pattern(client: redis.Redis, pattern: str) -> int:
    deleted = 0
    async for key in client.scan_iter(match=pattern):
        await client.delete(key)
        deleted += 1
    return deleted


async def main() -> None:
    print("About to reset Sparkle's identity data:")
    print(f"  Postgres ({PG_DSN.split('@')[-1]}): TRUNCATE {DID_TABLE}, {CREDENTIAL_LOG_TABLE}, {TRUST_REGISTRY_TABLE}")
    print(f"  Redis ({REDIS_URL}): DELETE keys matching {REDIS_KEY_PATTERNS}")
    if input("Type 'yes' to proceed: ").strip().lower() != "yes":
        print("Aborted.")
        return

    conn = await asyncpg.connect(PG_DSN)
    try:
        for table in (DID_TABLE, CREDENTIAL_LOG_TABLE, TRUST_REGISTRY_TABLE):
            await conn.execute(f"TRUNCATE {table}")
            print(f"Truncated {table}")
    finally:
        await conn.close()

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        for pattern in REDIS_KEY_PATTERNS:
            count = await _delete_by_pattern(client, pattern)
            print(f"Deleted {count} key(s) matching {pattern!r}")
    finally:
        await client.aclose()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
