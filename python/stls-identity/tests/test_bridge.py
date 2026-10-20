"""
bridge.py end-to-end over real HTTP semantics (ASGI transport, no live
server needed) — against the REAL Postgres stores, not mocks. Confirms
the actual response shape an external federation partner would receive.
"""

import datetime as dt
import os

import httpx
import pytest
from fastapi import FastAPI

from stls_identity import (
    CredentialProof,
    InMemoryIdentityKeystore,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    StatusListCredential,
    bootstrap_signing_key,
    build_did_document,
    create_identity_bridge_router,
)

DSN = os.getenv(
    "IDENTITY_TEST_PG_DSN",
    "postgresql://postgres:postgres@localhost:5432/stls_identity_test",
)

AUTHORITY_ID = "sofia-authority"
AUTHORITY_AGENT_ID = "governing-authority"


@pytest.fixture
async def client():
    did_store = PostgresDIDDocumentStore(dsn=DSN)
    status_store = PostgresStatusListCacheStore(dsn=DSN)
    pool = await did_store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(did_store.get_ddl())
        await conn.execute(status_store.get_ddl())
        await conn.execute(f"TRUNCATE {did_store.table}")
        await conn.execute(f"TRUNCATE {status_store.table}")

    app = FastAPI()
    app.include_router(create_identity_bridge_router(
        did_store=did_store,
        status_cache_store=status_store,
        authority_id=AUTHORITY_ID,
        authority_agent_id=AUTHORITY_AGENT_ID,
    ))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://bridge.test") as c:
        c.did_store = did_store          # stash for tests that need to seed data directly
        c.status_store = status_store
        yield c

    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {did_store.table}")
        await conn.execute(f"TRUNCATE {status_store.table}")
    await did_store.close()
    await status_store.close()


async def test_authority_did_json_404_when_not_bootstrapped(client):
    resp = await client.get("/.well-known/did.json")
    assert resp.status_code == 404


async def test_authority_did_json_served_after_bootstrap(client):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key(AUTHORITY_AGENT_ID, keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=[],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=pub,
    )
    await client.did_store.put(AUTHORITY_AGENT_ID, doc)

    resp = await client.get("/.well-known/did.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "did:web:sofia.sparkle.example.com"
    assert body["verificationMethod"][0]["id"].startswith("did:web:sofia.sparkle.example.com")


async def test_agent_did_json_404_for_unknown_agent(client):
    resp = await client.get("/agents/ghost/did.json")
    assert resp.status_code == 404


async def test_agent_did_json_served_for_known_agent(client):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("agent-087", keystore)
    doc = build_did_document(
        host="sofia.sparkle.example.com",
        agent_path_segments=["agents", "087"],
        governing_authority_did="did:web:sofia.sparkle.example.com",
        signing_public_key=pub,
    )
    await client.did_store.put("agent-087", doc)

    resp = await client.get("/agents/agent-087/did.json")
    assert resp.status_code == 200
    assert resp.json()["id"] == "did:web:sofia.sparkle.example.com:agents:087"


async def test_status_list_503_before_first_publish(client):
    """Correct fail-closed-adjacent behavior: no snapshot yet means the
    refresher hasn't run, which is a service-unavailable state to
    surface honestly, not a silent empty response a verifier might
    misread as 'nothing is revoked'."""
    resp = await client.get("/identity/status-list")
    assert resp.status_code == 503


async def test_status_list_served_after_cache_populated(client):
    doc = StatusListCredential(
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
    await client.status_store.put(AUTHORITY_ID, doc)

    resp = await client.get("/identity/status-list")
    assert resp.status_code == 200
    body = resp.json()
    assert body["encodedList"] == doc.encoded_list
    assert body["type"] == ["VerifiableCredential", "BitstringStatusListCredential"]