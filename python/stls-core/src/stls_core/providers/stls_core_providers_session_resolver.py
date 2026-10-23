"""
stls_core_providers_session_resolver.py
-----------------------------------------
Generic, provider-agnostic resolution of (session_key_identifier,
session_key_data) pairs for an arbitrary caller-defined target_key
(e.g. a base_url for stls-a2a, a server id for stls-mcp), extracted
from stls-a2a's SecureSessionManager so both stls-a2a and stls-mcp (and
any future protocol layer) can share it instead of reimplementing the
same cache/lock/config-override logic.

Scope: this class resolves raw key material for one target, safely
under concurrent callers for the SAME target_key. It does NOT cache a
higher-level object (an A2A transport adapter, an MCP toolset) — each
protocol layer keeps caching ITS OWN derived object, keyed the same
way, calling resolve() only on its own cache miss. This mirrors exactly
what SecureSessionManager already did before this refactor.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from .stls_core_providers_base import KeySessionProvider
from .stls_core_providers_factory import KeySessionProviderFactory


class KeySessionResolver:
    """
    Resolves session key material for N distinct targets from one
    process, optionally giving each target its own arqit_py device
    config (via options["config_path"], see
    stls_core_providers_arqit_py.py) instead of the single process-wide
    ARQIT_PY_CLIENT_CONFIG env var.

    Args:
        provider_name: "base" | "pqc" | "arqit_py".
        target_configs: optional {target_key: config_path} map, used
            only when provider_name == "arqit_py". A target_key with no
            entry here (or this argument omitted) falls back to the
            ARQIT_PY_CLIENT_CONFIG env var, exactly as before this
            class existed — no behavior change for existing callers.
    """

    def __init__(
        self,
        provider_name: str,
        target_configs: Optional[dict[str, str]] = None,
    ) -> None:
        self._provider_name = provider_name.strip().lower()
        self._target_configs = target_configs or {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, target_key: str) -> asyncio.Lock:
        lock = self._locks.get(target_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[target_key] = lock
        return lock

    async def resolve(
        self,
        target_key: str,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> tuple[str, bytes]:
        """
        Returns (session_key_identifier, session_key_data) for
        target_key, under a per-target_key lock so concurrent callers
        resolving the SAME target don't race each other into duplicate
        provider instances.

        getSessionKeyIdentifier()/getSessionKeyData() are declared
        `async def` on KeySessionProvider, so they're awaited directly
        here — NOT via KeySessionProviderFactory.resolve_if_awaitable(),
        which calls asyncio.run() internally and would break inside an
        already-running event loop (the expected calling context).
        """
        async with self._lock_for(target_key):
            options: dict[str, str] = {}
            if self._provider_name == "arqit_py":
                config_path = self._target_configs.get(target_key)
                if config_path:
                    options["config_path"] = config_path

            provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
                session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
                host=host,
                port=port,
                name=self._provider_name,
                options=options,
            )
            session_key_identifier = await provider.getSessionKeyIdentifier()
            session_key_data = await provider.getSessionKeyData(session_key_identifier)
            return session_key_identifier, session_key_data
