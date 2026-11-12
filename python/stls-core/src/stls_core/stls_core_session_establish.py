"""
stls_core_session_establish.py
--------------------------------
establish_session()/resolve_transport() — the public, non-leaking
replacements for the old "KeySessionResolver.resolve() then feed
session_key_data into perform_client_handshake()" two-step dance
(issue #5, "Restrict stls-core's public API surface").

- establish_session(): resolve + perform the STLS handshake (or, for
  pqc, the KEM handshake) in one call. Used by stls-mcp, whose wire
  protocol always does a full handshake round trip for every provider.
- resolve_transport(): resolve + build the StlsTransport directly, with
  NO handshake. Used by stls-a2a's base/arqit_py path, whose wire
  protocol has no handshake at all — client and server each
  independently resolve their own copy of the same key and trust it
  matches, with no proof-of-possession round trip. Folding THAT path
  into establish_session() would silently add a network round trip
  that doesn't exist today and require new server-side code — out of
  scope for this issue. resolve_transport() preserves today's exact
  wire behavior while still never handing raw key bytes back to its
  caller (previously the one thing stls-a2a's non-pqc path did).

Neither function ever returns the raw key bytes themselves. Both share
one lazily-created, module-private {provider_name: KeySessionResolver}
cache so the existing per-target_key lock (duplicate-handshake
protection) keeps working exactly as before.

Port of node/stls-core/src/providers/session-establish.ts.
"""

from __future__ import annotations

from typing import Optional

import httpx

from .providers.stls_core_providers_session_resolver import KeySessionResolver
from .stls_core_client_handshake import perform_client_handshake, perform_pqc_client_handshake
from .stls_core_crypto import StlsTransport

_resolvers: dict[str, KeySessionResolver] = {}


def _get_resolver(provider_name: str) -> KeySessionResolver:
    resolver = _resolvers.get(provider_name)
    if resolver is None:
        resolver = KeySessionResolver(provider_name)
        _resolvers[provider_name] = resolver
    return resolver


async def establish_session(
    *,
    provider_name: str,
    base_url: str,
    client_id: str,
    config_path: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    handshake_path: Optional[str] = None,
    httpx_client: Optional[httpx.AsyncClient] = None,
) -> tuple[str, str, StlsTransport]:
    """
    Resolves the session key for `base_url` under `provider_name` and
    performs the STLS handshake, returning a ready StlsTransport — never
    the raw key itself. For "pqc", resolution and the KEM handshake are
    the same step (no pre-shared key, nothing to resolve); `base_url` is
    also the resolver/lock cache key, mirroring every existing call site
    (which always used the same value for both).

    Returns:
        (session_id, kid, transport). For "pqc", kid is set to
        session_id: perform_pqc_client_handshake() deliberately keeps
        its original 2-tuple return shape (session_id, transport) for
        backward compatibility with its existing callers (unlike a
        TypeScript object, a Python tuple can't safely grow an element
        without breaking every positional unpacking call site) and
        doesn't surface the server's separate pqc `kid` — not a problem
        in practice, since no current caller uses `kid` for the pqc
        branch anyway.
    """
    normalized = provider_name.strip().lower()

    if normalized == "pqc":
        kwargs = {"handshake_path": handshake_path} if handshake_path else {}
        session_id, transport = await perform_pqc_client_handshake(
            base_url, client_id=client_id, httpx_client=httpx_client, **kwargs,
        )
        return session_id, session_id, transport

    resolver = _get_resolver(normalized)
    kid, kdata = await resolver.resolve(base_url, host=host, port=port, config_path=config_path)

    kwargs = {"handshake_path": handshake_path} if handshake_path else {}
    session_id, transport = await perform_client_handshake(
        base_url, kdata, client_id=client_id, kid=kid, httpx_client=httpx_client, **kwargs,
    )
    return session_id, kid, transport


async def resolve_transport(
    provider_name: str,
    target_key: str,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    config_path: Optional[str] = None,
) -> tuple[str, StlsTransport]:
    """
    Resolves (kid, StlsTransport) for target_key — NO HTTP handshake.
    For callers (stls-a2a's base/arqit_py path) whose own wire protocol
    doesn't do a handshake round trip and independently trusts both
    sides resolving the same key via the same provider configuration.
    Not valid for "pqc" (which has no such no-handshake mode — it always
    derives its key via the KEM handshake itself; use establish_session()).
    """
    resolver = _get_resolver(provider_name.strip().lower())
    kid, kdata = await resolver.resolve(target_key, host=host, port=port, config_path=config_path)
    return kid, StlsTransport(session_key=kdata, session_id=kid)
