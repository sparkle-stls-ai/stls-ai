"""
stls_core_providers_factory.py
-------------------------------
Singleton factory for selecting and instantiating the STLS key provider.

Contents:
  - KeyProviderFactory        : selects a KeyProvider based on KEY_PROVIDER_NAME
  - KeySessionProviderFactory : creates and starts a full KeySessionProvider

Supported providers:
  base      → KeyProviderBase       (zero dependencies, fixed key)
  arqit_py  → KeyProviderArqitPy   (native Arqit SKA-Platform wheels)
  pqc       → KeyProviderPQC       (hybrid X25519 + ML-KEM-768, liboqs-python)

Typical usage (server):
    factory  = KeySessionProviderFactory()
    provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=int(os.getenv("KEY_SESSION_PORT", "8091")),
    )
    # provider is already started and ready for perform_handshake()

Typical usage (client):
    factory  = KeySessionProviderFactory()
    provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
        host="server-host",
        port=8091,
    )
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from typing import Any, Optional

from .stls_core_providers_base import (
    KeyProvider,
    KeyProviderBase,
    KeySessionProvider,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# KeyProviderFactory — singleton
# ---------------------------------------------------------------------------

class KeyProviderFactory:
    """
    Singleton factory for selecting the KeyProvider.

    Reads KEY_PROVIDER_NAME from the environment to determine
    which provider to instantiate. Supports "base" and "arqit_py".

    The singleton guarantees that the provider is built
    only once per process — relevant for arqit_py, which
    initializes external connections.
    """

    _instance: Optional["KeyProviderFactory"] = None

    def __new__(cls) -> "KeyProviderFactory":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_provider(self, name: str) -> KeyProvider:
        """
        Returns the KeyProvider corresponding to the given name.

        Args:
            name: "base" | "arqit_py" | "pqc"

        Returns:
            KeyProvider instance ready for create_provider_client/server().

        Raises:
            ValueError: If the name is not recognized.
            ImportError: If arqit_py is requested but the wheels are not installed.
        """
        if name == "base":
            return KeyProviderBase()

        if name == "arqit_py":
            try:
                import uk.arqit.device.common  # noqa: F401
            except ImportError:
                raise ImportError(
                    "The 'arqit_py' provider requires the Arqit SKA-Platform™ wheels.\n"
                    "Installation:\n"
                    "  uv pip install -e '.[arqit_py]' \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-common/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-policy/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-registration/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-provisioning/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-mqtt-common/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-peering/ \\\n"
                    "    --find-links arqit_wheels/arqit-skaplatform-monitoring/"
                ) from None

            from .stls_core_providers_arqit_py import KeyProviderArqitPy
            return KeyProviderArqitPy()

        if name == "pqc":
            try:
                import oqs  # noqa: F401
            except ImportError:
                raise ImportError(
                    "The 'pqc' provider requires liboqs-python.\n"
                    "Installation:\n"
                    "  uv pip install -e '.[pqc]'\n"
                    "  or: pip install liboqs-python"
                ) from None

            from .stls_core_providers_pqc import KeyProviderPQC
            return KeyProviderPQC()

        logger.error("KeyProviderFactory: provider not found: %s", name)
        raise ValueError(
            f"Unsupported provider: {name!r}. "
            f"Valid values: 'base', 'arqit_py'"
        )


# ---------------------------------------------------------------------------
# KeySessionProviderFactory — singleton
# ---------------------------------------------------------------------------

class KeySessionProviderFactory:
    """
    Singleton factory that creates, starts and returns a fully operational
    KeySessionProvider.

    Combines KeyProviderFactory + create_provider_client/server + start()
    into a single call, correctly handling the resolution of
    async/sync/Java-future results.

    Usage:
        provider = KeySessionProviderFactory().get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.SERVER,
            port=8091,
        )
    """

    _instance: Optional["KeySessionProviderFactory"] = None

    def __new__(cls) -> "KeySessionProviderFactory":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_session_provider(
        self,
        session_type: KeySessionProvider.KeySessionProviderType,
        host: Optional[str] = None,
        port: Optional[int] = None,
        name: Optional[str] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> KeySessionProvider:
        """
        Creates and starts the KeySessionProvider.

        Args:
            session_type: CLIENT or SERVER.
            host:         Server host (CLIENT only).
            port:         Provider port (both CLIENT and SERVER).
            name:         Provider name override. If None, reads KEY_PROVIDER_NAME.
            options:      Provider-specific overrides passed straight through to
                          create_provider_client/server (e.g. arqit_py's
                          "config_path", which overrides ARQIT_PY_CLIENT_CONFIG/
                          ARQIT_PY_SERVER_CONFIG for this call only). Defaults to {}.

        Returns:
            KeySessionProvider, started and ready for getSessionKeyIdentifier().

        Raises:
            ValueError: If KEY_PROVIDER_NAME is neither set nor passed explicitly.
            ImportError: If arqit_py is requested but the wheels are missing.
        """
        provider_name = name or os.getenv("KEY_PROVIDER_NAME")
        if not provider_name:
            raise ValueError(
                "KEY_PROVIDER_NAME not set. "
                "Set the environment variable or pass name= explicitly."
            )

        key_provider: KeyProvider = KeyProviderFactory().get_provider(provider_name)
        resolved_options = options or {}

        if session_type == KeySessionProvider.KeySessionProviderType.SERVER:
            server = key_provider.create_provider_server(
                port=port, options=resolved_options
            )
            # start() can be sync or async depending on the provider
            result = server.start()
            resolved = self._resolve(result)
            if hasattr(resolved, "block"):
                resolved.block()
            return server.createProviderSession()
        else:
            client = key_provider.create_provider_client(
                host=host or "", port=port or 0, options=resolved_options
            )
            client.start()
            return client.createProviderSession()

    @staticmethod
    def resolve_if_awaitable(result: Any) -> Any:
        """
        Normalizes a result that may be:
          - an asyncio coroutine  → run with asyncio.run()
          - a direct value        → returned as-is

        Public utility method, usable outside get_session_provider() too,
        to manually resolve results from getSessionKeyIdentifier()/
        getSessionKeyData() that may be awaitable (e.g. arqit_py)
        or direct values (e.g. base).
        """
        if inspect.isawaitable(result):
            logger.debug(
                "KeySessionProviderFactory: awaitable result, "
                "resolving with asyncio.run()"
            )
            return asyncio.run(result)
        return result

    # Internal alias kept for clarity in get_session_provider() calls
    _resolve = resolve_if_awaitable
