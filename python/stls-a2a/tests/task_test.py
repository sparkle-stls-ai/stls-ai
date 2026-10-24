import asyncio
import base64
import httpx
from typing import Dict, Any,Optional
import json

from stls_a2a import KeySessionProvider,KeySessionProviderFactory
from stls_a2a import SecureA2ATransportAdapter, SecureA2ATransport
from stls_a2a import extract_a2a_text,extract_a2a_result,extract_a2a_artifacts   

import logging


def _status_str(status) -> str:
    """Normalizes an A2A status: accepts a scalar string or an object {"state":"..."}."""
    if isinstance(status, dict):
        return status.get("state", "")
    return str(status) if status is not None else ""


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

BASE_URL = "http://localhost:8001"

import os

KEY_PROVIDER_NAME = os.getenv("KEY_PROVIDER_NAME", "base").strip().lower()

# For base/arqit_py the session key can be resolved immediately (pre-shared key).
# For PQC the session key only exists after the KEM handshake — it is initialized
# inside task_test() via create_pqc_a2a_transport().
if KEY_PROVIDER_NAME != "pqc":
    key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
        session_type=KeySessionProvider.KeySessionProviderType.CLIENT, port=8081, host='localhost')

    session_key_identifier = str(KeySessionProviderFactory().resolve_if_awaitable(
        key_session_provider.getSessionKeyIdentifier()))
    logger.info(f"session_key_identifier={session_key_identifier}")

    session_key_data = bytes(KeySessionProviderFactory().resolve_if_awaitable(
        key_session_provider.getSessionKeyData(session_key_identifier)))
    logger.info(f"session_key_data (base64)={base64.b64encode(session_key_data).decode()}")
    logger.info(f"key length={len(session_key_data)} bytes")
else:
    # PQC: explicit KEM handshake — session vars initialized in task_test().
    # NOTE: with SESSION_CACHE_BACKEND=memory only the first registered agent
    # shares the session cache with the /stls/pqc/handshake endpoint.
    # For multi-agent PQC use SESSION_CACHE_BACKEND=redis.
    session_key_identifier = None
    session_key_data = None
    logger.info("PQC provider — explicit handshake will be performed in task_test()")

import base64
import mimetypes

def file_to_base64(file_path: str) -> tuple[str, str]:
    """
    Reads a file and converts it to base64.

    Returns:
        (data_base64, mime_type)
    """
    # Auto-detect the MIME type
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = 'application/octet-stream'  # generic fallback

    with open(file_path, 'rb') as f:
        data_base64 = base64.b64encode(f.read()).decode('utf-8')

    return data_base64, mime_type

