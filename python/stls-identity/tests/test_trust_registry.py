"""
Trust Anchor Registry — Fase 2.

Covers:
    - PostgresTrustAnchorRegistryStore CRUD contro Postgres reale
    - TrustAnchorRegistryClient: refresh, cache, staleness (fail-closed)
    - un'Authority revocata scompare dalla cache al refresh successivo
    - integrazione end-to-end con Sentinel: un'Identity Credential emessa
      da un'Authority onboardata verifica; la stessa emessa da
      un'Authority NON onboardata viene rifiutata — lo scenario
      "Operatore C" già visto a mano, ora fissato come test permanente
    - il bridge HTTP di sola lettura
"""

import datetime as dt
import os

import httpx
import pytest
from fastapi import FastAPI

from stls_identity import (
    DDL,
    CredentialVerificationError,
    IdentityManager,
    InMemoryIdentityKeystore,
    PostgresDIDDocumentStore,
    PostgresStatusListCacheStore,
    PostgresTrustAnchorRegistryStore,
    Sentinel,
    TrustAnchorRegistryClient,
    TrustAnchorRegistryStaleError,
    bootstrap_signing_key,
    build_did_document,
    build_trust_anchor_entry,
    create_trust_registry_router,
)

DSN = os.getenv(
    "IDENTITY_TEST_PG_DSN",
    "postgresql://postgres:postgres@localhost:5432/stls_identity_test",
)


async def _noop_audit_hook(entry):
    pass


class _InMemoryStatusListManager:
    def __init__(self):
        self._next = 0
        self._revoked = set()

    async def allocate_index(self):
        idx = self._next
        self._next += 1
        return idx

    async def revoke(self, index):
        self._revoked.add(index)

    async def is_revoked(self, index):
        return index in self._revoked


@pytest.fixture
async def store():
    s = PostgresTrustAnchorRegistryStore(dsn=DSN)
    pool = await s._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(s.get_ddl())
        await conn.execute(f"TRUNCATE {s.table}")
    yield s
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {s.table}")
    await s.close()


# ---------------------------------------------------------------------------
# PostgresTrustAnchorRegistryStore
# ---------------------------------------------------------------------------

async def test_store_get_missing_returns_none(store):
    assert await store.get("nobody") is None


async def test_store_put_then_get_roundtrips(store):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-authority", keystore)
    entry = build_trust_anchor_entry(
        authority_id="sparkle",
        did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        public_key=pub,
    )
    await store.put(entry)

    fetched = await store.get("sparkle")
    assert fetched is not None
    assert fetched.did == entry.did
    assert fetched.verification_method_id == entry.verification_method_id
    assert fetched.revoked_at is None


async def test_store_put_is_upsert_not_duplicate(store):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-authority", keystore)
    entry = build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1", public_key=pub,
    )
    await store.put(entry)
    await store.put(entry)  # simula un secondo onboarding/aggiornamento

    pool = await store._get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            f"SELECT count(*) FROM {store.table} WHERE authority_id = $1", "sparkle",
        )
    assert count == 1


async def test_list_active_excludes_revoked(store):
    keystore = InMemoryIdentityKeystore()
    _, pub_a, _ = bootstrap_signing_key("authority-a", keystore)
    _, pub_b, _ = bootstrap_signing_key("authority-b", keystore)

    await store.put(build_trust_anchor_entry(
        authority_id="a", did="did:web:a.example.com",
        verification_method_id="did:web:a.example.com#key-1", public_key=pub_a,
    ))
    await store.put(build_trust_anchor_entry(
        authority_id="b", did="did:web:b.example.com",
        verification_method_id="did:web:b.example.com#key-1", public_key=pub_b,
    ))

    active = await store.list_active()
    assert {e.authority_id for e in active} == {"a", "b"}

    await store.revoke_authority("b", dt.datetime.now(dt.timezone.utc))

    active_after = await store.list_active()
    assert {e.authority_id for e in active_after} == {"a"}

    # l'entry revocata resta in storage (audit), solo esclusa da list_active
    revoked_entry = await store.get("b")
    assert revoked_entry is not None
    assert revoked_entry.revoked_at is not None


# ---------------------------------------------------------------------------
# Key history — rotazione, credenziali vecchie restano verificabili
# ---------------------------------------------------------------------------

