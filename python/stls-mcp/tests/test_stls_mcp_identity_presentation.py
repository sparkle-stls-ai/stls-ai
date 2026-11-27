"""
test_stls_mcp_identity_presentation.py
-----------------------------------------
Regression suite for issue #46: the DID/VC identity-presentation hook
(issue #15 gap #1) brought to stls-mcp's _AuthGateASGI/create_secure_mcp_app
(`identity_verifier=`) and StlsHttpxTransport (`identity_challenge_handler=`).
Ported from python/stls-a2a/tests/identity_presentation_test.py, adapted
to stls-mcp's shape: the real _AuthGateASGI wrapping the real
StlsMcpMiddleware via httpx.ASGITransport (same "real server code, not
a hand-crafted mock" style as test_stls_mcp_transport_retry.py), keyed
off stls-core's own HEADER_SESSION_ID rather than a2a's custom
X-A2A-Session-ID.

This suite does NOT exercise real stls-identity crypto (Sentinel,
VerifiablePresentation) — that lives in
python/stls-identity/tests/test_request_verifier.py. Here
identity_verifier is a minimal fake duck-typed object, exactly the
shape _AuthGateASGI itself expects (`async def verify_request(request,
*, challenge) -> result` with `.ok`/`.error`) — proving the hook's
wiring and off-by-default guarantee, independent of any particular
identity backend.

Coverage:
  1. No identity_verifier configured (the default) -> the identity
     check never fires, confirmed by the absence of
     ERR_IDENTITY_CHALLENGE_REQUIRED even on an otherwise-failing
     request (no session established either).
  2. identity_verifier configured, no X-STLS-Identity-Presentation
     header yet -> 401 + ERR_IDENTITY_CHALLENGE_REQUIRED (-32005) + a
     challenge.
  3. Same session, header now present, fake verifier returns ok -> the
     identity check passes and the request reaches StlsMcpMiddleware
     (which then succeeds, since this test uses a real established
     session).
  4. Same flow, fake verifier returns not-ok -> 401, distinct error
     code from the challenge-required one.
  5. identity_verifier configured ALONE (no api_key_manager/auth) still
     engages the gate -> regression test for the _gate_enabled() fix
     this port needed (the pre-existing check only looked at
     api_key_manager/auth).
  6. api_key_manager success still falls through to the identity check
     cleanly when identity_verifier is None (no behavior change to the
     pre-existing success path).
  7. StlsHttpxTransport's client-side retry-once: a fake inner app
     (real _AuthGateASGI + real StlsMcpMiddleware, via
     httpx.ASGITransport) issues the challenge-required shape exactly
     once, identity_challenge_handler is called with the right
     challenge, and the retried call succeeds under a real established
     session — mirrors ERR_SESSION_EXPIRED's own retry-once coverage in
     test_stls_mcp_transport_retry.py.
"""

from __future__ import annotations

import json

import httpx
import pytest

from stls_core.stls_core_session import InMemorySessionCache
from stls_core.stls_core_crypto import StlsTransport

from stls_mcp.stls_mcp_auth import wrap_with_auth
from stls_mcp.stls_mcp_constants import ERR_IDENTITY_CHALLENGE_REQUIRED, MCP_PLAIN_PATH
from stls_mcp.stls_mcp_middleware import StlsMcpMiddleware
from stls_mcp.stls_mcp_transport import StlsHttpxTransport

KEY1 = b"\x01" * 32
SID1 = "session-identity-aaaa"
KID1 = "kid-identity-aaa"


class FakeIdentityVerifier:
    """Minimal duck-typed stand-in for stls-identity's bridge — only
    what _AuthGateASGI._check_identity_async() actually calls."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[str] = []

    async def verify_request(self, request, *, challenge: str):
        self.calls.append(challenge)

        class _Result:
            pass

        r = _Result()
        r.ok = self.ok
        r.error = None if self.ok else "fake rejection"
        return r


class _FakeApiKeyManager:
    async def validate_request(self, request):
        class _Result:
            ok = True
        return _Result()


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


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _error_info(body: dict) -> tuple:
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("message", "")
    return None, str(error)


@pytest.mark.asyncio
async def test_off_by_default():
    middleware = StlsMcpMiddleware(_echo_plain_app, InMemorySessionCache(max_size=10, ttl_seconds=60))
    app = wrap_with_auth(middleware, identity_verifier=None)
    async with _client(app) as client:
        resp = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": "unknown-session"},
        )
        code, _ = _error_info(resp.json())
        assert code != ERR_IDENTITY_CHALLENGE_REQUIRED, resp.text


@pytest.mark.asyncio
async def test_challenge_required():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=True)
    app = wrap_with_auth(middleware, identity_verifier=verifier)

    async with _client(app) as client:
        resp = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": SID1},
        )
        body = resp.json()
        error = body.get("error", {})
        assert resp.status_code == 401
        assert error.get("code") == ERR_IDENTITY_CHALLENGE_REQUIRED
        assert error.get("challenge")
        # Challenge issuance doesn't need the verifier at all.
        assert verifier.calls == []


@pytest.mark.asyncio
async def test_accepted_presentation_passes_identity_check():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=True)
    app = wrap_with_auth(middleware, identity_verifier=verifier)
    transport = StlsTransport(session_key=KEY1, session_id=SID1)

    async with _client(app) as client:
        # First call: get the challenge for this session.
        resp1 = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": SID1},
        )
        challenge = resp1.json()["error"]["challenge"]

        # Second call: present (a fake) VP header AND a real encrypted
        # body (the identity check runs before decryption, but the
        # request must still be well-formed for StlsMcpMiddleware to
        # succeed once the identity check passes).
        envelope = transport.encrypt_json({"jsonrpc": "2.0", "method": "ping", "id": 1})
        resp2 = await client.post(
            "/mcp-stls",
            json=envelope,
            headers={
                "X-STLS-Session-ID": SID1,
                "X-STLS-Identity-Presentation": "ZmFrZS12cA==",
            },
        )
        assert resp2.status_code == 200
        assert verifier.calls == [challenge]


@pytest.mark.asyncio
async def test_rejected_presentation_is_denied():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=False)
    app = wrap_with_auth(middleware, identity_verifier=verifier)

    async with _client(app) as client:
        resp1 = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": SID1},
        )
        assert resp1.json()["error"]["challenge"]

        resp2 = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={
                "X-STLS-Session-ID": SID1,
                "X-STLS-Identity-Presentation": "dGFtcGVyZWQ=",
            },
        )
        body2 = resp2.json()
        error2 = body2.get("error", {})
        assert resp2.status_code == 401
        assert error2.get("code") != ERR_IDENTITY_CHALLENGE_REQUIRED
        assert "identity" in error2.get("message", "")


@pytest.mark.asyncio
async def test_identity_verifier_alone_engages_the_gate():
    """Regression for the _gate_enabled() fix: before #46, the gate only
    engaged for api_key_manager/auth — an identity_verifier configured
    with neither would never even reach _check(), so the identity check
    could never fire. Confirmed here by observing a challenge actually
    gets issued with identity_verifier as the ONLY configured check."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=True)
    app = wrap_with_auth(middleware, auth=None, api_key_manager=None, identity_verifier=verifier)

    async with _client(app) as client:
        resp = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": SID1},
        )
        body = resp.json()
        assert resp.status_code == 401
        assert body["error"]["code"] == ERR_IDENTITY_CHALLENGE_REQUIRED