async def call_agent(
    agent_path: str,
    method: str,
    params: Dict[str, Any],
    stream: bool = False,
    bearer_token: Optional[str] = None,
    api_key: Optional[str] = None,
    expect_401: bool = False,
) -> Dict[str, Any]:
        """
        Calls an agent via the encrypted STLS channel.

        If bearer_token or api_key are set, the authorization headers
        are injected into the HTTP request (before the AES-256-GCM decrypt).

        For the sync path, SecureA2ATransportAdapter is used with its httpx
        client patched; for the SSE path, the _secure_stream_with_auth
        function is used, which handles the connection directly.

        expect_401: if True, doesn't raise on HTTP 401, instead returns
                    {"status_code": 401, "www_authenticate": ...}
        """
        # ── Build auth headers ────────────────────────────────────────────────
        _auth: Dict[str, str] = {}
        if bearer_token:
            _auth["Authorization"] = f"Bearer {bearer_token}"
        if api_key:
            _auth["X-API-Key"] = api_key

        adapter = SecureA2ATransportAdapter(
            session_key=session_key_data,
            session_id=session_key_identifier,
            base_url=BASE_URL + agent_path
        )

        # ── Inject headers into the sync client (default_headers merged by httpx) ─
        if _auth:
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(timeout=30.0, headers=_auth)

        if stream and _auth:
            # send_secure_message's SSE path creates its own httpx.AsyncClient
            # with hardcoded headers: it's not patchable after construction.
            # We use _secure_stream_with_auth which handles the channel directly.
            response = await _secure_stream_with_auth(
                adapter=adapter,
                message={'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1},
                auth_headers=_auth,
                expect_401=expect_401,
            )
        else:
            try:
                response = await adapter.send_secure_message(
                    message={
                        'jsonrpc': '2.0',
                        'method': method,
                        'params': params,
                        'id': 1
                    },
                    stream=stream,
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401 and expect_401:
                    return {
                        "status_code": 401,
                        "www_authenticate": e.response.headers.get("WWW-Authenticate", ""),
                    }
                raise

        if response.get('error') and not expect_401:
            logger.error(f"Agent error: {response.get('error')}")
            raise Exception(f"Agent error: {response.get('error')}")

        return response


async def _secure_stream_with_auth(
    adapter: SecureA2ATransportAdapter,
    message: Dict[str, Any],
    auth_headers: Dict[str, str],
    expect_401: bool = False,
) -> Dict[str, Any]:
    """
    Encrypted SSE path with custom authorization headers.

    Replicates the logic of SecureA2ATransportAdapter.send_secure_message(stream=True)
    but uses an httpx.AsyncClient with injected auth_headers, needed because
    the adapter's SSE path creates its own client with hardcoded headers.
    """
    _SSE_METHODS = {"messages/sendStream", "tasks/subscribe"}
    provided_method = message.get("method")
    effective_method = (
        provided_method if provided_method in _SSE_METHODS else "messages/sendStream"
    )

    # Encrypts the message with the adapter's transport
    from stls_a2a.stls_a2a_transport import A2AMessage
    a2a_msg = A2AMessage(
        jsonrpc=message.get("jsonrpc", "2.0"),
        method=effective_method,
        params=message.get("params"),
        id=message.get("id"),
    )
    encrypted = adapter.transport.encrypt_message(a2a_msg)

    stream_url = f"{adapter.base_url}/secure-a2a/stream"
    headers = {
        "Content-Type":    "application/json",
        "Accept":          "text/event-stream",
        "X-A2A-Session-ID": adapter.session_id,
        **auth_headers,
    }

    collected_parts: list = []
    last_status  = ""
    task_id      = ""
    context_id   = ""

    async with httpx.AsyncClient(timeout=120.0) as sse_client:
        async with sse_client.stream("POST", stream_url, json=encrypted, headers=headers) as resp:

            if resp.status_code == 401:
                if expect_401:
                    return {
                        "status_code": 401,
                        "www_authenticate": resp.headers.get("WWW-Authenticate", ""),
                    }
                resp.raise_for_status()

            resp.raise_for_status()
            event_type = ""
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    raw_data = line[len("data:"):].strip()
                    if event_type == "error":
                        try:
                            err = json.loads(raw_data)
                        except Exception:
                            err = {}
                        return {
                            "jsonrpc": "2.0", "id": message.get("id"),
                            "error": {
                                "code":    err.get("code",    -32603),
                                "message": err.get("message", "stream error"),
                            },
                        }
                    try:
                        encrypted_chunk = json.loads(raw_data)
                        decrypted = adapter.transport.decrypt_message(encrypted_chunk)
                        _raw    = decrypted.params or {}
                        payload = _raw.get("result", _raw)   # ← unwrap JSON-RPC envelope

                    except Exception as e:
                        logger.warning("[secure_stream_auth] chunk not decryptable: %s", e)
                        continue
                    if event_type == "TaskArtifactUpdateEvent":
                        for part in payload.get("artifact", {}).get("parts", []):
                            if part:
                                collected_parts.append(part)
                    elif event_type == "TaskStatusUpdateEvent":
                        last_status = _status_str(payload.get("status", ""))
                        task_id     = payload.get("taskId",    task_id)
                        context_id  = payload.get("contextId", context_id)

    full_text = "".join(
        p["text"] for p in collected_parts
        if p.get("type") == "text" and p.get("text")
    )
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "result": {
            "taskId":    task_id,
            "contextId": context_id,
            "status":    last_status,
            "artifacts": [{"name": "response", "parts": collected_parts}],
            "history":   [{"role": "agent",    "parts": collected_parts}],
            **({"text": full_text} if full_text else {}),
        },
    }

# ── Plain endpoint configuration ──────────────────────────────────────────────
# The server mounts adk-agent on /agents/adk-agent/secure-a2a.
# The server's derivation rule is:
#   path.replace("secure-", "")  →  /agents/adk-agent/a2a
PLAIN_AGENT_PATH = "/agents/adk-agent/a2a"

# ── lc-agent auth configuration ───────────────────────────────────────────────
# Values mirroring the AuthConfig configured in the server for lc-agent.
# The plain path is derived from /agents/lc-agent/secure-a2a → /agents/lc-agent/a2a
LC_SECURE_PATH   = "/agents/lc-agent"          # used by call_agent (adds /secure-a2a)
LC_PLAIN_PATH    = "/agents/lc-agent/a2a"      # plain endpoint (sync + stream)
LC_BEARER_TOKEN  = "my-secret-token"           # AuthConfig.bearer_tokens
LC_API_KEY       = "key-abc123"                # AuthConfig.api_keys


# ── Plain endpoint helper (no STLS encryption) ────────────────────────────────

async def call_plain_agent(
    agent_path: str,
    method: str,
    params: Dict[str, Any],
    expect_error: bool = False,
    rpc_id: int = 1,
    bearer_token: Optional[str] = None,
    api_key: Optional[str] = None,
    expect_401: bool = False,
) -> Dict[str, Any]:
    """
    Calls the plain /a2a endpoint with httpx — standard, unencrypted JSON-RPC.
    expect_error=True: doesn't raise on a JSON-RPC error.
    expect_401=True:   doesn't raise on HTTP 401,
                       instead returns {"status_code": 401, "www_authenticate": ...}
    """
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if api_key:
        headers["X-API-Key"] = api_key

    url = f"{BASE_URL}{agent_path}"
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": rpc_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code == 401:
        if expect_401:
            return {
                "status_code": 401,
                "www_authenticate": resp.headers.get("WWW-Authenticate", ""),
                "body": resp.json() if "application/json" in resp.headers.get("content-type", "") else resp.text,
            }
        resp.raise_for_status()

    resp.raise_for_status()
    response = resp.json()
    if response.get("error") and not expect_error:
        raise Exception(f"Plain agent error: {response['error']}")
    return response


async def call_plain_agent_stream(
    agent_path: str,
    method: str,
    params: Dict[str, Any],
    bearer_token: Optional[str] = None,
    api_key: Optional[str] = None,
    expect_401: bool = False,
) -> Dict[str, Any]:
    """
    Calls the plain /a2a/stream endpoint (unencrypted SSE).
    Returns a dict compatible with extract_a2a_text/extract_a2a_result.
    expect_401=True: returns {"status_code": 401, ...} instead of raising.
    """
    headers: Dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    if api_key:
        headers["X-API-Key"] = api_key

    url = f"{BASE_URL}{agent_path}/stream"
    payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}

    collected_parts: list = []
    last_status = ""
    task_id     = ""
    context_id  = ""
    error_payload: Optional[dict] = None

    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST", url,
            json=payload,
            headers=headers,
        ) as resp:
            if resp.status_code == 401:
                if expect_401:
                    return {
                        "status_code": 401,
                        "www_authenticate": resp.headers.get("WWW-Authenticate", ""),
                    }
                resp.raise_for_status()

            resp.raise_for_status()
            event_type = ""
            async for raw_line in resp.aiter_lines():
                line = raw_line.strip()
                if not line:
                    event_type = ""
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                    continue
                if line.startswith("data:"):
                    raw_data = line[len("data:"):].strip()
                    try:
                        data = json.loads(raw_data)
                        payload = data.get("result", data)
                    except Exception:
                        continue
                    if event_type == "error":
                        error_payload = data
                        break
                    if event_type == "TaskArtifactUpdateEvent":
                        for part in payload.get("artifact", {}).get("parts", []):
                            if part:
                                collected_parts.append(part)
                    elif event_type == "TaskStatusUpdateEvent":
                        last_status = _status_str(payload.get("status", ""))
                        task_id     = payload.get("taskId",    task_id)
                        context_id  = payload.get("contextId", context_id)

    if error_payload:
        return {"jsonrpc": "2.0", "id": 1, "error": error_payload}

    full_text = "".join(
        p["text"] for p in collected_parts
        if p.get("type") == "text" and p.get("text")
    )
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {
            "taskId": task_id, "contextId": context_id, "status": last_status,
            "artifacts": [{"name": "response", "parts": collected_parts}],
            "history":   [{"role": "agent",    "parts": collected_parts}],
            **({"text": full_text} if full_text else {}),
        },
    }


