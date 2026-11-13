"""
test_stls_core_session_cache.py
---------------------------------
Suite di test per SessionCacheBase e le sue implementazioni.

Copertura:

  InMemorySessionCache
  ─────────────────────
  M01 — put + get restituisce uno StlsTransport valido
  M02 — get su sessione inesistente restituisce None
  M03 — get ricostruisce correttamente il transport (encrypt/decrypt round-trip)
  M04 — put aggiorna la sessione esistente senza duplicati
  M05 — TTL scaduto: get restituisce None
  M06 — TTL sliding: get rinnova il TTL (sessione sopravvive se usata)
  M07 — eviction LRU: sessione meno usata espulsa quando cache è piena
  M08 — remove elimina la sessione, restituisce True
  M09 — remove su sessione inesistente restituisce False
  M10 — purge_expired rimuove solo le sessioni scadute
  M11 — __len__ e __contains__ corretti
  M12 — kid conservato e recuperabile tramite accesso interno
  M13 — max_age=0 (default): no absolute expiry
  M14 — max-age expired: get returns None
  M15 — an update put() does not reset the max-age clock
  M16 — max-age does not renew on access (unlike the idle TTL)

  RedisSessionCache  (fakeredis — nessun Redis reale richiesto)
  ──────────────────────────────────────────────────────────────
  R01 — put registra la sessione in Redis con TTL corretto
  R02 — get restituisce uno StlsTransport valido
  R03 — get ricostruisce correttamente il transport (encrypt/decrypt round-trip)
  R04 — get su sessione inesistente restituisce None
  R05 — get rinnova il TTL (GETEX sliding idle timeout)
  R06 — put aggiorna la sessione esistente
  R07 — remove elimina la sessione da Redis
  R08 — remove su sessione inesistente restituisce False
  R09 — purge_expired è no-op (Redis gestisce TTL nativamente)
  R10 — __contains__ corretto
  R11 — kid correttamente serializzato e conservato nel payload JSON
  R12 — sessione scaduta (TTL Redis) non trovata
  R13 — no collisione tra sessioni diverse sullo stesso Redis
  R14 — no collisione tra SessionCache e KeyStore sullo stesso Redis
         (prefissi stls:sc:* e stls:ks:* sono separati)
  R15 — max_age=0 (default): no absolute expiry
  R16 — max-age expired: get returns None and removes the entry from Redis
  R17 — max-age does not renew on access (unlike GETEX)
  R18 — no cross-instance coordination needed: two separate
         RedisSessionCache instances on the same Redis agree on max-age
         expiry by reading the same shared created_at (issue #6)
  R19 — the raw session_key is never stored, only the derived keys (issue #20 (a))
  R20 — with STLS_CORE_ATREST_KEY set, the stored payload is sealed and
         still round-trips through get() (issue #20 (b))
  R21 — get() with the wrong KEK fails closed (returns None), doesn't crash

  SessionCacheFactory
  ─────────────────────
  F01 — SESSION_CACHE_BACKEND non impostato → InMemorySessionCache (default)
  F02 — SESSION_CACHE_BACKEND=memory → InMemorySessionCache
  F03 — SESSION_CACHE_BACKEND=redis  → RedisSessionCache
  F04 — SESSION_CACHE_BACKEND=unknown → ValueError
  F05 — prefix letto da SESSION_CACHE_REDIS_NAMESPACE
  F06 — TTL letto da SESSION_CACHE_TTL
  F07 — TTL letto da STLS_SESSION_TTL (alias backward compat)
  F08 — entrambe le implementazioni rispettano SessionCacheBase ABC
  F09 — SESSION_KEY_MAX_AGE_SECONDS unset → max_age=0
  F10 — SESSION_KEY_MAX_AGE_SECONDS read correctly (memory backend)
  F11 — SESSION_KEY_MAX_AGE_SECONDS read correctly (redis backend)
"""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest

from stls_core.stls_core_session import (
    InMemorySessionCache,
    RedisSessionCache,
    SessionCacheBase,
    SessionCacheFactory,
)
from stls_core.stls_core_crypto import StlsTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

KID1 = "kid-aaa-111"
KID2 = "kid-bbb-222"
KID3 = "kid-ccc-333"
KEY1 = b"\x01" * 32
KEY2 = b"\x02" * 32
KEY3 = b"\x03" * 32
SID1 = "session-1111-aaaa"
SID2 = "session-2222-bbbb"
SID3 = "session-3333-cccc"

