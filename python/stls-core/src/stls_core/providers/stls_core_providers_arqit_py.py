"""
stls_core_providers_arqit_py.py
--------------------------------
Provider Arqit SKA-Platform™ tramite SDK Python nativo (uk.arqit.device.*).

Nessuna JVM, nessun JPype. Usa i wheel Arqit installati via:
    uv pip install -e ".[arqit_py]" --find-links arqit_wheels/...

Variabili d'ambiente:
    ARQIT_PY_CLIENT_CONFIG  → path al config JSON del client (initiator)
    ARQIT_PY_SERVER_CONFIG  → path al config JSON del server (receiver)
    ARQIT_PY_TRANSPORT      → "socket" (default) | "mqtt"

Architettura 1:1 (Option A):
    Ogni sessione STLS ha il proprio KeyProviderClient/Server e il proprio
    ArqitKeyStoreBase. create_provider_client() è chiamato una volta per
    sessione — il binding kid ↔ session_key è esclusivo per sessione.

Il peering Arqit avviene in thread daemon separati per non bloccare
il loop asyncio del server FastAPI/Starlette.
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
# _ArqitPyKeyNotifier — callback SDK Arqit
# ---------------------------------------------------------------------------

class _ArqitPyKeyNotifier:
    """
    Implementa KeyNotifierBase dell'SDK Python Arqit.

    Riceve le chiavi negoziate durante il peering e le salva
    nello store condiviso. Se receiver, le aggiunge anche al
    KeyManager SDK per la gestione interna.
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
        """Callback invocato dall'SDK per ogni chiave negoziata."""
        from uk.arqit.device.peering.key_manager import KeyType

        logger.info(
            "[ArqitPyKeyNotifier] tipo=%s id=%s peer=%s ttl=%s",
            key.key_type, key.key_id, key.peer_duid, key.ttl,
        )

        if key.key_type is KeyType.BILOCATION:
            self._last_typed_key = key
            self._key_store.add(str(key.key_id), bytes(key.key))
            if self._is_receiver and self._sdk_key_manager is not None:
                self._sdk_key_manager.add_key(key)


# ---------------------------------------------------------------------------
# KeySessionProviderArqitPy — interfaccia KeySessionProvider per ArqitPy
# ---------------------------------------------------------------------------