@pytest.mark.asyncio
async def test_api_key_manager_success_still_falls_through_cleanly():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    app = wrap_with_auth(middleware, api_key_manager=_FakeApiKeyManager(), identity_verifier=None)

    async with _client(app) as client:
        resp = await client.post(
            "/mcp-stls",
            json={"jsonrpc": "2.0", "method": "ping", "id": 1},
            headers={"X-STLS-Session-ID": "sess-km"},
        )
        code, _ = _error_info(resp.json())
        assert code != ERR_IDENTITY_CHALLENGE_REQUIRED, resp.text


# ---------------------------------------------------------------------------
# StlsHttpxTransport client-side retry-once
# ---------------------------------------------------------------------------


def _request(payload: dict) -> httpx.Request:
    return httpx.Request(
        "POST",
        "http://testserver/mcp-stls",
        content=json.dumps(payload).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_client_retries_once_on_identity_challenge():
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=True)
    app = wrap_with_auth(middleware, identity_verifier=verifier)
    inner = httpx.ASGITransport(app=app)

    transport = StlsTransport(session_key=KEY1, session_id=SID1)
    seen_challenges: list[str] = []

    def handler(challenge: str) -> dict:
        seen_challenges.append(challenge)
        return {"X-STLS-Identity-Presentation": "ZmFrZS12cA=="}

    client_transport = StlsHttpxTransport(
        SID1, transport, inner_transport=inner, identity_challenge_handler=handler,
    )

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )

    assert response.status_code == 200
    assert len(seen_challenges) == 1
    assert verifier.calls == seen_challenges
    decoded = json.loads(response.content)
    assert decoded["result"]["method"] == "ping"


@pytest.mark.asyncio
async def test_client_no_handler_configured_gives_up_like_before():
    """identity_challenge_handler=None (default): unchanged behavior,
    the 401 just propagates — same posture as rehandshake=None in
    test_stls_mcp_transport_retry.py's C05."""
    cache = InMemorySessionCache(max_size=10, ttl_seconds=60)
    cache.put(SID1, KID1, KEY1)
    middleware = StlsMcpMiddleware(_echo_plain_app, cache)
    verifier = FakeIdentityVerifier(ok=True)
    app = wrap_with_auth(middleware, identity_verifier=verifier)
    inner = httpx.ASGITransport(app=app)

    transport = StlsTransport(session_key=KEY1, session_id=SID1)
    client_transport = StlsHttpxTransport(SID1, transport, inner_transport=inner)

    response = await client_transport.handle_async_request(
        _request({"jsonrpc": "2.0", "method": "ping", "id": 1})
    )
    assert response.status_code == 401
    assert verifier.calls == []


def test_looks_like_identity_challenge_required_distinguishes_session_expired():
    from stls_mcp.stls_mcp_constants import ERR_SESSION_EXPIRED
    from stls_mcp.stls_mcp_transport import _looks_like_identity_challenge_required

    challenge_required = httpx.Response(
        401,
        json={
            "jsonrpc": "2.0",
            "error": {"code": ERR_IDENTITY_CHALLENGE_REQUIRED, "message": "x", "challenge": "chal-1"},
            "id": None,
        },
    )
    assert _looks_like_identity_challenge_required(challenge_required) == "chal-1"

    session_expired = httpx.Response(
        401, json={"jsonrpc": "2.0", "error": {"code": ERR_SESSION_EXPIRED, "message": "x"}, "id": None},
    )
    assert _looks_like_identity_challenge_required(session_expired) is None

    not_json = httpx.Response(401, content=b"plain text, not json")
    assert _looks_like_identity_challenge_required(not_json) is None
