"""
stls_core_providers_keystore.py
--------------------------------
Backend intercambiabili per lo store delle chiavi Arqit SKA-Platform™.

Selezione tramite variabile d'ambiente:

    KEY_STORE_BACKEND=memory   (default) — dict in-process, adatto a
                                          sviluppo e deployment single-instance.
    KEY_STORE_BACKEND=redis    — Redis esterno, adatto a deployment
                                          multi-istanza su Cloud Run / GKE.

Ciclo di vita delle chiavi nel backend Redis
--------------------------------------------
Una chiave attraversa tre fasi:

  1. Pending (ricevuta, non ancora consegnata)
     stls:ks:{ns}:key:{kid}     → bytes (key_data)      TTL = KEY_PENDING_TTL
     stls:ks:{ns}:pending       → ZSET score=timestamp  (member = kid)

  2. Delivered (consegnata a un handshake, in uso per una sessione attiva)
     stls:ks:{ns}:key:{kid}     → bytes (key_data)      TTL = KEY_SESSION_TTL (idle timeout)
     (rimossa da pending)

     Il TTL viene rinnovato ad ogni get() (GETEX): KEY_SESSION_TTL è un
     idle timeout, non un lifetime fisso. La chiave scade solo se non
     acceduta per KEY_SESSION_TTL secondi consecutivi.

  3. Evicted (sessione scaduta)
     Redis evict automaticamente alla scadenza del TTL — nessuna azione esplicita.

Il sorted set "pending" non ha TTL proprio: le entry scadute (il cui
corrispondente stls:ks:{ns}:key:{kid} è già stato evicted da Redis) vengono
pulite passivamente tramite ZRANGEBYSCORE al momento del polling —
wait_for_new_key() filtra automaticamente i kid la cui key_data non è più
presente in Redis.

Variabili d'ambiente (backend Redis)
--------------------------------------
    KEY_STORE_REDIS_URL           redis://... (default: redis://localhost:6379)
    KEY_STORE_REDIS_NAMESPACE     prefisso logico (default: "default")
    KEY_STORE_PENDING_TTL         TTL chiave pending  in secondi (default: 300)
    KEY_STORE_SESSION_TTL         TTL chiave consegnata in secondi (default: 3600)
    KEY_STORE_POLL_INTERVAL       intervallo polling wait_for_new_key (default: 0.3s)
"""

from __future__ import annotations

import logging
import os
import threading
import time as _time
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class ArqitKeyStoreBase(ABC):
    """
    Interfaccia comune per lo store delle chiavi Arqit SKA-Platform™.

    Implementazioni concrete: InMemoryArqitKeyStore, RedisArqitKeyStore.
    Selezionate tramite ArqitKeyStoreFactory.build().
    """

    @abstractmethod
    def add(self, key_id: str, key_bytes: bytes) -> None:
        """Aggiunge una chiave appena ricevuta dalla SKA-Platform."""

    @abstractmethod
    def get(self, key_id: str) -> Optional[bytes]:
        """Restituisce i bytes della chiave dato il suo key_id, o None se assente."""

    @abstractmethod
    def get_last(self) -> Optional[tuple[str, bytes]]:
        """Restituisce l'ultima chiave aggiunta (key_id, key_bytes), o None."""

    @abstractmethod
    def startup_seen_keys(self) -> set[str]:
        """InMemory: nessuna persistenza — restituisce set() vuoto."""
        return set()

    def wait_for_new_key(
        self, seen_key_ids: set[str], timeout: float = 30.0
    ) -> Optional[tuple[str, bytes]]:
        """
        Blocca finché una chiave con key_id NON in seen_key_ids è disponibile,
        oppure scade il timeout.

        Usato da provider server long-running per ottenere una chiave fresca
        per ogni nuovo round di handshake, evitando il riuso di chiavi già
        consegnate a sessioni precedenti.
        """

    @abstractmethod
    def mark_delivered(self, key_id: str) -> None:
        """
        Segna una chiave come consegnata (usata in un handshake).

        Il backend può usare questo segnale per:
        - aggiornare il TTL dell'entry (Redis: da KEY_PENDING_TTL a KEY_SESSION_TTL)
        - rimuovere la chiave dalla coda pending
        """

    @abstractmethod
    def startup_seen_keys(self) -> set[str]:
        """
        Restituisce il set di kid da considerare "già visti" al momento
        dell'avvio del provider.

        Usato da KeySessionProviderArqitPy.__init__ per pre-popolare
        _delivered_key_ids: in caso di riavvio del processo server, Redis
        conserva le chiavi delle sessioni precedenti nel ZSET pending
        (nessun ZREM per il server). Senza questa pre-popolazione, un server
        riavviato troverebbe chiavi obsolete nel ZSET (con timestamp più basso
        di quelle fresche) e le userebbe — causando un mismatch con il client
        che ha una chiave nuova dalla propria sessione Arqit.

        InMemoryArqitKeyStore: restituisce sempre set() (nessuna persistenza).
        RedisArqitKeyStore server role: restituisce tutti i kid attualmente
            nel ZSET pending, in modo che il server ignori le chiavi precedenti
            e attenda chiavi fresche dal nuovo peering Arqit.
        RedisArqitKeyStore client role: restituisce set() (namespace UUID per
            istanza, sempre vuoto all'avvio perché il namespace è nuovo).
        """


