"""
pg_quick_check.py
------------------
Quick manual check: PostgreSQL connection + TEST_PG_KEY lookup.

Manual script, NOT a pytest test: it runs immediately at module level
(`asyncio.run(main())` at the bottom of the file) a real connection to
AUTH_PG_DSN and an INSERT into the configured table. Deliberately
renamed out of pytest's collection pattern (`test_*.py` / `*_test.py`)
— it used to be test_pg_quick.py, and pytest ran it just by collecting
it, with no guard/skip: with AUTH_PG_DSN pointed (even by mistake) at a
real database, a plain `pytest` in this package's directory would have
written real data. See the 2026-11-20 incident
(project_test_fixture_prod_data_incident memory) for an analogous case.

    source .venv/bin/activate
    export AUTH_PG_DSN="postgresql://user:pass@host:5432/dbname"
    export TEST_PG_KEY="stls-pg_..."     # optional, verifies the key
    python3 pg_quick_check.py
"""

import asyncio, os
from dotenv import load_dotenv
load_dotenv()

DSN      = os.getenv("AUTH_PG_DSN", "")
TEST_KEY = os.getenv("TEST_PG_KEY", "")
TABLE    = os.getenv("AUTH_PG_TABLE", "sf_api_keys")


async def main():
    import asyncpg
    from stls_a2a import hash_key, generate_api_key

    # 1. connessione
    print(f"DSN: {DSN}")
    conn = await asyncpg.connect(DSN, timeout=5)
    ver = await conn.fetchval("SELECT version()")
    print(f"✅ Connesso: {ver[:70]}")

    # 2. conta chiavi attive
    count = await conn.fetchval(f"SELECT count(*) FROM {TABLE} WHERE revoked=false")
    print(f"✅ Chiavi attive in '{TABLE}': {count}")

    # 3. verifica TEST_PG_KEY se impostata
    if TEST_KEY:
        kid = hash_key(TEST_KEY)
        row = await conn.fetchrow(
            f"SELECT client_id, scopes, endpoints, revoked, expires_at FROM {TABLE} WHERE key_id=$1", kid
        )
        if row:
            print(f"✅ TEST_PG_KEY trovata → client={row['client_id']} scopes={row['scopes']} revoked={row['revoked']}")
        else:
            print(f"❌ TEST_PG_KEY NON trovata (key_id={kid[:16]}…)")
            print()
            # genera e inserisce una nuova chiave
            raw, kid2 = generate_api_key("stls-pg")
            await conn.execute(
                f"INSERT INTO {TABLE} (key_id, client_id, scopes, endpoints, description) "
                "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (key_id) DO NOTHING",
                kid2, "lc-test-client", ["secure-a2a"], [], "test key"
            )
            print(f"✅ Nuova chiave inserita. Imposta:")
            print(f'   export TEST_PG_KEY="{raw}"')
    else:
        print("⚠️  TEST_PG_KEY non impostata — solo connessione verificata")
        # genera e mostra come inserire
        raw, kid2 = generate_api_key("stls-pg")
        await conn.execute(
            f"INSERT INTO {TABLE} (key_id, client_id, scopes, endpoints, description) "
            "VALUES ($1,$2,$3,$4,$5) ON CONFLICT (key_id) DO NOTHING",
            kid2, "lc-test-client", ["secure-a2a"], [], "test key"
        )
        print(f"✅ Chiave generata e inserita. Imposta:")
        print(f'   export TEST_PG_KEY="{raw}"')

    await conn.close()

asyncio.run(main())