async def test_rotation_closes_previous_key_and_get_returns_the_new_one(store):
    keystore = InMemoryIdentityKeystore()
    _, pub_1, _ = bootstrap_signing_key("sparkle-key-1", keystore)
    _, pub_2, _ = bootstrap_signing_key("sparkle-key-2", keystore)

    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1", public_key=pub_1,
    ))
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-2", public_key=pub_2,
    ))

    current = await store.get("sparkle")
    assert current.verification_method_id == "did:web:sofia.sparkle.example.com#key-2"
    assert current.valid_until is None

    history = await store.get_history("sparkle")
    assert [e.verification_method_id for e in history] == [
        "did:web:sofia.sparkle.example.com#key-1",
        "did:web:sofia.sparkle.example.com#key-2",
    ]
    assert history[0].valid_until is not None  # la vecchia è chiusa...
    assert history[1].valid_until is None       # ...la nuova è quella corrente


async def test_old_key_still_resolves_for_its_own_validity_window(store):
    """Il punto centrale di tutto questo lavoro: una credenziale firmata
    PRIMA della rotazione deve restare verificabile, risolvendo la
    chiave che era davvero attiva quando è stata firmata — non quella
    corrente."""
    keystore = InMemoryIdentityKeystore()
    _, pub_1, _ = bootstrap_signing_key("sparkle-key-1", keystore)
    _, pub_2, _ = bootstrap_signing_key("sparkle-key-2", keystore)

    signed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        public_key=pub_1, onboarded_at=signed_at - dt.timedelta(days=30),
    ))
    # rotazione, oggi
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-2", public_key=pub_2,
    ))

    # la chiave vecchia, al momento in cui la credenziale fu firmata: c'è
    resolved = await store.get_key_valid_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-1", signed_at,
    )
    assert resolved is not None
    assert resolved.verification_method_id == "did:web:sofia.sparkle.example.com#key-1"

    # la chiave NUOVA, a quello stesso istante nel passato: non esisteva ancora
    not_yet = await store.get_key_valid_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-2", signed_at,
    )
    assert not_yet is None


async def test_revoke_key_invalidates_even_within_original_validity_window(store):
    """Una revoca puntuale (compromissione sospetta) è più forte di una
    normale rotazione: deve invalidare la chiave anche per istanti nel
    passato che altrimenti rientrerebbero nella sua finestra di
    validità."""
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-key-1", keystore)

    onboarded_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        public_key=pub, onboarded_at=onboarded_at,
    ))

    mid_window = onboarded_at + dt.timedelta(days=10)
    assert await store.get_key_valid_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-1", mid_window,
    ) is not None

    # compromissione scoperta oggi, retroattiva a un momento PRIMA di mid_window
    revoked_at = onboarded_at + dt.timedelta(days=5)
    await store.revoke_key("sparkle", "did:web:sofia.sparkle.example.com#key-1", revoked_at)

    assert await store.get_key_valid_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-1", mid_window,
    ) is None


async def test_revoke_authority_revokes_every_key_generation(store):
    keystore = InMemoryIdentityKeystore()
    _, pub_1, _ = bootstrap_signing_key("sparkle-key-1", keystore)
    _, pub_2, _ = bootstrap_signing_key("sparkle-key-2", keystore)

    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1", public_key=pub_1,
    ))
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-2", public_key=pub_2,
    ))

    await store.revoke_authority("sparkle", dt.datetime.now(dt.timezone.utc))

    history = await store.get_history("sparkle")
    assert all(e.revoked_at is not None for e in history)


# ---------------------------------------------------------------------------
# TrustAnchorRegistryClient
# ---------------------------------------------------------------------------

async def test_client_raises_before_first_refresh(store):
    client = TrustAnchorRegistryClient(store)
    with pytest.raises(TrustAnchorRegistryStaleError, match="never been called"):
        client.as_trusted_keys()


async def test_client_refresh_populates_cache_and_as_trusted_keys(store):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-authority", keystore)
    vm_id = "did:web:sofia.sparkle.example.com#key-1"
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id=vm_id, public_key=pub,
    ))

    client = TrustAnchorRegistryClient(store)
    count = await client.refresh()
    assert count == 1

    trusted = client.as_trusted_keys()
    assert vm_id in trusted
    assert client.resolve(vm_id) is not None
    assert client.resolve("did:web:unknown.example.com#key-1") is None


async def test_client_cache_is_fail_closed_when_stale(store, monkeypatch):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-authority", keystore)
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1", public_key=pub,
    ))

    client = TrustAnchorRegistryClient(store)
    await client.refresh()

    # simula il passare di più tempo del TTL, senza un nuovo refresh
    import stls_identity.stls_id_trust_registry_client as trc
    monkeypatch.setattr(trc, "TRUST_ANCHOR_CACHE_TTL_SECONDS", 0)

    with pytest.raises(TrustAnchorRegistryStaleError, match="stale"):
        client.as_trusted_keys()


