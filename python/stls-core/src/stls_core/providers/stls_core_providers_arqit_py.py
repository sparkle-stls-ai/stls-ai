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

    async def getSessionKeyIdentifier(self) -> str:
        """
        Waits for a key from peering and returns the kid.

        If setSessionKeyIdentifier() has already been called explicitly
        (client-side use, to pin a session known in advance), it is used
        with no waiting — callable repeatedly, always returning the same
        pinned value. Otherwise it waits for a key NOT previously
        delivered by this same instance — essential for long-running
        SERVER providers that serve multiple handshakes over time (see
        stls-mcp: a single instance serves the entire lifetime of the
        uvicorn process, one call per incoming handshake).
        """
        if self._pinned_key_identifier is not None:
            return self._pinned_key_identifier

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

        peer_identity = getattr(qc_config, "socket_receiver_ip_address", None)
        self._session = KeySessionProviderArqitPy(
            KeySessionProvider.KeySessionProviderType.CLIENT,
            self._key_store,
            peer_identity=peer_identity,
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
