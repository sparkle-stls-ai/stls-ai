"""
test_stls_mcp_endpoint_toggles.py
-----------------------------------
Regression suite for issue #80: create_secure_mcp_app()'s
enable_plain_endpoint / enable_secure_endpoint toggles — the stls-mcp
counterpart of python/stls-a2a/tests/endpoint_toggles_test.py.

Covers:
  1. enable_secure_endpoint=False: {stls_path} isn't reachable at all
     (not even the handshake endpoint); {plain_path} still works.
  2. enable_plain_endpoint=False: {plain_path} isn't reachable;
     {stls_path}'s handshake endpoint still is.
  3. Both False: create_secure_mcp_app() raises ValueError at setup
     time instead of silently registering nothing.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from stls_core import KeySessionProvider, KeySessionProviderFactory
from stls_mcp import create_secure_mcp_app

# Same ordering rationale as the identity-presentation suite: built
# once, synchronously, before any event loop starts.
_KEY_SESSION_PROVIDER: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=0, name="base",
)


def _build_mcp() -> FastMCP:
    mcp = FastMCP("toggles-test", stateless_http=True, json_response=True)

    @mcp.tool()
    def ping() -> str:
        return "pong"

    return mcp


def test_both_disabled_raises_value_error():
    with pytest.raises(ValueError, match="enable_plain_endpoint.*enable_secure_endpoint"):
        create_secure_mcp_app(
            _build_mcp(),
            _KEY_SESSION_PROVIDER,
            enable_plain_endpoint=False,
            enable_secure_endpoint=False,
        )


@pytest.mark.asyncio
async def test_secure_disabled_plain_only():
    app = create_secure_mcp_app(
        _build_mcp(),
        _KEY_SESSION_PROVIDER,
        enable_plain_endpoint=True,
        enable_secure_endpoint=False,
    )
    # raise_app_exceptions=False: the plain /mcp route IS mounted (what
    # this test is checking) even though a real round trip through it
    # needs the FastMCP session manager's lifespan started (uvicorn
    # does this normally; this test only cares about route presence,
    # not a full protocol handshake) — without this, httpx re-raises
    # the resulting "Task group is not initialized" RuntimeError
    # instead of giving back a response to assert on.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Plain endpoint route exists (reached the SDK's own handler,
        # whatever it then does with an unstarted session manager).
        resp = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1},
            headers={"Accept": "application/json, text/event-stream"},
        )
        assert resp.status_code != 404

        # Secure endpoint and the handshake endpoint are both gone.
        resp = await client.post("/stls/handshake", json={})
        assert resp.status_code == 404
        resp = await client.post("/mcp-stls", json={})
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plain_disabled_secure_only():
    app = create_secure_mcp_app(
        _build_mcp(),
        _KEY_SESSION_PROVIDER,
        enable_plain_endpoint=False,
        enable_secure_endpoint=True,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Plain endpoint gone.
        resp = await client.post("/mcp", json={})
        assert resp.status_code == 404

        # Handshake endpoint (part of the secure surface) still there —
        # a malformed body still gets a structured JSON-RPC error, not 404.
        resp = await client.post("/stls/handshake", json={"bad": "request"})
        assert resp.status_code != 404
