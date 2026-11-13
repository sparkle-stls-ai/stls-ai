"""
test_stls_core_keystore.py
---------------------------
Unit test per i backend dello store chiavi Arqit SKA-Platform™.

Copertura:

  InMemoryArqitKeyStore
  ─────────────────────
  M01 — add() + get() restituisce i bytes corretti
  M02 — get() su key_id inesistente restituisce None
  M03 — get_last() su store vuoto restituisce None
  M04 — get_last() restituisce l'ultima chiave aggiunta
  M05 — mark_delivered() è no-op: la chiave resta accessibile
  M06 — wait_for_new_key() trova una chiave aggiunta in ritardo
  M07 — wait_for_new_key() restituisce None al timeout se seen_key_ids copre tutto
  M08 — wait_for_new_key() salta i kid presenti in seen_key_ids

  RedisArqitKeyStore  (fakeredis — nessun Redis reale richiesto)
  ──────────────────────────────────────────────────────────────
  R01 — add() crea entry key:<kid> con TTL ≈ pending_ttl
  R02 — add() inserisce kid nel ZSET pending con score = timestamp
  R03 — get() restituisce bytes corretti
  R04 — get() su key_id inesistente restituisce None
  R05 — get() rinnova il TTL (sliding idle timeout via GETEX)
  R06 — get_last() restituisce la chiave più recente in pending
  R07 — get_last() su store vuoto restituisce None
  R08 — get_last() ignora entry pending con key:<kid> scaduta (pulizia passiva)
  R09 — mark_delivered() estende TTL da pending_ttl a session_ttl
  R10 — mark_delivered() rimuove kid dal ZSET pending
  R11 — wait_for_new_key() trova una chiave non in seen_key_ids
  R12 — wait_for_new_key() restituisce None al timeout
  R13 — wait_for_new_key() salta i kid presenti in seen_key_ids
  R14 — wait_for_new_key() ignora entry pending scadute (key:<kid> assente)
  R18 — default (no KEK): key_data is stored in the clear (unchanged behavior)
  R19 — with STLS_CORE_ATREST_KEY set, key_data is sealed and still
         round-trips through get() (issue #20 (b))
  R20 — get() with the wrong KEK fails closed (returns None), doesn't crash

  ArqitKeyStoreFactory
  ─────────────────────
  F01 — KEY_STORE_BACKEND non impostato → InMemoryArqitKeyStore (default)
  F02 — KEY_STORE_BACKEND=memory → InMemoryArqitKeyStore
  F03 — KEY_STORE_BACKEND=redis  → RedisArqitKeyStore
  F04 — KEY_STORE_BACKEND=unknown → ValueError
  F05 — namespace passato esplicitamente viene usato
  F06 — namespace letto da KEY_STORE_REDIS_NAMESPACE se non passato
"""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest

