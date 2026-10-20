"""
postgres_store.py against a REAL PostgreSQL instance — no mocks.

Requires a Postgres reachable at IDENTITY_TEST_PG_DSN (default:
postgresql://postgres:postgres@localhost:5432/stls_identity_test).
Creates the two tables if missing, truncates them before/after each test.
"""

import datetime as dt
import os

import pytest

from stls_identity import (
    CredentialProof,
    InMemoryIdentityKeystore,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    StatusListCredential,
    bootstrap_signing_key,
    build_did_document,
)

DSN = os.getenv(
    "IDENTITY_TEST_PG_DSN",
    "postgresql://postgres:postgres@localhost:5432/stls_identity_test",
)


@pytest.fixture
async def did_store():
    store = PostgresDIDDocumentStore(dsn=DSN)
    pool = await store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(store.get_ddl())
        await conn.execute(f"TRUNCATE {store.table}")
    yield store
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {store.table}")
    await store.close()


@pytest.fixture
async def status_cache_store():
    store = PostgresStatusListCacheStore(dsn=DSN)
    pool = await store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(store.get_ddl())
        await conn.execute(f"TRUNCATE {store.table}")
    yield store
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {store.table}")
    await store.close()


def _sample_did_document():
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("agent-087", keystore)
    return build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=["agents", "087"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=pub,
    )


def _sample_status_list_credential():
    return StatusListCredential(
        id="https://sofia.sparkle.example.com/identity/status-list",
        issuer="did:web:sofia.sparkle.example.com",
        valid_from=dt.datetime.now(dt.timezone.utc),
        encoded_list="H4sIAAAAAAAA_2NgAAAAAAA=",
        proof=CredentialProof(
            created=dt.datetime.now(dt.timezone.utc),
            verification_method="did:web:sofia.sparkle.example.com#key-1",
            proof_value="z" + "a" * 64,
        ),
    )


# ---------------------------------------------------------------------------
# DID Document store
# ---------------------------------------------------------------------------

async def test_did_store_get_missing_returns_none(did_store):
    assert await did_store.get("nobody-here") is None


async def test_did_store_put_then_get_roundtrips(did_store):
    doc = _sample_did_document()
    await did_store.put("agent-087", doc)

    fetched = await did_store.get("agent-087")
    assert fetched is not None
    assert fetched.id == doc.id
    assert fetched.controller == doc.controller
    assert fetched.verification_method[0].id == doc.verification_method[0].id


async def test_did_store_put_is_upsert_not_duplicate(did_store):
    """Re-bootstrapping the same agent must overwrite, not error or
    create a second row — same semantics as keystore.get_or_create()."""
    doc = _sample_did_document()
    await did_store.put("agent-087", doc)
    await did_store.put("agent-087", doc)  # simulate a second bootstrap/restart

    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT count(*) FROM {did_store.table} WHERE agent_id = $1",
            "agent-087",
        )
    assert count == 1


async def test_did_store_list_agent_ids(did_store):
    assert await did_store.list_agent_ids() == []

    keystore = InMemoryIdentityKeystore()
    _, pub_1, _ = bootstrap_signing_key("agent-087", keystore)
    doc_1 = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "087"],
        governing_authority_did="did:web:sofia.sparkle.example.com", signing_public_key=pub_1,
    )
    _, pub_2, _ = bootstrap_signing_key("agent-042", keystore)
    doc_2 = build_did_document(
        host="sofia.sparkle.example.com", agent_path_segments=["agents", "042"],
        governing_authority_did="did:web:sofia.sparkle.example.com", signing_public_key=pub_2,
    )

    await did_store.put("agent-087", doc_1)
    await did_store.put("agent-042", doc_2)

    assert await did_store.list_agent_ids() == ["agent-042", "agent-087"]  # ordine alfabetico


# ---------------------------------------------------------------------------
# Status list cache store
# ---------------------------------------------------------------------------

async def test_status_cache_get_missing_returns_none(status_cache_store):
    assert await status_cache_store.get("no-such-authority") is None


async def test_status_cache_put_then_get_roundtrips(status_cache_store):
    doc = _sample_status_list_credential()
    await status_cache_store.put("sofia-authority", doc)

    fetched = await status_cache_store.get("sofia-authority")
    assert fetched is not None
    assert fetched.encoded_list == doc.encoded_list
    assert fetched.issuer == doc.issuer


async def test_status_cache_put_updates_generated_at_on_refresh(status_cache_store):
    """Simulates the periodic refresher: same authority, new snapshot —
    must reflect the latest one, not accumulate rows."""
    first = _sample_status_list_credential()
    await status_cache_store.put("sofia-authority", first)

    second = _sample_status_list_credential()
    second.encoded_list = "H4sIAAAAAAAA_2NkAAAAAAA="  # different snapshot
    await status_cache_store.put("sofia-authority", second)

    fetched = await status_cache_store.get("sofia-authority")
    assert fetched.encoded_list == second.encoded_list

    pool = await status_cache_store._get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT count(*) FROM {status_cache_store.table} "
            f"WHERE authority_id = $1",
            "sofia-authority",
        )
    assert count == 1


async def test_table_property_and_ddl_respect_env_override(monkeypatch):
    """Regression test for the exact bug class this fixed: a raw SQL
    query built against DEFAULT_TABLE (fixed) instead of .table
    (instance-aware) silently reads/writes the wrong table whenever
    IDENTITY_PG_DID_TABLE is set to something other than the default —
    exactly what happened when this env var was configured to match an
    existing 'sofia' database's own naming convention."""
    monkeypatch.setenv("IDENTITY_PG_DID_TABLE", "custom_did_table_for_test")
    store = PostgresDIDDocumentStore(dsn=DSN)

    assert store.table == "custom_did_table_for_test"
    assert store.table != PostgresDIDDocumentStore.DEFAULT_TABLE
    assert "custom_did_table_for_test" in store.get_ddl()
    assert PostgresDIDDocumentStore.DEFAULT_TABLE not in store.get_ddl()

    pool = await store._get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(store.get_ddl())
            await conn.execute(f"TRUNCATE {store.table}")

        doc = _sample_did_document()
        await store.put("agent-custom", doc)

        # Verifying via the SAME property the fix introduces — this is
        # exactly what test_did_store_put_is_upsert_not_duplicate now
        # does instead of the old, broken `store.DEFAULT_TABLE` query.
        async with pool.acquire() as conn:
            count = await conn.fetchval(
                f"SELECT count(*) FROM {store.table} WHERE agent_id = $1",
                "agent-custom",
            )
        assert count == 1
    finally:
        async with pool.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS custom_did_table_for_test")
        await store.close()