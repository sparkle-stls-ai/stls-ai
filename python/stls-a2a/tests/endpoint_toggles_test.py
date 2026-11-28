"""
endpoint_toggles_test.py
-------------------------
Regression suite for issue #80: create_secure_a2a_endpoint()'s
enable_plain_endpoint / enable_secure_endpoint toggles.

enable_plain_endpoint already existed; enable_secure_endpoint is new
and symmetric. Covers:

  1. enable_secure_endpoint=False: /secure-a2a (+ /stream, +
     /capabilities) isn't registered at all; the plain /a2a endpoint
     still works normally.
  2. enable_plain_endpoint=False (pre-existing behavior, asserted here
     too for symmetry): /a2a isn't registered; /secure-a2a still works.
  3. Both False: create_secure_a2a_endpoint() raises ValueError at
     setup time instead of silently registering nothing.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from stls_a2a import (
    AgentResponse,
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
)
from mock_agent import MockAgent

# Same ordering rationale as a2a_protocol_conformance_test.py: built
# once, synchronously, before any event loop starts.
_KEY_SESSION_PROVIDER: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=0
)


def _calculator_handler():
    calculator = MockAgent(name="calculator", system_instruction="Performs mathematical calculations")

    async def handler(request):
        result = await calculator.process_message(request.method, request.params)
        return AgentResponse(result=result, id=request.id)

    return handler


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_both_disabled_raises_value_error():
    app = FastAPI(title="endpoint toggles test app")
    start_task_store_cleanup(app=app)

    with pytest.raises(ValueError, match="enable_plain_endpoint.*enable_secure_endpoint"):
        create_secure_a2a_endpoint(
            app=app,
            agent=_calculator_handler(),
            key_session_provider=_KEY_SESSION_PROVIDER,
            path="/secure-a2a",
            framework="custom",
            enable_plain_endpoint=False,
            enable_secure_endpoint=False,
        )


@pytest.mark.asyncio
async def test_secure_disabled_plain_only():
    app = FastAPI(title="endpoint toggles test app")
    start_task_store_cleanup(app=app)

    create_secure_a2a_endpoint(
        app=app,
        agent=_calculator_handler(),
        key_session_provider=_KEY_SESSION_PROVIDER,
        path="/secure-a2a",
        framework="custom",
        enable_plain_endpoint=True,
        enable_secure_endpoint=False,
    )

    async with _client(app) as client:
        # Plain endpoint still works.
        resp = await client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "method": "SendMessage", "params": {
                "message": {"role": "user", "parts": [{"text": "2+2"}]},
            }, "id": 1},
        )
        resp.raise_for_status()
        assert "result" in resp.json()

        # Secure endpoint, its stream, and its capabilities probe are
        # all gone — not registered at all.
        for path in ("/secure-a2a", "/secure-a2a/stream"):
            resp = await client.post(path, json={})
            assert resp.status_code == 404, f"{path} should be unregistered"
        resp = await client.get("/secure-a2a/capabilities")
        assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plain_disabled_secure_only():
    app = FastAPI(title="endpoint toggles test app")
    start_task_store_cleanup(app=app)

    create_secure_a2a_endpoint(
        app=app,
        agent=_calculator_handler(),
        key_session_provider=_KEY_SESSION_PROVIDER,
        path="/secure-a2a",
        framework="custom",
        enable_plain_endpoint=False,
        enable_secure_endpoint=True,
    )

    async with _client(app) as client:
        # Plain endpoint is gone.
        resp = await client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "method": "SendMessage", "params": {}, "id": 1},
        )
        assert resp.status_code == 404

        # Secure endpoint's capabilities probe (no handshake needed)
        # still responds — confirms the route IS registered.
        resp = await client.get("/secure-a2a/capabilities")
        assert resp.status_code == 200
