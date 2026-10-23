"""
stls_core_providers_base.py
---------------------------
ABC interfaces for the STLS key provider system and the Base implementation.

Contents:
  - ABCs: KeySessionProvider, KeyProviderClient, KeyProviderServer, KeyProvider
  - Base implementation: a fixed hardcoded key, zero external dependencies.
    Used for development, testing and environments where Arqit isn't available.

The Base provider requires neither a JVM nor the Arqit wheel — the module
imports with only the stdlib as a dependency.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, Optional

import logging
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ABCs — provider system interfaces
# ---------------------------------------------------------------------------

class KeySessionProvider(ABC):
    """
    Interface for a cryptographic session provider.

    Implemented by Base (fixed key) and ArqitPy (SKA-Platform).
    Used by perform_handshake() and SessionCache to retrieve
    the session key associated with a key identifier.
    """

    class KeySessionProviderType(Enum):
        CLIENT = "CLIENT"
        SERVER = "SERVER"

    @abstractmethod
    def getSessionType(self) -> "KeySessionProvider.KeySessionProviderType":
        """Returns the provider's role (CLIENT or SERVER)."""
        pass

    @abstractmethod
    async def getSessionKeyIdentifier(self, hint_kid: Optional[str] = None) -> str:
        """
        Returns the key identifier (kid) of the current session.

        May be asynchronous: on ArqitPy it waits for the peering
        process to produce at least one key before returning the kid.

        hint_kid: optional kid the caller already knows (e.g. the
        kid a client resolved on its own side and sent along in its
        HandshakeRequest). Implementations that serve a single,
        unambiguous key (Base, PQC) ignore it. ArqitPy uses it to
        resolve that EXACT already-delivered key directly instead of
        "whichever key arrived and hasn't been claimed yet" — needed
        because, on a long-running SERVER instance serving multiple
        concurrent handshakes, plain FIFO delivery order does not
        necessarily match the order handshake requests arrive in (see
        stls_core_providers_arqit_py.py's own docstring for the full
        explanation and the real failure this fixes).
        """
        pass

    @abstractmethod
    def setSessionKeyIdentifier(self, session_key_identifier: str) -> None:
        """Explicitly sets the kid (used in session-reuse scenarios)."""
        pass

    @abstractmethod
    async def getSessionKeyData(self, session_key_identifier: str) -> bytes:
        """
        Returns the session key associated with the given kid.

        Args:
            session_key_identifier: Key identifier (kid) to resolve.

        Returns:
            Session key as bytes.

        Raises:
            KeyError: If the kid is not known to the provider.
            TimeoutError: If the provider fails to produce the key
                          within the timeout (ArqitPy).
        """
        pass

    @abstractmethod
    def getPeerIdentity(self) -> str | None:
        """Returns the peer's identity (IP, device_id, etc.) or None."""
        pass


class KeyProviderClient(ABC):
    """Interface for the client side of the key provider."""

    @abstractmethod
    def start(self) -> None:
        """Starts the client provider (key negotiation, authentication)."""
        pass

    @abstractmethod
    def createProviderSession(self) -> KeySessionProvider:
        """Returns the provider session after start()."""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stops the client provider and releases resources."""
        pass


class KeyProviderServer(ABC):
    """Interface for the server side of the key provider."""

    @abstractmethod
    async def start(self) -> None:
        """Starts the server provider (listening, authentication)."""
        pass

    @abstractmethod
    def createProviderSession(self) -> KeySessionProvider:
        """Returns the provider session after start()."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stops the server provider and releases resources."""
        pass


class KeyProvider(ABC):
    """
    Factory ABC for creating client and server providers.

    Implemented by KeyProviderBase and KeyProviderArqitPy.
    KeyProviderFactory uses this interface to instantiate
    the correct provider based on KEY_PROVIDER_NAME.
    """

    @abstractmethod
    def get_name(self) -> str:
        """The provider's identifying name (e.g. "base", "arqit_py")."""
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
    Object returned by start() on synchronous server providers.
    Compatible with the .block() pattern used by KeySessionProviderFactory.
    """
    def block(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Base provider — fixed key, zero dependencies
# ---------------------------------------------------------------------------

_BASE_KEY_ID  = "base-session-key-identifier"
_BASE_KEY_DATA = b"0123456789abcdef"   # 16 bytes — sufficient for HKDF-Expand


class KeySessionProviderBase(KeySessionProvider):
    """
    KeySessionProvider with a fixed, hardcoded key.

    Used for development, testing and CI. Requires neither a JVM nor Arqit.
    The key is known to both sides with no negotiation.
    """

    def __init__(
        self,
        session_type: KeySessionProvider.KeySessionProviderType,
    ) -> None:
        self._session_type = session_type
        self._kid = _BASE_KEY_ID

    def getSessionType(self) -> KeySessionProvider.KeySessionProviderType:
        return self._session_type

    async def getSessionKeyIdentifier(self, hint_kid: Optional[str] = None) -> str:
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
    Base KeyProvider — fixed key, no external dependencies.

    Selection: KEY_PROVIDER_NAME=base
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