from stls_core.providers.stls_core_providers_keystore import (
    ArqitKeyStoreFactory,
    ArqitKeyStoreBase,
    InMemoryArqitKeyStore,
    RedisArqitKeyStore,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KID1 = "kid-aaa-111"
KID2 = "kid-bbb-222"
KID3 = "kid-ccc-333"
DATA1 = b"\x01" * 32
DATA2 = b"\x02" * 32
DATA3 = b"\x03" * 32


def make_redis_store(
    server,
    namespace: str = "test",
    pending_ttl: int = 60,
    session_ttl: int = 300,
    poll_interval: float = 0.05,
    zrem_on_deliver: bool = False,
    cipher=None,
) -> RedisArqitKeyStore:
    """Costruisce un RedisArqitKeyStore collegato a un fakeredis server.

    cipher=None (default) matches the no-KEK fallback (plaintext bytes);
    pass an AtRestCipher to test the encryption-at-rest path (issue #20).
    """
    store = RedisArqitKeyStore.__new__(RedisArqitKeyStore)
    store._r = fakeredis.FakeRedis(server=server, decode_responses=False)
    store._ns = namespace
    store._pending_ttl = pending_ttl
    store._session_ttl = session_ttl
    store._poll_interval = poll_interval
    store._zrem_on_deliver = zrem_on_deliver
    store._cipher = cipher
    return store


# ---------------------------------------------------------------------------
# M — InMemoryArqitKeyStore
# ---------------------------------------------------------------------------

class TestInMemoryArqitKeyStore:

    def test_m01_add_get(self):
        """M01 — add() + get() restituisce i bytes corretti."""
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)
        assert store.get(KID1) == DATA1

    def test_m02_get_nonexistent_returns_none(self):
        """M02 — get() su key_id inesistente restituisce None."""
        store = InMemoryArqitKeyStore()
        assert store.get("nonexistent") is None

    def test_m03_get_last_empty_returns_none(self):
        """M03 — get_last() su store vuoto restituisce None."""
        store = InMemoryArqitKeyStore()
        assert store.get_last() is None

    def test_m04_get_last_returns_most_recent(self):
        """M04 — get_last() restituisce l'ultima chiave aggiunta."""
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)
        store.add(KID2, DATA2)
        assert store.get_last() == (KID2, DATA2)

    def test_m05_mark_delivered_noop(self):
        """M05 — mark_delivered() è no-op: la chiave resta accessibile."""
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)
        store.mark_delivered(KID1)
        assert store.get(KID1) == DATA1
        assert store.get_last() == (KID1, DATA1)

    def test_m06_wait_for_new_key_finds_key(self):
        """M06 — wait_for_new_key() trova una chiave aggiunta in ritardo."""
        store = InMemoryArqitKeyStore()

        def adder():
            time.sleep(0.2)
            store.add(KID1, DATA1)

        threading.Thread(target=adder, daemon=True).start()
        result = store.wait_for_new_key(seen_key_ids=set(), timeout=2.0)
        assert result == (KID1, DATA1)

    def test_m07_wait_for_new_key_timeout(self):
        """M07 — wait_for_new_key() restituisce None al timeout se seen_key_ids copre tutto."""
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)
        result = store.wait_for_new_key(seen_key_ids={KID1}, timeout=0.3)
        assert result is None

    def test_m08_wait_for_new_key_skips_seen(self):
        """M08 — wait_for_new_key() salta i kid presenti in seen_key_ids."""
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)

        def adder():
            time.sleep(0.2)
            store.add(KID2, DATA2)

        threading.Thread(target=adder, daemon=True).start()
        result = store.wait_for_new_key(seen_key_ids={KID1}, timeout=2.0)
        assert result == (KID2, DATA2)

    def test_m09_startup_seen_keys_always_empty(self):
        """M09 — InMemoryArqitKeyStore.startup_seen_keys() restituisce sempre set().

        L'in-memory non ha persistenza — ogni processo inizia da zero.
        """
        store = InMemoryArqitKeyStore()
        store.add(KID1, DATA1)
        store.add(KID2, DATA2)
        assert store.startup_seen_keys() == set()


# ---------------------------------------------------------------------------
# R — RedisArqitKeyStore  (fakeredis)
# ---------------------------------------------------------------------------

fakeredis = pytest.importorskip(
    "fakeredis",
    reason="fakeredis non installato — esegui: pip install fakeredis",
)


@pytest.fixture
def rserver():
    """fakeredis server condiviso per ogni test Redis."""
    server = fakeredis.FakeServer()
    yield server


@pytest.fixture
def rstore(rserver):
    return make_redis_store(rserver)


@pytest.fixture
def rfake(rserver):
    """Restituisce un FakeRedis collegato allo stesso server — utile per ispezione diretta."""
    return fakeredis.FakeRedis(server=rserver, decode_responses=False)


