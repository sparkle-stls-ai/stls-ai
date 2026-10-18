"""
test_stls_core_session.py
--------------------------
Unit test per SessionCache e StlsSession.

Copertura:
  S01 — put + get recupera il transport
  S02 — get su sessione inesistente restituisce None
  S03 — eviction LRU: inserimento oltre max_size espelle il più vecchio
  S04 — TTL scaduto: get restituisce None dopo scadenza
  S05 — remove elimina la sessione
  S06 — remove su sessione inesistente restituisce False
  S07 — purge_expired rimuove solo le sessioni scadute
  S08 — __len__ riflette il numero di sessioni attive
  S09 — __contains__ funziona correttamente
  S10 — put aggiorna il timestamp (LRU refresh)
  S11 — StlsSession.age_seconds() cresce nel tempo
"""

import os
import time

import pytest

from stls_core.stls_core_session import InMemorySessionCache, SessionCache, StlsSession


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_key(sid: str) -> tuple[str, bytes]:
    """Restituisce (kid, session_key) per il nuovo put(sid, kid, key)."""
    return f"kid-{sid}", os.urandom(32)


# ---------------------------------------------------------------------------
# S01 — put + get
# ---------------------------------------------------------------------------

def test_S01_put_and_get(session_cache):
    kid, key = make_key("sid-001")
    session_cache.put("sid-001", kid, key)
    result = session_cache.get("sid-001")
    assert result is not None
    # get() restituisce uno StlsTransport ricreato dalla session_key
    from stls_core.stls_core_crypto import StlsTransport
    assert isinstance(result, StlsTransport)


# ---------------------------------------------------------------------------
# S02 — get miss
# ---------------------------------------------------------------------------

def test_S02_get_missing(session_cache):
    assert session_cache.get("non-esiste") is None


# ---------------------------------------------------------------------------
# S03 — LRU eviction
# ---------------------------------------------------------------------------

def test_S03_lru_eviction():
    cache = InMemorySessionCache(max_size=3, ttl_seconds=60)
    for sid in ["sid-1", "sid-2", "sid-3"]:
        cache.put(sid, *make_key(sid))

    # Accedo sid-1 per renderlo recently used
    cache.get("sid-1")

    # Inserisco sid-4: deve espellere sid-2 (LRU)
    cache.put("sid-4", *make_key("sid-4"))

    assert cache.get("sid-1") is not None  # recently used → sopravvive
    assert cache.get("sid-2") is None      # LRU → espulso
    assert cache.get("sid-3") is not None
    assert cache.get("sid-4") is not None


# ---------------------------------------------------------------------------
# S04 — TTL scaduto
# ---------------------------------------------------------------------------

def test_S04_ttl_expiry():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=1)
    cache.put("sid-ttl", *make_key("sid-ttl"))

    assert cache.get("sid-ttl") is not None
    time.sleep(1.1)
    assert cache.get("sid-ttl") is None


# ---------------------------------------------------------------------------
# S05 — remove
# ---------------------------------------------------------------------------

def test_S05_remove(session_cache):
    session_cache.put("sid-rm", *make_key("sid-rm"))
    assert session_cache.remove("sid-rm") is True
    assert session_cache.get("sid-rm") is None


# ---------------------------------------------------------------------------
# S06 — remove miss
# ---------------------------------------------------------------------------

def test_S06_remove_missing(session_cache):
    assert session_cache.remove("non-esiste") is False


# ---------------------------------------------------------------------------
# S07 — purge_expired
# ---------------------------------------------------------------------------

def test_S07_purge_expired():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=1)
    cache.put("sid-a", *make_key("sid-a"))
    cache.put("sid-b", *make_key("sid-b"))
    time.sleep(1.1)
    cache.put("sid-c", *make_key("sid-c"))  # sessione fresca

    removed = cache.purge_expired()
    assert removed == 2
    assert cache.get("sid-a") is None
    assert cache.get("sid-b") is None
    assert cache.get("sid-c") is not None


# ---------------------------------------------------------------------------
# S08 — __len__
# ---------------------------------------------------------------------------

def test_S08_len(session_cache):
    assert len(session_cache) == 0
    session_cache.put("sid-1", *make_key("sid-1"))
    assert len(session_cache) == 1
    session_cache.put("sid-2", *make_key("sid-2"))
    assert len(session_cache) == 2
    session_cache.remove("sid-1")
    assert len(session_cache) == 1


# ---------------------------------------------------------------------------
# S09 — __contains__
# ---------------------------------------------------------------------------

def test_S09_contains(session_cache):
    session_cache.put("sid-x", *make_key("sid-x"))
    assert "sid-x" in session_cache
    assert "sid-y" not in session_cache


# ---------------------------------------------------------------------------
# S10 — put aggiorna timestamp (non duplica)
# ---------------------------------------------------------------------------

def test_S10_put_updates_existing(session_cache):
    session_cache.put("sid-upd", *make_key("sid-upd"))
    session_cache.put("sid-upd", *make_key("sid-upd"))  # sovrascrive
    assert len(session_cache) == 1
    assert session_cache.get("sid-upd") is not None


# ---------------------------------------------------------------------------
# S11 — StlsSession.age_seconds
# ---------------------------------------------------------------------------

def test_S11_stls_session_age():
    from stls_core.stls_core_crypto import StlsTransport
    key = b"k" * 32
    t = StlsTransport(session_key=key, session_id="sid-age")
    session = StlsSession(session_id="sid-age", kid="kid-001", transport=t)
    assert session.age_seconds() >= 0.0
    time.sleep(0.1)
    assert session.age_seconds() >= 0.1