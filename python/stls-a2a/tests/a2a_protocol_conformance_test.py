"""
a2a_protocol_conformance_test.py
---------------------------------
Regression suite for A2A JSON-RPC method-name conformance (issue #14).

A2A 1.0 (spec §9.4) renamed the JSON-RPC methods to PascalCase
(SendMessage, GetTask, CancelTask, ListTasks, SubscribeToTask,
Create/Get/List/DeleteTaskPushNotificationConfig, SendStreamingMessage),
replacing 0.3's slash-style names (messages/send, tasks/get, ...).
stls_a2a_adapters.py normalizes the 1.0 names into their 0.3 equivalents
at a2a_to_agent_request() (sync path) and stls_a2a_integration.py's
SSE_STREAM_METHODS/SSE_SEND_METHODS/SSE_SUBSCRIBE_METHODS recognize both
spellings directly (SSE path).

This suite verifies both ends of that contract against a real
FastAPI app wired exactly as create_secure_a2a_endpoint() builds it,
in-process via httpx.ASGITransport (no server process, no network):

  1. 0.3 names still behave exactly as before (backward-compat guard —
     this suite exists specifically because this must never regress).
  2. 1.0 names produce the same outcome as their 0.3 equivalent.
  3. An unrecognized method name still gets -32601 (the alias map must
     not swallow real errors).

Run directly:
    python a2a_protocol_conformance_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# The agent registry and Redis session cache are irrelevant to this
# suite (plain endpoint only) and must not require real infrastructure.
# Set BEFORE importing stls_a2a: python-dotenv's load_dotenv() (called at
# import time by several stls_a2a modules) won't override a key that's
# already present in os.environ, even if empty — so this sticks.
os.environ.setdefault("KEY_PROVIDER_NAME", "base")
os.environ.setdefault("TASK_STORE_BACKEND", "memory")
os.environ["AUTH_PG_DSN"] = ""

sys.path.insert(0, os.path.dirname(__file__))  # for `import mock_agent`

import httpx
from fastapi import FastAPI

from stls_a2a import (
    AgentResponse,
    KeySessionProvider,
    KeySessionProviderFactory,
    create_secure_a2a_endpoint,
    start_task_store_cleanup,
)
from mock_agent import MockAgent

import logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── Helper output (same convention as client_context_test.py) ──────────────

def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌ {label}{': ' + detail if detail else ''}")


# ── App builder ──────────────────────────────────────────────────────────

# Built once, synchronously, before any event loop starts: the "base"
# provider's get_session_provider() internally does asyncio.run() to
# resolve its start() coroutine, which raises if called from inside a
# running loop (i.e. from within an `async def test_*` function).
# Same ordering multi_agent_server.py relies on (provider created in a
# sync function; the event loop only starts later, inside uvicorn.run()).
_KEY_SESSION_PROVIDER: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=0
)


def _build_app() -> FastAPI:
    """
    A single calculator agent (CustomFunctionAdapter, same fixture used
    by multi_agent_server.py) mounted at the default path, with the
    plain (unencrypted) /a2a + /a2a/stream endpoints enabled — the ones
    meant for standard-A2A-client interop and the target of this suite.
    """
    app = FastAPI(title="A2A conformance test app")
    start_task_store_cleanup(app=app)

    calculator = MockAgent(name="calculator", system_instruction="Performs mathematical calculations")

    async def calculator_handler(request):
        result = await calculator.process_message(request.method, request.params)
        return AgentResponse(result=result, id=request.id)

    create_secure_a2a_endpoint(
        app=app,
        agent=calculator_handler,
        key_session_provider=_KEY_SESSION_PROVIDER,
        path="/secure-a2a",
        framework="custom",
        enable_plain_endpoint=True,
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _rpc(client: httpx.AsyncClient, method: str, params: dict, rpc_id: int = 1) -> dict:
    resp = await client.post(
        "/a2a",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id},
    )
    resp.raise_for_status()
    return resp.json()


async def _sse_events(client: httpx.AsyncClient, method: str, params: dict, rpc_id: int = 1) -> list[dict]:
    """POSTs to /a2a/stream and collects the parsed SSE `result` payloads."""
    events: list[dict] = []
    async with client.stream(
        "POST", "/a2a/stream",
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id},
    ) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            line = line.strip()
            if line.startswith("data:"):
                events.append(json.loads(line[len("data:"):].strip()))
    return events


# ── Test 1: 0.3 names still work (backward-compat guard) ──────────────────

async def test_0_3_names_unchanged() -> bool:
    print("\n── Test 1: A2A 0.3 method names (backward-compat guard) ─────────")
    app = _build_app()
    ok = True
    async with _client(app) as client:
        send = await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        if send.get("result", {}).get("status", {}).get("state") == "completed":
            _ok("messages/send", "task completed")
        else:
            _fail("messages/send", json.dumps(send))
            ok = False
        task_id = send["result"]["taskId"]

        get = await _rpc(client, "tasks/get", {"taskId": task_id})
        if get.get("result", {}).get("id") == task_id:
            _ok("tasks/get")
        else:
            _fail("tasks/get", json.dumps(get))
            ok = False

        lst = await _rpc(client, "tasks/list", {})
        if isinstance(lst.get("result"), list):
            _ok("tasks/list", f"{len(lst['result'])} task(s)")
        else:
            _fail("tasks/list", json.dumps(lst))
            ok = False

        cancel = await _rpc(client, "tasks/cancel", {"taskId": task_id})
        if cancel.get("error", {}).get("code") == -32002:
            _ok("tasks/cancel", "correctly rejected (task already completed)")
        else:
            _fail("tasks/cancel", json.dumps(cancel))
            ok = False

        push = await _rpc(client, "tasks/pushNotificationConfig/set", {})
        if push.get("error", {}).get("code") == -32003:
            _ok("tasks/pushNotificationConfig/set", "correctly reports unsupported")
        else:
            _fail("tasks/pushNotificationConfig/set", json.dumps(push))
            ok = False
    return ok


# ── Test 2: 1.0 names produce the same outcome as their 0.3 equivalent ────

async def test_1_0_names_match_0_3() -> bool:
    print("\n── Test 2: A2A 1.0 method names (spec §9.4) ──────────────────────")
    app = _build_app()
    ok = True
    async with _client(app) as client:
        send = await _rpc(client, "SendMessage", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        if send.get("result", {}).get("status", {}).get("state") == "completed":
            _ok("SendMessage", "-> same outcome as messages/send")
        else:
            _fail("SendMessage", json.dumps(send))
            ok = False
        task_id = send["result"]["taskId"]

        get = await _rpc(client, "GetTask", {"taskId": task_id})
        if get.get("result", {}).get("id") == task_id:
            _ok("GetTask", "-> same outcome as tasks/get")
        else:
            _fail("GetTask", json.dumps(get))
            ok = False

        lst = await _rpc(client, "ListTasks", {})
        if isinstance(lst.get("result"), list):
            _ok("ListTasks", "-> same outcome as tasks/list")
        else:
            _fail("ListTasks", json.dumps(lst))
            ok = False

        cancel = await _rpc(client, "CancelTask", {"taskId": task_id})
        if cancel.get("error", {}).get("code") == -32002:
            _ok("CancelTask", "-> same outcome as tasks/cancel")
        else:
            _fail("CancelTask", json.dumps(cancel))
            ok = False

        for method in (
            "CreateTaskPushNotificationConfig",
            "GetTaskPushNotificationConfig",
            "ListTaskPushNotificationConfigs",
            "DeleteTaskPushNotificationConfig",
        ):
            resp = await _rpc(client, method, {})
            if resp.get("error", {}).get("code") == -32003:
                _ok(method, "-> correctly reports unsupported (not -32601)")
            else:
                _fail(method, json.dumps(resp))
                ok = False
    return ok


# ── Test 3: SSE — 1.0 streaming names match their 0.3 equivalent ──────────

async def test_1_0_sse_names() -> bool:
    print("\n── Test 3: A2A 1.0 SSE method names ──────────────────────────────")
    app = _build_app()
    ok = True
    async with _client(app) as client:
        events = await _sse_events(
            client, "SendStreamingMessage",
            {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}, "expression": "add(2,3)"},
        )
        states = [e["result"]["status"]["state"] for e in events if e["result"]["kind"] == "status-update"]
        if states == ["submitted", "working", "completed"]:
            _ok("SendStreamingMessage", f"lifecycle={states}")
        else:
            _fail("SendStreamingMessage", f"lifecycle={states}")
            ok = False
        task_id = next(e["result"]["taskId"] for e in events)

        sub_events = await _sse_events(client, "SubscribeToTask", {"taskId": task_id})
        sub_states = [e["result"]["status"]["state"] for e in sub_events if e["result"]["kind"] == "status-update"]
        if sub_states == ["completed"]:
            _ok("SubscribeToTask", f"on completed task -> immediate {sub_states}")
        else:
            _fail("SubscribeToTask", f"lifecycle={sub_states}")
            ok = False
    return ok


# ── Test 4: unknown methods are still rejected (no over-broad aliasing) ───

async def test_unknown_method_still_rejected() -> bool:
    print("\n── Test 4: unrecognized method name guard ────────────────────────")
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "TotallyMadeUpMethod", {})
        if resp.get("error", {}).get("code") == -32601:
            _ok("TotallyMadeUpMethod", "-32601 as expected")
            return True
        _fail("TotallyMadeUpMethod", json.dumps(resp))
        return False


async def main() -> None:
    print("\n" + "=" * 70)
    print("A2A Protocol Method-Name Conformance (issue #14)")
    print("=" * 70)

    results: dict[str, bool] = {}
    results["1 — 0.3 names unchanged"]      = await test_0_3_names_unchanged()
    results["2 — 1.0 names == 0.3 outcome"] = await test_1_0_names_match_0_3()
    results["3 — 1.0 SSE names"]            = await test_1_0_sse_names()
    results["4 — unknown method guard"]     = await test_unknown_method_still_rejected()

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    all_ok = True
    for label, ok in results.items():
        print(f"  {'✅' if ok else '❌'}  Test {label}")
        all_ok = all_ok and ok

    print()
    print("  🎉 All tests passed." if all_ok else "  ⚠️  Some tests failed — check the logs above.")
    print("=" * 70 + "\n")

    if not all_ok:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