class TestRedisArqitKeyStore:

    def test_r01_add_creates_key_with_pending_ttl(self, rstore):
        """R01 — add() crea entry key:<kid> con TTL ≈ pending_ttl."""
        rstore.add(KID1, DATA1)
        ttl = rstore._r.ttl(rstore._key(KID1))
        assert 0 < ttl <= rstore._pending_ttl

    def test_r02_add_inserts_kid_in_pending_zset(self, rstore):
        """R02 — add() inserisce kid nel ZSET pending con score = timestamp."""
        rstore.add(KID1, DATA1)
        members = rstore._r.zrange(rstore._pending(), 0, -1)
        decoded = [m.decode() if isinstance(m, bytes) else m for m in members]
        assert KID1 in decoded

    def test_r03_get_returns_bytes(self, rstore):
        """R03 — get() restituisce bytes corretti."""
        rstore.add(KID1, DATA1)
        assert rstore.get(KID1) == DATA1

    def test_r04_get_nonexistent_returns_none(self, rstore):
        """R04 — get() su key_id inesistente restituisce None."""
        assert rstore.get("nonexistent") is None

    def test_r05_get_renews_ttl(self, rstore):
        """R05 — get() rinnova il TTL (sliding idle timeout via GETEX)."""
        rstore.add(KID1, DATA1)
        rstore.mark_delivered(KID1)
        # Abbassa artificialmente il TTL per simulare avanzamento del tempo
        rstore._r.expire(rstore._key(KID1), 10)
        assert rstore._r.ttl(rstore._key(KID1)) == 10
        # Una get() deve riportarlo a session_ttl tramite GETEX
        rstore.get(KID1)
        ttl_after = rstore._r.ttl(rstore._key(KID1))
        assert ttl_after > 10
        assert ttl_after <= rstore._session_ttl

    def test_r06_get_last_returns_most_recent(self, rstore):
        """R06 — get_last() restituisce la chiave più recente in pending."""
        rstore.add(KID1, DATA1)
        time.sleep(0.01)
        rstore.add(KID2, DATA2)
        result = rstore.get_last()
        assert result == (KID2, DATA2)

    def test_r07_get_last_empty_returns_none(self, rstore):
        """R07 — get_last() su store vuoto restituisce None."""
        assert rstore.get_last() is None

    def test_r08_get_last_skips_expired_pending_entry(self, rstore):
        """R08 — get_last() ignora entry pending con key:<kid> scaduta (pulizia passiva).

        KID1 è inserito nel ZSET con score più alto (più recente) ma la sua
        key:<kid> non esiste (simula scadenza). KID2 è più vecchio ma valido.
        get_last() deve scavalcare KID1, rimuoverlo passivamente e restituire KID2.
        """
        # KID2 aggiunto per primo (score minore = più vecchio)
        rstore.add(KID2, DATA2)
        time.sleep(0.01)
        # KID1 inserito solo nel ZSET pending — senza key:<kid> (simula scadenza TTL)
        rstore._r.zadd(rstore._pending(), {KID1: time.time()})

        result = rstore.get_last()
        # Deve trovare KID2 (valido), non KID1 (scaduto)
        assert result == (KID2, DATA2)
        # KID1 deve essere stato rimosso dal pending durante la pulizia passiva
        members = rstore._r.zrange(rstore._pending(), 0, -1)
        decoded = [m.decode() if isinstance(m, bytes) else m for m in members]
        assert KID1 not in decoded

    def test_r09_mark_delivered_extends_ttl(self, rstore):
        """R09 — mark_delivered() estende TTL da pending_ttl a session_ttl."""
        rstore.add(KID1, DATA1)
        ttl_pending = rstore._r.ttl(rstore._key(KID1))
        assert ttl_pending <= rstore._pending_ttl

        rstore.mark_delivered(KID1)
        ttl_session = rstore._r.ttl(rstore._key(KID1))
        assert ttl_session > ttl_pending
        assert ttl_session <= rstore._session_ttl

    def test_r10_mark_delivered_keeps_kid_in_pending_for_sibling_instances(self, rstore):
        """R10 — server (zrem_on_deliver=False): mark_delivered() NON rimuove dal ZSET.

        In multi-istanza (Cloud Run), più provider server condividono il namespace Redis.
        Rimuovere il kid impedirebbe alle istanze sorelle di trovarlo.
        La deduplicazione è garantita da _delivered_key_ids per istanza (in-memory).
        """
        # rstore ha zrem_on_deliver=False (default — comportamento server)
        rstore.add(KID1, DATA1)
        rstore.mark_delivered(KID1)
        # Il kid deve RESTARE nel ZSET pending
        members = rstore._r.zrange(rstore._pending(), 0, -1)
        decoded = [m.decode() if isinstance(m, bytes) else m for m in members]
        assert KID1 in decoded
        # Il TTL deve essere stato aggiornato a session_ttl
        ttl = rstore._r.ttl(rstore._key(KID1))
        assert ttl <= rstore._session_ttl
        assert ttl > rstore._pending_ttl - 5

    def test_r10b_client_mark_delivered_removes_from_pending(self, rserver):
        """R10b — client (zrem_on_deliver=True): mark_delivered() rimuove dal ZSET.

        Ogni run del client è indipendente: i kid già usati devono essere
        rimossi dal ZSET pending affinché run successivi trovino solo chiavi fresche.
        """
        client_store = make_redis_store(rserver, zrem_on_deliver=True)
        client_store.add(KID1, DATA1)
        client_store.mark_delivered(KID1)
        members = client_store._r.zrange(client_store._pending(), 0, -1)
        decoded = [m.decode() if isinstance(m, bytes) else m for m in members]
        assert KID1 not in decoded
        # La key:<kid> deve ancora esistere (TTL aggiornato a session_ttl)
        assert client_store.get(KID1) == DATA1

    def test_r11_wait_for_new_key_finds_key(self, rstore):
        """R11 — wait_for_new_key() trova una chiave non in seen_key_ids."""
        rstore.add(KID1, DATA1)
        result = rstore.wait_for_new_key(seen_key_ids=set(), timeout=2.0)
        assert result == (KID1, DATA1)

    def test_r12_wait_for_new_key_timeout(self, rstore):
        """R12 — wait_for_new_key() restituisce None al timeout."""
        rstore.add(KID1, DATA1)
        result = rstore.wait_for_new_key(seen_key_ids={KID1}, timeout=0.3)
        assert result is None

    def test_r13_wait_for_new_key_skips_seen(self, rstore):
        """R13 — wait_for_new_key() salta i kid presenti in seen_key_ids."""
        rstore.add(KID1, DATA1)
        rstore.add(KID2, DATA2)
        result = rstore.wait_for_new_key(seen_key_ids={KID1}, timeout=2.0)
        assert result == (KID2, DATA2)

    def test_r14_wait_for_new_key_ignores_expired_pending(self, rstore):
        """R14 — wait_for_new_key() ignora entry pending scadute (key:<kid> assente)."""
        # KID1 nel pending ZSET ma nessuna key:<kid> corrispondente
        rstore._r.zadd(rstore._pending(), {KID1: time.time()})
        # KID2 correttamente presente
        rstore.add(KID2, DATA2)
        result = rstore.wait_for_new_key(seen_key_ids=set(), timeout=2.0)
        assert result == (KID2, DATA2)

    def test_r15_startup_seen_keys_returns_all_pending(self, rstore):
        """R15 — startup_seen_keys() restituisce tutti i kid nel ZSET pending.

        Scenario riavvio server: il Redis ZSET conserva chiavi di sessioni
        precedenti. startup_seen_keys() le elenca affinché _delivered_key_ids
        venga pre-popolato e il server non le riusi per nuovi handshake.
        """
        rstore.add(KID1, DATA1)
        rstore.add(KID2, DATA2)
        rstore.add(KID3, DATA3)

        seen = rstore.startup_seen_keys()
        assert KID1 in seen
        assert KID2 in seen
        assert KID3 in seen
        assert len(seen) == 3

    def test_r16_startup_seen_keys_empty_when_no_pending(self, rstore):
        """R16 — startup_seen_keys() restituisce set() vuoto se il ZSET è vuoto."""
        assert rstore.startup_seen_keys() == set()

    def test_r17_server_restart_does_not_reuse_stale_keys(self, rserver):
        """R17 — scenario riavvio server: le chiavi stale in Redis vengono ignorate.

        Simula: server precedente consegnò KID1, poi riavvio. Il nuovo server
        chiama startup_seen_keys() → trova KID1 → lo aggiunge a _delivered_key_ids
        → wait_for_new_key({KID1}) lo salta e aspetta KID2 (nuova chiave).
        """
        import threading

        store = make_redis_store(rserver, zrem_on_deliver=False)

        # Simula chiave lasciata da sessione precedente (SERVER role: no ZREM)
        store.add(KID1, DATA1)
        store.mark_delivered(KID1)  # TTL=session_ttl, ZSET entry rimane

        # Nuovo processo server: legge startup_seen_keys e pre-popola delivered
        stale_keys = store.startup_seen_keys()
        assert KID1 in stale_keys

        # Simula arrivo di una nuova chiave fresca (nuovo peering Arqit)
        def add_new_key():
            time.sleep(0.2)
            store.add(KID2, DATA2)

        threading.Thread(target=add_new_key, daemon=True).start()

        # wait_for_new_key con stale_keys come seen → deve skippare KID1 e trovare KID2
        result = store.wait_for_new_key(seen_key_ids=stale_keys, timeout=2.0)
        assert result == (KID2, DATA2)

    def test_r18_no_cipher_stores_key_data_in_the_clear(self, rstore):
        """R18 — default (no KEK): key_data is stored byte-for-byte, as before issue #20."""
        rstore.add(KID1, DATA1)
        raw = rstore._r.get(rstore._key(KID1))
        assert bytes(raw) == DATA1

    def test_r19_atrest_cipher_set_seals_key_data(self, rserver):
        """R19 — with a KEK configured, the stored value is opaque and still round-trips via get() (issue #20 (b))."""
        from stls_core.stls_core_atrest import AtRestCipher

        cipher = AtRestCipher(kek=b"\x42" * 32, label=b"stls-core/keystore")
        store = make_redis_store(rserver, cipher=cipher)
        store.add(KID1, DATA1)

        raw = store._r.get(store._key(KID1))
        assert bytes(raw) != DATA1

        assert store.get(KID1) == DATA1

    def test_r20_atrest_wrong_kek_fails_closed(self, rserver):
        """R20 — get() with the wrong KEK treats the entry as a miss, not a crash."""
        from stls_core.stls_core_atrest import AtRestCipher

        writer = make_redis_store(
            rserver, cipher=AtRestCipher(kek=b"\x01" * 32, label=b"stls-core/keystore")
        )
        reader = make_redis_store(
            rserver, cipher=AtRestCipher(kek=b"\x02" * 32, label=b"stls-core/keystore")
        )
        writer.add(KID1, DATA1)
        assert reader.get(KID1) is None


