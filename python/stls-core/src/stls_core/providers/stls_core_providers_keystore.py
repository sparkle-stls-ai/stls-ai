"""
stls_core_providers_keystore.py
--------------------------------
Interchangeable backends for the Arqit SKA-Platform™ key store.

Selected via environment variable:

    KEY_STORE_BACKEND=memory   (default) — in-process dict, suited for
                                          development and single-instance deployments.
    KEY_STORE_BACKEND=redis    — external Redis, suited for
                                          multi-instance deployments on Cloud Run / GKE.

Key lifecycle in the Redis backend
--------------------------------------------
A key goes through three phases:

  1. Pending (received, not yet delivered)
     stls:ks:{ns}:key:{kid}     → bytes (key_data)      TTL = KEY_PENDING_TTL
     stls:ks:{ns}:pending       → ZSET score=timestamp  (member = kid)

  2. Delivered (handed to a handshake, in use for an active session)
     stls:ks:{ns}:key:{kid}     → bytes (key_data)      TTL = KEY_SESSION_TTL (idle timeout)
     (removed from pending)

     The TTL is renewed on every get() (GETEX): KEY_SESSION_TTL is an
     idle timeout, not a fixed lifetime. The key expires only if it
     hasn't been accessed for KEY_SESSION_TTL consecutive seconds.

  3. Evicted (session expired)
     Redis evicts it automatically once the TTL expires — no explicit action needed.

The "pending" sorted set has no TTL of its own: expired entries (whose
corresponding stls:ks:{ns}:key:{kid} has already been evicted by Redis)
are passively cleaned up via ZRANGEBYSCORE at polling time —
wait_for_new_key() automatically filters out kids whose key_data is no
longer present in Redis.

Environment variables (Redis backend)
--------------------------------------
    KEY_STORE_REDIS_URL           redis://... (default: redis://localhost:6379)
    KEY_STORE_REDIS_NAMESPACE     logical prefix (default: "default")
    KEY_STORE_PENDING_TTL         pending key TTL in seconds (default: 300)
    KEY_STORE_SESSION_TTL         delivered key TTL in seconds (default: 3600)
    KEY_STORE_POLL_INTERVAL       wait_for_new_key polling interval (default: 0.3s)
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
    Common interface for the Arqit SKA-Platform™ key store.

    Concrete implementations: InMemoryArqitKeyStore, RedisArqitKeyStore.
    Selected via ArqitKeyStoreFactory.build().
    """

    @abstractmethod
    def add(self, key_id: str, key_bytes: bytes) -> None:
        """Adds a key just received from the SKA-Platform."""

    @abstractmethod
    def get(self, key_id: str) -> Optional[bytes]:
        """Returns the key's bytes given its key_id, or None if absent."""

    @abstractmethod
    def get_last(self) -> Optional[tuple[str, bytes]]:
        """Returns the last added key (key_id, key_bytes), or None."""

    @abstractmethod
    def startup_seen_keys(self) -> set[str]:
        """InMemory: no persistence — returns an empty set()."""
        return set()

    def wait_for_new_key(
        self, seen_key_ids: set[str], timeout: float = 30.0
    ) -> Optional[tuple[str, bytes]]:
        """
        Blocks until a key with a key_id NOT in seen_key_ids is available,
        or the timeout expires.

        Used by long-running server providers to obtain a fresh key
        for each new handshake round, avoiding the reuse of keys already
        delivered to previous sessions.
        """

    @abstractmethod
    def mark_delivered(self, key_id: str) -> None:
        """
        Marks a key as delivered (used in a handshake).

        The backend can use this signal to:
        - update the entry's TTL (Redis: from KEY_PENDING_TTL to KEY_SESSION_TTL)
        - remove the key from the pending queue
        """

    @abstractmethod
    def startup_seen_keys(self) -> set[str]:
        """
        Returns the set of kids to be considered "already seen" at the
        time the provider starts up.

        Used by KeySessionProviderArqitPy.__init__ to pre-populate
        _delivered_key_ids: if the server process restarts, Redis
        still holds the keys from previous sessions in the pending ZSET
        (no ZREM for the server). Without this pre-population, a
        restarted server would find stale keys in the ZSET (with a lower
        timestamp than fresh ones) and would use them — causing a
        mismatch with the client, which holds a new key from its own
        Arqit session.

        InMemoryArqitKeyStore: always returns set() (no persistence).
        RedisArqitKeyStore server role: returns all kids currently in
            the pending ZSET, so the server ignores the old keys
            and waits for fresh keys from the new Arqit peering.
        RedisArqitKeyStore client role: returns set() (per-instance UUID
            namespace, always empty at startup since the namespace is new).
        """


# ---------------------------------------------------------------------------
# InMemoryArqitKeyStore
# ---------------------------------------------------------------------------

