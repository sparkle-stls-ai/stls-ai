"""
stls_core_providers_factory.py
-------------------------------
Factory singleton per la selezione e l'istanziazione del key provider STLS.

Contenuto:
  - KeyProviderFactory        : seleziona KeyProvider da KEY_PROVIDER_NAME
  - KeySessionProviderFactory : crea e avvia KeySessionProvider completo

Providers supportati:
  base      → KeyProviderBase       (zero dipendenze, chiave fissa)
  arqit_py  → KeyProviderArqitPy   (wheel Arqit SKA-Platform nativi)
  pqc       → KeyProviderPQC       (ibrido X25519 + ML-KEM-768, liboqs-python)

Uso tipico (server):
    factory  = KeySessionProviderFactory()
    provider = factory.get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.SERVER,
        port=int(os.getenv("KEY_SESSION_PORT", "8091")),
    )
    # provider è già avviato e pronto per perform_handshake()

Uso tipico (client):
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
    Singleton factory per la selezione del KeyProvider.

    Legge KEY_PROVIDER_NAME dall'ambiente per determinare
    quale provider istanziare. Supporta "base" e "arqit_py".

    Il singleton garantisce che il provider venga costruito
    una sola volta per processo — rilevante per arqit_py che
    inizializza connessioni esterne.
    """

    _instance: Optional["KeyProviderFactory"] = None

    def __new__(cls) -> "KeyProviderFactory":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_provider(self, name: str) -> KeyProvider:
        """
        Restituisce il KeyProvider corrispondente al nome.

        Args:
            name: "base" | "arqit_py" | "pqc"

        Returns:
            Istanza KeyProvider pronta per create_provider_client/server().

        Raises:
            ValueError: Se il nome non è riconosciuto.
            ImportError: Se arqit_py è richiesto ma i wheel non sono installati.
        """
        if name == "base":
            return KeyProviderBase()

        if name == "arqit_py":
            try:
                import uk.arqit.device.common  # noqa: F401
            except ImportError:
                raise ImportError(
                    "Il provider 'arqit_py' richiede i wheel Arqit SKA-Platform™.\n"
                    "Installazione:\n"
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
                    "Il provider 'pqc' richiede liboqs-python.\n"
                    "Installazione:\n"
                    "  uv pip install -e '.[pqc]'\n"
                    "  oppure: pip install liboqs-python"
                ) from None

            from .stls_core_providers_pqc import KeyProviderPQC
            return KeyProviderPQC()

        logger.error("KeyProviderFactory: provider non trovato: %s", name)
        raise ValueError(
            f"Provider non supportato: {name!r}. "
            f"Valori validi: 'base', 'arqit_py'"
        )


# ---------------------------------------------------------------------------
# KeySessionProviderFactory — singleton
# ---------------------------------------------------------------------------

class KeySessionProviderFactory:
    """
    Singleton factory che crea, avvia e restituisce un KeySessionProvider
    completamente operativo.

    Combina KeyProviderFactory + create_provider_client/server + start()
    in un'unica chiamata, gestendo correttamente la risoluzione dei
    risultati async/sync/Java-future.

    Uso:
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
    ) -> KeySessionProvider:
        """
        Crea e avvia il KeySessionProvider.

        Args:
            session_type: CLIENT o SERVER.
            host:         Host del server (solo CLIENT).
            port:         Porta del provider (CLIENT e SERVER).
            name:         Nome provider override. Se None legge KEY_PROVIDER_NAME.

        Returns:
            KeySessionProvider avviato e pronto per getSessionKeyIdentifier().

        Raises:
            ValueError: Se KEY_PROVIDER_NAME non è impostato né passato.
            ImportError: Se arqit_py è richiesto ma i wheel mancano.
        """
        provider_name = name or os.getenv("KEY_PROVIDER_NAME")
        if not provider_name:
            raise ValueError(
                "KEY_PROVIDER_NAME non impostato. "
                "Imposta la variabile d'ambiente o passa name= esplicitamente."
            )

        key_provider: KeyProvider = KeyProviderFactory().get_provider(provider_name)

        if session_type == KeySessionProvider.KeySessionProviderType.SERVER:
            server = key_provider.create_provider_server(
                port=port, options={}
            )
            # start() può essere sync o async a seconda del provider
            result = server.start()
            resolved = self._resolve(result)
            if hasattr(resolved, "block"):
                resolved.block()
            return server.createProviderSession()
        else:
            client = key_provider.create_provider_client(
                host=host or "", port=port or 0, options={}
            )
            client.start()
            return client.createProviderSession()

    @staticmethod
    def resolve_if_awaitable(result: Any) -> Any:
        """
        Normalizza un risultato che può essere:
          - coroutine asyncio  → esegue con asyncio.run()
          - valore diretto     → restituisce as-is

        Metodo pubblico di utilità, usabile anche fuori da get_session_provider()
        per risolvere manualmente risultati di getSessionKeyIdentifier()/
        getSessionKeyData() che potrebbero essere awaitable (es. arqit_py)
        o valori diretti (es. base).
        """
        if inspect.isawaitable(result):
            logger.debug(
                "KeySessionProviderFactory: risultato awaitable, "
                "risolvo con asyncio.run()"
            )
            return asyncio.run(result)
        return result

    # Alias interno mantenuto per chiarezza nelle chiamate di get_session_provider()
    _resolve = resolve_if_awaitable