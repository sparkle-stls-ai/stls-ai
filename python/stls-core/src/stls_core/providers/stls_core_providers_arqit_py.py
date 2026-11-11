"""
stls_core_providers_arqit_py.py
--------------------------------
Arqit SKA-Platform™ provider via the native Python SDK (uk.arqit.device.*).

No JVM, no JPype. Uses the Arqit wheels installed via:
    uv pip install -e ".[arqit_py]" --find-links arqit_wheels/...

Environment variables:
    ARQIT_PY_CLIENT_CONFIG  → path to the client (initiator) JSON config
    ARQIT_PY_SERVER_CONFIG  → path to the server (receiver) JSON config
    ARQIT_PY_TRANSPORT      → "socket" (default) | "mqtt"

create_provider_client()/create_provider_server() also accept an
options["config_path"] override, taking priority over the env var above
when present — lets a single process hold several KeyProviderArqitPy
clients against different device configs (see SecureSessionManager's
arqit_client_configs= in stls_a2a_secure_remote.py), instead of being
limited to the one config the whole process's environment points at.

1:1 architecture (Option A):
    Each STLS session has its own KeyProviderClient/Server and its own
    ArqitKeyStoreBase. create_provider_client() is called once per
    session — the kid ↔ session_key binding is exclusive to that session.

Arqit peering runs in separate daemon threads so it doesn't block
the asyncio event loop of the FastAPI/Starlette server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as _time
from typing import Optional

from .stls_core_providers_base import (
    BlockableResult,
    KeyProvider,
    KeyProviderClient,
    KeyProviderServer,
    KeySessionProvider,
)
from .stls_core_providers_keystore import (
    ArqitKeyStoreBase,
    ArqitKeyStoreFactory,
    InMemoryArqitKeyStore,
    RedisArqitKeyStore,
)

logger = logging.getLogger(__name__)

# Arqit SDK's DeviceMetadata is a true process-wide singleton
# (uk.arqit.device.common.device_metadata.DeviceMetadata.__instance),
# and AuthenticationClient.__mutex (authentication.py) is a *class*-
# level lock shared by every AuthenticationClient instance in the
# process — neither is protected against two start() calls overlapping
# from different OS threads, and the SDK's own critical section around
# DeviceMetadata.load() has no try/finally, so an unrelated exception
# there leaks that class-level mutex for the rest of the process,
# wedging every later start() call forever.
#
# Confirmed by direct reproduction against the real Arqit cloud
# (2026-10-23, throwaway devices, 3-way asyncio.to_thread(provider.start)
# race): with no lock at all, two of three concurrent start() calls
# deadlocked forever inside the SDK's own authenticate_device(). With
# only authentication serialized (an earlier version of this lock that
# released right after spawning the negotiation thread), authentication
# itself stopped deadlocking, but letting a second session's
# authentication begin while a first session's negotiation was still
# connecting made the SDK's socket bilocation handshake fail outright
# (ConnectionRefusedError / PeerUnableToConnectException) for every
# session involved — the race isn't confined to authenticate_device(),
# it extends through the whole negotiation. This lock therefore
# serializes the *entire* start() process-wide (across ALL devices, not
# just same-device, since DeviceMetadata is a single global singleton
# regardless of which device's config is passed) — client start() holds
# it through the full negotiation (joins the negotiation thread before
# releasing, see KeyProviderClientArqitPy.start()), not just through
# authentication — until the SDK itself is fixed.
_ARQIT_PY_START_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# _ArqitPyKeyNotifier — Arqit SDK callback
# ---------------------------------------------------------------------------

class _ArqitPyKeyNotifier:
    """
    Implements the Arqit Python SDK's KeyNotifierBase.

    Receives the keys negotiated during peering and saves them
    into the shared store. If receiver, also adds them to the
    SDK's KeyManager for internal management.
    """

    def __init__(
        self,
        key_store: ArqitKeyStoreBase,
        is_receiver: bool,
        sdk_key_manager: Optional[object] = None,
    ) -> None:
        self._key_store = key_store
        self._is_receiver = is_receiver
        self._sdk_key_manager = sdk_key_manager
        self._last_typed_key = None

    def notify_key(self, key: object) -> None:
        """Callback invoked by the SDK for every negotiated key."""
        from uk.arqit.device.peering.key_manager import KeyType

        logger.info(
            "[ArqitPyKeyNotifier] type=%s id=%s peer=%s ttl=%s",
            key.key_type, key.key_id, key.peer_duid, key.ttl,
        )

        if key.key_type is KeyType.BILOCATION:
            self._last_typed_key = key
            self._key_store.add(str(key.key_id), bytes(key.key))
            if self._is_receiver and self._sdk_key_manager is not None:
                self._sdk_key_manager.add_key(key)


# ---------------------------------------------------------------------------
# KeySessionProviderArqitPy — KeySessionProvider interface for ArqitPy
# ---------------------------------------------------------------------------

class KeySessionProviderArqitPy(KeySessionProvider):
    """
    KeySessionProvider that delegates to the key store populated by Arqit peering.

    getSessionKeyIdentifier() waits for a key from peering (async, with a
    30s timeout). On the CLIENT side, each instance typically serves a
    single session, and the first available key is the correct one. On
    the SERVER side (long-running, see
    KeyProviderServerArqitPy.createProviderSession, which always returns
    the same instance for the entire life of the process), the provider
    must instead be able to serve multiple handshakes over time: each
    call waits for a key NOT YET DELIVERED in a previous call, so it
    never reuses the first handshake's kid forever once peering has
    negotiated new keys in the meantime.
    getSessionKeyData() reads from the store by key_id.
    """

    def __init__(
        self,
        session_type: KeySessionProvider.KeySessionProviderType,
        key_store: ArqitKeyStoreBase,
        peer_identity: Optional[str] = None,
    ) -> None:
        self._session_type = session_type
        self._key_store = key_store
        self._peer_identity = peer_identity
        # Set ONLY by an explicit call to setSessionKeyIdentifier()
        # (used on the client side to pin a session known in advance).
        # getSessionKeyIdentifier(), when auto-resolving, does NOT set
        # it: doing so would pin it forever for subsequent calls too,
        # which is exactly the bug this fix resolves.
        self._pinned_key_identifier: Optional[str] = None
        # All kids ever returned by getSessionKeyIdentifier() over the
        # lifetime of this instance — used only on the SERVER side to
        # avoid re-delivering the same kid to a new handshake (see the
        # class docstring). On the CLIENT side this typically holds a
        # single element: a single client session resolves only once.
        # Pre-populated with startup_seen_keys(): if the server process
        # restarts with KEY_STORE_BACKEND=redis, the ZSET still holds
        # keys from previous sessions. Without pre-population the
        # server would use them for new handshakes → mismatch with the
        # client.
        self._delivered_key_ids: set[str] = set(key_store.startup_seen_keys())
        # Guards the read-wait-then-mutate sequence in
        # getSessionKeyIdentifier() below: on the SERVER side, this one
        # instance is shared by every incoming handshake for the whole
        # process lifetime (see the class docstring), so concurrent
        # handshakes call getSessionKeyIdentifier() concurrently on the
        # SAME instance. Without this lock, two concurrent calls can
        # both read _delivered_key_ids before either adds its own
        # result, racing on which kid each one claims — confirmed by
        # direct reproduction (2026-10-23, 3 concurrent MCP handshakes
        # against one long-running server): "Invalid proof of
        # possession" from a client ending up matched against a
        # different client's key server-side.
        self._key_lock = asyncio.Lock()
        # Last kid auto-resolved by getSessionKeyIdentifier()
        # (not pinned) — used ONLY as a fallback in getSessionKeyData()
        # when the direct lookup fails, for backward compatibility with
        # stls-a2a (its lazy flow, pre-explicit-handshake): there, the
        # middleware calls getSessionKeyData(session_id) passing the
        # STLS session_id (a client-generated UUID) instead of the
        # Arqit kid, because at that point in the flow it doesn't yet
        # know the kid — see stls-a2a's CHANGELOG 0.1.1, the fix for
        # "KeySessionProviderArqitPy.getSessionKeyData() wrong lookup".
        self._last_resolved_key_identifier: Optional[str] = None

    def getSessionType(self) -> KeySessionProvider.KeySessionProviderType:
        return self._session_type

    async def getSessionKeyIdentifier(self, hint_kid: Optional[str] = None) -> str:
        """
        Waits for a key from peering and returns the kid.

        If setSessionKeyIdentifier() has already been called explicitly
        (client-side use, to pin a session known in advance), it is used
        with no waiting — callable repeatedly, always returning the same
        pinned value. Otherwise, if hint_kid is given (the caller
        already knows which exact kid it wants — see perform_handshake()
        passing the client's own request.kid), waits for THAT SPECIFIC
        kid to become available and returns it directly. Without a
        hint, waits for a key NOT previously delivered by this same
        instance (plain FIFO) — essential for long-running SERVER
        providers that serve multiple handshakes over time (see
        stls-mcp: a single instance serves the entire lifetime of the
        uvicorn process, one call per incoming handshake), but NOT
        sufficient on its own once more than one handshake can be
        in flight concurrently against the same long-running server:
        FIFO delivery order has no guaranteed relationship to the
        order concurrent handshake requests actually arrive in, so two
        concurrent callers can each get handed the OTHER one's key.
        Confirmed by direct reproduction (2026-10-23, 3 concurrent MCP
        sessions, same device, same server): one handshake completed
        server-side with a stale/wrong kid, and the genuinely
        corresponding client failed with "Invalid proof of possession"
        — not a locking bug (that race is separately closed by
        _key_lock below), a protocol-level ordering assumption that
        plain FIFO cannot satisfy under real concurrency. hint_kid
        closes this because the kid itself unambiguously names an
        already-delivered key, independent of arrival order.
        """
        if self._pinned_key_identifier is not None:
            return self._pinned_key_identifier

        if hint_kid is not None:
            async with self._key_lock:
                loop = asyncio.get_event_loop()

                def _wait_for_hint() -> Optional[bytes]:
                    deadline = _time.monotonic() + 30.0
                    while _time.monotonic() < deadline:
                        data = self._key_store.get(hint_kid)
                        if data is not None:
                            return data
                        _time.sleep(0.3)
                    return None

                key_data = await loop.run_in_executor(None, _wait_for_hint)
                if key_data is None:
                    raise TimeoutError(
                        f"[KeySessionProviderArqitPy] hint_kid={hint_kid!r} "
                        "never became available within 30s"
                    )
                self._delivered_key_ids.add(hint_kid)
            self._last_resolved_key_identifier = hint_kid
            self._key_store.mark_delivered(hint_kid)
            logger.info(
                "[KeySessionProviderArqitPy] session_key_identifier=%s "
                "(via hint_kid, total delivered=%d)",
                hint_kid, len(self._delivered_key_ids),
            )
            return hint_kid

        # See _key_lock's own comment: serializes the whole
        # wait-then-claim sequence per instance, so concurrent
        # handshakes on a shared SERVER-role instance can't race on
        # which kid each one ends up claiming.
        async with self._key_lock:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: self._key_store.wait_for_new_key(
                    seen_key_ids=self._delivered_key_ids, timeout=30.0
                ),
            )
            if result is None:
                raise TimeoutError(
                    "[KeySessionProviderArqitPy] No new key available within 30s "
                    f"({len(self._delivered_key_ids)} already delivered previously)"
                )

            key_id, _ = result
            self._delivered_key_ids.add(key_id)
        self._last_resolved_key_identifier = key_id
        self._key_store.mark_delivered(key_id)
        logger.info(
            "[KeySessionProviderArqitPy] session_key_identifier=%s (total delivered=%d)",
            key_id, len(self._delivered_key_ids),
        )
        return key_id

    def setSessionKeyIdentifier(self, session_key_identifier: str) -> None:
        self._pinned_key_identifier = session_key_identifier
        self._delivered_key_ids.add(session_key_identifier)
        self._key_store.mark_delivered(session_key_identifier)
        logger.debug(
            "[KeySessionProviderArqitPy] setSessionKeyIdentifier=%s",
            session_key_identifier,
        )

    async def getSessionKeyData(self, session_key_identifier: str) -> bytes:
        """
        Returns the key from the key store.

        Primary lookup on the explicit kid passed by the caller (always
        correct in the explicit-handshake flow, see
        stls_core_handshake.perform_handshake). Falls back to the last
        auto-resolved kid if the direct lookup fails — needed for
        backward compatibility with stls-a2a's lazy flow (pre-explicit
        handshake), where the caller passes the STLS session_id (a
        client-generated UUID) instead of the Arqit kid, because at
        that point in the flow the kid isn't yet known to the caller.
        The fallback ALWAYS uses the last dynamically resolved kid,
        never a permanently pinned value — so as not to reintroduce the
        bug where a long-running SERVER provider would forever serve
        the very first handshake's key.
        """
        key_bytes = self._key_store.get(session_key_identifier)
        if key_bytes is None and self._last_resolved_key_identifier is not None:
            key_bytes = self._key_store.get(self._last_resolved_key_identifier)
        if key_bytes is None:
            raise KeyError(
                f"[KeySessionProviderArqitPy] key not found: "
                f"session_key_identifier={session_key_identifier!r} "
                f"(fallback last_resolved={self._last_resolved_key_identifier!r})"
            )
        logger.debug(
            "[KeySessionProviderArqitPy] getSessionKeyData "
            "session_key_identifier=%r (%d bytes)",
            session_key_identifier, len(key_bytes),
        )
        return key_bytes

    def getPeerIdentity(self) -> Optional[str]:
        return self._peer_identity

    def releaseSessionKeyData(self, session_key_identifier: str) -> None:
        """
        Evicts the kid from the underlying ArqitKeyStoreBase.

        Safe and immediate for InMemoryArqitKeyStore and
        RedisArqitKeyStore's client role; a documented no-op for
        RedisArqitKeyStore's server role (shared across replicas) — see
        ArqitKeyStoreBase.remove()'s docstring.
        """
        self._key_store.remove(session_key_identifier)


# ---------------------------------------------------------------------------
# KeyProviderClientArqitPy — initiator side
# ---------------------------------------------------------------------------

class KeyProviderClientArqitPy(KeyProviderClient):
    """
    CLIENT (initiator): starts native Python Arqit peering and negotiates
    the bilocation + P2P keys in a dedicated daemon thread.
    """

    def __init__(self, config_path: str, transport: str) -> None:
        import uuid as _uuid
        self._config_path = config_path
        self._transport = transport.lower()
        # Unique UUID for this client instance: guarantees that
        # concurrent client sessions (asyncio.gather) don't share the
        # same Redis namespace and don't read each other's kid.
        self._instance_id = str(_uuid.uuid4())
        self._key_store = ArqitKeyStoreFactory.build(role="client", instance_id=self._instance_id)
        self._session: Optional[KeySessionProviderArqitPy] = None

    def start(self) -> None:
        # See _ARQIT_PY_START_LOCK: serializes this whole method
        # process-wide against every other concurrent arqit_py
        # start() (client or server, same device or not), to avoid
        # the confirmed DeviceMetadata-singleton deadlock in the
        # Arqit SDK under real concurrency.
        with _ARQIT_PY_START_LOCK:
            from uk.arqit.device.common import QCConfig
            from uk.arqit.device.peering.key_manager import KeyManager
            from uk.arqit.device.peering.comms.mqtt import MqttClientInitiator
            from uk.arqit.device.peering.comms.sockets import SocketClientInitiator
            from uk.arqit.device.common.authentication.authentication_session_manager import (
                AuthenticationSessionManager,
            )
            from uk.arqit.device.common.device_metadata import DeviceMetadata

            logger.info(
                "[KeyProviderClientArqitPy] start config=%s transport=%s",
                self._config_path, self._transport,
            )

            qc_config = QCConfig(self._config_path)
            sdk_key_manager = KeyManager()
            notifier = _ArqitPyKeyNotifier(
                self._key_store, is_receiver=False, sdk_key_manager=sdk_key_manager
            )

            auth_manager = AuthenticationSessionManager(qc_config, reprovision_handler=None)
            auth_manager.set_auto_refresh(True)
            auth_manager.authenticate_device()
            logger.info("[KeyProviderClientArqitPy] authentication completed")

            if self._transport == "mqtt":
                client = MqttClientInitiator(qc_config)
                client.connect_to_broker(auth_manager.get_authentication_session())
            else:
                client = SocketClientInitiator(qc_config)

            # Built BEFORE starting the negotiation thread, not after:
            # KeySessionProviderArqitPy.__init__() snapshots
            # key_store.startup_seen_keys() to pre-seed
            # _delivered_key_ids with whatever is already sitting in
            # the store (stale leftovers from a previous process, for
            # the Redis-backed store). With the negotiation thread now
            # joined before returning (see below), building this
            # *after* the join would snapshot the keys THIS SAME
            # negotiation just delivered as "already seen" — silently
            # discarding them and making every getSessionKeyIdentifier()
            # call time out forever waiting for a "new" key that will
            # never come (confirmed by direct reproduction, 2026-10-23,
            # samples/client/10_client_ms/client_multi_server.py against
            # its Redis-backed key store).
            peer_identity = getattr(qc_config, "socket_receiver_ip_address", None)
            self._session = KeySessionProviderArqitPy(
                KeySessionProvider.KeySessionProviderType.CLIENT,
                self._key_store,
                peer_identity=peer_identity,
            )

            def _negotiate() -> None:
                try:
                    client.agree_bilocation_key(
                        auth_manager.get_authentication_session, notifier
                    )
                    if notifier._last_typed_key is not None:
                        for _ in range(10):
                            client.agree_p2p_key(notifier._last_typed_key, notifier)
                except Exception as exc:
                    logger.error(
                        "[KeyProviderClientArqitPy] negotiation error: %s",
                        exc, exc_info=True,
                    )

            t = threading.Thread(
                target=_negotiate, daemon=True, name="arqit-py-client-negotiate"
            )
            t.start()
            logger.info("[KeyProviderClientArqitPy] negotiation thread started")

            # Join (still holding _ARQIT_PY_START_LOCK) instead of
            # returning immediately: confirmed by direct reproduction
            # (2026-10-23) that letting a second start() call begin its
            # own authentication while a first call's negotiation is
            # still connecting makes the SDK's socket bilocation
            # handshake fail outright (ConnectionRefusedError /
            # PeerUnableToConnectException) for every session involved
            # — the DeviceMetadata/AuthenticationClient race isn't
            # confined to authenticate_device(), it extends through the
            # whole negotiation. 30s matches this provider's own
            # key-delivery timeout convention (see
            # KeySessionProviderArqitPy.getSessionKeyIdentifier()); if
            # negotiation is still running past it we log and move on
            # rather than block the process forever on a stuck network
            # call — the caller's own key-wait timeout still applies on
            # top of this.
            t.join(timeout=30.0)
            if t.is_alive():
                logger.warning(
                    "[KeyProviderClientArqitPy] negotiation still running "
                    "after 30s — releasing the process-wide arqit_py "
                    "start() lock anyway so other sessions aren't blocked "
                    "indefinitely by one slow negotiation"
                )

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError(
                "[KeyProviderClientArqitPy] start() not yet called"
            )
        return self._session

    def stop(self) -> None:
        logger.info("[KeyProviderClientArqitPy] stop()")


# ---------------------------------------------------------------------------
# KeyProviderServerArqitPy — receiver side
# ---------------------------------------------------------------------------

class KeyProviderServerArqitPy(KeyProviderServer):
    """
    SERVER (receiver): listens for native Python Arqit peering
    in a dedicated daemon thread.
    """

    def __init__(self, config_path: str, transport: str) -> None:
        self._config_path = config_path
        self._transport = transport.lower()
        self._key_store = ArqitKeyStoreFactory.build(role="server")
        self._session: Optional[KeySessionProviderArqitPy] = None
        self._is_cancel_ref: list[bool] = [False]
        # Handle to the SDK's own receiver object (SocketClientReceiver /
        # MqttClientReceiver), needed in stop() to actually release the
        # listening socket via its end_key_negotiation() — see stop().
        self._sdk_receiver: Optional[object] = None

    async def start(self) -> BlockableResult:
        # See _ARQIT_PY_START_LOCK: same process-wide serialization as
        # KeyProviderClientArqitPy.start() — this method is also a
        # plain synchronous body (no internal await) that touches the
        # same DeviceMetadata/AuthenticationSessionManager singletons.
        with _ARQIT_PY_START_LOCK:
            from uk.arqit.device.common import QCConfig
            from uk.arqit.device.peering.key_manager import KeyManager
            from uk.arqit.device.peering.comms.mqtt import MqttClientReceiver
            from uk.arqit.device.peering.comms.sockets import SocketClientReceiver
            from uk.arqit.device.common.authentication.authentication_session_manager import (
                AuthenticationSessionManager,
            )
            from uk.arqit.device.policy.client.policy_client import PolicyClient
            from uk.arqit.device.policy.client.policy_client_config import PolicyClientConfig
            from uk.arqit.device.policy.policystore.device_policy_store import DevicePolicyStore
            from uk.arqit.device.common.device_metadata import DeviceMetadata

            logger.info(
                "[KeyProviderServerArqitPy] start config=%s transport=%s",
                self._config_path, self._transport,
            )

            qc_config = QCConfig(self._config_path)
            sdk_key_manager = KeyManager()
            notifier = _ArqitPyKeyNotifier(
                self._key_store, is_receiver=True, sdk_key_manager=sdk_key_manager
            )

            policy_store = DevicePolicyStore(qc_config.device_policy_filepath)
            policy_client_config = PolicyClientConfig(
                DeviceMetadata.get_instance(qc_config),
                policy_store,
                qc_config.domain_name,
                qc_config.use_tls,
            )
            policy_client = PolicyClient(policy_client_config)

            auth_manager = AuthenticationSessionManager(qc_config, reprovision_handler=None)
            auth_manager.set_auto_refresh(True)
            auth_manager.authenticate_device()
            logger.info("[KeyProviderServerArqitPy] authentication completed")

            if self._transport == "mqtt":
                server = MqttClientReceiver(qc_config, policy_client)
                server.connect_to_broker(auth_manager.get_authentication_session())
            else:
                server = SocketClientReceiver(qc_config, policy_client)
                logger.info(
                    "[KeyProviderServerArqitPy] listening on port %s",
                    qc_config.socket_listening_port,
                )

            self._sdk_receiver = server
            is_cancel_ref = self._is_cancel_ref

            def _listen() -> None:
                try:
                    server.listen_for_key_negotiation(
                        auth_manager.get_authentication_session,
                        qc_config.region,
                        notifier,
                        sdk_key_manager,
                    )
                    while not is_cancel_ref[0]:
                        _time.sleep(2.0)
                except Exception as exc:
                    logger.error(
                        "[KeyProviderServerArqitPy] listen error: %s",
                        exc, exc_info=True,
                    )

            t = threading.Thread(
                target=_listen, daemon=True, name="arqit-py-server-listen"
            )
            t.start()
            logger.info("[KeyProviderServerArqitPy] listen thread started")

            self._session = KeySessionProviderArqitPy(
                KeySessionProvider.KeySessionProviderType.SERVER,
                self._key_store,
                peer_identity=None,
            )
            return BlockableResult()

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError(
                "[KeyProviderServerArqitPy] start() not yet called"
            )
        return self._session

    async def stop(self) -> None:
        """
        Stops the server provider.

        Beyond signaling the idle loop in _listen() to exit (which does
        NOT by itself release the listening socket — that loop only
        runs AFTER listen_for_key_negotiation() has already returned,
        with the real listener kept alive by the SDK internally), this
        also calls end_key_negotiation() on the SDK's own receiver
        object to actually shut down and close the listening socket.
        Without this, the port stays bound for the remaining lifetime
        of the process — harmless when the provider runs in its own OS
        process (killing the process releases it regardless), but fatal
        for any caller that starts/stops this provider in-process and
        then needs the same port again (see
        tests/test_arqit_py_real_peering.py::test_same_process_reproduces_401).
        """
        logger.info("[KeyProviderServerArqitPy] stop()")
        self._is_cancel_ref[0] = True
        if self._sdk_receiver is not None:
            try:
                self._sdk_receiver.end_key_negotiation()
            except Exception as exc:
                logger.warning(
                    "[KeyProviderServerArqitPy] stop(): error closing "
                    "SDK listener (probably never started): %s", exc,
                )


# ---------------------------------------------------------------------------
# KeyProviderArqitPy — factory
# ---------------------------------------------------------------------------

class KeyProviderArqitPy(KeyProvider):
    """
    Native Python Arqit KeyProvider.

    Selection: KEY_PROVIDER_NAME=arqit_py
    Requires the Arqit SKA-Platform wheels installed via the [arqit_py] extra.
    """

    def get_name(self) -> str:
        return "arqit_py"

    def _get_transport(self) -> str:
        return os.getenv("ARQIT_PY_TRANSPORT", "socket").lower()

    def create_provider_client(
        self, host: str, port: int, options: dict
    ) -> KeyProviderClient:
        config_path = options.get("config_path") or os.getenv("ARQIT_PY_CLIENT_CONFIG")
        if not config_path:
            raise ValueError(
                "[KeyProviderArqitPy] no config_path in options and "
                "ARQIT_PY_CLIENT_CONFIG not set"
            )
        logger.info(
            "[KeyProviderArqitPy] create_provider_client config=%s", config_path
        )
        return KeyProviderClientArqitPy(
            config_path=config_path, transport=self._get_transport()
        )

    def create_provider_server(
        self, port: int, options: dict
    ) -> KeyProviderServer:
        config_path = options.get("config_path") or os.getenv("ARQIT_PY_SERVER_CONFIG")
        if not config_path:
            raise ValueError(
                "[KeyProviderArqitPy] no config_path in options and "
                "ARQIT_PY_SERVER_CONFIG not set"
            )
        logger.info(
            "[KeyProviderArqitPy] create_provider_server config=%s", config_path
        )
        return KeyProviderServerArqitPy(
            config_path=config_path, transport=self._get_transport()
        )
