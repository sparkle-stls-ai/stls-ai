"""
stls_core_session.py
--------------------
Modello di sessione STLS e cache protocol-agnostic con backend intercambiabili.

Contenuto:
  - StlsSession        : dataclass che aggrega session_id, kid, StlsTransport
  - SessionCacheBase   : ABC per lo store delle sessioni attive
  - InMemorySessionCache : LRU+TTL in-process (default)
  - RedisSessionCache  : Redis-backed per deployment multi-istanza
  - SessionCacheFactory: seleziona il backend da SESSION_CACHE_BACKEND
  - SessionCache       : alias di InMemorySessionCache (backward compat)

Variabili d'ambiente:
  SESSION_CACHE_BACKEND         memory (default) | redis
  SESSION_CACHE_REDIS_URL       redis://localhost:6379
  SESSION_CACHE_REDIS_NAMESPACE stls:sc  (prefisso chiavi Redis)
  SESSION_CACHE_TTL             TTL idle in secondi (default 3600)
  STLS_SESSION_CACHE_MAX        massimo sessioni in cache memory (default 256)
  STLS_SESSION_TTL              alias di SESSION_CACHE_TTL per compatibilità

Struttura Redis:
  {prefix}:{session_id}  →  JSON {"session_key": "<base64>", "kid": "<kid>",
                                   "created_at": <timestamp>}
                             TTL = SESSION_CACHE_TTL (idle timeout, rinnovato su get)

  Il prefix default "stls:sc" separa nettamente le sessioni dalle entry
  del keystore ("stls:ks:...") — nessuna collisione anche su Redis condiviso
  tra client e server.

Backward compatibility:
  SessionCache = InMemorySessionCache  (alias)

Interfaccia pubblica (SessionCacheBase):
  put(session_id, kid, session_key)  → None
  get(session_id)                    → Optional[StlsTransport]
  remove(session_id)                 → bool
  purge_expired()                    → int
  __len__(), __contains__()
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .stls_core_crypto import StlsTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# StlsSession — record di una sessione attiva
# ---------------------------------------------------------------------------

@dataclass
class StlsSession:
    """
    Rappresenta una sessione STLS attiva.

    Attributes:
        session_id:    UUID univoco generato dal server durante l'handshake.
        kid:           Key identifier Arqit (o base). Utile per audit e
                       correlazione con le entry stls:ks:* in Redis.
        transport:     StlsTransport inizializzato con la session_key derivata.
        created_at:    Timestamp UTC di creazione.
        peer_identity: Identità del peer (opzionale, da KeyProvider).
    """

    session_id: str
    kid: str
    transport: StlsTransport
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    peer_identity: Optional[str] = None

    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()

    def __repr__(self) -> str:
        return (
            f"StlsSession(session_id={self.session_id!r}, "
            f"kid={self.kid!r}, "
            f"peer={self.peer_identity!r}, "
            f"age={self.age_seconds():.0f}s)"
        )


# ---------------------------------------------------------------------------
# SessionCacheBase — ABC
# ---------------------------------------------------------------------------

class SessionCacheBase(ABC):
    """
    Interfaccia comune per lo store delle sessioni STLS attive.

    Implementazioni: InMemorySessionCache, RedisSessionCache.
    Backend selezionato da SessionCacheFactory.build().

    Il metodo put() riceve i dati grezzi (session_id, kid, session_key)
    anziché un oggetto StlsTransport, per permettere la serializzazione
    su Redis. Internamente ogni implementazione costruisce il transport
    appropriato. get() restituisce sempre un StlsTransport pronto all'uso.
    """

    @abstractmethod
    def put(self, session_id: str, kid: str, session_key: bytes) -> None:
        """
        Registra una nuova sessione STLS dopo un handshake completato.

        Args:
            session_id:  UUID generato dal server (chiave di lookup).
            kid:         Key identifier Arqit — conservato per audit e
                         correlazione con stls:ks:* in Redis.
            session_key: Chiave grezza 32 bytes da cui StlsTransport deriva
                         le chiavi AES e HMAC via HKDF.
        """

    @abstractmethod
    def get(self, session_id: str) -> Optional[StlsTransport]:
        """
        Recupera il transport di una sessione attiva.

        Rinnova il TTL idle ad ogni accesso (sliding window).
        Restituisce None se la sessione non esiste o è scaduta.
        """

    @abstractmethod
    def remove(self, session_id: str) -> bool:
        """Rimuove esplicitamente una sessione. Restituisce True se esisteva."""

    @abstractmethod
    def purge_expired(self) -> int:
        """Rimuove le sessioni scadute. Restituisce il numero rimosso."""

    @abstractmethod
    def __len__(self) -> int: ...

    @abstractmethod
    def __contains__(self, session_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# InMemorySessionCache — LRU + TTL in-process
# ---------------------------------------------------------------------------

class InMemorySessionCache(SessionCacheBase):
    """
    Cache LRU+TTL in-process. Default per sviluppo e single-instance.

    Non sopravvive a restart del processo né a scale-out multi-istanza.
    Per Cloud Run multi-replica usare RedisSessionCache.

    Struttura interna:
      OrderedDict[session_id → (StlsTransport, kid, last_access_float)]
    """

    def __init__(
        self,
        max_size: Optional[int] = None,
        ttl_seconds: Optional[int] = None,
    ) -> None:
        self._max_size: int = max_size or int(
            os.getenv("STLS_SESSION_CACHE_MAX", "256")
        )
        self._ttl: int = ttl_seconds or int(
            os.getenv("SESSION_CACHE_TTL",
                os.getenv("STLS_SESSION_TTL", "3600"))
        )
        self._store: OrderedDict[str, tuple[StlsTransport, str, float]] = OrderedDict()
        logger.debug(
            "InMemorySessionCache inizializzata: max=%d ttl=%ds",
            self._max_size, self._ttl,
        )

    def put(self, session_id: str, kid: str, session_key: bytes) -> None:
        now = time.monotonic()
        transport = StlsTransport(session_key=session_key, session_id=session_id)

        if session_id in self._store:
            self._store.move_to_end(session_id)
            self._store[session_id] = (transport, kid, now)
            logger.debug("InMemorySessionCache.put: aggiornata %s", session_id)
            return

        if len(self._store) >= self._max_size:
            evicted_id, _ = self._store.popitem(last=False)
            logger.info(
                "InMemorySessionCache: evizione LRU %s (max=%d)",
                evicted_id, self._max_size,
            )

        self._store[session_id] = (transport, kid, now)
        self._store.move_to_end(session_id)
        logger.debug("InMemorySessionCache.put: nuova sessione %s kid=%s", session_id, kid)

    def get(self, session_id: str) -> Optional[StlsTransport]:
        entry = self._store.get(session_id)
        if entry is None:
            return None

        transport, kid, last_access = entry
        now = time.monotonic()

        if now - last_access > self._ttl:
            del self._store[session_id]
            logger.info(
                "InMemorySessionCache: sessione %s scaduta (TTL=%ds)",
                session_id, self._ttl,
            )
            return None

        # LRU refresh + sliding TTL
        self._store.move_to_end(session_id)
        self._store[session_id] = (transport, kid, now)
        return transport

    def remove(self, session_id: str) -> bool:
        if session_id in self._store:
            del self._store[session_id]
            logger.debug("InMemorySessionCache.remove: %s", session_id)
            return True
        return False

    def purge_expired(self) -> int:
        now = time.monotonic()
        expired = [
            sid for sid, (_, __, last) in self._store.items()
            if now - last > self._ttl
        ]
        for sid in expired:
            del self._store[sid]
        if expired:
            logger.info(
                "InMemorySessionCache.purge_expired: rimossi %d sessioni",
                len(expired),
            )
        return len(expired)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._store

    def __repr__(self) -> str:
        return (
            f"InMemorySessionCache(size={len(self._store)}, "
            f"max={self._max_size}, ttl={self._ttl}s)"
        )


# ---------------------------------------------------------------------------
# RedisSessionCache — backend Redis per deployment multi-istanza
# ---------------------------------------------------------------------------

class RedisSessionCache(SessionCacheBase):
    """
    Cache sessioni STLS su Redis. Per Cloud Run multi-replica e stateless.

    Struttura Redis:
      {prefix}:{session_id}  →  JSON string   TTL = ttl_seconds (idle timeout)

    Il JSON contiene:
      {
        "session_key": "<base64(32 bytes)>",  ← chiave grezza per ricostruzione
        "kid":         "<kid>",               ← audit e correlazione stls:ks:*
        "created_at":  <unix timestamp float>  ← metadato diagnostico
      }

    Il TTL è sliding (rinnovato su ogni get() tramite GETEX) — una sessione
    scade solo dopo SESSION_CACHE_TTL secondi di inattività.

    Nessuna collisione con il keystore (stls:ks:...) né con altre sessioni:
      - session_id è UUID4 (unicità garantita dal server)
      - prefix default "stls:sc" è distinto da "stls:ks"
      - Funziona correttamente anche su Redis condiviso tra client e server
        perché solo il server scrive in questo namespace.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        prefix: str = "stls:sc",
        ttl_seconds: int = 3600,
    ) -> None:
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise ImportError(
                "redis>=7.4.0 è necessario per SESSION_CACHE_BACKEND=redis. "
                "Installare con: pip install 'redis>=7.4.0'"
            ) from exc

        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=True)
        self._prefix = prefix
        self._ttl = ttl_seconds

        logger.info(
            "RedisSessionCache inizializzata: prefix=%s url=%s ttl=%ds",
            prefix, redis_url, ttl_seconds,
        )

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}:{session_id}"

    def put(self, session_id: str, kid: str, session_key: bytes) -> None:
        payload = json.dumps({
            "session_key": base64.b64encode(session_key).decode(),
            "kid":         kid,
            "created_at":  time.time(),
        })
        self._r.set(self._key(session_id), payload, ex=self._ttl)
        logger.debug(
            "RedisSessionCache.put: session_id=%s kid=%s ttl=%ds",
            session_id, kid, self._ttl,
        )

    def get(self, session_id: str) -> Optional[StlsTransport]:
        # GETEX: restituisce il valore E rinnova il TTL (idle timeout sliding)
        raw = self._r.getex(self._key(session_id), ex=self._ttl)
        if raw is None:
            logger.debug("RedisSessionCache.get: %s non trovata", session_id)
            return None

        try:
            data = json.loads(raw)
            session_key = base64.b64decode(data["session_key"])
            return StlsTransport(session_key=session_key, session_id=session_id)
        except Exception as exc:
            logger.error(
                "RedisSessionCache.get: errore deserializzazione session_id=%s: %s",
                session_id, exc,
            )
            return None

    def remove(self, session_id: str) -> bool:
        deleted = self._r.delete(self._key(session_id))
        if deleted:
            logger.debug("RedisSessionCache.remove: %s", session_id)
        return bool(deleted)

    def purge_expired(self) -> int:
        # Redis gestisce l'eviction automaticamente via TTL.
        # Questo metodo è no-op per compatibilità con l'interfaccia.
        return 0

    def __len__(self) -> int:
        # KEYS è O(N) — accettabile per diagnostica, non per hot path.
        return len(self._r.keys(f"{self._prefix}:*"))

    def __contains__(self, session_id: str) -> bool:
        return bool(self._r.exists(self._key(session_id)))

    def __repr__(self) -> str:
        return (
            f"RedisSessionCache(prefix={self._prefix!r}, ttl={self._ttl}s)"
        )


