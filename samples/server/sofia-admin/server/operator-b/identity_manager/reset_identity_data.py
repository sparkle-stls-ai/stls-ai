"""
reset_identity_data.py
--------------------------
Full reset of operator-b's identity data — same purpose/companion as
identity_manager/reset_identity_data.py, before starting #49 from a
clean slate. Standalone — does NOT import tools_identity.py, for the
same reason as the Sparkle-side script: importing it triggers _deps(),
which re-bootstraps the Governing Authority's key as a side effect.

Deliberately does NOT touch sf_identity_trust_anchors: it's the SAME
physical table Sparkle's own registry uses (Option A, one centralized
registry, no separate one for operator-b — see TRUST_REGISTRY_PG_TABLE
in this platform's own .env). Run identity_manager/reset_identity_data.py
on the Sparkle side for that one, once, not from here.

Scoped precisely to identity data, nothing else:
- Postgres: TRUNCATEs opb_identity_did_documents, opb_identity_credential_log.
- Redis (DB /1 — dedicated to this platform's identity data only,
  nothing else lives there, confirmed against this platform's own
  REDIS_URL (task store/API-key cache) being DB 0 instead; still uses
  pattern-scoped DELETE rather than FLUSHDB, consistent with the
  Sparkle-side script and with the lesson from the earlier test-fixture
  incident — never assume DB isolation is enough on its own):
  stls:identity:signingkey:*, stls:identity:credential:*,
  stls:identity:opcred:*, stls:identity:opcred_index:*,
  stls:identity:statuslist:{IDENTITY_AUTHORITY_ID}*.

Run from this directory (same env as server.py):

    python reset_identity_data.py

Asks for confirmation before doing anything.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import redis.asyncio as redis
from dotenv import load_dotenv

load_dotenv()

PG_DSN = os.environ["IDENTITY_PG_DSN"]
DID_TABLE = os.environ.get("IDENTITY_PG_DID_TABLE", "opb_identity_did_documents")
CREDENTIAL_LOG_TABLE = os.environ.get("IDENTITY_PG_CREDENTIAL_LOG_TABLE", "opb_identity_credential_log")

REDIS_URL = os.environ.get("IDENTITY_KEYSTORE_REDIS_URL", "redis://localhost:6379/1")
KEYSTORE_NAMESPACE = os.environ.get("IDENTITY_KEYSTORE_REDIS_NAMESPACE", "stls:identity:signingkey")
CREDENTIAL_CACHE_NAMESPACE = os.environ.get("IDENTITY_CREDENTIAL_CACHE_REDIS_NAMESPACE", "stls:identity:credential")
AUTHORITY_ID = os.environ.get("IDENTITY_AUTHORITY_ID", "operatore-b-authority")

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
    print("About to reset operator-b's identity data:")
    print(f"  Postgres ({PG_DSN.split('@')[-1]}): TRUNCATE {DID_TABLE}, {CREDENTIAL_LOG_TABLE}")
    print(f"  (sf_identity_trust_anchors NOT touched here — shared table, reset it from the Sparkle side)")
    print(f"  Redis ({REDIS_URL}): DELETE keys matching {REDIS_KEY_PATTERNS}")
    if input("Type 'yes' to proceed: ").strip().lower() != "yes":
        print("Aborted.")
        return

    conn = await asyncpg.connect(PG_DSN)
    try:
        for table in (DID_TABLE, CREDENTIAL_LOG_TABLE):
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