class KeySessionProviderArqitPy(KeySessionProvider):
    """
    KeySessionProvider che delega al key store popolato dal peering Arqit.

    getSessionKeyIdentifier() attende una chiave dal peering (async, con
    timeout 30s). Lato CLIENT, ogni istanza serve tipicamente una sola
    sessione, e la prima chiave disponibile è quella corretta. Lato
    SERVER (long-running, vedi KeyProviderServerArqitPy.createProviderSession,
    che restituisce sempre la stessa istanza per l'intera vita del
    processo), il provider deve invece poter servire più handshake nel
    tempo: ogni chiamata attende una chiave NON ANCORA CONSEGNATA in una
    chiamata precedente, così da non riusare per sempre il kid del primo
    handshake quando il peering ha negoziato chiavi nuove nel frattempo.
    getSessionKeyData() legge dallo store per key_id.
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
        # Impostato SOLO da una chiamata esplicita a setSessionKeyIdentifier()
        # (uso lato client per fissare una sessione nota in anticipo).
        # getSessionKeyIdentifier() risolto automaticamente NON lo imposta:
        # farlo lo fisserebbe per sempre anche per le chiamate successive,
        # che è esattamente il bug che questo fix risolve.
        self._pinned_key_identifier: Optional[str] = None
        # Tutti i kid già restituiti da getSessionKeyIdentifier() nel corso
        # della vita di questa istanza — usato solo lato SERVER per evitare
        # di riconsegnare lo stesso kid a un nuovo handshake (vedi docstring
        # della classe). Lato CLIENT resta tipicamente con un solo elemento:
        # una singola sessione client risolve una volta sola.
        # Pre-popolato con startup_seen_keys(): in caso di riavvio del
        # processo server con KEY_STORE_BACKEND=redis, il ZSET conserva
        # chiavi di sessioni precedenti. Senza pre-popolazione il server
        # le userebbe per nuovi handshake → mismatch con il client.
        self._delivered_key_ids: set[str] = set(key_store.startup_seen_keys())
        # Ultimo kid risolto automaticamente da getSessionKeyIdentifier()
        # (non pinned) — usato SOLO come fallback in getSessionKeyData()
        # quando il lookup diretto fallisce, per retrocompatibilità con
        # stls-a2a (flusso lazy, pre-handshake esplicito): lì il middleware
        # chiama getSessionKeyData(session_id) passando il session_id STLS
        # (UUID client-generated) anziché il kid Arqit, perché non esegue
        # ancora un vero handshake — vedi CHANGELOG stls-a2a 0.1.1, fix
        # "KeySessionProviderArqitPy.getSessionKeyData() lookup errato".
        self._last_resolved_key_identifier: Optional[str] = None

    def getSessionType(self) -> KeySessionProvider.KeySessionProviderType:
        return self._session_type

    async def getSessionKeyIdentifier(self) -> str:
        """
        Attende una chiave dal peering e restituisce il kid.

        Se setSessionKeyIdentifier() è già stato chiamato esplicitamente
        (uso lato client per fissare una sessione nota), usa quello senza
        attendere — chiamabile ripetutamente, restituisce sempre lo stesso
        valore pinned. Altrimenti attende una chiave NON ANCORA consegnata
        in precedenza da questa stessa istanza — fondamentale per provider
        SERVER long-running che servono più handshake nel tempo (vedi
        stls-mcp: un'unica istanza serve l'intera vita del processo
        uvicorn, una chiamata per ogni nuovo handshake in arrivo).
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
                "[KeySessionProviderArqitPy] Nessuna chiave nuova disponibile entro 30s "
                f"({len(self._delivered_key_ids)} già consegnate in precedenza)"
            )

        key_id, _ = result
        self._delivered_key_ids.add(key_id)
        self._last_resolved_key_identifier = key_id
        self._key_store.mark_delivered(key_id)
        logger.info(
            "[KeySessionProviderArqitPy] session_key_identifier=%s (totale consegnate=%d)",
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
        Restituisce la chiave dal key store.

        Lookup primario sul kid esplicito passato dal chiamante (sempre
        corretto nel flusso con handshake esplicito, vedi
        stls_core_handshake.perform_handshake). Fallback sull'ultimo kid
        risolto automaticamente da getSessionKeyIdentifier() se il lookup
        diretto fallisce — necessario per retrocompatibilità con il
        flusso lazy di stls-a2a (pre-handshake esplicito), dove il
        chiamante passa il session_id STLS (UUID client-generated) invece
        del kid Arqit, perché a quel punto del flusso il kid non è ancora
        noto al chiamante. Il fallback usa SEMPRE l'ultimo kid risolto
        dinamicamente, mai un valore fissato in modo permanente — per non
        reintrodurre il bug per cui un provider SERVER long-running
        servirebbe per sempre la chiave del primissimo handshake.
        """
        key_bytes = self._key_store.get(session_key_identifier)
        if key_bytes is None and self._last_resolved_key_identifier is not None:
            key_bytes = self._key_store.get(self._last_resolved_key_identifier)
        if key_bytes is None:
            raise KeyError(
                f"[KeySessionProviderArqitPy] chiave non trovata: "
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
# KeyProviderClientArqitPy — lato initiator
# ---------------------------------------------------------------------------

class KeyProviderClientArqitPy(KeyProviderClient):
    """
    CLIENT (initiator): avvia il peering Arqit Python nativo e negozia
    bilocation + P2P keys in un thread daemon dedicato.
    """

    def __init__(self, config_path: str, transport: str) -> None:
        import uuid as _uuid
        self._config_path = config_path
        self._transport = transport.lower()
        # UUID univoco per questa istanza client: garantisce che sessioni
        # client concorrenti (asyncio.gather) non condividano lo stesso
        # namespace Redis e non leggano kid l'una dell'altra.
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
        logger.info("[KeyProviderClientArqitPy] autenticazione completata")

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
                    "[KeyProviderClientArqitPy] errore negoziazione: %s",
                    exc, exc_info=True,
                )

        t = threading.Thread(
            target=_negotiate, daemon=True, name="arqit-py-client-negotiate"
        )
        t.start()
        logger.info("[KeyProviderClientArqitPy] thread negoziazione avviato")

        peer_identity = getattr(qc_config, "socket_receiver_ip_address", None)
        self._session = KeySessionProviderArqitPy(
            KeySessionProvider.KeySessionProviderType.CLIENT,
            self._key_store,
            peer_identity=peer_identity,
        )

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError(
                "[KeyProviderClientArqitPy] start() non ancora chiamato"
            )
        return self._session

    def stop(self) -> None:
        logger.info("[KeyProviderClientArqitPy] stop()")


# ---------------------------------------------------------------------------
# KeyProviderServerArqitPy — lato receiver
# ---------------------------------------------------------------------------

class KeyProviderServerArqitPy(KeyProviderServer):
    """
    SERVER (receiver): mette in ascolto il peering Arqit Python nativo
    in un thread daemon dedicato.
    """

    def __init__(self, config_path: str, transport: str) -> None:
        self._config_path = config_path
        self._transport = transport.lower()
        self._key_store = ArqitKeyStoreFactory.build(role="server")
        self._session: Optional[KeySessionProviderArqitPy] = None
        self._is_cancel_ref: list[bool] = [False]

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
        logger.info("[KeyProviderServerArqitPy] autenticazione completata")

        if self._transport == "mqtt":
            server = MqttClientReceiver(qc_config, policy_client)
            server.connect_to_broker(auth_manager.get_authentication_session())
        else:
            server = SocketClientReceiver(qc_config, policy_client)
            logger.info(
                "[KeyProviderServerArqitPy] in ascolto su porta %s",
                qc_config.socket_listening_port,
            )

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
                    "[KeyProviderServerArqitPy] errore ascolto: %s",
                    exc, exc_info=True,
                )

        t = threading.Thread(
            target=_listen, daemon=True, name="arqit-py-server-listen"
        )
        t.start()
        logger.info("[KeyProviderServerArqitPy] thread ascolto avviato")

        self._session = KeySessionProviderArqitPy(
            KeySessionProvider.KeySessionProviderType.SERVER,
            self._key_store,
            peer_identity=None,
        )
        return BlockableResult()

    def createProviderSession(self) -> KeySessionProvider:
        if self._session is None:
            raise RuntimeError(
                "[KeyProviderServerArqitPy] start() non ancora chiamato"
            )
        return self._session

    async def stop(self) -> None:
        logger.info("[KeyProviderServerArqitPy] stop()")
        self._is_cancel_ref[0] = True


# ---------------------------------------------------------------------------
# KeyProviderArqitPy — factory
# ---------------------------------------------------------------------------

class KeyProviderArqitPy(KeyProvider):
    """
    KeyProvider Arqit Python nativo.

    Selezione: KEY_PROVIDER_NAME=arqit_py
    Richiede i wheel Arqit SKA-Platform installati via [arqit_py] extra.
    """

    def get_name(self) -> str:
        return "arqit_py"

    def _get_transport(self) -> str:
        return os.getenv("ARQIT_PY_TRANSPORT", "socket").lower()

    def create_provider_client(
        self, host: str, port: int, options: dict
    ) -> KeyProviderClient:
        config_path = os.getenv("ARQIT_PY_CLIENT_CONFIG")
        if not config_path:
            raise ValueError(
                "[KeyProviderArqitPy] ARQIT_PY_CLIENT_CONFIG non impostato"
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
        config_path = os.getenv("ARQIT_PY_SERVER_CONFIG")
        if not config_path:
            raise ValueError(
                "[KeyProviderArqitPy] ARQIT_PY_SERVER_CONFIG non impostato"
            )
        logger.info(
            "[KeyProviderArqitPy] create_provider_server config=%s", config_path
        )
        return KeyProviderServerArqitPy(
            config_path=config_path, transport=self._get_transport()
        )