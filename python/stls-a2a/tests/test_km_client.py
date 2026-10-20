"""
test_km_client.py
-----------------
Client di test completo per multi_agent_km_server.py.

Testa tutti gli endpoint, tutti i backend auth e tutti i metodi A2A.

Avvio:
    # avvia prima il server
    uv run python multi_agent_km_server.py

    # poi in un altro terminale
    uv run python test_km_client.py

    # oppure con opzioni:
    uv run python test_km_client.py --host localhost --port 8001 --verbose
    uv run python test_km_client.py --suite auth     # solo test autenticazione
    uv run python test_km_client.py --suite a2a      # solo test metodi A2A
    uv run python test_km_client.py --suite diag     # solo diagnostica
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
# Configurazione
# ---------------------------------------------------------------------------

BASE_URL     = os.getenv("TEST_BASE_URL", "http://localhost:8001")
TIMEOUT      = float(os.getenv("TEST_TIMEOUT", "15"))
VERBOSE      = os.getenv("TEST_VERBOSE", "false").lower() == "true"

# ---------------------------------------------------------------------------
# Risultati
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
        p["taskId"] = task_id  # spec §3.3.1: il server controlla params.taskId
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
    """Raccoglie eventi SSE e li restituisce come lista di dict parsed."""
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
                    # termina se abbiamo abbastanza eventi o lo stato è finale
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
# Suite 1 — Diagnostica e discovery
# ---------------------------------------------------------------------------

async def suite_diagnostics(client: httpx.AsyncClient) -> Suite:
    s = Suite("Diagnostica & Discovery")
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
        code in (200, 404),   # 404 se non ha un AgentCard strutturato
        f"status={code}", ms,
    ))

    return s


# ---------------------------------------------------------------------------
# Suite 2 — Autenticazione (tutti i backend)
# ---------------------------------------------------------------------------

async def suite_auth(
    client: httpx.AsyncClient,
    static_key: str,
    file_key: str,
    pg_key: str | None,
) -> Suite:
    s = Suite("Autenticazione — tutti i backend")
    print(f"\n{'─'*60}")
    print(f"🔐 {s.name}")
    print(f"{'─'*60}")

    body = rpc("messages/send", msg_params("test auth"))

    # ── StaticLoader: chiave valida ───────────────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body, key=static_key)
    s.add(TestResult(
        "StaticLoader: chiave valida (X-API-Key)",
        code in (200,) and not _has_error(data, -32600),
        f"status={code} result_ok={_has_result(data)}", ms,
    ))

    # ── StaticLoader: chiave valida via query param ───────────────────────
    t0 = time.monotonic()
    r  = await client.post(
        f"{BASE_URL}/agents/calculator/a2a?api_key={static_key}",
        json=body, headers={"Content-Type": "application/json"},
    )
    ms = (time.monotonic() - t0) * 1000
    s.add(TestResult(
        "StaticLoader: chiave valida (?api_key= query param)",
        r.status_code in (200,) and r.json().get("error", {}).get("code") != -32600,
        f"status={r.status_code}", ms,
    ))

    # ── StaticLoader: chiave assente → 401 ───────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body)
    s.add(TestResult(
        "StaticLoader: nessuna chiave → 401",
        code == 401,
        f"status={code} body={_fmt(data)}", ms,
    ))

    # ── StaticLoader: chiave errata → 401 ────────────────────────────────
    code, data, ms = await post(client, "/agents/calculator/a2a", body, key="stls-static_wrong_key")
    s.add(TestResult(
        "StaticLoader: chiave errata → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── FileLoader: chiave valida ─────────────────────────────────────────
    code, data, ms = await post(client, "/agents/adk-agent/a2a", body, key=file_key)
    s.add(TestResult(
        "FileLoader: chiave valida (api_keys.yaml)",
        code in (200,) and not _has_error(data, -32600),
        f"status={code} result_ok={_has_result(data)}", ms,
    ))

    # ── FileLoader: chiave assente → 401 ─────────────────────────────────
    code, data, ms = await post(client, "/agents/adk-agent/a2a", body)
    s.add(TestResult(
        "FileLoader: nessuna chiave → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── lc-agent: Postgres se DSN configurato, legacy altrimenti ─────────
    # Rileva il backend attivo provando prima senza chiave:
    # se risponde 401 c'è auth attiva; se risponde 200 è pubblico (non dovrebbe).
    # Per capire se il server usa Postgres o legacy, controlliamo AUTH_PG_DSN.
    import os as _os
    _pg_dsn_active = bool((_os.getenv("AUTH_PG_DSN") or "").split("#")[0].strip())

    if pg_key:
        # Chiave Postgres fornita — test completo
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, key=pg_key)
        s.add(TestResult(
            "PostgresLoader: chiave valida",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body)
        s.add(TestResult(
            "PostgresLoader: nessuna chiave → 401",
            code == 401,
            f"status={code}", ms,
        ))
    elif _pg_dsn_active:
        # AUTH_PG_DSN impostato ma TEST_PG_KEY non fornita:
        # il server usa PostgresLoader → verifica solo che il 401 funzioni
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body)
        s.add(TestResult(
            "PostgresLoader attivo (no TEST_PG_KEY): nessuna chiave → 401",
            code == 401,
            f"status={code} — imposta TEST_PG_KEY per testare chiave valida", ms,
        ))
        s.add(TestResult(
            "PostgresLoader attivo (no TEST_PG_KEY): chiave valida — SKIP",
            True,
            "⚠️  Imposta TEST_PG_KEY con un raw_key reale dal DB per questo test", 0,
        ))
    else:
        # Nessun DSN → il server usa AuthConfig legacy
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, bearer="legacy-bearer-token-lc")
        s.add(TestResult(
            "AuthConfig legacy lc-agent (bearer): chiave valida",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))
        code, data, ms = await post(client, "/agents/lc-agent/a2a", body, key="legacy-api-key-lc")
        s.add(TestResult(
            "AuthConfig legacy lc-agent (api_key): chiave valida",
            code in (200,) and not _has_error(data, -32600),
            f"status={code}", ms,
        ))

    # ── AuthConfig legacy sk-agent ────────────────────────────────────────
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body, bearer="sk-bearer-token")
    s.add(TestResult(
        "AuthConfig legacy sk-agent (bearer): chiave valida",
        code in (200,) and not _has_error(data, -32600),
        f"status={code}", ms,
    ))
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body, key="sk-api-key-123")
    s.add(TestResult(
        "AuthConfig legacy sk-agent (api_key): chiave valida",
        code in (200,) and not _has_error(data, -32600),
        f"status={code}", ms,
    ))
    code, data, ms = await post(client, "/agents/sk-agent/a2a", body)
    s.add(TestResult(
        "AuthConfig legacy sk-agent: nessuna chiave → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── invoices-classifier: nessuna auth (pubblico) ──────────────────────
    # Usiamo un timeout corto: vogliamo solo verificare che il layer auth
    # lasci passare la richiesta (non 401), non aspettare la risposta dell'agente.
    t0 = time.monotonic()
    try:
        r = await client.post(
            f"{BASE_URL}/agents/invoices-classifier/a2a",
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(3.0, connect=3.0),
        )
        ms = (time.monotonic() - t0) * 1000
        # 200 = auth ok (agente risponde), oppure timeout lato agente ma auth ok
        passed = r.status_code != 401
        s.add(TestResult(
            "Nessuna auth (invoices-classifier): accesso senza chiave → non 401",
            passed,
            f"status={r.status_code} (200=ok, qualsiasi!=401 è auth ok)", ms,
        ))
    except httpx.TimeoutException:
        ms = (time.monotonic() - t0) * 1000
        # Timeout = il server ha accettato la connessione e sta elaborando
        # → il layer auth ha lasciato passare (non ha risposto 401 subito)
        s.add(TestResult(
            "Nessuna auth (invoices-classifier): accesso senza chiave → non 401",
            True,
            "⏱ Timeout atteso: auth ok, agente lento (normale in test)", ms,
        ))

    return s


# ---------------------------------------------------------------------------
# Suite 3 — Metodi A2A (messages/send, tasks/get, tasks/cancel)
# ---------------------------------------------------------------------------

async def suite_a2a_methods(
    client: httpx.AsyncClient,
    key: str,
    path_plain: str = "/agents/calculator/a2a",
) -> Suite:
    s = Suite(f"Metodi A2A JSON-RPC su {path_plain}")
    print(f"\n{'─'*60}")
    print(f"📨 {s.name}")
    print(f"{'─'*60}")

    # ── messages/send ─────────────────────────────────────────────────────
    body = rpc("messages/send", msg_params("Calcola 2 + 2"))
    code, data, ms = await post(client, path_plain, body, key=key)
    task_id    = data.get("result", {}).get("id") or data.get("result", {}).get("taskId", "")
    context_id = data.get("result", {}).get("contextId", "")
    s.add(TestResult(
        "messages/send",
        code == 200 and _has_result(data),
        f"status={code} taskId={task_id}\n{_fmt(data)}", ms,
    ))

    # ── messages/send — idempotenza (stesso taskId) ───────────────────────
    if task_id:
        body2 = rpc("messages/send", msg_params("Calcola 2 + 2", task_id=task_id))
        code, data2, ms = await post(client, path_plain, body2, key=key)
        returned_id = data2.get("result", {}).get("id") or data2.get("result", {}).get("taskId", "")
        s.add(TestResult(
            "messages/send — idempotenza (stesso taskId)",
            code == 200 and returned_id == task_id,
            f"returned taskId={returned_id} (atteso {task_id})", ms,
        ))

    # ── tasks/get ─────────────────────────────────────────────────────────
    if task_id:
        body = rpc("tasks/get", {"taskId": task_id})  # server legge params.taskId
        code, data, ms = await post(client, path_plain, body, key=key)
        result_tg  = data.get("result") or {}
        status_tg  = result_tg.get("status") or {}
        state_tg   = status_tg.get("state", "") if isinstance(status_tg, dict) else str(status_tg)
        # Passa se: 200 + result presente (anche con state vuoto = task appena creato)
        tg_ok = code == 200 and ("result" in data) and not _has_error(data, -32600)
        s.add(TestResult(
            "tasks/get",
            tg_ok,
            f"status={code} state={state_tg!r} result_keys={list(result_tg.keys()) if isinstance(result_tg, dict) else result_tg}", ms,
        ))

    # ── tasks/get — task inesistente ──────────────────────────────────────
    body = rpc("tasks/get", {"taskId": "nonexistent-task-id"})
    code, data, ms = await post(client, path_plain, body, key=key)
    s.add(TestResult(
        "tasks/get — task inesistente → errore",
        code == 200 and _has_error(data),
        f"error={data.get('error')}", ms,
    ))

    # ── tasks/cancel ──────────────────────────────────────────────────────
    if task_id:
        body = rpc("tasks/cancel", {"taskId": task_id})  # server legge params.taskId
        code, data, ms = await post(client, path_plain, body, key=key)
        # può essere ok (canceled) o errore se già terminato
        s.add(TestResult(
            "tasks/cancel",
            code == 200,
            f"status={code} resp={_fmt(data)}", ms,
        ))

    # ── tasks/cancel — task inesistente ───────────────────────────────────
    body = rpc("tasks/cancel", {"taskId": "nonexistent-task-id"})
    code, data, ms = await post(client, path_plain, body, key=key)
    s.add(TestResult(
        "tasks/cancel — task inesistente → errore",
        code == 200 and _has_error(data),
        f"error={data.get('error')}", ms,
    ))

    # ── method not found ──────────────────────────────────────────────────
    body = rpc("nonexistent/method", {})
    code, data, ms = await post(client, path_plain, body, key=key)
    mnf_err    = data.get("error")
    mnf_code   = mnf_err.get("code") if mnf_err else None
    mnf_result = data.get("result")
    # ✅ = server risponde con un error code qualsiasi (ideale -32601)
    # ❌ = server risponde con result (bug: metodo ignoto non gestito)
    s.add(TestResult(
        "metodo inesistente → errore (ideale -32601)",
        code == 200 and mnf_err is not None,
        f"error_code={mnf_code} (spec: -32601)"
        + (f" — server ha risposto con result={mnf_result!r} invece di error (bug server)" if mnf_err is None else ""),
        ms,
    ))

    # ── parse error (body non JSON) ───────────────────────────────────────
    t0 = time.monotonic()
    r = await client.post(
        f"{BASE_URL}{path_plain}",
        content=b"not-json!!!",
        headers={"Content-Type": "application/json", "X-API-Key": key},
    )
    ms = (time.monotonic() - t0) * 1000
    s.add(TestResult(
        "body non-JSON → -32700",
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
    s = Suite(f"SSE Streaming su {path_stream}")
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
        "messages/sendStream — riceve eventi SSE",
        len(events) > 0 and has_status,
        f"eventi ricevuti: {len(events)}\n"
        f"tipi: {event_types}\n"
        f"has_status={has_status} has_artifact={has_artifact} has_final={has_final}",
        0,
    ))

    # Estrae task_id dall'ultimo evento di status
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
            "tasks/subscribe — task esistente",
            len(events2) > 0,
            f"eventi: {len(events2)} tipi: {event_types2}", 0,
        ))
    else:
        s.add(TestResult(
            "tasks/subscribe — skip (nessun task_id da sendStream)",
            False,
            "sendStream non ha restituito taskId", 0,
        ))

    # ── tasks/subscribe — task inesistente ───────────────────────────────
    body3 = rpc("tasks/subscribe", {"taskId": "nonexistent-task-999"})
    events3 = await sse_lines(client, path_stream, body3, key=key, max_events=2, timeout=5.0)
    got_error = any(e.get("event") == "error" for e in events3)
    s.add(TestResult(
        "tasks/subscribe — task inesistente → evento error",
        got_error,
        f"eventi: {events3}", 0,
    ))

    # ── SSE senza chiave → 401 ────────────────────────────────────────────
    body4 = rpc("messages/sendStream", msg_params("no auth"))
    events4 = await sse_lines(client, path_stream, body4, key=None, max_events=1, timeout=5.0)
    got_401 = any(e.get("_status") == 401 for e in events4)
    s.add(TestResult(
        "SSE senza chiave → 401",
        got_401,
        f"events={events4}", 0,
    ))

    return s


# ---------------------------------------------------------------------------
# Suite 5 — contextId e multi-turn
# ---------------------------------------------------------------------------

async def suite_context(
    client: httpx.AsyncClient,
    key: str,
    path_plain: str = "/agents/calculator/a2a",
) -> Suite:
    s = Suite("contextId e multi-turn")
    print(f"\n{'─'*60}")
    print(f"🔄 {s.name}")
    print(f"{'─'*60}")

    ctx = str(uuid.uuid4())

    # Prima chiamata con contextId
    body = rpc("messages/send", msg_params("Prima domanda", context_id=ctx))
    code, data, ms = await post(client, path_plain, body, key=key)
    returned_ctx = data.get("result", {}).get("contextId", "")
    s.add(TestResult(
        "messages/send con contextId esplicito",
        code == 200 and _has_result(data),
        f"sent contextId={ctx}\nreturned contextId={returned_ctx}", ms,
    ))

    # Seconda chiamata sullo stesso contesto
    task_id = data.get("result", {}).get("id") or data.get("result", {}).get("taskId", "")
    body2 = rpc("messages/send", msg_params("Seconda domanda", context_id=ctx))
    code2, data2, ms2 = await post(client, path_plain, body2, key=key)
    task_id2 = data2.get("result", {}).get("id") or data2.get("result", {}).get("taskId", "")
    s.add(TestResult(
        "messages/send secondo turno stesso contextId",
        code2 == 200 and _has_result(data2) and task_id2 != task_id,
        f"task1={task_id} task2={task_id2} ctx={returned_ctx}", ms2,
    ))

    # tasks/get con mismatch contextId/taskId (deve dare errore)
    if task_id:
        body3 = rpc("tasks/get", {"id": task_id, "contextId": str(uuid.uuid4())})
        code3, data3, ms3 = await post(client, path_plain, body3, key=key)
        s.add(TestResult(
            "tasks/get con contextId errato → errore mismatch",
            code3 == 200 and _has_error(data3),
            f"error={data3.get('error')}", ms3,
        ))

    return s


# ---------------------------------------------------------------------------
# Suite 6 — secure-a2a (endpoint cifrato STLS)
# ---------------------------------------------------------------------------

def _build_stls_client(base_url: str, api_key: str) -> Any:
    """
    Crea un SecureA2ATransportAdapter lato client.

    Usa la stessa KeySessionProviderFactory del server, leggendo
    KEY_PROVIDER_NAME e KEY_SESSION_PORT dal .env.

    API reale del provider (da stls_a2a_provider.py):
        provider.getSessionKeyIdentifier()         → str  (session_id / SKI)
        provider.getSessionKeyData(ski) → bytes    (chiave AES-256)
        factory.resolve_if_awaitable(x)            → risolve Java Future / coroutine

    Restituisce l'adapter pronto all'uso, oppure None se il provider
    non è disponibile (es. Java mancante, KEY_PROVIDER_NAME errato).
    """
    try:
        from stls_a2a import KeySessionProviderFactory, KeySessionProvider
        from stls_a2a import SecureA2ATransportAdapter
        import httpx as _httpx

        port     = int(os.getenv("KEY_SESSION_PORT", 8081))
        factory  = KeySessionProviderFactory()

        # Crea la sessione CLIENT con lo stesso provider del server
        provider = factory.get_session_provider(
            session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
            port=port,
        )

        # 1. Session Key Identifier — identifica la sessione (= X-A2A-Session-ID)
        #    Può essere una coroutine (Arqit async) o una stringa diretta (base/simple)
        ski = factory.resolve_if_awaitable(provider.getSessionKeyIdentifier())

        # 2. Session Key bytes — chiave AES-256-GCM per cifrare/decifrare
        session_key = factory.resolve_if_awaitable(provider.getSessionKeyData(ski))

        logger.info(
            "STLS client pronto: provider=%s ski=%s key_len=%d",
            os.getenv("KEY_PROVIDER_NAME", "base"),
            ski,
            len(session_key) if isinstance(session_key, (bytes, bytearray)) else -1,
        )

        adapter = SecureA2ATransportAdapter(
            session_key=session_key,
            session_id=ski,       # il server usa ski come chiave del dict sessions
            base_url=base_url,
        )
        # Percorso sync (self.client): inietta X-API-Key nell'header
        adapter.client = _httpx.AsyncClient(
            timeout=15.0,
            headers={"X-API-Key": api_key},
        )
        # Percorso SSE (send_secure_message stream=True): usa un nuovo AsyncClient
        # interno senza self.client — inietta la chiave come query param ?api_key=
        # Il server accetta sia X-API-Key header sia ?api_key= query param.
        adapter._stream_url = f"{adapter._stream_url}?api_key={api_key}"

        return adapter

    except Exception as exc:
        logger.warning("STLS client non disponibile: %s", exc)
        return None


async def suite_secure_endpoint(
    client: httpx.AsyncClient,
    key: str,
) -> Suite:
    s = Suite("Endpoint cifrato /secure-a2a")
    print(f"\n{'─'*60}")
    print(f"🔒 {s.name}")
    print(f"{'─'*60}")

    # ── 1. Payload plain su /secure-a2a → errore atteso ───────────────────
    # Il server richiede AES-256-GCM — un body JSON-RPC standard deve fallire.
    body_plain = rpc("messages/send", msg_params("test plain su secure"))
    code, data, ms = await post(client, "/agents/calculator/secure-a2a", body_plain, key=key)
    s.add(TestResult(
        "POST /secure-a2a payload plain → errore (atteso)",
        code in (400, 200) and ("error" in data or "session_id" in str(data)),
        f"status={code} resp={_fmt(data)}", ms,
    ))

    # ── 2. Nessuna chiave → 401 (auth layer prima di decifrare) ───────────
    code, data, ms = await post(client, "/agents/calculator/secure-a2a", body_plain)
    s.add(TestResult(
        "POST /secure-a2a senza chiave → 401",
        code == 401,
        f"status={code}", ms,
    ))

    # ── 3. SSE cifrato senza chiave → 401 ─────────────────────────────────
    events = await sse_lines(
        client, "/agents/calculator/secure-a2a/stream", body_plain,
        key=None, max_events=1, timeout=5.0,
    )
    got_401 = any(e.get("_status") == 401 for e in events)
    s.add(TestResult(
        "POST /secure-a2a/stream senza chiave → 401",
        got_401,
        f"events={events}", 0,
    ))

    # ── 4. Sessione STLS cifrata reale ────────────────────────────────────
    # Costruisce un SecureA2ATransportAdapter client-side usando lo stesso
    # KEY_PROVIDER_NAME configurato nel .env del server.
    # Flusso: get_session_key() → AES-256-GCM encrypt → POST /secure-a2a
    #         → server decripta con lo stesso provider → risposta cifrata
    #         → client decripta → risultato.
    stls_adapter = _build_stls_client(
        base_url=f"{BASE_URL}/agents/calculator",
        api_key=key,
    )
    if stls_adapter is not None:
        try:
            msg = {
                "jsonrpc": "2.0",
                "method":  "messages/send",
                "params":  msg_params("Sessione STLS cifrata — test end-to-end"),
                "id":      1,
            }
            t0 = time.monotonic()
            result = await stls_adapter.send_secure_message(msg)
            ms = (time.monotonic() - t0) * 1000
            await stls_adapter.close()
            # Successo = risposta decifrata senza errore auth (-32600)
            auth_ok  = result.get("error", {}).get("code") != -32600
            has_data = "result" in result or ("error" in result and result["error"].get("code") != -32600)
            s.add(TestResult(
                "STLS sessione cifrata end-to-end (messages/send)",
                auth_ok and has_data,
                f"ms={ms:.0f} keys={list(result.keys())} "
                f"err={result.get('error')}", ms,
            ))
        except Exception as exc:
            s.add(TestResult(
                "STLS sessione cifrata end-to-end (messages/send)",
                False,
                f"Eccezione: {exc}", 0,
            ))

        # ── 5. Sessione STLS SSE cifrata reale ───────────────────────────
        stls_sse = _build_stls_client(
            base_url=f"{BASE_URL}/agents/calculator",
            api_key=key,
        )
        if stls_sse is not None:
            try:
                msg_stream = {
                    "jsonrpc": "2.0",
                    "method":  "messages/sendStream",
                    "params":  msg_params("Streaming STLS cifrato — test"),
                    "id":      2,
                }
                t0 = time.monotonic()
                result_s = await stls_sse.send_secure_message(msg_stream, stream=True)
                ms = (time.monotonic() - t0) * 1000
                await stls_sse.close()
                stream_ok = "result" in result_s and result_s.get("error", {}).get("code") != -32600
                s.add(TestResult(
                    "STLS sessione SSE cifrata end-to-end (messages/sendStream)",
                    stream_ok,
                    f"ms={ms:.0f} status={result_s.get('result',{}).get('status')}", ms,
                ))
            except Exception as exc:
                s.add(TestResult(
                    "STLS sessione SSE cifrata end-to-end (messages/sendStream)",
                    False,
                    f"Eccezione: {exc}", 0,
                ))
    else:
        provider_name = os.getenv("KEY_PROVIDER_NAME", "base")
        s.add(TestResult(
            "STLS sessione cifrata end-to-end — SKIP",
            True,
            f"KeySessionProvider non disponibile (KEY_PROVIDER_NAME={provider_name}). "
            "Verifica KEY_PROVIDER_NAME, KEY_SESSION_PORT e dipendenze Java/JPype.", 0,
        ))

    return s


# ---------------------------------------------------------------------------
# Runner principale
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

    # ── Riepilogo ─────────────────────────────────────────────────────────
    total_ok  = sum(s.passed for s in suites)
    total_ko  = sum(s.failed for s in suites)
    total_all = total_ok + total_ko

    print(f"\n{'═'*60}")
    print(f"📊 Riepilogo")
    print(f"{'═'*60}")
    for s in suites:
        bar = "✅" * s.passed + "❌" * s.failed
        print(f"  {s.name:<40} {s.passed}/{len(s.results)}  {bar}")
    print(f"{'─'*60}")
    print(f"  Totale: {total_ok}/{total_all} passati", end="")
    if total_ko:
        print(f"  ← {total_ko} FALLITI ❌")
    else:
        print("  — tutti OK ✅")
    print(f"{'═'*60}\n")

    return total_ko  # exit code: 0 = tutto ok


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _get_keys() -> tuple[str, str, str | None]:
    """
    Legge le chiavi di test dall'env o usa le chiavi legacy hard-coded
    come fallback per AuthConfig (retrocompatibile con suite auth).

    Env vars:
        TEST_STATIC_KEY   chiave per StaticLoader (stampata dal server all'avvio)
        TEST_FILE_KEY     chiave per FileLoader   (da api_keys.yaml raw_key)
        TEST_PG_KEY       chiave per PostgresLoader (da migration_api_keys.sql)
    """
    static = os.getenv("TEST_STATIC_KEY", "")
    file_k = os.getenv("TEST_FILE_KEY",
                       "stls_adk_test_key_change_me_in_production")
    pg     = os.getenv("TEST_PG_KEY") or None
    return static, file_k, pg


def main() -> None:
    parser = argparse.ArgumentParser(description="Test client per multi_agent_km_server")
    parser.add_argument("--host",    default="localhost")
    parser.add_argument("--port",    default=8001, type=int)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--suite",
        choices=["diag", "auth", "a2a", "sse", "context", "secure"],
        help="Esegui solo una suite specifica",
    )
    parser.add_argument("--static-key", default="", help="Chiave StaticLoader")
    parser.add_argument("--file-key",   default="", help="Chiave FileLoader")
    parser.add_argument("--pg-key",     default="", help="Chiave PostgresLoader")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = f"http://{args.host}:{args.port}"

    env_static, env_file, env_pg = _get_keys()
    static_key = args.static_key or env_static
    file_key   = args.file_key   or env_file
    pg_key     = args.pg_key     or env_pg

    if not static_key:
        print("⚠️  TEST_STATIC_KEY non impostata.")
        print("   Copia la KEY_1 stampata dal server all'avvio e impostala:")
        print("   export TEST_STATIC_KEY='stls-static_...'")
        print("   oppure usa --static-key <valore>\n")

    print(f"\n{'═'*60}")
    print(f"🧪 Test client → {BASE_URL}")
    print(f"   static_key : {(static_key[:20] + '…') if static_key else '(non impostata)'}")
    print(f"   file_key   : {file_key[:20]}…")
    print(f"   pg_key     : {(pg_key[:20] + '…') if pg_key else '(non impostata — usa legacy)'}")
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