class InMemoryArqitKeyStore(ArqitKeyStoreBase):
    """
    Thread-safe in-process store (original implementation, now with an ABC).

    Suited for development, testing, and single-instance deployment.
    Does not survive a process restart nor a multi-instance scale-out.
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
        # In-memory needs no TTL update — no-op.
        pass

    def startup_seen_keys(self) -> set[str]:
        """InMemory: no persistence — returns an empty set()."""
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
    Redis-backed store for multi-instance deployments (Cloud Run, GKE, …).

    Redis structures used (prefix = "stls:ks:{namespace}"):

      stls:ks:{ns}:key:{kid}   STRING  key_data (bytes)
                                        TTL = KEY_PENDING_TTL while pending,
                                        TTL = KEY_SESSION_TTL after mark_delivered()

      stls:ks:{ns}:pending     ZSET    score = UNIX timestamp (float)
                                        member = kid
                                        Used by wait_for_new_key() to find
                                        keys that arrived after seen_key_ids.
                                        Cleaned passively: a member with no
                                        corresponding :key:{kid} is ignored.

    Requires redis>=7.4.0 installed in the virtualenv.
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
            zrem_on_deliver: if True, mark_delivered() removes the kid from
                the pending ZSET after delivery (correct behavior for the
                client, where each process is independent and the key must
                not be reused by subsequent runs).
                If False (default for the server), the kid remains in the
                pending ZSET so that sibling server instances (Cloud Run
                multi-replica) can find it. Deduplication is guaranteed by
                the in-memory _delivered_key_ids on each instance.
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
        # GETEX: atomic — returns the value AND renews the TTL if the key
        # exists. KEY_STORE_SESSION_TTL thus becomes an idle timeout (the key
        # expires only if unused for TTL consecutive seconds) rather than a
        # fixed lifetime — the correct semantics for active cryptographic sessions.
        val = self._r.getex(self._key(key_id), ex=self._session_ttl)
        return bytes(val) if val is not None else None

    def get_last(self) -> Optional[tuple[str, bytes]]:
        """Returns the most recent pending key (highest score in the ZSET).

        Scans from most recent to least recent, passively removing
        expired entries (whose stls:ks:{ns}:key:{kid} is no longer present
        in Redis) until it finds a valid one.

        Uses self.get() internally, which applies GETEX and renews the TTL.
        """
        # ZREVRANGE returns all members in descending score order.
        # Scanned from the most recent to find the first still-valid entry,
        # passively removing expired ones along the way.
        members = self._r.zrevrange(self._pending(), 0, -1)
        for raw in members:
            kid = raw.decode() if isinstance(raw, bytes) else raw
            key_data = self.get(kid)  # GETEX: automatically renews the TTL
            if key_data is None:
                # expired pending entry: passive cleanup
                self._r.zrem(self._pending(), kid)
                continue
            return kid, key_data
        return None

    def mark_delivered(self, key_id: str) -> None:
        """
        Updates the key's TTL from pending_ttl to session_ttl.

        If zrem_on_deliver=True (client): removes the kid from the pending
        ZSET so that subsequent client runs don't find already-used keys.

        If zrem_on_deliver=False (server, default): keeps the kid in the
        pending ZSET so that sibling server instances (Cloud Run
        multi-replica) can find it. Deduplication is guaranteed by
        _delivered_key_ids in memory on each instance.

        In both cases the TTL of key:{kid} is extended to session_ttl
        to cover the duration of the active session.
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
        """
        Reads the pending ZSET and returns all kids currently present.

        Called by KeySessionProviderArqitPy.__init__ (server role) to
        pre-populate _delivered_key_ids on process restart: the server
        will ignore the stale keys left in Redis from previous sessions
        and wait for fresh keys from the new Arqit peering.

        For the client role this method is never called (each instance
        has a unique UUID namespace, always empty at startup).
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
        Polls Redis until a pending key with a kid NOT in seen_key_ids
        becomes available, or the timeout expires.

        Uses ZRANGEBYSCORE to get only the kids that arrived after
        the last handshake (score > timestamp of the last known kid),
        then filters out those already present in seen_key_ids.
        """
        deadline = _time.monotonic() + timeout

        # Reference score = timestamp of the most recent already-seen kid,
        # or 0 if seen_key_ids is empty (accepts any key).
        min_score = "-inf"

        while _time.monotonic() < deadline:
            # Reads all pending kids with score > min_score
            members = self._r.zrangebyscore(
                self._pending(), min_score, "+inf", withscores=False
            )
            for raw in members:
                kid = raw.decode() if isinstance(raw, bytes) else raw
                if kid in seen_key_ids:
                    continue
                key_data = self.get(kid)
                if key_data is None:
                    # key expired while pending — passive cleanup
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
    Builds the appropriate backend based on KEY_STORE_BACKEND.

    Usage:
        store = ArqitKeyStoreFactory.build()        # per-session (Option A)
        store = ArqitKeyStoreFactory.build(ns="srv") # per-process with a namespace
    """

    @staticmethod
    def build(
        namespace: Optional[str] = None,
        role: str = "server",
        instance_id: Optional[str] = None,
    ) -> ArqitKeyStoreBase:
        """
        Builds the appropriate backend based on KEY_STORE_BACKEND.

        Args:
            namespace: Explicit Redis namespace. If None, read from
                KEY_STORE_REDIS_NAMESPACE (default: "default").
            role: "server" (default) or "client". Determines:
                - the Redis sub-namespace and the ZREM behavior.
                - Server: {ns}:server — no ZREM, shared across replicas.
                - Client: {ns}:client:{instance_id} — isolated namespace per
                  instance, avoiding collisions between concurrent client sessions.
            instance_id: Unique UUID per client instance (generated by
                KeyProviderClientArqitPy). Ignored for the server role.
                If None and role=client, one is generated automatically.
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
                # Per-instance isolated namespace: each concurrent client
                # session has its own pending ZSET → impossible to read
                # another's kid.
                iid = instance_id or str(_uuid.uuid4())
                ns  = f"{base_ns}:client:{iid}"
                zrem = True   # cleanup after delivery (independent process)
            else:
                # Server: namespace shared across Cloud Run replicas.
                ns   = f"{base_ns}:server"
                zrem = False  # no ZREM: replicas must find the same kid

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