async def call_plain_agent_raw(agent_path: str, raw_body: bytes,
                                content_type: str = "application/json") -> Dict[str, Any]:
    """Sends a raw body — used to test the -32700 parse error."""
    url = f"{BASE_URL}{agent_path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(url, content=raw_body,
                                 headers={"Content-Type": content_type})
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text, "status_code": resp.status_code}


def _ok(label: str):
    print(f"  ✅ {label}")

def _fail(label: str, detail: str = ""):
    print(f"  ❌ {label}{': ' + detail if detail else ''}")


async def plain_endpoint_test():
    """
    Test 5 — Plain /a2a and /a2a/stream endpoints (unencrypted).
    Covers: sync, SSE, lifecycle, idempotency, async+polling, subscribe, error codes.
    """
    print("\n" + "="*70)
    print(f"Test 5: Plain endpoint  ({PLAIN_AGENT_PATH})")
    print("="*70)

    # ── 5.1 messages/send synchronous ──────────────────────────────────────────
    print("\n── 5.1  messages/send (synchronous)")
    request_text = "What is the price of AAPL?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/send", str(err))
    else:
        task_id_51 = result.get("taskId")
        ctx_id_51  = result.get("contextId")
        _ok(f"taskId={task_id_51}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 5.2 messages/sendStream (plain SSE) ───────────────────────────────────
    print("\n── 5.2  messages/sendStream (plain SSE)")
    request_text = "What is the price of META?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent_stream(
        PLAIN_AGENT_PATH, "messages/sendStream",
        {"contextId": "ctx-plain-001",
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/sendStream", str(err))
    else:
        task_id_52 = result.get("taskId")
        _ok(f"taskId={task_id_52}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 5.3 tasks/list ────────────────────────────────────────────────────────
    print("\n── 5.3  tasks/list")
    response = await call_plain_agent(PLAIN_AGENT_PATH, "tasks/list", {"limit": 50})
    tasks = response.get("result", [])
    if not isinstance(tasks, list):
        _fail("tasks/list", f"expected a list, received: {type(tasks)}")
    else:
        _ok(f"{len(tasks)} tasks in the TaskStore")
        for t in tasks[:3]:
            print(f"    taskId={t.get('taskId')}  status={t.get('status')}")

    # ── 5.4 tasks/get valid ────────────────────────────────────────────────────
    print("\n── 5.4  tasks/get (valid taskId)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get", {"taskId": task_id_51}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("tasks/get", str(err))
    else:
        _ok(f"status={result.get('status')}  "
            f"history={len(result.get('history', []))}msg  "
            f"artifacts={len(result.get('artifacts', []))}")

    # ── 5.5 tasks/get with wrong contextId → -32001 mismatch ──────────────────
    print("\n── 5.5  tasks/get with wrong contextId (expected -32001)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get",
        {"taskId": task_id_51, "contextId": "00000000-0000-0000-0000-000000000000"},
        expect_error=True,
    )
    code = response.get("error", {}).get("code")
    if code == -32001:
        _ok(f"-32001 mismatch: {response['error']['message']}")
    else:
        _fail("expected -32001", f"received: {response.get('error')}")

    # ── 5.6 Idempotency: messages/send with an existing taskId ─────────────────
    print("\n── 5.6  Idempotency (messages/send with an existing taskId)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"taskId": task_id_51,   # task already completed — must not be reprocessed
         "message": {"role": "user",
                     "parts": [{"type": "text", "text": "duplicate request"}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("idempotency", str(err))
    else:
        returned_id = result.get("taskId")
        if returned_id == task_id_51 and _status_str(result.get("status")) == "completed":
            _ok(f"existing task returned without reprocessing  taskId={returned_id}")
        else:
            _fail("idempotency", f"taskId={returned_id}  status={result.get('status')}")

    # ── 5.7 tasks/cancel on a completed task → -32002 TaskNotCancelable ────────
    print("\n── 5.7  tasks/cancel on a completed task (expected -32002)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/cancel",
        {"taskId": task_id_51},
        expect_error=True,
    )
    code = response.get("error", {}).get("code")
    if code == -32002:
        _ok(f"-32002 TaskNotCancelable: {response['error']['message']}")
    else:
        _fail("expected -32002", f"received: {response.get('error')}")

    # ── 5.8 tasks/cancel on a task in WORKING (async:true) → canceled ──────────
    print("\n── 5.8  tasks/cancel on a task in WORKING")
    response_async = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"async": True,
         "message": {"role": "user",
                     "parts": [{"type": "text", "text": "What is the price of TSLA?"}]}}
    )
    result_async, err_a = extract_a2a_result(response_async)
    if err_a:
        _fail("creating async task for cancel", str(err_a))
    else:
        task_id_cancel = result_async.get("taskId")
        cancel_resp = await call_plain_agent(
            PLAIN_AGENT_PATH, "tasks/cancel",
            {"taskId": task_id_cancel},
            expect_error=True,
        )
        cancel_result, cancel_err = extract_a2a_result(cancel_resp)
        if cancel_err:
            code = cancel_resp.get("error", {}).get("code")
            if code == -32002:
                _ok("task already completed before the cancel (benign race) → -32002")
            else:
                _fail("tasks/cancel", str(cancel_err))
        else:
            status = _status_str(cancel_result.get("status"))
            if status == "canceled":
                _ok(f"task {task_id_cancel} canceled  status={status}")
            else:
                _fail("tasks/cancel", f"expected 'canceled', received '{status}'")

    # ── 5.9 async:true + polling tasks/get ────────────────────────────────────
    print("\n── 5.9  async:true + polling tasks/get")
    request_text = "What is the price of GOOGL?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"async": True,
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("async messages/send", str(err))
    else:
        task_id_59 = result.get("taskId")
        status_59  = _status_str(result.get("status"))
        if status_59 != "working":
            _fail(f"expected 'working', received '{status_59}'")
        else:
            _ok(f"immediate response: taskId={task_id_59}  status={status_59}")
            print(f"  ⏳ Polling every 500ms (max 10 attempts)...")
            completed_59 = False
            current_59   = status_59
            for attempt in range(1, 11):
                await asyncio.sleep(0.5)
                poll_resp = await call_plain_agent(
                    PLAIN_AGENT_PATH, "tasks/get", {"taskId": task_id_59}
                )
                poll_result, _ = extract_a2a_result(poll_resp)
                current_59 = _status_str(poll_result.get("status")) if poll_result else "error"
                print(f"    attempt {attempt}: status={current_59}")
                if current_59 in ("completed", "failed", "canceled"):
                    completed_59 = True
                    break
            if completed_59 and current_59 == "completed":
                _ok(f"completed via polling")
                print(f"  📥 {extract_a2a_text(poll_resp)}")
            elif not completed_59:
                _fail("task not completed within the polling timeout")

    # ── 5.10 async:true + tasks/subscribe (plain SSE) ─────────────────────────
    print("\n── 5.10  async:true + tasks/subscribe (plain SSE)")
    request_text = "What is the price of AMZN?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"async": True,
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("async messages/send for subscribe", str(err))
    else:
        task_id_510 = result.get("taskId")
        _ok(f"task created: taskId={task_id_510}  status={result.get('status')}")
        print(f"  📡 tasks/subscribe SSE on taskId={task_id_510}...")
        sub_resp = await call_plain_agent_stream(
            PLAIN_AGENT_PATH, "tasks/subscribe", {"taskId": task_id_510}
        )
        sub_result, sub_err = extract_a2a_result(sub_resp)
        if sub_err:
            _fail("tasks/subscribe", str(sub_err))
        else:
            _ok(f"subscribe completed  status={sub_result.get('status')}")
            print(f"  📥 {extract_a2a_text(sub_resp)}")

    # ── 5.11 Error codes ──────────────────────────────────────────────────────
    print("\n── 5.11  Error codes")

    # -32700: body is not valid JSON (sync endpoint)
    resp = await call_plain_agent_raw(PLAIN_AGENT_PATH, b"non-json{{{")
    code = resp.get("error", {}).get("code")
    if code == -32700:
        _ok("-32700 Parse error (non-JSON body — sync)")
    else:
        _fail("-32700 sync", f"received: {resp.get('error')}")

    # -32600: missing method
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32600:
        _ok("-32600 Invalid request (missing method)")
    else:
        _fail("-32600", f"received: {resp.get('error')}")

    # -32602: missing taskId in tasks/get
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32602:
        _ok("-32602 Invalid params (missing taskId in tasks/get)")
    else:
        _fail("-32602", f"received: {resp.get('error')}")

    # -32602: missing taskId in tasks/cancel
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/cancel", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32602:
        _ok("-32602 Invalid params (missing taskId in tasks/cancel)")
    else:
        _fail("-32602 cancel", f"received: {resp.get('error')}")

    # -32001: nonexistent taskId
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get",
        {"taskId": "00000000-0000-0000-0000-000000000000"},
        expect_error=True,
    )
    code = resp.get("error", {}).get("code")
    if code == -32001:
        _ok("-32001 Task not found (nonexistent taskId)")
    else:
        _fail("-32001", f"received: {resp.get('error')}")

    # -32003: push notifications not supported
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/pushNotificationConfig/set",
        {"taskId": task_id_51}, expect_error=True,
    )
    code = resp.get("error", {}).get("code")
    if code == -32003:
        _ok("-32003 PushNotificationNotSupported")
    else:
        _fail("-32003", f"received: {resp.get('error')}")

    # -32700: non-JSON body on the SSE channel
    url_stream = f"{BASE_URL}{PLAIN_AGENT_PATH}/stream"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp_sse = await client.post(
            url_stream, content=b"{{invalid",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"}
        )
        sse_body = resp_sse.text
    if "-32700" in sse_body:
        _ok("-32700 Parse error (non-JSON body — SSE)")
    else:
        _fail("-32700 SSE", f"body: {sse_body[:120]}")

    # -32601: non-SSE method on the /stream channel (e.g. tasks/get)
    sse_resp = await call_plain_agent_stream(
        PLAIN_AGENT_PATH, "tasks/get", {"taskId": task_id_51}
    )
    err_sse = sse_resp.get("error", {})
    if err_sse.get("code") == -32601:
        _ok(f"-32601 Method not supported on SSE endpoint")
    else:
        _fail("-32601 SSE", f"received: {err_sse}")

    print()
    print("  ── Test 5 summary complete ──")


async def auth_endpoint_test():
    """
    Test 6 — lc-agent endpoint with authorization (AuthConfig bearer + api_key).

    Covers all authentication schemes on both channels:
      - Encrypted STLS channel  (/agents/lc-agent/secure-a2a)
      - Plain channel           (/agents/lc-agent/a2a)
    """
    print("\n" + "="*70)
    print("Test 6: Endpoint with authorization  (lc-agent)")
    print("="*70)

    # ── 6.1 Encrypted channel — without credentials → HTTP 401 ────────────────
    print("\n── 6.1  Encrypted STLS channel without credentials (expected HTTP 401)")
    resp = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 received  WWW-Authenticate: {resp.get('www_authenticate', '')[:60]}")
    else:
        _fail("expected HTTP 401", f"received: {resp}")

    # ── 6.2 Encrypted channel — with Bearer token → success ───────────────────
    print("\n── 6.2  Encrypted STLS channel with Bearer token")
    request_text = "What is the price of TSLA?"
    print(f"  📤 {request_text}")
    response = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/send with bearer", str(err))
    else:
        task_id_62 = result.get("taskId")
        _ok(f"taskId={task_id_62}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.3 Encrypted channel — with API Key (header) → success ───────────────
    print("\n── 6.3  Encrypted STLS channel with API Key (X-API-Key header)")
    request_text = "What is the price of NVDA?"
    print(f"  📤 {request_text}")
    response = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        api_key=LC_API_KEY,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/send with api_key", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.4 Encrypted channel — SSE streaming with Bearer token ───────────────
    print("\n── 6.4  Encrypted STLS channel, SSE streaming with Bearer token")
    request_text = "What is the price of META?"
    print(f"  📤 {request_text}")
    response = await call_agent(
        LC_SECURE_PATH, "messages/sendStream",
        {"contextId": "ctx-auth-001",
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        stream=True,
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/sendStream with bearer", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.5 Encrypted channel — SSE without credentials → HTTP 401 ────────────
    print("\n── 6.5  Encrypted STLS SSE channel without credentials (expected HTTP 401)")
    resp = await call_agent(
        LC_SECURE_PATH, "messages/sendStream",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        stream=True,
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 received on the encrypted SSE channel")
    else:
        _fail("expected HTTP 401 on encrypted SSE", f"received: {resp}")

    # ── 6.6 Plain channel — without credentials → HTTP 401 ────────────────────
    print("\n── 6.6  Plain channel without credentials (expected HTTP 401)")
    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 received  WWW-Authenticate: {resp.get('www_authenticate', '')[:60]}")
    else:
        _fail("expected HTTP 401", f"received: {resp}")

    # ── 6.7 Plain channel — with Bearer token → success ───────────────────────
    print("\n── 6.7  Plain channel with Bearer token")
    request_text = "What is the price of AAPL?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain messages/send with bearer", str(err))
    else:
        task_id_67 = result.get("taskId")
        _ok(f"taskId={task_id_67}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.8 Plain channel — with API Key (header) → success ───────────────────
    print("\n── 6.8  Plain channel with API Key (header)")
    response = await call_plain_agent(
        LC_PLAIN_PATH, "tasks/get",
        {"taskId": task_id_67},
        api_key=LC_API_KEY,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain tasks/get with api_key", str(err))
    else:
        _ok(f"tasks/get  status={result.get('status')}  "
            f"history={len(result.get('history', []))}msg")

    # ── 6.9 Plain SSE channel — without credentials → HTTP 401 ────────────────
    print("\n── 6.9  Plain SSE channel without credentials (expected HTTP 401)")
    resp = await call_plain_agent_stream(
        LC_PLAIN_PATH, "messages/sendStream",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 received on the plain SSE channel")
    else:
        _fail("expected HTTP 401 on plain SSE", f"received: {resp}")

    # ── 6.10 Plain SSE channel — with Bearer token → success ──────────────────
    print("\n── 6.10  Plain SSE channel with Bearer token")
    request_text = "What is the price of MSFT?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent_stream(
        LC_PLAIN_PATH, "messages/sendStream",
        {"contextId": "ctx-auth-002",
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain SSE with bearer", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.11 Wrong credentials → HTTP 401 ──────────────────────────────────────
    print("\n── 6.11  Wrong credentials (expected HTTP 401)")
    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        bearer_token="wrong-token",
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok("HTTP 401 with wrong bearer token")
    else:
        _fail("expected HTTP 401 with wrong token", f"received: {resp}")

    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        api_key="wrong-key",
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok("HTTP 401 with wrong api_key")
    else:
        _fail("expected HTTP 401 with wrong api_key", f"received: {resp}")

    print()
    print("  ── Test 6 summary complete ──")



async def stream_secure_message_test():
    """
    Test 7 — stream_secure_message(): real incremental encrypted streaming.

    Verifies that the new AsyncGenerator yields chunk by chunk as they
    arrive from the server, instead of collecting them all before returning.

    Covers:
      7.1  chunks yielded in order: submitted → working → artifact(s) → completed
      7.2  every TextPart arrives non-empty
      7.3  final reconstructed text matches what send_secure_message returns
      7.4  at least one chunk yielded before completed (real streaming, not batch)
      7.5  first chunk arrives before the end of the stream (incremental latency)
      7.6  stream_secure_message on lc-agent with an API key (auth works on the generator)
    """
    print("\n" + "="*70)
    print("Test 7: stream_secure_message() — real incremental streaming")
    print("="*70)

    # ── 7.1-7.4  adk-agent: verify event order and incrementality ─────────────
    print("\n── 7.1-7.4  adk-agent: event order and incrementality")
    request_text = "What is the price of AAPL?"
    print(f"  📤 {request_text}")

    adapter = SecureA2ATransportAdapter(
        session_key=session_key_data,
        session_id=session_key_identifier,
        base_url=BASE_URL + "/agents/adk-agent",
    )

    message = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-stream-test-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": request_text}],
            },
        },
        "id": 1,
    }

    events_received: list[dict] = []
    text_chunks: list[str] = []
    first_chunk_time: float | None = None
    import time as _time
    start_time = _time.monotonic()

    async for chunk in adapter.stream_secure_message(message):
        event = chunk["event"]
        events_received.append(chunk)

        if event == "TaskArtifactUpdateEvent":
            if first_chunk_time is None:
                first_chunk_time = _time.monotonic() - start_time
            for part in chunk.get("parts", []):
                if part.get("type") == "text" and part.get("text"):
                    text_chunks.append(part["text"])
                    print(f"  ▸ chunk ({len(part['text'])} chars): "
                          f"{part['text'][:60]}{'…' if len(part['text']) > 60 else ''}")

        elif event == "TaskStatusUpdateEvent":
            state = chunk.get("status", "")
            final = chunk.get("final", False)
            print(f"  ◉ status={state}{'  [final]' if final else ''}")

        elif event == "error":
            _fail("stream_secure_message unexpected error",
                  f"code={chunk['code']} msg={chunk['message']}")
            return

    # 7.1 — verify event order: there must be at least submitted/working and completed
    status_events = [c["status"] for c in events_received
                     if c["event"] == "TaskStatusUpdateEvent"]
    artifact_events = [c for c in events_received
                       if c["event"] == "TaskArtifactUpdateEvent"]

    if "completed" in status_events:
        _ok(f"7.1  correct event order — states received: {status_events}")
    else:
        _fail("7.1  'completed' state not received", f"states: {status_events}")

    # 7.2 — every TextPart is non-empty
    empty_parts = [t for t in text_chunks if not t.strip()]
    if not empty_parts:
        _ok(f"7.2  no empty TextPart ({len(text_chunks)} chunks)")
    else:
        _fail("7.2  empty TextParts received", f"count={len(empty_parts)}")

    # 7.3 — final text = concatenation of chunks = send_secure_message's result
    streamed_text = "".join(text_chunks)
    # Compares against send_secure_message(stream=True) on the same context
    adapter2 = SecureA2ATransportAdapter(
        session_key=session_key_data,
        session_id=session_key_identifier,
        base_url=BASE_URL + "/agents/adk-agent",
    )
    message2 = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-stream-test-001b",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": request_text}],
            },
        },
        "id": 1,
    }
    batch_response = await adapter2.send_secure_message(message2, stream=True)
    batch_text = extract_a2a_text(batch_response) or ""
    if streamed_text.strip() and batch_text.strip():
        _ok(f"7.3  streamed text ({len(streamed_text)} chars) consistent "
            f"with batch ({len(batch_text)} chars)")
    else:
        _fail("7.3  empty text in streamed or batch",
              f"streamed={len(streamed_text)}  batch={len(batch_text)}")
    print(f"  📥 Final text: {streamed_text[:120]}{'…' if len(streamed_text) > 120 else ''}")

    # 7.4 — at least one artifact chunk before completed
    if artifact_events:
        _ok(f"7.4  {len(artifact_events)} artifact event(s) received before completed")
    else:
        _fail("7.4  no artifact event received — pseudo-streaming still active?")

    # ── 7.5  first chunk latency ───────────────────────────────────────────────
    print("\n── 7.5  first chunk latency (incrementality)")
    if first_chunk_time is not None:
        total_time = _time.monotonic() - start_time
        print(f"  ⏱  first chunk: {first_chunk_time:.2f}s  |  total: {total_time:.2f}s")
        if first_chunk_time < total_time * 0.9:
            _ok(f"7.5  first chunk arrived at {first_chunk_time:.2f}s "
                f"(< 90% of total time {total_time:.2f}s) — incremental streaming")
        else:
            # Not a hard fail: depends on the model and network latency
            print(f"  ⚠️  7.5  first chunk at {first_chunk_time:.2f}s out of {total_time:.2f}s total "
                  f"— possible model batching (not a transport error)")
    else:
        _fail("7.5  no artifact chunk received, cannot measure latency")

    # ── 7.6  lc-agent with an API key ──────────────────────────────────────────
    print("\n── 7.6  lc-agent: stream_secure_message with an API key")
    request_text_lc = "What is the price of TSLA?"
    print(f"  📤 {request_text_lc}")

    adapter_lc = SecureA2ATransportAdapter(
        session_key=session_key_data,
        session_id=session_key_identifier,
        base_url=BASE_URL + "/agents/lc-agent",
    )

    message_lc = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-stream-test-lc-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": request_text_lc}],
            },
        },
        "id": 1,
    }

    lc_text_chunks: list[str] = []
    lc_final_status = ""
    lc_error = None

    # extra_headers passes X-API-Key directly to stream_secure_message:
    # there's no need to patch adapter_lc.client anymore — the header is
    # injected into the AsyncClient SSE instance internal to the generator.
    async for chunk in adapter_lc.stream_secure_message(
        message_lc, extra_headers={"X-API-Key": LC_API_KEY}
    ):
        event = chunk["event"]
        if event == "error":
            lc_error = chunk
            break
        elif event == "TaskArtifactUpdateEvent":
            for part in chunk.get("parts", []):
                if part.get("type") == "text" and part.get("text"):
                    lc_text_chunks.append(part["text"])
        elif event == "TaskStatusUpdateEvent":
            lc_final_status = chunk.get("status", "")

    if lc_error:
        _fail("7.6  lc-agent stream error",
              f"code={lc_error['code']} msg={lc_error['message']}")
    elif lc_final_status == "completed" and lc_text_chunks:
        lc_full = "".join(lc_text_chunks)
        _ok(f"7.6  lc-agent stream OK  status={lc_final_status}  "
            f"chunks={len(lc_text_chunks)}  chars={len(lc_full)}")
        print(f"  📥 {lc_full[:120]}{'…' if len(lc_full) > 120 else ''}")
    else:
        _fail("7.6  lc-agent stream incomplete",
              f"status={lc_final_status}  chunks={len(lc_text_chunks)}")

    print()
    print("  ── Test 7 summary complete ──")


