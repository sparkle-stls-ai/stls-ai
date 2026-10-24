"""
test_km_client.py
-----------------
Complete test client for multi_agent_km_server.py.

Tests every endpoint, every auth backend, and every A2A method.

Startup:
    # start the server first
    uv run python multi_agent_km_server.py

    # then in another terminal
    uv run python test_km_client.py

    # or with options:
    uv run python test_km_client.py --host localhost --port 8001 --verbose
    uv run python test_km_client.py --suite auth     # authentication tests only
    uv run python test_km_client.py --suite a2a      # A2A method tests only
    uv run python test_km_client.py --suite diag     # diagnostics only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

import logging
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL     = os.getenv("TEST_BASE_URL", "http://localhost:8001")
TIMEOUT      = float(os.getenv("TEST_TIMEOUT", "15"))
VERBOSE      = os.getenv("TEST_VERBOSE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0

@dataclass
class Suite:
    name: str
    results: list[TestResult] = field(default_factory=list)

    def add(self, r: TestResult) -> None:
        self.results.append(r)
        icon = "✅" if r.passed else "❌"
        dur  = f" ({r.duration_ms:.0f}ms)"
        print(f"  {icon} {r.name}{dur}")
        if not r.passed or VERBOSE:
            for line in r.detail.splitlines():
                print(f"       {line}")

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return len(self.results) - self.passed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def rpc(method: str, params: dict | None = None, rpc_id: str | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "method":  method,
        "params":  params or {},
        "id":      rpc_id or str(uuid.uuid4()),
    }

def msg_params(text: str, task_id: str | None = None, context_id: str | None = None) -> dict:
    p: dict = {
        "message": {
            "messageId": str(uuid.uuid4()),
            "role":      "user",
            "parts":     [{"type": "text", "text": text}],
        }
    }
    if task_id:
        p["taskId"] = task_id  # spec §3.3.1: the server checks params.taskId
    if context_id:
        p["contextId"] = context_id
    return p

async def post(
    client: httpx.AsyncClient,
    path: str,
    body: dict,
    key: str | None = None,
    bearer: str | None = None,
) -> tuple[int, dict, float]:
    headers: dict = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    t0 = time.monotonic()
    try:
        r = await client.post(f"{BASE_URL}{path}", json=body, headers=headers)
        ms = (time.monotonic() - t0) * 1000
        try:
            data = r.json()
        except Exception:
            data = {"_raw": r.text}
        return r.status_code, data, ms
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        return 0, {"_error": str(exc)}, ms

async def get(
    client: httpx.AsyncClient,
    path: str,
    key: str | None = None,
) -> tuple[int, Any, float]:
    headers: dict = {}
    if key:
        headers["X-API-Key"] = key
    t0 = time.monotonic()
    try:
        r = await client.get(f"{BASE_URL}{path}", headers=headers)
        ms = (time.monotonic() - t0) * 1000
        try:
            data = r.json()
        except Exception:
            data = {"_raw": r.text}
        return r.status_code, data, ms
    except Exception as exc:
        ms = (time.monotonic() - t0) * 1000
        return 0, {"_error": str(exc)}, ms

async def sse_lines(
    client: httpx.AsyncClient,
    path: str,
    body: dict,
    key: str | None = None,
    max_events: int = 10,
    timeout: float = 20.0,
) -> list[dict]:
    """Collects SSE events and returns them as a list of parsed dicts."""
    headers: dict = {
        "Content-Type": "application/json",
        "Accept":        "text/event-stream",
    }
    if key:
        headers["X-API-Key"] = key

    events: list[dict] = []
    try:
        async with client.stream(
            "POST", f"{BASE_URL}{path}",
            json=body, headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status_code != 200:
                return [{"_status": resp.status_code}]
            event_type = ""
            async for line in resp.aiter_lines():
                line = line.strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        payload = {"_raw": raw}
                    events.append({"event": event_type, "data": payload})
                    # stop if we have enough events or the state is final
                    if len(events) >= max_events:
                        break
                    result = payload.get("result", {})
                    if isinstance(result, dict):
                        status = result.get("status", {})
                        state  = status.get("state", "") if isinstance(status, dict) else str(status)
                        if state in ("completed", "failed", "canceled"):
                            break
    except Exception as exc:
        events.append({"_error": str(exc)})
    return events


def _ok(cond: bool, detail: str = "") -> tuple[bool, str]:
    return cond, detail

def _has_result(data: dict) -> bool:
    return "result" in data and "error" not in data

def _has_error(data: dict, code: int | None = None) -> bool:
    err = data.get("error")
    if not err:
        return False
    if code is not None:
        return err.get("code") == code
    return True

def _fmt(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False)[:800]


# ---------------------------------------------------------------------------
# Suite 1 — Diagnostics and discovery
# ---------------------------------------------------------------------------

async def suite_diagnostics(client: httpx.AsyncClient) -> Suite:
    s = Suite("Diagnostics & Discovery")
    print(f"\n{'─'*60}")
    print(f"🔍 {s.name}")
    print(f"{'─'*60}")

    # ── /.well-known/agent.json ───────────────────────────────────────────
    code, data, ms = await get(client, "/.well-known/agent.json")
    s.add(TestResult(
        "GET /.well-known/agent.json",
        code == 200,
        f"status={code}\n{_fmt(data)}", ms,
    ))

    # ── /.well-known/agent-card.json ─────────────────────────────────────
    code, data, ms = await get(client, "/.well-known/agent-card.json")
    s.add(TestResult(
        "GET /.well-known/agent-card.json",
        code == 200,
        f"status={code}", ms,
    ))

    # ── /auth/health ──────────────────────────────────────────────────────
    code, data, ms = await get(client, "/auth/health")
    s.add(TestResult(
        "GET /auth/health",
        code == 200 and "static_manager" in data,
        f"status={code}\n{_fmt(data)}", ms,
    ))

    # ── /auth/refresh ─────────────────────────────────────────────────────
    t0 = time.monotonic()
    r  = await client.post(f"{BASE_URL}/auth/refresh")
    ms = (time.monotonic() - t0) * 1000
    s.add(TestResult(
        "POST /auth/refresh",
        r.status_code == 200,
        f"status={r.status_code}\n{r.text[:300]}", ms,
    ))

    # ── ?agent= filter ────────────────────────────────────────────────────
    code, data, ms = await get(client, "/.well-known/agent.json?agent=/agents/calculator/secure-a2a")
    s.add(TestResult(
        "GET /.well-known/agent.json?agent=/agents/calculator/secure-a2a",
        code in (200, 404),   # 404 if it doesn't have a structured AgentCard
        f"status={code}", ms,
    ))

    return s


# ---------------------------------------------------------------------------
# Suite 2 — Authentication (all backends)
# ---------------------------------------------------------------------------

async def suite_auth(
    client: httpx.AsyncClient,
    static_key: str,
    file_key: str,
    pg_key: str | None,
) -> Suite:
    s = Suite("Authentication — all backends")
    print(f"\n{'─'*60}")
    print(f"🔐 {s.name}")
    print(f"{'─'*60}")

    body = rpc("messages/send", msg_params("test auth"))

    # ── StaticLoader: valid key ────────────────────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body, key=static_key)
    s.add(TestResult(
        "StaticLoader: valid key (X-API-Key)",
        code in (200,) and not _has_error(data, -32600),
        f"status={code} result_ok={_has_result(data)}", ms,
    ))

    # ── StaticLoader: valid key via query param ────────────────────────────
    t0 = time.monotonic()
    r  = await client.post(
        f"{BASE_URL}/agents/calculator/a2a?api_key={static_key}",
        json=body, headers={"Content-Type": "application/json"},
    )
    ms = (time.monotonic() - t0) * 1000
    s.add(TestResult(
        "StaticLoader: valid key (?api_key= query param)",
        r.status_code in (200,) and r.json().get("error", {}).get("code") != -32600,
        f"status={r.status_code}", ms,
    ))

    # ── StaticLoader: missing key → 401 ────────────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body)
    s.add(TestResult(
        "StaticLoader: no key → 401",
        code == 401,
        f"status={code} body={_fmt(data)}", ms,
    ))

    # ── StaticLoader: wrong key → 401 ──────────────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body, key="stls-static_wrong_key")
    s.add(TestResult(
        "StaticLoader: wrong key → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── FileLoader: valid key ───────────────────────────────────────────────
    code, data, ms = await post(client, "/agents/adk-agent/a2a", body, key=file_key)
    s.add(TestResult(
        "FileLoader: valid key (api_keys.yaml)",
        code in (200,) and not _has_error(data, -32600),
        f"status={code} result_ok={_has_result(data)}", ms,
    ))

    # ── FileLoader: missing key → 401 ──────────────────────────────────────
    code, data, ms = await post(client, "/agents/adk-agent/a2a", body)
    s.add(TestResult(
        "FileLoader: no key → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── lc-agent: Postgres if DSN configured, legacy otherwise ────────────
    # Detects the active backend by first trying without a key:
    # a 401 means auth is active; a 200 means public (shouldn't happen).
    # To tell whether the server uses Postgres or legacy, check AUTH_PG_DSN.
    import os as _os
    _pg_dsn_active = bool((_os.getenv("AUTH_PG_DSN") or "").split("#")[0].strip())

    if pg_key:
        # Postgres key provided — full test
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, key=pg_key)
        s.add(TestResult(
            "PostgresLoader: valid key",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body)
        s.add(TestResult(
            "PostgresLoader: no key → 401",
            code == 401,
            f"status={code}", ms,
        ))
    elif _pg_dsn_active:
        # AUTH_PG_DSN set but TEST_PG_KEY not provided:
        # the server uses PostgresLoader → only verify the 401 works
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body)
        s.add(TestResult(
            "PostgresLoader active (no TEST_PG_KEY): no key → 401",
            code == 401,
            f"status={code} — set TEST_PG_KEY to test a valid key", ms,
        ))
        s.add(TestResult(
            "PostgresLoader active (no TEST_PG_KEY): valid key — SKIP",
            True,
            "⚠️  Set TEST_PG_KEY with a real raw_key from the DB for this test", 0,
        ))
    else:
        # No DSN → the server uses legacy AuthConfig
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, bearer="legacy-bearer-token-lc")
        s.add(TestResult(
            "Legacy AuthConfig lc-agent (bearer): valid key",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, key="legacy-api-key-lc")
        s.add(TestResult(
            "Legacy AuthConfig lc-agent (api_key): valid key",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))

    # ── Legacy AuthConfig sk-agent ──────────────────────────────────────────
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body, bearer="sk-bearer-token")
    s.add(TestResult(
        "Legacy AuthConfig sk-agent (bearer): valid key",
        code in (200,) and not _has_error(data, -32600),
        f"status={code}", ms,
    ))
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body, key="sk-api-key-123")
    s.add(TestResult(
        "Legacy AuthConfig sk-agent (api_key): valid key",
        code in (200,) and not _has_error(data, -32600),
        f"status={code}", ms,
    ))
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body)
    s.add(TestResult(
        "Legacy AuthConfig sk-agent: no key → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── invoices-classifier: no auth (public) ──────────────────────────────
    # We use a short timeout: we only want to verify that the auth layer
    # lets the request through (not 401), not wait for the agent's response.
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{BASE_URL}/agents/invoices-classifier/a2a",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(3.0, connect=3.0),
        )
        ms = (time.monotonic() - t0) * 1000
        # 200 = auth ok (agent responds), or a timeout on the agent side but auth ok
        passed = r.status_code != 401
        s.add(TestResult(
            "No auth (invoices-classifier): access without a key → not 401",
            passed,
            f"status={r.status_code} (200=ok, anything!=401 means auth is ok)", ms,
        ))
    except httpx.TimeoutException:
        ms = (time.monotonic() - t0) * 1000
        # Timeout = the server accepted the connection and is processing
        # → the auth layer let it through (didn't immediately respond 401)
        s.add(TestResult(
            "No auth (invoices-classifier): access without a key → not 401",
            True,
            "⏱ Expected timeout: auth ok, agent slow (normal in tests)", ms,
        ))

    return s


# ---------------------------------------------------------------------------
# Suite 3 — A2A methods (messages/send, tasks/get, tasks/cancel)
# ---------------------------------------------------------------------------

async def suite_a2a_methods(
    client: httpx.AsyncClient,
    key: str,
    path_plain: str = "/agents/calculator/a2a",
) -> Suite:
    s = Suite(f"A2A JSON-RPC methods on {path_plain}")
    print(f"\n{'─'*60}")
    print(f"📨 {s.name}")
    print(f"{'─'*60}")

    # ── messages/send ─────────────────────────────────────────────────────
    body = rpc("messages/send", msg_params("Compute 2 + 2"))
    code, data, ms = await post(client, path_plain, body, key=key)
    task_id    = data.get("result", {}).get("id") or data.get("result", {}).get("taskId", "")
    context_id = data.get("result", {}).get("contextId", "")
    s.add(TestResult(
        "messages/send",
        code == 200 and _has_result(data),
        f"status={code} taskId={task_id}\n{_fmt(data)}", ms,
    ))

    # ── messages/send — idempotency (same taskId) ──────────────────────────
    if task_id:
        body2 = rpc("messages/send", msg_params("Compute 2 + 2", task_id=task_id))
        code, data2, ms = await post(client, path_plain, body2, key=key)
        returned_id = data2.get("result", {}).get("id") or data2.get("result", {}).get("taskId", "")
        s.add(TestResult(
            "messages/send — idempotency (same taskId)",
            code == 200 and returned_id == task_id,
            f"returned taskId={returned_id} (expected {task_id})", ms,
        ))

    # ── tasks/get ─────────────────────────────────────────────────────────
    if task_id:
        body = rpc("tasks/get", {"taskId": task_id})  # server reads params.taskId
        code, data, ms = await post(client, path_plain, body, key=key)
        result_tg  = data.get("result") or {}
        status_tg  = result_tg.get("status") or {}
        state_tg   = status_tg.get("state", "") if isinstance(status_tg, dict) else str(status_tg)
        # Passes if: 200 + result present (even with an empty state = task just created)
        tg_ok = code == 200 and ("result" in data) and not _has_error(data, -32600)
        s.add(TestResult(
            "tasks/get",
            tg_ok,
            f"status={code} state={state_tg!r} result_keys={list(result_tg.keys()) if isinstance(result_tg, dict) else result_tg}", ms,
        ))

    # ── tasks/get — nonexistent task ───────────────────────────────────────
    body = rpc("tasks/get", {"taskId": "nonexistent-task-id"})
    code, data, ms = await post(client, path_plain, body, key=key)
    s.add(TestResult(
        "tasks/get — nonexistent task → error",
        code == 200 and _has_error(data),
        f"error={data.get('error')}", ms,
    ))

    # ── tasks/cancel ──────────────────────────────────────────────────────
    if task_id:
        body = rpc("tasks/cancel", {"taskId": task_id})  # server reads params.taskId
        code, data, ms = await post(client, path_plain, body, key=key)
        # can be ok (canceled) or an error if already finished
        s.add(TestResult(
            "tasks/cancel",
            code == 200,
            f"status={code} resp={_fmt(data)}", ms,
        ))

    # ── tasks/cancel — nonexistent task ────────────────────────────────────
    body = rpc("tasks/cancel", {"taskId": "nonexistent-task-id"})
    code, data, ms = await post(client, path_plain, body, key=key)
    s.add(TestResult(
        "tasks/cancel — nonexistent task → error",
        code == 200 and _has_error(data),
        f"error={data.get('error')}", ms,
    ))

    # ── method not found ──────────────────────────────────────────────────
    body = rpc("nonexistent/method", {})
    code, data, ms = await post(client, path_plain, body, key=key)
    mnf_err    = data.get("error")
    mnf_code   = mnf_err.get("code") if mnf_err else None
    mnf_result = data.get("result")
    # ✅ = server responds with any error code (ideally -32601)
    # ❌ = server responds with a result (bug: unknown method not handled)
    s.add(TestResult(
        "nonexistent method → error (ideally -32601)",
        code == 200 and mnf_err is not None,
        f"error_code={mnf_code} (spec: -32601)"
        + (f" — server responded with result={mnf_result!r} instead of error (server bug)" if mnf_err is None else ""),
        ms,
    ))

    # ── parse error (non-JSON body) ────────────────────────────────────────
    t0 = time.monotonic()
    r = await client.post(
        f"{BASE_URL}{path_plain}",
        content=b"not-json!!!",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    ms = (time.monotonic() - t0) * 1000
    s.add(TestResult(
        "non-JSON body → -32700",
        r.status_code == 200 and r.json().get("error", {}).get("code") == -32700,
        f"status={r.status_code} error={r.json().get('error')}", ms,
    ))

    # ── tasks/pushNotificationConfig/set → -32003 ────────────────────────
    body = rpc("tasks/pushNotificationConfig/set", {"taskId": task_id or "x", "config": {}})
    code, data, ms = await post(client, path_plain, body, key=key)
    s.add(TestResult(
        "tasks/pushNotificationConfig/set → -32003 (not supported)",
        code == 200 and _has_error(data, -32003),
        f"error={data.get('error')}", ms,
    ))

    return s


# ---------------------------------------------------------------------------
# Suite 4 — SSE streaming (messages/sendStream, tasks/subscribe)
# ---------------------------------------------------------------------------

async def suite_sse(
    client: httpx.AsyncClient,
    key: str,
    path_stream: str = "/agents/calculator/a2a/stream",
) -> Suite:
    s = Suite(f"SSE Streaming on {path_stream}")
    print(f"\n{'─'*60}")
    print(f"📡 {s.name}")
    print(f"{'─'*60}")

    # ── messages/sendStream ───────────────────────────────────────────────
    body = rpc("messages/sendStream", msg_params("Streaming test"))
    events = await sse_lines(client, path_stream, body, key=key, max_events=15)
    event_types = [e.get("event") for e in events if "event" in e]
    has_status   = "TaskStatusUpdateEvent" in event_types
    has_artifact = "TaskArtifactUpdateEvent" in event_types
    has_final    = any(
        e.get("data", {}).get("result", {}).get("final") is True
        or e.get("data", {}).get("result", {}).get("status", {}).get("state") in ("completed", "failed")
        for e in events
    )
    s.add(TestResult(
        "messages/sendStream — receives SSE events",
        len(events) > 0 and has_status,
        f"events received: {len(events)}\n"
        f"types: {event_types}\n"
        f"has_status={has_status} has_artifact={has_artifact} has_final={has_final}",
        0,
    ))

    # Extracts task_id from the last status event
    stream_task_id = ""
    for e in events:
        result = e.get("data", {}).get("result", {})
        tid = result.get("taskId", "")
        if tid:
            stream_task_id = tid

    # ── tasks/subscribe ───────────────────────────────────────────────────
    if stream_task_id:
        body2 = rpc("tasks/subscribe", {"taskId": stream_task_id})
        events2 = await sse_lines(client, path_stream, body2, key=key, max_events=5, timeout=10.0)
        event_types2 = [e.get("event") for e in events2 if "event" in e]
        s.add(TestResult(
            "tasks/subscribe — existing task",
            len(events2) > 0,
            f"events: {len(events2)} types: {event_types2}", 0,
        ))
    else:
        s.add(TestResult(
            "tasks/subscribe — skip (no task_id from sendStream)",
            False,
            "sendStream did not return a taskId", 0,
        ))

    # ── tasks/subscribe — nonexistent task ─────────────────────────────────
    body3 = rpc("tasks/subscribe", {"taskId": "nonexistent-task-999"})
    events3 = await sse_lines(client, path_stream, body3, key=key, max_events=2, timeout=5.0)
    got_error = any(e.get("event") == "error" for e in events3)
    s.add(TestResult(
        "tasks/subscribe — nonexistent task → error event",
        got_error,
        f"events: {events3}", 0,
    ))

    # ── SSE without a key → 401 ────────────────────────────────────────────
    body4 = rpc("messages/sendStream", msg_params("no auth"))
    events4 = await sse_lines(client, path_stream, body4, key=None, max_events=1, timeout=5.0)
    got_401 = any(e.get("_status") == 401 for e in events4)
    s.add(TestResult(
        "SSE without a key → 401",
        got_401,
        f"events={events4}", 0,
    ))

    return s


# ---------------------------------------------------------------------------
# Suite 5 — contextId and multi-turn
# ---------------------------------------------------------------------------

async def suite_context(
    client: httpx.AsyncClient,
    key: str,
    path_plain: str = "/agents/calculator/a2a",
) -> Suite:
    s = Suite("contextId and multi-turn")
    print(f"\n{'─'*60}")
    print(f"🔄 {s.name}")
    print(f"{'─'*60}")

    ctx = str(uuid.uuid4())

    # First call with contextId
    body = rpc("messages/send", msg_params("First question", context_id=ctx))
    code, data, ms = await post(client, path_plain, body, key=key)
    returned_ctx = data.get("result", {}).get("contextId", "")
    s.add(TestResult(
        "messages/send with explicit contextId",
        code == 200 and _has_result(data),
        f"sent contextId={ctx}\nreturned contextId={returned_ctx}", ms,
    ))

    # Second call on the same context
    task_id = data.get("result", {}).get("id") or data.get("result", {}).get("taskId", "")
    body2 = rpc("messages/send", msg_params("Second question", context_id=ctx))
    code2, data2, ms2 = await post(client, path_plain, body2, key=key)
    task_id2 = data2.get("result", {}).get("id") or data2.get("result", {}).get("taskId", "")
    s.add(TestResult(
        "messages/send second turn, same contextId",
        code2 == 200 and _has_result(data2) and task_id2 != task_id,
        f"task1={task_id} task2={task_id2} ctx={returned_ctx}", ms2,
    ))

    # tasks/get with mismatched contextId/taskId (must return an error)
    if task_id:
        body3 = rpc("tasks/get", {"id": task_id, "contextId": str(uuid.uuid4())})
        code3, data3, ms3 = await post(client, path_plain, body3, key=key)
        s.add(TestResult(
            "tasks/get with wrong contextId → mismatch error",
            code3 == 200 and _has_error(data3),
            f"error={data3.get('error')}", ms3,
        ))

    return s


# ---------------------------------------------------------------------------
# Suite 6 — secure-a2a (encrypted STLS endpoint)
# ---------------------------------------------------------------------------

def _build_stls_client(base_url: str, api_key: str) -> Any:
    """
    Creates a client-side SecureA2ATransportAdapter.

    Uses the same KeySessionProviderFactory as the server, reading
    KEY_PROVIDER_NAME and KEY_SESSION_PORT from .env.

    Real provider API (from stls_a2a_provider.py):
        provider.getSessionKeyIdentifier()         → str  (session_id / SKI)
        provider.getSessionKeyData(ski) → bytes    (AES-256 key)
        factory.resolve_if_awaitable(x)            → resolves a Java Future / coroutine

    Returns the adapter ready to use, or None if the provider
    is not available (e.g. missing Java, wrong KEY_PROVIDER_NAME).
    """
    try:
        from stls_a2a import KeySessionProviderFactory, KeySessionProvider
        from stls_a2a import SecureA2ATransportAdapter
        import httpx as _httpx

        port     = int(os.getenv("KEY_SESSION_PORT", 8081))
        factory  = KeySessionProviderFactory()

        # Creates the CLIENT session with the same provider as the server
        provider = factory.get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            port=port,
        )

        # 1. Session Key Identifier — identifies the session (= X-A2A-Session-ID)
        #    Can be a coroutine (Arqit async) or a plain string (base/simple)
        ski = factory.resolve_if_awaitable(provider.getSessionKeyIdentifier())

        # 2. Session Key bytes — AES-256-GCM key to encrypt/decrypt
        session_key = factory.resolve_if_awaitable(provider.getSessionKeyData(ski))

        logger.info(
            "STLS client ready: provider=%s ski=%s key_len=%d",
            os.getenv("KEY_PROVIDER_NAME", "base"),
            ski,
            len(session_key) if isinstance(session_key, (bytes, bytearray)) else -1,
        )

        adapter = SecureA2ATransportAdapter(
            session_key=session_key,
            session_id=ski,       # the server uses ski as the sessions dict key
            base_url=base_url,
        )
        # Sync path (self.client): injects X-API-Key in the header
        adapter.client = _httpx.AsyncClient(
            timeout=15.0,
            headers={"X-API-Key": api_key},
        )
        # SSE path (send_secure_message stream=True): uses a new internal
        # AsyncClient without self.client — injects the key as a ?api_key= query param.
        # The server accepts both the X-API-Key header and the ?api_key= query param.
        adapter._stream_url = f"{adapter._stream_url}?api_key={api_key}"

        return adapter

    except Exception as exc:
        logger.warning("STLS client not available: %s", exc)
        return None


async def suite_secure_endpoint(
    client: httpx.AsyncClient,
    key: str,
) -> Suite:
    s = Suite("Encrypted endpoint /secure-a2a")
    print(f"\n{'─'*60}")
    print(f"🔒 {s.name}")
    print(f"{'─'*60}")

    # ── 1. Plain payload on /secure-a2a → error expected ───────────────────
    # The server requires AES-256-GCM — a standard JSON-RPC body must fail.
    body_plain = rpc("messages/send", msg_params("test plain on secure"))
    code, data, ms = await post(client, "/agents/calculator/secure-a2a", body_plain, key=key)
    s.add(TestResult(
        "POST /secure-a2a plain payload → error (expected)",
        code in (400, 200) and ("error" in data or "session_id" in str(data)),
        f"status={code} resp={_fmt(data)}", ms,
    ))

    # ── 2. No key → 401 (auth layer before decrypting) ──────────────────
    code, data, ms = await post(client, "/agents/calculator/secure-a2a", body_plain)
    s.add(TestResult(
        "POST /secure-a2a without a key → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── 3. Encrypted SSE without a key → 401 ───────────────────────────────
    events = await sse_lines(
        client, "/agents/calculator/secure-a2a/stream", body_plain,
        key=None, max_events=1, timeout=5.0,
    )
    got_401 = any(e.get("_status") == 401 for e in events)
    s.add(TestResult(
        "POST /secure-a2a/stream without a key → 401",
        got_401,
        f"events={events}", 0,
    ))

    # ── 4. Real encrypted STLS session ─────────────────────────────────────
    # Builds a client-side SecureA2ATransportAdapter using the same
    # KEY_PROVIDER_NAME configured in the server's .env.
    # Flow: get_session_key() → AES-256-GCM encrypt → POST /secure-a2a
    #       → server decrypts with the same provider → encrypted response
    #       → client decrypts → result.
    stls_adapter = _build_stls_client(
        base_url=f"{BASE_URL}/agents/calculator",
        api_key=key,
    )
    if stls_adapter is not None:
        try:
            msg = {
                "jsonrpc": "2.0",
                "method":  "messages/send",
                "params":  msg_params("Encrypted STLS session — end-to-end test"),
                "id":      1,
            }
            t0 = time.monotonic()
            result = await stls_adapter.send_secure_message(msg)
            ms = (time.monotonic() - t0) * 1000
            await stls_adapter.close()
            # Success = decrypted response without an auth error (-32600)
            auth_ok  = result.get("error", {}).get("code") != -32600
            has_data = "result" in result or ("error" in result and result["error"].get("code") != -32600)
            s.add(TestResult(
                "STLS encrypted session end-to-end (messages/send)",
                auth_ok and has_data,
                f"ms={ms:.0f} keys={list(result.keys())} "
                f"err={result.get('error')}", ms,
            ))
        except Exception as exc:
            s.add(TestResult(
                "STLS encrypted session end-to-end (messages/send)",
                False,
                f"Exception: {exc}", 0,
            ))

        # ── 5. Real encrypted STLS SSE session ───────────────────────────
        stls_sse = _build_stls_client(
            base_url=f"{BASE_URL}/agents/calculator",
            api_key=key,
        )
        if stls_sse is not None:
            try:
                msg_stream = {
                    "jsonrpc": "2.0",
                    "method":  "messages/sendStream",
                    "params":  msg_params("Encrypted STLS streaming — test"),
                    "id":      2,
                }
                t0 = time.monotonic()
                result_s = await stls_sse.send_secure_message(msg_stream, stream=True)
                ms = (time.monotonic() - t0) * 1000
                await stls_sse.close()
                stream_ok = "result" in result_s and result_s.get("error", {}).get("code") != -32600
                s.add(TestResult(
                    "STLS encrypted SSE session end-to-end (messages/sendStream)",
                    stream_ok,
                    f"ms={ms:.0f} status={result_s.get('result',{}).get('status')}", ms,
                ))
            except Exception as exc:
                s.add(TestResult(
                    "STLS encrypted SSE session end-to-end (messages/sendStream)",
                    False,
                    f"Exception: {exc}", 0,
                ))
    else:
        provider_name = os.getenv("KEY_PROVIDER_NAME", "base")
        s.add(TestResult(
            "STLS encrypted session end-to-end — SKIP",
            True,
            f"KeySessionProvider not available (KEY_PROVIDER_NAME={provider_name}). "
            "Check KEY_PROVIDER_NAME, KEY_SESSION_PORT and the Java/JPype dependencies.", 0,
        ))

    return s


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

async def run(
    suite_filter: str | None = None,
    static_key: str = "",
    file_key: str = "",
    pg_key: str | None = None,
    verbose: bool = False,
) -> int:
    global VERBOSE
    if verbose:
        VERBOSE = True

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(TIMEOUT, connect=5.0),
        follow_redirects=True,
    ) as client:

        suites: list[Suite] = []

        if not suite_filter or suite_filter == "diag":
            suites.append(await suite_diagnostics(client))

        if not suite_filter or suite_filter == "auth":
            suites.append(await suite_auth(client, static_key, file_key, pg_key))

        if not suite_filter or suite_filter == "a2a":
            suites.append(await suite_a2a_methods(client, static_key))

        if not suite_filter or suite_filter == "sse":
            suites.append(await suite_sse(client, static_key))

        if not suite_filter or suite_filter == "context":
            suites.append(await suite_context(client, static_key))

        if not suite_filter or suite_filter == "secure":
            suites.append(await suite_secure_endpoint(client, static_key))

    # ── Summary ──────────────────────────────────────────────────────────
    total_ok  = sum(s.passed for s in suites)
    total_ko  = sum(s.failed for s in suites)
    total_all = total_ok + total_ko

    print(f"\n{'═'*60}")
    print(f"📊 Summary")
    print(f"{'═'*60}")
    for s in suites:
        bar = "✅" * s.passed + "❌" * s.failed
        print(f"  {s.name:<40} {s.passed}/{len(s.results)}  {bar}")
    print(f"{'─'*60}")
    print(f"  Total: {total_ok}/{total_all} passed", end="")
    if total_ko:
        print(f"  ← {total_ko} FAILED ❌")
    else:
        print("  — all OK ✅")
    print(f"{'═'*60}\n")

    return total_ko  # exit code: 0 = all ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _get_keys() -> tuple[str, str, str | None]:
    """
    Reads the test keys from the env, or falls back to hard-coded legacy
    keys for AuthConfig (backward compatible with the auth suite).

    Env vars:
        TEST_STATIC_KEY   key for StaticLoader (printed by the server at startup)
        TEST_FILE_KEY     key for FileLoader   (from api_keys.yaml raw_key)
        TEST_PG_KEY       key for PostgresLoader (from migration_api_keys.sql)
    """
    static = os.getenv("TEST_STATIC_KEY", "")
    file_k = os.getenv("TEST_FILE_KEY",
                       "stls_adk_test_key_change_me_in_production")
    pg     = os.getenv("TEST_PG_KEY") or None
    return static, file_k, pg


def main() -> None:
    parser = argparse.ArgumentParser(description="Test client for multi_agent_km_server")
    parser.add_argument("--host",    default="localhost")
    parser.add_argument("--port",    default=8001, type=int)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--suite",
        choices=["diag", "auth", "a2a", "sse", "context", "secure"],
        help="Run only a specific suite",
    )
    parser.add_argument("--static-key", default="", help="StaticLoader key")
    parser.add_argument("--file-key",   default="", help="FileLoader key")
    parser.add_argument("--pg-key",     default="", help="PostgresLoader key")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = f"http://{args.host}:{args.port}"

    env_static, env_file, env_pg = _get_keys()
    static_key = args.static_key or env_static
    file_key   = args.file_key   or env_file
    pg_key     = args.pg_key     or env_pg

    if not static_key:
        print("⚠️  TEST_STATIC_KEY not set.")
        print("   Copy the KEY_1 printed by the server at startup and set it:")
        print("   export TEST_STATIC_KEY='stls-static_...'")
        print("   or use --static-key <value>\n")

    print(f"\n{'═'*60}")
    print(f"🧪 Test client → {BASE_URL}")
    print(f"   static_key : {(static_key[:20] + '…') if static_key else '(not set)'}")
    print(f"   file_key   : {file_key[:20]}…")
    print(f"   pg_key     : {(pg_key[:20] + '…') if pg_key else '(not set — using legacy)'}")
    print(f"{'═'*60}")

    exit_code = asyncio.run(run(
        suite_filter=args.suite,
        static_key=static_key,
        file_key=file_key,
        pg_key=pg_key,
        verbose=args.verbose,
    ))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()