# ---------------------------------------------------------------------------
# InMemoryArqitKeyStore
# ---------------------------------------------------------------------------

class InMemoryArqitKeyStore(ArqitKeyStoreBase):
    """
    Store in-process thread-safe (implementazione originale, ora con ABC).

    Adatto per sviluppo, test, e deployment single-instance.
    Non sopravvive a restart del processo né a scale-out multi-istanza.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, bytes] = {}
        self._last_key_id: Optional[str] = None

    def add(self, key_id: str, key_bytes: bytes) -> None:
        with self._lock:
            self._keys[key_id] = key_bytes
            self._last_key_id = key_id
            logger.debug(
                "[InMemoryKeyStore] chiave aggiunta: id=%s len=%d",
                key_id, len(key_bytes),
            )

    def get(self, key_id: str) -> Optional[bytes]:
        with self._lock:
            return self._keys.get(key_id)

    def get_last(self) -> Optional[tuple[str, bytes]]:
        with self._lock:
            if self._last_key_id is None:
                return None
            return self._last_key_id, self._keys[self._last_key_id]

    def mark_delivered(self, key_id: str) -> None:
        # In memory non serve aggiornare TTL — no-op.
        pass

    def startup_seen_keys(self) -> set[str]:
        """InMemory: nessuna persistenza — restituisce set() vuoto."""
        return set()

    def wait_for_new_key(
        self, seen_key_ids: set[str], timeout: float = 30.0
    ) -> Optional[tuple[str, bytes]]:
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            with self._lock:
                if (
                    self._last_key_id is not None
                    and self._last_key_id not in seen_key_ids
                ):
                    return self._last_key_id, self._keys[self._last_key_id]
            _time.sleep(0.2)
        logger.warning(
            "[InMemoryKeyStore] timeout (%ss) in attesa di una nuova chiave "
            "(escluse %d già viste)", timeout, len(seen_key_ids),
        )
        return None


# ---------------------------------------------------------------------------
# RedisArqitKeyStore
# ---------------------------------------------------------------------------

class RedisArqitKeyStore(ArqitKeyStoreBase):
    """
    Store Redis-backed per deployment multi-istanza (Cloud Run, GKE, …).

    Strutture Redis utilizzate (prefisso = "stls:ks:{namespace}"):

      stls:ks:{ns}:key:{kid}   STRING  key_data (bytes)
                                        TTL = KEY_PENDING_TTL mentre pending,
                                        TTL = KEY_SESSION_TTL dopo mark_delivered()

      stls:ks:{ns}:pending     ZSET    score = timestamp UNIX (float)
                                        member = kid
                                        Usato da wait_for_new_key() per trovare
                                        chiavi arrivate dopo seen_key_ids.
                                        Pulito passivamente: member senza
                                        corrispondente :key:{kid} viene ignorato.

    Richiede redis>=7.4.0 installato nel virtualenv.
    """

    _PREFIX_KEY     = "stls:ks:{ns}:key:{kid}"
    _PREFIX_PENDING = "stls:ks:{ns}:pending"

    def __init__(
        self,
        redis_url: str,
        namespace: str = "default",
        pending_ttl: int = 300,
        session_ttl: int = 3600,
        poll_interval: float = 0.3,
        zrem_on_deliver: bool = False,
    ) -> None:
        """
        Args:
            zrem_on_deliver: se True, mark_delivered() rimuove il kid dal ZSET
                pending dopo la consegna (comportamento corretto per client,
                dove ogni processo è indipendente e la chiave non deve essere
                riutilizzata da run successivi).
                Se False (default per server), il kid rimane nel ZSET pending
                in modo che istanze server sorelle (Cloud Run multi-replica)
                possano trovarlo. La deduplicazione è garantita da
                _delivered_key_ids in memoria per ogni istanza.
        """
        try:
            import redis as redis_lib
        except ImportError as exc:
            raise ImportError(
                "redis>=7.4.0 è necessario per KEY_STORE_BACKEND=redis. "
                "Installare con: pip install 'redis>=7.4.0'"
            ) from exc

        self._r = redis_lib.Redis.from_url(redis_url, decode_responses=False)
        self._ns = namespace
        self._pending_ttl = pending_ttl
        self._session_ttl = session_ttl
        self._poll_interval = poll_interval
        self._zrem_on_deliver = zrem_on_deliver

        logger.info(
            "[RedisKeyStore] ns=%s url=%s pending_ttl=%ds session_ttl=%ds zrem_on_deliver=%s",
            namespace, redis_url, pending_ttl, session_ttl, zrem_on_deliver,
        )

    # -- helpers ----------------------------------------------------------------

    def _key(self, kid: str) -> str:
        return self._PREFIX_KEY.format(ns=self._ns, kid=kid)

    def _pending(self) -> str:
        return self._PREFIX_PENDING.format(ns=self._ns)

    # -- ABC implementation -----------------------------------------------------

    def add(self, key_id: str, key_bytes: bytes) -> None:
        now = _time.time()
        pipe = self._r.pipeline()
        pipe.set(self._key(key_id), key_bytes, ex=self._pending_ttl)
        pipe.zadd(self._pending(), {key_id: now})
        pipe.execute()
        logger.debug(
            "[RedisKeyStore] chiave aggiunta: id=%s len=%d pending_ttl=%ds",
            key_id, len(key_bytes), self._pending_ttl,
        )

    def get(self, key_id: str) -> Optional[bytes]:
        # GETEX: atomico — restituisce il valore E rinnova il TTL se la chiave
        # esiste. KEY_STORE_SESSION_TTL diventa così un idle timeout (la chiave
        # scade solo se non usata per TTL secondi consecutivi) anziché un
        # lifetime fisso — semantica corretta per sessioni crittografiche attive.
        val = self._r.getex(self._key(key_id), ex=self._session_ttl)
        return bytes(val) if val is not None else None

    def get_last(self) -> Optional[tuple[str, bytes]]:
        """Restituisce la chiave pending più recente (score massimo nel ZSET).

        Scansiona dal più recente al meno recente, rimuovendo passivamente le
        entry scadute (il cui stls:ks:{ns}:key:{kid} non è più presente in Redis)
        finché non ne trova una valida.

        Usa self.get() internamente, che applica GETEX e rinnova il TTL.
        """
        # ZREVRANGE restituisce tutti i member in ordine decrescente di score.
        # Si scansiona dal più recente per trovare la prima entry ancora valida,
        # rimuovendo passivamente quelle scadute lungo il percorso.
        members = self._r.zrevrange(self._pending(), 0, -1)
        for raw in members:
            kid = raw.decode() if isinstance(raw, bytes) else raw
            key_data = self.get(kid)  # GETEX: rinnova TTL automaticamente
            if key_data is None:
                # entry pending scaduta: pulizia passiva
                self._r.zrem(self._pending(), kid)
                continue
            return kid, key_data
        return None

    def mark_delivered(self, key_id: str) -> None:
        """
        Aggiorna il TTL della chiave da pending_ttl a session_ttl.

        Se zrem_on_deliver=True (client): rimuove il kid dal ZSET pending
        in modo che run successivi del client non trovino chiavi già usate.

        Se zrem_on_deliver=False (server, default): mantiene il kid nel ZSET
        pending in modo che istanze server sorelle (Cloud Run multi-replica)
        possano trovarlo. La deduplicazione è garantita da _delivered_key_ids
        in memoria per ogni istanza.

        In entrambi i casi il TTL della key:{kid} viene esteso a session_ttl
        per coprire la durata della sessione attiva.
        """
        pipe = self._r.pipeline()
        pipe.expire(self._key(key_id), self._session_ttl)
        if self._zrem_on_deliver:
            pipe.zrem(self._pending(), key_id)
        pipe.execute()
        logger.debug(
            "[RedisKeyStore] chiave consegnata: id=%s session_ttl=%ds zrem=%s",
            key_id, self._session_ttl, self._zrem_on_deliver,
        )

    def startup_seen_keys(self) -> set[str]:
        """InMemory: nessuna persistenza — restituisce set() vuoto."""
        return set()

    def startup_seen_keys(self) -> set[str]:
        """
        Legge il ZSET pending e restituisce tutti i kid attualmente presenti.

        Chiamato da KeySessionProviderArqitPy.__init__ (server role) per
        pre-popolare _delivered_key_ids al riavvio del processo: il server
        ignorerà le chiavi obsolete rimaste in Redis da sessioni precedenti
        e aspetterà chiavi fresche dal nuovo peering Arqit.

        Per il client role questo metodo non viene chiamato (ogni istanza ha
        un namespace UUID unico, sempre vuoto all'avvio).
        """
        try:
            members = self._r.zrange(self._pending(), 0, -1)
            keys = set()
            for raw in members:
                kid = raw.decode() if isinstance(raw, bytes) else raw
                keys.add(kid)
            if keys:
                logger.info(
                    "[RedisKeyStore] startup_seen_keys: %d chiavi obsolete trovate "
                    "nel ZSET pending — saranno ignorate dal provider server riavviato.",
                    len(keys),
                )
            return keys
        except Exception as exc:
            logger.warning(
                "[RedisKeyStore] startup_seen_keys: errore lettura ZSET: %s — "
                "il provider inizia con seen_key_ids={}. Potrebbero essere usate "
                "chiavi obsolete.", exc,
            )
            return set()

    def wait_for_new_key(
        self, seen_key_ids: set[str], timeout: float = 30.0
    ) -> Optional[tuple[str, bytes]]:
        """
        Polling su Redis finché una chiave pending con kid NON in seen_key_ids
        è disponibile, oppure scade il timeout.

        Usa ZRANGEBYSCORE per ottenere solo i kid arrivati dopo
        l'ultimo handshake (score > timestamp dell'ultimo kid noto),
        poi filtra quelli già presenti in seen_key_ids.
        """
        deadline = _time.monotonic() + timeout

        # score di riferimento = timestamp del kid più recente già visto,
        # oppure 0 se seen_key_ids è vuoto (accetta qualunque chiave).
        min_score = "-inf"

        while _time.monotonic() < deadline:
            # Legge tutti i kid pending con score > min_score
            members = self._r.zrangebyscore(
                self._pending(), min_score, "+inf", withscores=False
            )
            for raw in members:
                kid = raw.decode() if isinstance(raw, bytes) else raw
                if kid in seen_key_ids:
                    continue
                key_data = self.get(kid)
                if key_data is None:
                    # chiave scaduta in pending — pulizia passiva
                    self._r.zrem(self._pending(), kid)
                    continue
                return kid, key_data

            _time.sleep(self._poll_interval)

        logger.warning(
            "[RedisKeyStore] timeout (%ss) in attesa di una nuova chiave "
            "(escluse %d già viste)", timeout, len(seen_key_ids),
        )
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

class ArqitKeyStoreFactory:
    """
    Costruisce il backend appropriato in base a KEY_STORE_BACKEND.

    Uso:
        store = ArqitKeyStoreFactory.build()        # per-sessione (Option A)
        store = ArqitKeyStoreFactory.build(ns="srv") # per-processo con namespace
    """

    @staticmethod
    def build(
        namespace: Optional[str] = None,
        role: str = "server",
        instance_id: Optional[str] = None,
    ) -> ArqitKeyStoreBase:
        """
        Costruisce il backend appropriato in base a KEY_STORE_BACKEND.

        Args:
            namespace: namespace Redis esplicito. Se None, letto da
                KEY_STORE_REDIS_NAMESPACE (default: "default").
            role: "server" (default) o "client". Determina:
                - sub-namespace Redis e comportamento ZREM.
                - Server: {ns}:server — nessun ZREM, condiviso tra repliche.
                - Client: {ns}:client:{instance_id} — namespace isolato per
                  istanza, evita collisioni tra sessioni client concorrenti.
            instance_id: UUID univoco per istanza client (generato da
                KeyProviderClientArqitPy). Ignorato per il ruolo server.
                Se None e role=client, viene generato automaticamente.
        """
        backend = os.getenv("KEY_STORE_BACKEND", "memory").strip().lower()

        if backend == "redis":
            import uuid as _uuid
            url         = os.getenv("KEY_STORE_REDIS_URL", "redis://localhost:6379")
            base_ns     = namespace or os.getenv("KEY_STORE_REDIS_NAMESPACE", "default")
            pending_ttl = int(os.getenv("KEY_STORE_PENDING_TTL", "300"))
            session_ttl = int(os.getenv("KEY_STORE_SESSION_TTL", "3600"))
            poll        = float(os.getenv("KEY_STORE_POLL_INTERVAL", "0.3"))

            if role == "client":
                # Namespace isolato per istanza: ogni sessione client concorrente
                # ha il proprio ZSET pending → impossibile leggere kid altrui.
                iid = instance_id or str(_uuid.uuid4())
                ns  = f"{base_ns}:client:{iid}"
                zrem = True   # pulizia dopo consegna (processo indipendente)
            else:
                # Server: namespace condiviso tra repliche Cloud Run.
                ns   = f"{base_ns}:server"
                zrem = False  # no ZREM: le repliche devono trovare lo stesso kid

            return RedisArqitKeyStore(
                redis_url=url,
                namespace=ns,
                pending_ttl=pending_ttl,
                session_ttl=session_ttl,
                poll_interval=poll,
                zrem_on_deliver=zrem,
            )

        if backend == "memory":
            return InMemoryArqitKeyStore()

        raise ValueError(
            f"KEY_STORE_BACKEND='{backend}' non riconosciuto. "
            "Valori supportati: 'memory' (default), 'redis'."
        )