# ---------------------------------------------------------------------------
# F — ArqitKeyStoreFactory
# ---------------------------------------------------------------------------

class TestArqitKeyStoreFactory:

    def test_f01_default_is_memory(self):
        """F01 — KEY_STORE_BACKEND non impostato → InMemoryArqitKeyStore."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KEY_STORE_BACKEND", None)
            store = ArqitKeyStoreFactory.build()
        assert isinstance(store, InMemoryArqitKeyStore)

    def test_f02_memory_explicit(self):
        """F02 — KEY_STORE_BACKEND=memory → InMemoryArqitKeyStore."""
        with patch.dict(os.environ, {"KEY_STORE_BACKEND": "memory"}):
            store = ArqitKeyStoreFactory.build()
        assert isinstance(store, InMemoryArqitKeyStore)

    def test_f03_redis_returns_redis_store(self):
        """F03 — KEY_STORE_BACKEND=redis → RedisArqitKeyStore."""
        env = {
            "KEY_STORE_BACKEND": "redis",
            "KEY_STORE_REDIS_URL": "redis://localhost:6379",
            "KEY_STORE_REDIS_NAMESPACE": "test",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                store = ArqitKeyStoreFactory.build(role="server")
        assert isinstance(store, RedisArqitKeyStore)
        assert store._ns == "test:server"
        assert store._zrem_on_deliver is False

    def test_f03b_client_role_uses_zrem_and_isolated_namespace(self):
        """F03b — role=client → zrem_on_deliver=True, namespace={ns}:client:{uuid}.

        Ogni chiamata a build(role=client) produce un namespace univoco per
        evitare collisioni tra sessioni client concorrenti (asyncio.gather).
        """
        env = {
            "KEY_STORE_BACKEND": "redis",
            "KEY_STORE_REDIS_URL": "redis://localhost:6379",
            "KEY_STORE_REDIS_NAMESPACE": "test",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                store1 = ArqitKeyStoreFactory.build(role="client")
                store2 = ArqitKeyStoreFactory.build(role="client")
        assert isinstance(store1, RedisArqitKeyStore)
        assert isinstance(store2, RedisArqitKeyStore)
        assert store1._zrem_on_deliver is True
        assert store2._zrem_on_deliver is True
        assert store1._ns.startswith("test:client:")
        assert store2._ns.startswith("test:client:")
        # Namespace diversi: sessioni concorrenti non si contaminano
        assert store1._ns != store2._ns

    def test_f03c_client_explicit_instance_id(self):
        """F03c — instance_id esplicito viene usato nel namespace."""
        env = {
            "KEY_STORE_BACKEND": "redis",
            "KEY_STORE_REDIS_URL": "redis://localhost:6379",
            "KEY_STORE_REDIS_NAMESPACE": "test",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                store = ArqitKeyStoreFactory.build(role="client", instance_id="abc-123")
        assert store._ns == "test:client:abc-123"

    def test_f04_unknown_backend_raises_value_error(self):
        """F04 — KEY_STORE_BACKEND=unknown → ValueError."""
        with patch.dict(os.environ, {"KEY_STORE_BACKEND": "unknown"}):
            with pytest.raises(ValueError, match="KEY_STORE_BACKEND"):
                ArqitKeyStoreFactory.build()

    def test_f05_explicit_namespace_used(self):
        """F05 — namespace passato esplicitamente viene usato."""
        with patch.dict(os.environ, {"KEY_STORE_BACKEND": "memory"}):
            store = ArqitKeyStoreFactory.build(namespace="my-ns")
        # InMemoryArqitKeyStore non usa namespace — verifica solo che non esploda
        assert isinstance(store, InMemoryArqitKeyStore)

    def test_f06_namespace_from_env(self):
        """F06 — namespace letto da KEY_STORE_REDIS_NAMESPACE se non passato, con suffisso ruolo."""
        env = {
            "KEY_STORE_BACKEND": "redis",
            "KEY_STORE_REDIS_URL": "redis://localhost:6379",
            "KEY_STORE_REDIS_NAMESPACE": "stls-sk",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                store = ArqitKeyStoreFactory.build(role="server")
        assert isinstance(store, RedisArqitKeyStore)
        assert store._ns == "stls-sk:server"

    def test_f07_implements_abc(self):
        """F07 — entrambe le implementazioni rispettano l'ABC ArqitKeyStoreBase."""
        memory = InMemoryArqitKeyStore()
        assert isinstance(memory, ArqitKeyStoreBase)

        with patch.dict(os.environ, {
            "KEY_STORE_BACKEND": "redis",
            "KEY_STORE_REDIS_URL": "redis://localhost:6379",
        }):
            with patch("redis.Redis.from_url"):
                redis_store = ArqitKeyStoreFactory.build()
        assert isinstance(redis_store, ArqitKeyStoreBase)