async def test_client_refresh_drops_revoked_authority_from_cache(store):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("authority-b", keystore)
    vm_id = "did:web:b.example.com#key-1"
    await store.put(build_trust_anchor_entry(
        authority_id="b", did="did:web:b.example.com",
        verification_method_id=vm_id, public_key=pub,
    ))

    client = TrustAnchorRegistryClient(store)
    await client.refresh()
    assert client.resolve(vm_id) is not None

    await store.revoke_authority("b", dt.datetime.now(dt.timezone.utc))
    await client.refresh()

    assert client.resolve(vm_id) is None


async def test_client_resolve_at_finds_rotated_out_key_bypassing_cache(store):
    """resolve_at() non passa dalla cache (refresh() non viene mai
    chiamato qui) — verifica una chiave storica direttamente contro lo
    store, il percorso pensato per il caso investigativo/di verifica di
    una credenziale vecchia, non per l'hot path per-richiesta."""
    keystore = InMemoryIdentityKeystore()
    _, pub_1, _ = bootstrap_signing_key("sparkle-key-1", keystore)
    _, pub_2, _ = bootstrap_signing_key("sparkle-key-2", keystore)

    signed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1",
        public_key=pub_1, onboarded_at=signed_at - dt.timedelta(days=5),
    ))
    await store.put(build_trust_anchor_entry(  # rotazione
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-2", public_key=pub_2,
    ))

    client = TrustAnchorRegistryClient(store)
    # nota: nessun client.refresh() chiamato — resolve_at() non ne ha bisogno

    resolved = await client.resolve_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-1", signed_at,
    )
    assert resolved is not None

    not_yet = await client.resolve_at(
        "sparkle", "did:web:sofia.sparkle.example.com#key-2", signed_at,
    )
    assert not_yet is None


# ---------------------------------------------------------------------------
# Integrazione end-to-end con Sentinel — lo scenario "Operatore C"
# ---------------------------------------------------------------------------