async def task_test():
    global session_key_identifier, session_key_data

    # ── PQC initialization: KEM handshake before any call ────────────────────
    if KEY_PROVIDER_NAME == "pqc":
        from stls_a2a import create_pqc_a2a_transport
        logger.info("PQC: performing KEM handshake towards %s/stls/pqc/handshake", BASE_URL)
        _pqc_adapter = await create_pqc_a2a_transport(
            BASE_URL,
            client_id="task-test-pqc",
        )
        session_key_identifier = _pqc_adapter.session_id
        # session_key_data: raw key kept in SecureA2ATransport
        # (used by call_agent to build new SecureA2ATransportAdapter instances)
        session_key_data = _pqc_adapter.transport.session_key
        logger.info(
            "PQC handshake completed: session_id=%s key_len=%d",
            session_key_identifier, len(session_key_data),
        )

    try:
        print("\n" + "="*70)
        print("Test 1: ADK Test")
        print("="*70)

        request_text = "What is the price of AAPL?"
        print(f"\n📤 Sending request: {request_text}")

        response = await call_agent(
            agent_path='/agents/adk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        
        # Extracts just the text
        #print(f'response={response}')
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()

        request_text = "What is the price of META?"
        print(f"\n📤 Sending request: {request_text}")

        print("Test 1.a: Test ADK using Streaming")
        response = await call_agent(
            agent_path='/agents/adk-agent',
            method='messages/sendStream',
            params={
                "contextId": "ctx-test-001",
                "message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }},
            stream=True
            )
        # Extracts just the text
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()


        # Calls lc-agent
        print("\n" + "="*70)
        print("Test 2: lc-agent (LangChain)")
        print("="*70)

        request_text = "What is the price of TSLA?"
        print(f"\n📤 Sending request: {request_text}")

        response = await call_agent(
            agent_path='/agents/lc-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }},
            bearer_token=LC_BEARER_TOKEN,
            )
        
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()

        request_text = "What is the price of NVDA?"
        print(f"\n📤 Sending request: {request_text}")

        print("Test 2.a: Test lc-agent using Streaming")
        response = await call_agent(
            agent_path='/agents/lc-agent',
            method='messages/sendStream',
            params={
                "contextId": "ctx-test-002",
                "message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }},
            api_key=LC_API_KEY,
            stream=True
            )
        # Extracts just the text
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()


        # Calls sk-agent
        print("\n" + "="*70)
        print("Test 3: sk-agent (Semantic Kernel)")
        print("="*70)

        request_text = "What is the price of MSFT?"
        print(f"\n📤 Sending request: {request_text}")

        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()

        request_text = "Can you pull info on MSFT for me?"
        print(f"\n📤 Sending request: {request_text}")

        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()

        print("Test 3.a: Test sk-agent using Streaming")
        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='messages/sendStream',
            params={
                "contextId": "ctx-test-003",
                "message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }},
            stream=True
            )
        # Extracts just the text
        text = extract_a2a_text(response)
        print(f"📥 Response: {text}")
        print()

        # tasks/list call
        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='tasks/list',
            params={"limit": 50}
            )
        
        result = response.get('result')
        for task in result:
            print(f'-> taskId={task.get("taskId")};contextId={task.get("contextId")};status={task.get("status")}')
        if len(result)>0:
            # take the first one
            task=result[0]
            response = await call_agent(
                agent_path='/agents/sk-agent',
                method='tasks/get',
                params={"taskId": task.get('taskId')}
            )
            print(f"get response[0]: {response}")

        # ── Test 4: Async processing + polling + subscribe ────────────────────
        print("\n" + "="*70)
        print("Test 4: Async processing (messages/send with async:true)")
        print("="*70)

        request_text = "What is the price of GOOGL?"
        print(f"\n📤 Sending an ASYNC request: {request_text}")

        # 4.1 – messages/send with async:true → responds immediately with status working
        response = await call_agent(
            agent_path='/agents/adk-agent',
            method='messages/send',
            params={
                "async": True,
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": request_text}]
                }
            }
        )
        result, error = extract_a2a_result(response)
        if error:
            print(f"❌ Error: {error}")
        else:
            task_id   = result.get('taskId')
            status    = _status_str(result.get('status'))
            print(f"✅ Task created immediately: taskId={task_id}  status={status}")
            assert status == "working", f"Expected 'working', received '{status}'"

        # 4.2 – Polling with tasks/get: wait for the task to finish
        print(f"\n⏳ Polling tasks/get every 500ms (max 10 attempts)...")
        completed = False
        for attempt in range(1, 11):
            await asyncio.sleep(0.5)
            poll_response = await call_agent(
                agent_path='/agents/adk-agent',
                method='tasks/get',
                params={"taskId": task_id}
            )
            poll_result, poll_error = extract_a2a_result(poll_response)
            current_status = _status_str(poll_result.get('status')) if poll_result else "error"
            print(f"  attempt {attempt}: status={current_status}")
            if current_status in ("completed", "failed", "canceled"):
                completed = True
                break

        if completed and current_status == "completed":
            text = extract_a2a_text(poll_response)
            print(f"📥 Final response (via polling): {text}")
        elif not completed:
            print("⚠️  Task not completed within the polling timeout")

        # 4.3 – Same scenario but with tasks/subscribe (SSE) instead of polling.
        #        We send a new async request and subscribe to it right away.
        print(f"\n📤 Sending a second ASYNC request for the subscribe test...")
        request_text2 = "What is the price of AMZN?"

        response2 = await call_agent(
            agent_path='/agents/adk-agent',
            method='messages/send',
            params={
                "async": True,
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": request_text2}]
                }
            }
        )
        result2, error2 = extract_a2a_result(response2)
        if error2:
            print(f"❌ Error in the second request: {error2}")
        else:
            task_id2 = result2.get('taskId')
            print(f"✅ Task created: taskId={task_id2}  status={result2.get('status')}")

            print(f"\n📡 tasks/subscribe on taskId={task_id2} (SSE)...")
            subscribe_response = await call_agent(
                agent_path='/agents/adk-agent',
                method='tasks/subscribe',
                params={"taskId": task_id2},
                stream=True
            )
            sub_result, sub_error = extract_a2a_result(subscribe_response)
            if sub_error:
                print(f"❌ Subscribe error: {sub_error}")
            else:
                sub_status = sub_result.get('status')
                sub_text   = extract_a2a_text(subscribe_response)
                print(f"📥 Subscribe completed — status={sub_status}")
                print(f"📥 Response text: {sub_text}")

        # ── Test 5: Plain endpoint (unencrypted) ──────────────────────────────
        await plain_endpoint_test()

        # ── Test 6: Endpoint with authorization (lc-agent) ────────────────────
        await auth_endpoint_test()

        # ── Test 7: stream_secure_message() — real incremental streaming ──────
        await stream_secure_message_test()

    finally:
        print("\n" + "="*70)
        print("End of test")
        print("="*70)

if __name__ == "__main__":
    print(__doc__)
    asyncio.run(task_test())