"""
test_stls_mcp_transport_retry.py
---------------------------------
Pytest unit coverage for the client-side retry-on-expiry added to
StlsHttpxTransport (issue #6, second increment). Previously stls-mcp had
no automatic recovery at all on session expiry (idle-TTL or the new
absolute max-age from stls-core 0.1.7) — unlike stls-a2a's
STLSHttpxTransport, which already had retry-once.

Uses httpx.ASGITransport to run StlsMcpMiddleware (the REAL server-side
code, not a hand-crafted mock of its JSON shape) as the "inner"
transport, so these tests prove the client and server sides actually
agree on the wire format, not just that each half does what this file
assumes the other half does.

Coverage:
  C01 — happy path: no expiry, no retry attempted, request succeeds
  C02 — session expired (cache evicted) -> ERR_SESSION_EXPIRED 401 ->
        rehandshake callback invoked once -> retried request succeeds
        under the NEW session_id/transport
  C03 — retry-once only: if the retried request ALSO comes back expired
        (rehandshake callback doesn't actually fix the cache), the
        second 401 is returned to the caller, no infinite loop
  C04 — rehandshake callback itself raises -> propagates immediately,
        no retry attempted at all
  C05 — no rehandshake callback configured (default) -> behavior
        unchanged from before this feature existed: the 401 is just
        returned, exactly like test C03's "give up" path
  C06 — _looks_like_session_expired: a 401 NOT carrying
        ERR_SESSION_EXPIRED (e.g. an auth-layer rejection) must NOT be
        treated as a retry trigger
"""

from __future__ import annotations

import json

import httpx
import pytest

from stls_core.stls_core_session import InMemorySessionCache
from stls_core.stls_core_crypto import StlsTransport

from stls_mcp.stls_mcp_constants import ERR_SESSION_EXPIRED, MCP_PLAIN_PATH
from stls_mcp.stls_mcp_middleware import StlsMcpMiddleware
from stls_mcp.stls_mcp_transport import StlsHttpxTransport

KEY1 = b"\x01" * 32
KEY2 = b"\x02" * 32
SID1 = "session-old-aaaa"
SID2 = "session-new-bbbb"
KID1 = "kid-aaa"
KID2 = "kid-bbb"


async def _echo_plain_app(scope, receive, send):
    """Minimal ASGI app standing in for the real MCP app: echoes the
    request body back as the response, wrapped as a JSON-RPC result."""
    assert scope["path"] == MCP_PLAIN_PATH
    body = b""
    more_body = True
    while more_body:
        message = await receive()
        body += message.get("body", b"")
        more_body = message.get("more_body", False)

    payload = json.loads(body) if body else {}
    response_body = json.dumps({"jsonrpc": "2.0", "result": payload, "id": payload.get("id")}).encode()

    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": response_body, "more_body": False})


def _request(payload: dict) -> httpx.Request:
    return httpx.Request(
        "POST",
        "http://testserver/mcp-stls",
        content=json.dumps(payload).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_c01_happy_path_no_retry():
    """C01 — no expiry: request succeeds without ever invoking rehandshake."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    transport = StlsTransport(session_key=KEY1, session_id=SID1)

    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    inner = httpx.ASGITransport(app=middleware)

    rehandshake_calls = 0

    async def _rehandshake():
        nonlocal rehandshake_calls
        rehandshake_calls += 1
        raise AssertionError("should not be called on the happy path")

    client_transport = StlsHttpxTransport(
        SID1, transport, inner_transport=inner, rehandshake=_rehandshake,
    )

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 200
    # handle_async_request() already returns the decrypted plaintext
    # (transparent to the MCP SDK layer above it) — no decrypt_json() here.
    decoded = json.loads(response.content)
    assert decoded["result"]["method"] == "ping"
    assert rehandshake_calls == 0


@pytest.mark.asyncio
async def test_c02_expired_session_retried_successfully():
    """C02 — expired session -> rehandshake -> retry succeeds under the new session."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    # Deliberately NOT put into the cache: simulates an already-expired
    # (idle-TTL or max-age) session the client doesn't know about yet.
    old_transport = StlsTransport(session_key=KEY1, session_id=SID1)

    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    inner = httpx.ASGITransport(app=middleware)

    rehandshake_calls = 0

    async def _rehandshake():
        nonlocal rehandshake_calls
        rehandshake_calls += 1
        new_transport = StlsTransport(session_key=KEY2, session_id=SID2)
        cache.put(SID2, KID2, KEY2)  # what a real handshake does server-side
        return SID2, new_transport

    client_transport = StlsHttpxTransport(
        SID1, old_transport, inner_transport=inner, rehandshake=_rehandshake,
    )

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 200
    assert rehandshake_calls == 1
    # The transport's internal state was swapped to the new session.
    assert client_transport._session_id == SID2
    # Already decrypted by handle_async_request() under the NEW session.
    decoded = json.loads(response.content)
    assert decoded["result"]["method"] == "ping"


@pytest.mark.asyncio
async def test_c03_retry_once_only_then_gives_up():
    """C03 — if the retried request is ALSO expired, give up: no second retry."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    old_transport = StlsTransport(session_key=KEY1, session_id=SID1)

    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    inner = httpx.ASGITransport(app=middleware)

    rehandshake_calls = 0

    async def _rehandshake():
        nonlocal rehandshake_calls
        rehandshake_calls += 1
        # Returns a session that is STILL not in the cache -> the retried
        # request will ALSO come back as ERR_SESSION_EXPIRED.
        return SID2, StlsTransport(session_key=KEY2, session_id=SID2)

    client_transport = StlsHttpxTransport(
        SID1, old_transport, inner_transport=inner, rehandshake=_rehandshake,
    )

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 401
    # Exactly one retry attempt — not an infinite loop.
    assert rehandshake_calls == 1


@pytest.mark.asyncio
async def test_c04_rehandshake_failure_propagates_without_retry():
    """C04 — the rehandshake callback itself raising propagates immediately."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    old_transport = StlsTransport(session_key=KEY1, session_id=SID1)

    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    inner = httpx.ASGITransport(app=middleware)

    async def _rehandshake():
        raise RuntimeError("handshake endpoint unreachable")

    client_transport = StlsHttpxTransport(
        SID1, old_transport, inner_transport=inner, rehandshake=_rehandshake,
    )

    with pytest.raises(RuntimeError, match="handshake endpoint unreachable"):
        await client_transport.handle_async_request(
            _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
        )


@pytest.mark.asyncio
async def test_c05_no_rehandshake_configured_behaves_like_before():
    """C05 — rehandshake=None (default): unchanged behavior, no retry attempted."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    old_transport = StlsTransport(session_key=KEY1, session_id=SID1)

    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    inner = httpx.ASGITransport(app=middleware)

    client_transport = StlsHttpxTransport(SID1, old_transport, inner_transport=inner)

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 401


def test_c06_looks_like_session_expired_distinguishes_auth_401():
    """C06 — a 401 without ERR_SESSION_EXPIRED must NOT look like an expired session."""
    from stls_mcp.stls_mcp_transport import _looks_like_session_expired

    expired = httpx.Response(
        401, json={"jsonrpc": "2.0", "error": {"code": ERR_SESSION_EXPIRED, "message": "x"}, "id": None},
    )
    assert _looks_like_session_expired(expired) is True

    auth_failure = httpx.Response(
        401, json={"jsonrpc": "2.0", "error": {"code": -32600, "message": "invalid api key"}, "id": None},
    )
    assert _looks_like_session_expired(auth_failure) is False

    not_json = httpx.Response(401, content=b"plain text, not json")
    assert _looks_like_session_expired(not_json) is False