async def test_identity_credential_verifies_once_authority_is_onboarded(store):
    keystore = InMemoryIdentityKeystore()
    c_authority_did = "did:web:c.example.com"
    c_key, c_pub, _ = bootstrap_signing_key("c-governing-authority", keystore)
    c_vm_id = f"{c_authority_did}#key-1"

    _, agent_pub, _ = bootstrap_signing_key("c-agent-015", keystore)
    agent_doc = build_did_document(
        host="c.example.com", agent_path_segments=["agents", "015"],
        governing_authority_did=c_authority_did, signing_public_key=agent_pub,
    )

    issuer = IdentityManager(
        governing_authority_did=c_authority_did, signing_key=c_key,
        signing_verification_method_id=c_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://c.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=agent_doc.id, client_id="c-agent-015")

    # Onboarding: Operatore C viene registrato nel Trust Anchor Registry di Sparkle
    await store.put(build_trust_anchor_entry(
        authority_id="operatore-c", did=c_authority_did,
        verification_method_id=c_vm_id, public_key=c_pub,
    ))

    client = TrustAnchorRegistryClient(store)
    await client.refresh()

    sentinel = Sentinel(trusted_issuer_keys=client.as_trusted_keys())
    now = dt.datetime.now(dt.timezone.utc)
    # non deve sollevare — C è onboardata
    await sentinel.verify(credential, status_bitstring_fetch_time=now, is_revoked=False)


async def test_old_credential_verifies_via_trust_registry_fallback_after_rotation(store):
    """Il punto centrale di questo step: senza trust_registry passato al
    Sentinel, una credenziale firmata con una chiave da allora ruotata
    fallisce (la chiave non è più nella cache "corrente"). Con
    trust_registry passato, il fallback la risolve correttamente contro
    il momento in cui fu firmata — non "adesso"."""
    keystore = InMemoryIdentityKeystore()
    c_authority_did = "did:web:c.example.com"
    c_key_1, c_pub_1, _ = bootstrap_signing_key("c-governing-authority-key1", keystore)
    c_vm_id_1 = f"{c_authority_did}#key-1"

    _, agent_pub, _ = bootstrap_signing_key("c-agent-015", keystore)
    agent_doc = build_did_document(
        host="c.example.com", agent_path_segments=["agents", "015"],
        governing_authority_did=c_authority_did, signing_public_key=agent_pub,
    )

    onboarded_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    await store.put(build_trust_anchor_entry(
        authority_id="operatore-c", did=c_authority_did,
        verification_method_id=c_vm_id_1, public_key=c_pub_1, onboarded_at=onboarded_at,
    ))

    # La credenziale viene emessa (e firmata per davvero con key-1) mentre
    # key-1 è ancora corrente...
    issuer = IdentityManager(
        governing_authority_did=c_authority_did, signing_key=c_key_1,
        signing_verification_method_id=c_vm_id_1,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://c.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=agent_doc.id, client_id="c-agent-015")
    # ...simuliamo che sia stata firmata 20 giorni fa: proof.created non fa
    # parte dei byte firmati (è impostato DOPO il calcolo della firma in
    # _sign()), quindi questo non invalida la firma reale.
    signed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=20)
    credential.proof.created = signed_at

    # Rotazione: Operatore C ruota la propria chiave OGGI. key-1 resta
    # nello storico (valid_until chiuso), key-2 diventa quella corrente.
    c_key_2, c_pub_2, _ = bootstrap_signing_key("c-governing-authority-key2", keystore)
    c_vm_id_2 = f"{c_authority_did}#key-2"
    await store.put(build_trust_anchor_entry(
        authority_id="operatore-c", did=c_authority_did,
        verification_method_id=c_vm_id_2, public_key=c_pub_2,
    ))

    client = TrustAnchorRegistryClient(store)
    await client.refresh()  # la cache ora contiene SOLO key-2

    now = dt.datetime.now(dt.timezone.utc)

    # SENZA trust_registry: il fallback non esiste, key-1 non è in cache -> rifiuta
    sentinel_without_fallback = Sentinel(trusted_issuer_keys=client.as_trusted_keys())
    with pytest.raises(CredentialVerificationError, match="unknown or untrusted"):
        await sentinel_without_fallback.verify(credential, status_bitstring_fetch_time=now, is_revoked=False)

    # CON trust_registry: il fallback risolve key-1 contro il momento della firma -> verifica
    sentinel_with_fallback = Sentinel(
        trusted_issuer_keys=client.as_trusted_keys(), trust_registry=client,
    )
    await sentinel_with_fallback.verify(credential, status_bitstring_fetch_time=now, is_revoked=False)


async def test_identity_credential_rejected_when_authority_not_onboarded(store):
    keystore = InMemoryIdentityKeystore()
    c_authority_did = "did:web:c.example.com"
    c_key, c_pub, _ = bootstrap_signing_key("c-governing-authority", keystore)
    c_vm_id = f"{c_authority_did}#key-1"

    _, agent_pub, _ = bootstrap_signing_key("c-agent-015", keystore)
    agent_doc = build_did_document(
        host="c.example.com", agent_path_segments=["agents", "015"],
        governing_authority_did=c_authority_did, signing_public_key=agent_pub,
    )

    issuer = IdentityManager(
        governing_authority_did=c_authority_did, signing_key=c_key,
        signing_verification_method_id=c_vm_id,
        status_list_manager=_InMemoryStatusListManager(),
        status_list_publish_url="https://c.example.com/identity/status-list",
        audit_hook=_noop_audit_hook,
    )
    credential = await issuer.issue_identity_credential(agent_did=agent_doc.id, client_id="c-agent-015")

    # Registry vuoto: nessun onboarding di Operatore C
    client = TrustAnchorRegistryClient(store)
    await client.refresh()

    sentinel = Sentinel(trusted_issuer_keys=client.as_trusted_keys())
    now = dt.datetime.now(dt.timezone.utc)

    from stls_identity import CredentialVerificationError
    with pytest.raises(CredentialVerificationError, match="unknown or untrusted"):
        await sentinel.verify(credential, status_bitstring_fetch_time=now, is_revoked=False)


# ---------------------------------------------------------------------------
# Bridge HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
async def http_client(store):
    app = FastAPI()
    app.include_router(create_trust_registry_router(store=store))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://registry.test") as c:
        yield c


async def test_list_anchors_empty_registry(http_client):
    resp = await http_client.get("/trust-registry/anchors")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_anchors_returns_onboarded_authorities(store, http_client):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("sparkle-authority", keystore)
    await store.put(build_trust_anchor_entry(
        authority_id="sparkle", did="did:web:sofia.sparkle.example.com",
        verification_method_id="did:web:sofia.sparkle.example.com#key-1", public_key=pub,
    ))

    resp = await http_client.get("/trust-registry/anchors")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["authorityId"] == "sparkle"


async def test_get_anchor_404_for_unknown(http_client):
    resp = await http_client.get("/trust-registry/anchors/nobody")
    assert resp.status_code == 404


async def test_get_anchor_404_for_revoked(store, http_client):
    keystore = InMemoryIdentityKeystore()
    _, pub, _ = bootstrap_signing_key("authority-b", keystore)
    await store.put(build_trust_anchor_entry(
        authority_id="b", did="did:web:b.example.com",
        verification_method_id="did:web:b.example.com#key-1", public_key=pub,
    ))
    await store.revoke_authority("b", dt.datetime.now(dt.timezone.utc))

    resp = await http_client.get("/trust-registry/anchors/b")
    assert resp.status_code == 404