# Testo di test per round-trip encrypt/decrypt
PLAINTEXT = b'{"jsonrpc":"2.0","method":"messages/send","id":1}'

fakeredis = pytest.importorskip(
    "fakeredis",
    reason="fakeredis non installato — esegui: pip install fakeredis",
)


def make_redis_cache(
    server,
    prefix: str = "stls:sc",
    ttl_seconds: int = 300,
    max_age_seconds: int = 0,
    cipher=None,
) -> RedisSessionCache:
    """Costruisce un RedisSessionCache collegato a un fakeredis server.

    cipher=None (default) matches the no-KEK fallback (plaintext JSON);
    pass an AtRestCipher to test the encryption-at-rest path (issue #20).
    """
    cache = RedisSessionCache.__new__(RedisSessionCache)
    cache._r = fakeredis.FakeRedis(server=server, decode_responses=True)
    cache._prefix = prefix
    cache._ttl = ttl_seconds
    cache._max_age = max_age_seconds
    cache._cipher = cipher
    return cache


def encrypt_decrypt_roundtrip(transport: StlsTransport, session_id: str) -> bool:
    """Verifica che il transport possa cifrare e decifrare correttamente."""
    try:
        envelope = transport.encrypt(PLAINTEXT)
        decrypted = transport.decrypt(envelope)
        return decrypted == PLAINTEXT
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mem_cache():
    return InMemorySessionCache(max_size=10, ttl_seconds=10)


@pytest.fixture
def rserver():
    return fakeredis.FakeServer()


@pytest.fixture
def rcache(rserver):
    return make_redis_cache(rserver)


# ---------------------------------------------------------------------------
# M — InMemorySessionCache
# ---------------------------------------------------------------------------

