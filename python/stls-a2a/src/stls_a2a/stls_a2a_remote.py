"""
stls_a2a_remote.py
------------------
Extension of RemoteA2aAgent for automatically propagating user
context (user_id, profile) to remote STLS agents via the native
ADK ``a2a_request_meta_provider`` mechanism.

Problem solved
~~~~~~~~~~~~~~
When a root ADK agent delegates to a remote sub-agent via
``RemoteA2aAgent``, the user context present in ``session.state``
(user_id, profile) is not propagated into the outgoing A2A payload.
The remote agent receives the call without knowing who the end user is.

Solution
~~~~~~~~
``ContextualRemoteA2aAgent`` is a drop-in replacement for
``RemoteA2aAgent`` that automatically injects the user context
as ``params.metadata`` in the A2A payload. On the STLS server side,
``_handle_messages_send`` and ``_handle_messages_send_stream`` already
read ``params.metadata`` as a second source (after ``message.metadata``).

Server-side priority (unchanged, backward-compatible):
    1. message.metadata.user_id / profile  ← direct STLS client
    2. params.metadata.user_id / profile   ← ContextualRemoteA2aAgent
    3. request.context                     ← middleware
    4. "default_user"                      ← final fallback

Dependency on ADK
~~~~~~~~~~~~~~~~~~
This module does NOT import google-adk at module level — the import is
lazy, performed only inside ``ContextualRemoteA2aAgent.__init__``.
This way ``import stls_a2a`` doesn't crash if ADK isn't installed.
The ``ImportError`` exception is only raised at instantiation time,
with an explicit message.

Usage
~~~~~
Minimal replacement in agent_coordinator.py::

    # Before
    from google.adk.agents.remote_a2a_agent import RemoteA2aAgent

    sofia_monitoring_agent = RemoteA2aAgent(
        name="sofia_monitoring_agent",
        agent_card=f"{BASE_URL_MONITORING}{AGENT_CARD_WELL_KNOWN_PATH}",
        httpx_client=httpx.AsyncClient(
            headers={"X-API-Key": API_KEY_MONITORING},
        ),
    )

    # After — drop-in, zero other changes
    from stls_a2a import ContextualRemoteA2aAgent

    sofia_monitoring_agent = ContextualRemoteA2aAgent(
        name="sofia_monitoring_agent",
        agent_card=f"{BASE_URL_MONITORING}{AGENT_CARD_WELL_KNOWN_PATH}",
        httpx_client=httpx.AsyncClient(
            headers={"X-API-Key": API_KEY_MONITORING},
        ),
        context_keys=["user_id", "profile"],   # default: propagates both
    )

context_keys
~~~~~~~~~~~~
List of keys to propagate from ``session.state`` to ``params.metadata``.
Default: ``["user_id", "profile"]``

    context_keys=["user_id"]           # only user_id, profile ignored
    context_keys=["user_id", "tenant"] # additional custom keys

Keys not present in ``session.state`` are silently
ignored — no exception, no empty field in the payload.

Compatibility
~~~~~~~~~~~~~
- Requires google-adk>=1.25.0 and a2a-sdk>=0.3.22 only when instantiated.
- Backward-compatible: empty session.state → identical behavior to
  plain RemoteA2aAgent.
- If the developer passes a custom ``a2a_request_meta_provider``,
  it is wrapped and the results merged (custom takes priority).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

__all__ = ["ContextualRemoteA2aAgent"]

# Keys propagated by default from session.state → params.metadata
_DEFAULT_CONTEXT_KEYS: list[str] = ["user_id", "profile"]

# Lazy type hint: only used by type checkers, not at runtime
if TYPE_CHECKING:
    pass


def _make_meta_provider(
    context_keys: list[str],
) -> Any:
    """
    Factory that returns an ``a2a_request_meta_provider`` configured
    with the specified keys.

    Reads ``ctx.session.state`` and builds a dict with only the keys
    that are present and non-None. Returns None if no key is available
    (no metadata added to the payload — identical to plain RemoteA2aAgent).

    Doesn't import anything from ADK: receives InvocationContext as Any
    and accesses attributes via duck typing — works with any ADK version
    that exposes session.state as a dict.
    """
    def _provider(ctx: Any, a2a_request: Any) -> dict[str, Any] | None:
        state: dict[str, Any] = {}
        try:
            session_state: dict[str, Any] = ctx.session.state or {}
        except AttributeError:
            # session or state not available — graceful degradation
            logger.debug(
                "ContextualRemoteA2aAgent: session.state not accessible, "
                "params.metadata will not be populated."
            )
            return None

        for key in context_keys:
            value = session_state.get(key)
            if value is not None:
                state[key] = value

        if not state:
            logger.debug(
                "ContextualRemoteA2aAgent: no key %s found in "
                "session.state — metadata not propagated.",
                context_keys,
            )
            return None

        # Doesn't log the full profile to avoid exposing sensitive data
        logger.debug(
            "ContextualRemoteA2aAgent: propagating metadata → keys=%s",
            list(state.keys()),
        )
        return state

    return _provider


class ContextualRemoteA2aAgent:
    """
    Drop-in replacement for ``RemoteA2aAgent`` with automatic propagation
    of user context (user_id, profile) to remote STLS agents.

    Reads ``session.state`` via the official ADK hook
    ``a2a_request_meta_provider`` and injects the configured keys as
    ``params.metadata`` in the outgoing A2A payload.

    The google-adk import is lazy: it happens only at instantiation.
    The module is importable without ADK installed.

    Args:
        name:         Agent name (identical to RemoteA2aAgent).
        agent_card:   AgentCard, URL, or path (identical to RemoteA2aAgent).
        context_keys: Keys to propagate from session.state.
                      Default: ["user_id", "profile"].
        **kwargs:     All other RemoteA2aAgent parameters
                      (httpx_client, timeout, description, etc.).

    Raises:
        ImportError: If google-adk isn't installed, raised
                     at instantiation with an explicit message.
    """

    def __new__(
        cls,
        name: str,
        agent_card: Any,
        *,
        context_keys: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        # ── Lazy ADK import — only here, not at module level ──────────────────
        try:
            from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
        except ImportError as exc:
            raise ImportError(
                "ContextualRemoteA2aAgent requires google-adk. "
                "Install it with: pip install stls_a2a[google-adk] "
                "or: pip install 'google-adk[a2a]>=1.25.0'"
            ) from exc

        _keys = context_keys if context_keys is not None else _DEFAULT_CONTEXT_KEYS
        _stls_provider = _make_meta_provider(_keys)

        # If the developer already passed a custom a2a_request_meta_provider,
        # wrap it instead of overwriting it — the results are merged
        # with priority given to the custom provider.
        existing_provider = kwargs.pop("a2a_request_meta_provider", None)

        if existing_provider is not None:
            def _merged_provider(ctx: Any, msg: Any) -> dict[str, Any] | None:
                stls_meta   = _stls_provider(ctx, msg) or {}
                custom_meta = existing_provider(ctx, msg) or {}
                # custom_meta takes priority: overwrites stls_meta on conflict
                merged = {**stls_meta, **custom_meta}
                return merged if merged else None
            final_provider = _merged_provider
        else:
            final_provider = _stls_provider

        # Dynamically builds a RemoteA2aAgent subclass with the provider
        # injected — Returns a real RemoteA2aAgent instance (not a
        # ContextualRemoteA2aAgent one), guaranteeing full compatibility
        # with isinstance() and ADK duck typing.
        instance = RemoteA2aAgent(
            name=name,
            agent_card=agent_card,
            a2a_request_meta_provider=final_provider,
            **kwargs,
        )

        logger.debug(
            "ContextualRemoteA2aAgent '%s' initialized — "
            "context propagation: %s",
            name,
            _keys,
        )

        return instance