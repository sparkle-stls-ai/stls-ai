"""
stls_core_providers_base.py
---------------------------
Interfacce ABC del sistema key provider STLS e implementazione Base.

Contenuto:
  - ABC: KeySessionProvider, KeyProviderClient, KeyProviderServer, KeyProvider
  - Implementazione Base: chiave fissa cablata, zero dipendenze esterne.
    Usata per sviluppo, test e ambienti dove Arqit non è disponibile.

Il provider Base non richiede né JVM né wheel Arqit — il modulo si importa
con la sola dipendenza su stdlib.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABC — interfacce del sistema provider
# ---------------------------------------------------------------------------

class KeySessionProvider(ABC):
    """
    Interfaccia per un provider di sessione crittografica.

    Implementata da Base (chiave fissa) e ArqitPy (SKA-Platform).
    Usata da perform_handshake() e SessionCache per recuperare
    la chiave di sessione associata a un key identifier.
    """

    class KeySessionProviderType(Enum):
        CLIENT = "CLIENT"
        SERVER = "SERVER"

    @abstractmethod
    def getSessionType(self) -> "KeySessionProvider.KeySessionProviderType":
        """Restituisce il ruolo del provider (CLIENT o SERVER)."""
        pass

    @abstractmethod
    async def getSessionKeyIdentifier(self) -> str:
        """
        Restituisce il key identifier (kid) della sessione corrente.

        Può essere asincrono: su ArqitPy attende che il peering
        produca almeno una chiave prima di restituire il kid.
        """
        pass

    @abstractmethod
    def setSessionKeyIdentifier(self, session_key_identifier: str) -> None:
        """Imposta esplicitamente il kid (usato in scenari di riuso sessione)."""
        pass

    @abstractmethod
    async def getSessionKeyData(self, session_key_identifier: str) -> bytes:
        """
        Restituisce la chiave di sessione associata al kid fornito.

        Args:
            session_key_identifier: Key identifier (kid) da risolvere.

        Returns:
            Chiave di sessione come bytes.

        Raises:
            KeyError: Se il kid non è noto al provider.
            TimeoutError: Se il provider non riesce a produrre la chiave
                          entro il timeout (ArqitPy).
        """
        pass

    @abstractmethod
    def getPeerIdentity(self) -> str | None:
        """Restituisce l'identità del peer (IP, device_id, ecc.) o None."""
        pass


class KeyProviderClient(ABC):
    """Interfaccia per il lato client del key provider."""

    @abstractmethod
    def start(self) -> None:
        """Avvia il provider client (negoziazione chiavi, autenticazione)."""
        pass

    @abstractmethod
    def createProviderSession(self) -> KeySessionProvider:
        """Restituisce la sessione provider dopo start()."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Ferma il provider client e rilascia risorse."""
        pass


class KeyProviderServer(ABC):
    """Interfaccia per il lato server del key provider."""

    @abstractmethod
    async def start(self) -> None:
        """Avvia il provider server (ascolto, autenticazione)."""
        pass

    @abstractmethod
    def createProviderSession(self) -> KeySessionProvider:
        """Restituisce la sessione provider dopo start()."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Ferma il provider server e rilascia risorse."""
        pass


class KeyProvider(ABC):
    """
    Factory ABC per la creazione di client e server provider.

    Implementata da KeyProviderBase e KeyProviderArqitPy.
    KeyProviderFactory usa questa interfaccia per istanziare
    il provider corretto in base a KEY_PROVIDER_NAME.
    """

    @abstractmethod
    def get_name(self) -> str:
        """Nome identificativo del provider (es. "base", "arqit_py")."""
        pass

    @abstractmethod
    def create_provider_client(
        self, host: str, port: int, options: dict[str, Any]
    ) -> KeyProviderClient:
        pass

    @abstractmethod
    def create_provider_server(
        self, port: int, options: dict[str, Any]
    ) -> KeyProviderServer:
        pass


class BlockableResult:
    """
    Oggetto restituito da start() dei provider server sincroni.
    Compatibile con il pattern .block() usato da KeySessionProviderFactory.
    """
    def block(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Provider Base — chiave fissa, zero dipendenze
# ---------------------------------------------------------------------------

_BASE_KEY_ID  = "base-session-key-identifier"
_BASE_KEY_DATA = b"0123456789abcdef"   # 16 byte — sufficiente per HKDF-Expand


class KeySessionProviderBase(KeySessionProvider):
    """
    KeySessionProvider con chiave fissa cablata.

    Usato per sviluppo, test e CI. Non richiede né JVM né Arqit.
    La chiave è nota a entrambi i lati senza negoziazione.
    """

    def __init__(
        self,
        session_type: KeySessionProvider.KeySessionProviderType,
    ) -> None:
        self._session_type = session_type
        self._kid = _BASE_KEY_ID

    def getSessionType(self) -> KeySessionProvider.KeySessionProviderType:
        return self._session_type

    async def getSessionKeyIdentifier(self) -> str:
        return self._kid

    def setSessionKeyIdentifier(self, session_key_identifier: str) -> None:
        self._kid = session_key_identifier

    async def getSessionKeyData(self, session_key_identifier: str) -> bytes:
        return _BASE_KEY_DATA

    def getPeerIdentity(self) -> str | None:
        return "base-peer"


class KeyProviderClientBase(KeyProviderClient):

    def start(self) -> None:
        logger.debug("[KeyProviderClientBase] start()")

    def createProviderSession(self) -> KeySessionProvider:
        return KeySessionProviderBase(
            KeySessionProvider.KeySessionProviderType.CLIENT
        )

    def stop(self) -> None:
        logger.debug("[KeyProviderClientBase] stop()")


class KeyProviderServerBase(KeyProviderServer):

    async def start(self) -> BlockableResult:
        logger.debug("[KeyProviderServerBase] start()")
        return BlockableResult()

    def createProviderSession(self) -> KeySessionProvider:
        return KeySessionProviderBase(
            KeySessionProvider.KeySessionProviderType.SERVER
        )

    async def stop(self) -> None:
        logger.debug("[KeyProviderServerBase] stop()")


class KeyProviderBase(KeyProvider):
    """
    KeyProvider Base — chiave fissa, zero dipendenze esterne.

    Selezione: KEY_PROVIDER_NAME=base
    """

    def get_name(self) -> str:
        return "base"

    def create_provider_client(
        self, host: str, port: int, options: dict[str, Any]
    ) -> KeyProviderClient:
        return KeyProviderClientBase()

    def create_provider_server(
        self, port: int, options: dict[str, Any]
    ) -> KeyProviderServer:
        return KeyProviderServerBase()