class TestInMemorySessionCache:

    def test_m01_put_get_returns_transport(self, mem_cache):
        """M01 — put + get restituisce uno StlsTransport valido."""
        mem_cache.put(SID1, KID1, KEY1)
        result = mem_cache.get(SID1)
        assert result is not None
        assert isinstance(result, StlsTransport)

    def test_m02_get_missing_returns_none(self, mem_cache):
        """M02 — get su sessione inesistente restituisce None."""
        assert mem_cache.get("non-esiste") is None

    def test_m03_transport_encrypt_decrypt_roundtrip(self, mem_cache):
        """M03 — il transport ricostruito cifra e decifra correttamente."""
        mem_cache.put(SID1, KID1, KEY1)
        transport = mem_cache.get(SID1)
        assert encrypt_decrypt_roundtrip(transport, SID1)

    def test_m04_put_updates_existing(self, mem_cache):
        """M04 — put aggiorna la sessione esistente senza duplicati."""
        mem_cache.put(SID1, KID1, KEY1)
        mem_cache.put(SID1, KID2, KEY2)  # sovrascrive
        assert len(mem_cache) == 1
        assert mem_cache.get(SID1) is not None

    def test_m05_ttl_expiry(self):
        """M05 — TTL scaduto: get restituisce None."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=1)
        cache.put(SID1, KID1, KEY1)
        assert cache.get(SID1) is not None
        time.sleep(1.1)
        assert cache.get(SID1) is None

    def test_m06_ttl_sliding(self):
        """M06 — TTL sliding: get rinnova il TTL (sessione vive se usata)."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=1)
        cache.put(SID1, KID1, KEY1)
        # Accedi ripetutamente prima della scadenza
        for _ in range(3):
            time.sleep(0.4)
            result = cache.get(SID1)
            assert result is not None, "Sessione scaduta prematuramente"
        # Lascia scadere
        time.sleep(1.1)
        assert cache.get(SID1) is None

    def test_m07_lru_eviction(self):
        """M07 — eviction LRU: sessione meno usata espulsa quando cache è piena."""
        cache = InMemorySessionCache(max_size=3, ttl_seconds=60)
        cache.put(SID1, KID1, KEY1)
        cache.put(SID2, KID2, KEY2)
        cache.put(SID3, KID3, KEY3)

        # Accedo SID1 per renderlo MRU
        cache.get(SID1)

        # Inserisco SID3: SID2 è LRU → espulso
        cache.put("sid4", "kid4", b"\x04" * 32)

        assert cache.get(SID1) is not None  # MRU → sopravvive
        assert cache.get(SID2) is None       # LRU → espulso
        assert cache.get(SID3) is not None
        assert cache.get("sid4") is not None

    def test_m08_remove(self, mem_cache):
        """M08 — remove elimina la sessione, restituisce True."""
        mem_cache.put(SID1, KID1, KEY1)
        assert mem_cache.remove(SID1) is True
        assert mem_cache.get(SID1) is None

    def test_m09_remove_missing(self, mem_cache):
        """M09 — remove su sessione inesistente restituisce False."""
        assert mem_cache.remove("non-esiste") is False

    def test_m10_purge_expired(self):
        """M10 — purge_expired rimuove solo le sessioni scadute."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=1)
        cache.put(SID1, KID1, KEY1)
        cache.put(SID2, KID2, KEY2)
        time.sleep(1.1)
        cache.put(SID3, KID3, KEY3)  # fresca

        removed = cache.purge_expired()
        assert removed == 2
        assert cache.get(SID1) is None
        assert cache.get(SID2) is None
        assert cache.get(SID3) is not None

    def test_m11_len_and_contains(self, mem_cache):
        """M11 — __len__ e __contains__ corretti."""
        assert len(mem_cache) == 0
        assert SID1 not in mem_cache
        mem_cache.put(SID1, KID1, KEY1)
        assert len(mem_cache) == 1
        assert SID1 in mem_cache
        mem_cache.remove(SID1)
        assert len(mem_cache) == 0
        assert SID1 not in mem_cache

    def test_m12_kid_preserved_in_internal_store(self, mem_cache):
        """M12 — kid conservato nella struttura interna."""
        mem_cache.put(SID1, KID1, KEY1)
        # Accede alla struttura interna per verificare che kid sia salvato
        _transport, kid_stored, _last_access, _created_at = mem_cache._store[SID1]
        assert kid_stored == KID1

    def test_m13_max_age_disabled_by_default(self, mem_cache):
        """M13 — max_age=0 (default): no absolute expiry, idle TTL only."""
        assert mem_cache._max_age == 0
        mem_cache.put(SID1, KID1, KEY1)
        assert mem_cache.get(SID1) is not None

    def test_m14_max_age_expiry(self):
        """M14 — max-age expired: get returns None even with a generous idle TTL."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=60, max_age_seconds=1)
        cache.put(SID1, KID1, KEY1)
        assert cache.get(SID1) is not None
        time.sleep(1.1)
        assert cache.get(SID1) is None

    def test_m15_max_age_not_reset_by_put_update(self):
        """M15 — an update put() does NOT reset the max-age clock."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=60, max_age_seconds=1)
        cache.put(SID1, KID1, KEY1)
        time.sleep(0.6)
        cache.put(SID1, KID2, KEY2)  # idempotent "update", same session_id
        time.sleep(0.6)
        # ~1.2s since the original creation, > max_age=1s
        assert cache.get(SID1) is None

    def test_m16_max_age_not_reset_by_get(self):
        """M16 — unlike the idle TTL, max-age does not renew on access."""
        cache = InMemorySessionCache(max_size=10, ttl_seconds=60, max_age_seconds=1)
        cache.put(SID1, KID1, KEY1)
        for _ in range(3):
            time.sleep(0.4)
            cache.get(SID1)  # repeated access, mirroring test M06 for the idle TTL
        # ~1.2s since creation, despite the frequent access
        assert cache.get(SID1) is None


# ---------------------------------------------------------------------------
# R — RedisSessionCache  (fakeredis)
# ---------------------------------------------------------------------------

class TestRedisSessionCache:

    def test_r01_put_creates_key_with_ttl(self, rcache):
        """R01 — put registra la sessione in Redis con TTL corretto."""
        rcache.put(SID1, KID1, KEY1)
        ttl = rcache._r.ttl(rcache._key(SID1))
        assert 0 < ttl <= rcache._ttl

    def test_r02_get_returns_transport(self, rcache):
        """R02 — get restituisce uno StlsTransport valido."""
        rcache.put(SID1, KID1, KEY1)
        result = rcache.get(SID1)
        assert result is not None
        assert isinstance(result, StlsTransport)

    def test_r03_transport_encrypt_decrypt_roundtrip(self, rcache):
        """R03 — il transport ricostruito cifra e decifra correttamente."""
        rcache.put(SID1, KID1, KEY1)
        transport = rcache.get(SID1)
        assert encrypt_decrypt_roundtrip(transport, SID1)

    def test_r04_get_missing_returns_none(self, rcache):
        """R04 — get su sessione inesistente restituisce None."""
        assert rcache.get("non-esiste") is None

    def test_r05_get_renews_ttl(self, rcache):
        """R05 — get rinnova il TTL tramite GETEX (sliding idle timeout)."""
        rcache.put(SID1, KID1, KEY1)
        # Abbassa artificialmente il TTL
        rcache._r.expire(rcache._key(SID1), 10)
        assert rcache._r.ttl(rcache._key(SID1)) == 10
        # get() deve riportarlo a session_ttl
        rcache.get(SID1)
        ttl_after = rcache._r.ttl(rcache._key(SID1))
        assert ttl_after > 10
        assert ttl_after <= rcache._ttl

    def test_r06_put_updates_existing(self, rcache):
        """R06 — put aggiorna la sessione esistente."""
        rcache.put(SID1, KID1, KEY1)
        rcache.put(SID1, KID2, KEY2)
        result = rcache.get(SID1)
        assert result is not None

    def test_r07_remove(self, rcache):
        """R07 — remove elimina la sessione da Redis."""
        rcache.put(SID1, KID1, KEY1)
        assert rcache.remove(SID1) is True
        assert rcache.get(SID1) is None
        assert not rcache._r.exists(rcache._key(SID1))

    def test_r08_remove_missing(self, rcache):
        """R08 — remove su sessione inesistente restituisce False."""
        assert rcache.remove("non-esiste") is False

    def test_r09_purge_expired_is_noop(self, rcache):
        """R09 — purge_expired è no-op (Redis gestisce TTL nativamente)."""
        rcache.put(SID1, KID1, KEY1)
        removed = rcache.purge_expired()
        assert removed == 0
        assert rcache.get(SID1) is not None

    def test_r10_contains(self, rcache):
        """R10 — __contains__ corretto."""
        rcache.put(SID1, KID1, KEY1)
        assert SID1 in rcache
        assert SID2 not in rcache

    def test_r11_kid_serialized_in_payload(self, rcache):
        """R11 — kid correttamente serializzato e conservato nel payload JSON."""
        import json
        rcache.put(SID1, KID1, KEY1)
        raw = rcache._r.get(rcache._key(SID1))
        data = json.loads(raw)
        assert data["kid"] == KID1
        # The payload holds the already-derived keys, never the raw
        # session_key — issue #20 (a).
        assert "session_key" not in data
        assert set(data.keys()) >= {
            "enc_aes_key", "dec_aes_key", "enc_hmac_key", "dec_hmac_key",
        }

    def test_r19_raw_session_key_never_stored(self, rcache):
        """R19 — the raw session_key bytes never appear in what's written to Redis (issue #20 (a))."""
        import base64
        rcache.put(SID1, KID1, KEY1)
        raw = rcache._r.get(rcache._key(SID1))
        assert base64.b64encode(KEY1).decode() not in raw

    def test_r20_atrest_cipher_set_seals_payload(self, rserver):
        """R20 — with a KEK configured, the stored value is opaque (not JSON) and round-trips via get() (issue #20 (b))."""
        import json
        from stls_core.stls_core_atrest import AtRestCipher

        cipher = AtRestCipher(kek=b"\x42" * 32, label=b"stls-core/session-cache")
        cache = make_redis_cache(rserver, cipher=cipher)
        cache.put(SID1, KID1, KEY1)

        raw = cache._r.get(cache._key(SID1))
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw)

        transport = cache.get(SID1)
        assert transport is not None
        assert encrypt_decrypt_roundtrip(transport, SID1)

    def test_r21_atrest_wrong_kek_fails_closed(self, rserver):
        """R21 — a cache reading with the wrong KEK treats the entry as a miss, not a crash."""
        from stls_core.stls_core_atrest import AtRestCipher

        writer = make_redis_cache(
            rserver, cipher=AtRestCipher(kek=b"\x01" * 32, label=b"stls-core/session-cache")
        )
        reader = make_redis_cache(
            rserver, cipher=AtRestCipher(kek=b"\x02" * 32, label=b"stls-core/session-cache")
        )
        writer.put(SID1, KID1, KEY1)
        assert reader.get(SID1) is None

    def test_r12_expired_session_not_found(self, rserver):
        """R12 — sessione scaduta (TTL Redis) non viene trovata."""
        cache = make_redis_cache(rserver, ttl_seconds=1)
        cache.put(SID1, KID1, KEY1)
        assert cache.get(SID1) is not None
        # Simula scadenza TTL forzando expire a 0
        cache._r.expire(cache._key(SID1), 1)
        time.sleep(1.1)
        assert cache.get(SID1) is None

    def test_r13_no_collision_between_sessions(self, rcache):
        """R13 — sessioni diverse sullo stesso Redis non si contaminano."""
        rcache.put(SID1, KID1, KEY1)
        rcache.put(SID2, KID2, KEY2)
        rcache.put(SID3, KID3, KEY3)

        t1 = rcache.get(SID1)
        t2 = rcache.get(SID2)
        t3 = rcache.get(SID3)

        # Ogni transport deve cifrare/decifrare solo con la propria chiave
        assert encrypt_decrypt_roundtrip(t1, SID1)
        assert encrypt_decrypt_roundtrip(t2, SID2)
        assert encrypt_decrypt_roundtrip(t3, SID3)

    def test_r14_no_collision_with_keystore_namespace(self, rserver):
        """R14 — SessionCache (stls:sc:*) e KeyStore (stls:ks:*) non si sovrappongono."""
        r = fakeredis.FakeRedis(server=rserver, decode_responses=True)

        # Simula una entry del keystore
        r.set("stls:ks:default:key:some-kid", b"\xff" * 32)

        # SessionCache usa prefix stls:sc
        cache = make_redis_cache(rserver, prefix="stls:sc")
        cache.put(SID1, KID1, KEY1)

        # La chiave del keystore non deve essere toccata
        assert r.exists("stls:ks:default:key:some-kid")
        # La chiave della sessione ha il prefix corretto
        assert r.exists(f"stls:sc:{SID1}")
        # Nessuna sovrapposizione
        all_keys = r.keys("*")
        sc_keys = [k for k in all_keys if k.startswith("stls:sc:")]
        ks_keys = [k for k in all_keys if k.startswith("stls:ks:")]
        assert len(sc_keys) == 1
        assert len(ks_keys) == 1

    def test_r15_max_age_disabled_by_default(self, rcache):
        """R15 — max_age=0 (default): no absolute expiry, idle TTL only."""
        assert rcache._max_age == 0
        rcache.put(SID1, KID1, KEY1)
        assert rcache.get(SID1) is not None

    def test_r16_max_age_expiry(self, rserver):
        """R16 — max-age expired: get returns None even with a generous idle TTL."""
        cache = make_redis_cache(rserver, ttl_seconds=300, max_age_seconds=1)
        cache.put(SID1, KID1, KEY1)
        assert cache.get(SID1) is not None
        time.sleep(1.1)
        assert cache.get(SID1) is None
        # Must also have removed the entry from Redis, not just ignored it
        assert not cache._r.exists(cache._key(SID1))

    def test_r17_max_age_not_reset_by_get(self, rserver):
        """R17 — unlike the idle TTL (GETEX), max-age does not renew on access."""
        cache = make_redis_cache(rserver, ttl_seconds=300, max_age_seconds=1)
        cache.put(SID1, KID1, KEY1)
        for _ in range(3):
            time.sleep(0.4)
            cache.get(SID1)
        assert cache.get(SID1) is None

    def test_r18_max_age_no_cross_instance_coordination_needed(self, rserver):
        """
        R18 — proves the "N instances, no lock needed" claim instead of just
        asserting it: two separate RedisSessionCache instances (same Redis,
        simulating two stateless replicas), no lock between them. put()
        from the first, get() from the second after the max-age window:
        both must agree on the expiry, because they read the same shared
        created_at from the one Redis record.
        """
        instance_a = make_redis_cache(rserver, ttl_seconds=300, max_age_seconds=1)
        instance_b = make_redis_cache(rserver, ttl_seconds=300, max_age_seconds=1)

        instance_a.put(SID1, KID1, KEY1)
        # Both instances see the session as valid before expiry.
        assert instance_a.get(SID1) is not None
        assert instance_b.get(SID1) is not None

        time.sleep(1.1)

        # Both instances, independently, see it as expired — no explicit
        # coordination between instance_a and instance_b.
        assert instance_b.get(SID1) is None
        assert instance_a.get(SID1) is None