# ---------------------------------------------------------------------------
# SessionCacheFactory
# ---------------------------------------------------------------------------

class SessionCacheFactory:
    """
    Costruisce il backend SessionCache in base a SESSION_CACHE_BACKEND.

    Variabili d'ambiente:
      SESSION_CACHE_BACKEND         memory (default) | redis
      SESSION_CACHE_REDIS_URL       redis://localhost:6379
      SESSION_CACHE_REDIS_NAMESPACE stls:sc  (prefisso chiavi Redis)
      SESSION_CACHE_TTL             TTL idle in secondi (default 3600)
      STLS_SESSION_CACHE_MAX        max sessioni (solo memory, default 256)
    """

    @staticmethod
    def build() -> SessionCacheBase:
        """
        Crea l'istanza SessionCache appropriata.

        Returns:
            InMemorySessionCache se SESSION_CACHE_BACKEND=memory (default).
            RedisSessionCache    se SESSION_CACHE_BACKEND=redis.
        """
        backend = os.getenv("SESSION_CACHE_BACKEND", "memory").strip().lower()

        if backend == "redis":
            url    = os.getenv("SESSION_CACHE_REDIS_URL", "redis://localhost:6379")
            prefix = os.getenv("SESSION_CACHE_REDIS_NAMESPACE", "stls:sc")
            ttl    = int(os.getenv("SESSION_CACHE_TTL",
                         os.getenv("STLS_SESSION_TTL", "3600")))
            return RedisSessionCache(redis_url=url, prefix=prefix, ttl_seconds=ttl)

        if backend == "memory":
            return InMemorySessionCache()

        raise ValueError(
            f"SESSION_CACHE_BACKEND='{backend}' non riconosciuto. "
            "Valori supportati: 'memory' (default), 'redis'."
        )


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

# SessionCache = InMemorySessionCache per non rompere il codice esistente
# che fa `from stls_core import SessionCache` o `SessionCache()`.
SessionCache = InMemorySessionCache