# ---------------------------------------------------------------------------
# F — SessionCacheFactory
# ---------------------------------------------------------------------------

class TestSessionCacheFactory:

    def test_f01_default_is_memory(self):
        """F01 — SESSION_CACHE_BACKEND non impostato → InMemorySessionCache."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SESSION_CACHE_BACKEND", None)
            cache = SessionCacheFactory.build()
        assert isinstance(cache, InMemorySessionCache)

    def test_f02_memory_explicit(self):
        """F02 — SESSION_CACHE_BACKEND=memory → InMemorySessionCache."""
        with patch.dict(os.environ, {"SESSION_CACHE_BACKEND": "memory"}):
            cache = SessionCacheFactory.build()
        assert isinstance(cache, InMemorySessionCache)

    def test_f03_redis_returns_redis_cache(self):
        """F03 — SESSION_CACHE_BACKEND=redis → RedisSessionCache."""
        env = {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                cache = SessionCacheFactory.build()
        assert isinstance(cache, RedisSessionCache)

    def test_f04_unknown_backend_raises(self):
        """F04 — SESSION_CACHE_BACKEND=unknown → ValueError."""
        with patch.dict(os.environ, {"SESSION_CACHE_BACKEND": "unknown"}):
            with pytest.raises(ValueError, match="SESSION_CACHE_BACKEND"):
                SessionCacheFactory.build()

    def test_f05_prefix_from_env(self):
        """F05 — prefix letto da SESSION_CACHE_REDIS_NAMESPACE."""
        env = {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
            "SESSION_CACHE_REDIS_NAMESPACE": "myapp:sessions",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                cache = SessionCacheFactory.build()
        assert cache._prefix == "myapp:sessions"

    def test_f06_ttl_from_session_cache_ttl(self):
        """F06 — TTL letto da SESSION_CACHE_TTL."""
        env = {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
            "SESSION_CACHE_TTL": "7200",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                cache = SessionCacheFactory.build()
        assert cache._ttl == 7200

    def test_f07_ttl_fallback_to_stls_session_ttl(self):
        """F07 — TTL letto da STLS_SESSION_TTL (alias backward compat)."""
        env = {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
            "STLS_SESSION_TTL": "1800",
        }
        # Rimuoviamo SESSION_CACHE_TTL per verificare il fallback
        with patch.dict(os.environ, env):
            os.environ.pop("SESSION_CACHE_TTL", None)
            with patch("redis.Redis.from_url"):
                cache = SessionCacheFactory.build()
        assert cache._ttl == 1800

    def test_f08_both_implement_abc(self):
        """F08 — entrambe le implementazioni rispettano SessionCacheBase ABC."""
        mem = InMemorySessionCache()
        assert isinstance(mem, SessionCacheBase)

        with patch.dict(os.environ, {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
        }):
            with patch("redis.Redis.from_url"):
                redis_cache = SessionCacheFactory.build()
        assert isinstance(redis_cache, SessionCacheBase)

    def test_f09_max_age_disabled_when_unset(self):
        """F09 — SESSION_KEY_MAX_AGE_SECONDS unset → max_age=0 (no behavior change)."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SESSION_KEY_MAX_AGE_SECONDS", None)
            cache = SessionCacheFactory.build()
        assert cache._max_age == 0

    def test_f10_max_age_from_env_memory_backend(self):
        """F10 — SESSION_KEY_MAX_AGE_SECONDS read correctly (memory backend)."""
        with patch.dict(os.environ, {"SESSION_KEY_MAX_AGE_SECONDS": "86400"}):
            cache = SessionCacheFactory.build()
        assert cache._max_age == 86400

    def test_f11_max_age_from_env_redis_backend(self):
        """F11 — SESSION_KEY_MAX_AGE_SECONDS read correctly (redis backend)."""
        env = {
            "SESSION_CACHE_BACKEND": "redis",
            "SESSION_CACHE_REDIS_URL": "redis://localhost:6379",
            "SESSION_KEY_MAX_AGE_SECONDS": "86400",
        }
        with patch.dict(os.environ, env):
            with patch("redis.Redis.from_url"):
                cache = SessionCacheFactory.build()
        assert cache._max_age == 86400