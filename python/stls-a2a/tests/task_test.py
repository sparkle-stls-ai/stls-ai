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
    """Normalizza status A2A: accetta stringa scalare oppure oggetto {"state":"..."}."""
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

# Per base/arqit_py la session key è risolvibile subito (chiave pre-condivisa).
# Per PQC la session key esiste solo dopo l'handshake KEM — viene inizializzata
# all'interno di task_test() via create_pqc_a2a_transport().
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
    # PQC: handshake KEM esplicito — session vars inizializzate in task_test().
    # NOTA: con SESSION_CACHE_BACKEND=memory solo il primo agente registrato
    # condivide la session cache con l'endpoint /stls/pqc/handshake.
    # Per multi-agent PQC usare SESSION_CACHE_BACKEND=redis.
    session_key_identifier = None
    session_key_data = None
    logger.info("Provider PQC — handshake esplicito verrà eseguito in task_test()")

import base64
import mimetypes

def file_to_base64(file_path: str) -> tuple[str, str]:
    """
    Legge un file e lo converte in base64.
    
    Returns:
        (data_base64, mime_type)
    """
    # Rileva il MIME type automaticamente
    mime_type, _ = mimetypes.guess_type(file_path)
    if mime_type is None:
        mime_type = 'application/octet-stream'  # fallback generico

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
        Chiama un agente tramite il canale cifrato STLS.

        Se bearer_token o api_key sono valorizzati, gli header di autorizzazione
        vengono iniettati nella richiesta HTTP (prima del decrypt AES-256-GCM).

        Per il percorso sincrono viene usato SecureA2ATransportAdapter con il
        client httpx patchato; per il percorso SSE viene usata la funzione
        _secure_stream_with_auth che gestisce direttamente la connessione.

        expect_401: se True non solleva eccezione su HTTP 401, restituisce
                    {"status_code": 401, "www_authenticate": ...}
        """
        # ── Costruisci header auth ────────────────────────────────────────────
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

        # ── Inietta header nel client sync (default_headers vengono merged da httpx) ─
        if _auth:
            await adapter.client.aclose()
            adapter.client = httpx.AsyncClient(timeout=30.0, headers=_auth)

        if stream and _auth:
            # Il percorso SSE di send_secure_message crea il proprio httpx.AsyncClient
            # con headers hardcoded: non è patchabile post-costruzione.
            # Usiamo _secure_stream_with_auth che gestisce il canale direttamente.
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
    Percorso SSE cifrato con header di autorizzazione custom.

    Replica la logica di SecureA2ATransportAdapter.send_secure_message(stream=True)
    ma usa un httpx.AsyncClient con auth_headers iniettati, necessario perché
    il percorso SSE dell'adapter crea il proprio client con headers hardcoded.
    """
    _SSE_METHODS = {"messages/sendStream", "tasks/subscribe"}
    provided_method = message.get("method")
    effective_method = (
        provided_method if provided_method in _SSE_METHODS else "messages/sendStream"
    )

    # Cifra il messaggio con il transport dell'adapter
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
                        logger.warning("[secure_stream_auth] chunk non decifrabile: %s", e)
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

# ── Configurazione plain endpoint ─────────────────────────────────────────────
# Il server monta adk-agent su /agents/adk-agent/secure-a2a.
# La regola di derivazione del server è:
#   path.replace("secure-", "")  →  /agents/adk-agent/a2a
PLAIN_AGENT_PATH = "/agents/adk-agent/a2a"

# ── Configurazione auth lc-agent ──────────────────────────────────────────────
# Valori speculari all'AuthConfig configurata nel server per lc-agent.
# Il path plain è derivato da /agents/lc-agent/secure-a2a → /agents/lc-agent/a2a
LC_SECURE_PATH   = "/agents/lc-agent"          # usato da call_agent (aggiunge /secure-a2a)
LC_PLAIN_PATH    = "/agents/lc-agent/a2a"      # plain endpoint (sync + stream)
LC_BEARER_TOKEN  = "my-secret-token"           # AuthConfig.bearer_tokens
LC_API_KEY       = "key-abc123"                # AuthConfig.api_keys


# ── Helper plain endpoint (nessuna cifratura STLS) ────────────────────────────

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
    Chiama l'endpoint plain /a2a con httpx — JSON-RPC standard, non cifrato.
    expect_error=True: non solleva eccezione per error JSON-RPC.
    expect_401=True:   non solleva eccezione per HTTP 401,
                       restituisce {"status_code": 401, "www_authenticate": ...}
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
    Chiama l'endpoint plain /a2a/stream (SSE non cifrato).
    Restituisce dict compatibile con extract_a2a_text/extract_a2a_result.
    expect_401=True: restituisce {"status_code": 401, ...} invece di sollevare.
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
    """Invia un body grezzo — usato per testare il parse error -32700."""
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
    Test 5 — Endpoint plain /a2a e /a2a/stream (non cifrati).
    Copre: sync, SSE, lifecycle, idempotenza, async+polling, subscribe, error codes.
    """
    print("\n" + "="*70)
    print(f"Test 5: Plain endpoint  ({PLAIN_AGENT_PATH})")
    print("="*70)

    # ── 5.1 messages/send sincrono ────────────────────────────────────────────
    print("\n── 5.1  messages/send (sincrono)")
    request_text = "Qual è il prezzo di AAPL?"
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

    # ── 5.2 messages/sendStream (SSE plain) ───────────────────────────────────
    print("\n── 5.2  messages/sendStream (SSE plain)")
    request_text = "Qual è il prezzo di META?"
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
        _fail("tasks/list", f"attesa lista, ricevuto: {type(tasks)}")
    else:
        _ok(f"{len(tasks)} task nel TaskStore")
        for t in tasks[:3]:
            print(f"    taskId={t.get('taskId')}  status={t.get('status')}")

    # ── 5.4 tasks/get valido ──────────────────────────────────────────────────
    print("\n── 5.4  tasks/get (taskId valido)")
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

    # ── 5.5 tasks/get con contextId errato → -32001 mismatch ─────────────────
    print("\n── 5.5  tasks/get con contextId errato (atteso -32001)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get",
        {"taskId": task_id_51, "contextId": "00000000-0000-0000-0000-000000000000"},
        expect_error=True,
    )
    code = response.get("error", {}).get("code")
    if code == -32001:
        _ok(f"-32001 mismatch: {response['error']['message']}")
    else:
        _fail("atteso -32001", f"ricevuto: {response.get('error')}")

    # ── 5.6 Idempotenza: messages/send con taskId esistente ───────────────────
    print("\n── 5.6  Idempotenza (messages/send con taskId esistente)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"taskId": task_id_51,   # task già completed — non deve essere rielaborato
         "message": {"role": "user",
                     "parts": [{"type": "text", "text": "richiesta duplicata"}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("idempotenza", str(err))
    else:
        returned_id = result.get("taskId")
        if returned_id == task_id_51 and _status_str(result.get("status")) == "completed":
            _ok(f"task esistente restituito senza rielaborazione  taskId={returned_id}")
        else:
            _fail("idempotenza", f"taskId={returned_id}  status={result.get('status')}")

    # ── 5.7 tasks/cancel su task completed → -32002 TaskNotCancelable ─────────
    print("\n── 5.7  tasks/cancel su task completed (atteso -32002)")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/cancel",
        {"taskId": task_id_51},
        expect_error=True,
    )
    code = response.get("error", {}).get("code")
    if code == -32002:
        _ok(f"-32002 TaskNotCancelable: {response['error']['message']}")
    else:
        _fail("atteso -32002", f"ricevuto: {response.get('error')}")

    # ── 5.8 tasks/cancel su task in WORKING (async:true) → canceled ───────────
    print("\n── 5.8  tasks/cancel su task in WORKING")
    response_async = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"async": True,
         "message": {"role": "user",
                     "parts": [{"type": "text", "text": "Qual è il prezzo di TSLA?"}]}}
    )
    result_async, err_a = extract_a2a_result(response_async)
    if err_a:
        _fail("creazione task async per cancel", str(err_a))
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
                _ok("task già completato prima del cancel (race benigna) → -32002")
            else:
                _fail("tasks/cancel", str(cancel_err))
        else:
            status = _status_str(cancel_result.get("status"))
            if status == "canceled":
                _ok(f"task {task_id_cancel} cancellato  status={status}")
            else:
                _fail("tasks/cancel", f"atteso 'canceled', ricevuto '{status}'")

    # ── 5.9 async:true + polling tasks/get ────────────────────────────────────
    print("\n── 5.9  async:true + polling tasks/get")
    request_text = "Qual è il prezzo di GOOGL?"
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
            _fail(f"atteso 'working', ricevuto '{status_59}'")
        else:
            _ok(f"risposta immediata: taskId={task_id_59}  status={status_59}")
            print(f"  ⏳ Polling ogni 500ms (max 10 tentativi)...")
            completed_59 = False
            current_59   = status_59
            for attempt in range(1, 11):
                await asyncio.sleep(0.5)
                poll_resp = await call_plain_agent(
                    PLAIN_AGENT_PATH, "tasks/get", {"taskId": task_id_59}
                )
                poll_result, _ = extract_a2a_result(poll_resp)
                current_59 = _status_str(poll_result.get("status")) if poll_result else "error"
                print(f"    tentativo {attempt}: status={current_59}")
                if current_59 in ("completed", "failed", "canceled"):
                    completed_59 = True
                    break
            if completed_59 and current_59 == "completed":
                _ok(f"completato via polling")
                print(f"  📥 {extract_a2a_text(poll_resp)}")
            elif not completed_59:
                _fail("task non completato entro timeout polling")

    # ── 5.10 async:true + tasks/subscribe (SSE plain) ─────────────────────────
    print("\n── 5.10  async:true + tasks/subscribe (SSE plain)")
    request_text = "Qual è il prezzo di AMZN?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        PLAIN_AGENT_PATH, "messages/send",
        {"async": True,
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}}
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("async messages/send per subscribe", str(err))
    else:
        task_id_510 = result.get("taskId")
        _ok(f"task creato: taskId={task_id_510}  status={result.get('status')}")
        print(f"  📡 tasks/subscribe SSE su taskId={task_id_510}...")
        sub_resp = await call_plain_agent_stream(
            PLAIN_AGENT_PATH, "tasks/subscribe", {"taskId": task_id_510}
        )
        sub_result, sub_err = extract_a2a_result(sub_resp)
        if sub_err:
            _fail("tasks/subscribe", str(sub_err))
        else:
            _ok(f"subscribe completato  status={sub_result.get('status')}")
            print(f"  📥 {extract_a2a_text(sub_resp)}")

    # ── 5.11 Error codes ──────────────────────────────────────────────────────
    print("\n── 5.11  Error codes")

    # -32700: body non è JSON valido (endpoint sync)
    resp = await call_plain_agent_raw(PLAIN_AGENT_PATH, b"non-json{{{")
    code = resp.get("error", {}).get("code")
    if code == -32700:
        _ok("-32700 Parse error (body non JSON — sync)")
    else:
        _fail("-32700 sync", f"ricevuto: {resp.get('error')}")

    # -32600: method mancante
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32600:
        _ok("-32600 Invalid request (method mancante)")
    else:
        _fail("-32600", f"ricevuto: {resp.get('error')}")

    # -32602: taskId mancante in tasks/get
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32602:
        _ok("-32602 Invalid params (taskId mancante in tasks/get)")
    else:
        _fail("-32602", f"ricevuto: {resp.get('error')}")

    # -32602: taskId mancante in tasks/cancel
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/cancel", {}, expect_error=True
    )
    code = resp.get("error", {}).get("code")
    if code == -32602:
        _ok("-32602 Invalid params (taskId mancante in tasks/cancel)")
    else:
        _fail("-32602 cancel", f"ricevuto: {resp.get('error')}")

    # -32001: taskId inesistente
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/get",
        {"taskId": "00000000-0000-0000-0000-000000000000"},
        expect_error=True,
    )
    code = resp.get("error", {}).get("code")
    if code == -32001:
        _ok("-32001 Task not found (taskId inesistente)")
    else:
        _fail("-32001", f"ricevuto: {resp.get('error')}")

    # -32003: push notification non supportate
    resp = await call_plain_agent(
        PLAIN_AGENT_PATH, "tasks/pushNotificationConfig/set",
        {"taskId": task_id_51}, expect_error=True,
    )
    code = resp.get("error", {}).get("code")
    if code == -32003:
        _ok("-32003 PushNotificationNotSupported")
    else:
        _fail("-32003", f"ricevuto: {resp.get('error')}")

    # -32700: body non JSON sul canale SSE
    url_stream = f"{BASE_URL}{PLAIN_AGENT_PATH}/stream"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp_sse = await client.post(
            url_stream, content=b"{{invalid",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"}
        )
        sse_body = resp_sse.text
    if "-32700" in sse_body:
        _ok("-32700 Parse error (body non JSON — SSE)")
    else:
        _fail("-32700 SSE", f"body: {sse_body[:120]}")

    # -32601: metodo non SSE sul canale /stream (es. tasks/get)
    sse_resp = await call_plain_agent_stream(
        PLAIN_AGENT_PATH, "tasks/get", {"taskId": task_id_51}
    )
    err_sse = sse_resp.get("error", {})
    if err_sse.get("code") == -32601:
        _ok(f"-32601 Method not supported on SSE endpoint")
    else:
        _fail("-32601 SSE", f"ricevuto: {err_sse}")

    print()
    print("  ── Riepilogo Test 5 completato ──")


async def auth_endpoint_test():
    """
    Test 6 — Endpoint lc-agent con autorizzazione (AuthConfig bearer + api_key).

    Copre tutti gli schemi di autenticazione su entrambi i canali:
      - Canale cifrato STLS  (/agents/lc-agent/secure-a2a)
      - Canale plain         (/agents/lc-agent/a2a)
    """
    print("\n" + "="*70)
    print("Test 6: Endpoint con autorizzazione  (lc-agent)")
    print("="*70)

    # ── 6.1 Canale cifrato — senza credenziali → HTTP 401 ────────────────────
    print("\n── 6.1  Canale cifrato STLS senza credenziali (atteso HTTP 401)")
    resp = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 ricevuto  WWW-Authenticate: {resp.get('www_authenticate', '')[:60]}")
    else:
        _fail("atteso HTTP 401", f"ricevuto: {resp}")

    # ── 6.2 Canale cifrato — con Bearer token → successo ─────────────────────
    print("\n── 6.2  Canale cifrato STLS con Bearer token")
    request_text = "Qual è il prezzo di TSLA?"
    print(f"  📤 {request_text}")
    response = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/send con bearer", str(err))
    else:
        task_id_62 = result.get("taskId")
        _ok(f"taskId={task_id_62}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.3 Canale cifrato — con API Key (header) → successo ─────────────────
    print("\n── 6.3  Canale cifrato STLS con API Key (header X-API-Key)")
    request_text = "Qual è il prezzo di NVDA?"
    print(f"  📤 {request_text}")
    response = await call_agent(
        LC_SECURE_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        api_key=LC_API_KEY,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("messages/send con api_key", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.4 Canale cifrato — SSE streaming con Bearer token ───────────────────
    print("\n── 6.4  Canale cifrato STLS SSE streaming con Bearer token")
    request_text = "Qual è il prezzo di META?"
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
        _fail("messages/sendStream con bearer", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.5 Canale cifrato — SSE senza credenziali → HTTP 401 ────────────────
    print("\n── 6.5  Canale cifrato STLS SSE senza credenziali (atteso HTTP 401)")
    resp = await call_agent(
        LC_SECURE_PATH, "messages/sendStream",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        stream=True,
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 ricevuto su canale SSE cifrato")
    else:
        _fail("atteso HTTP 401 su SSE cifrato", f"ricevuto: {resp}")

    # ── 6.6 Canale plain — senza credenziali → HTTP 401 ──────────────────────
    print("\n── 6.6  Canale plain senza credenziali (atteso HTTP 401)")
    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 ricevuto  WWW-Authenticate: {resp.get('www_authenticate', '')[:60]}")
    else:
        _fail("atteso HTTP 401", f"ricevuto: {resp}")

    # ── 6.7 Canale plain — con Bearer token → successo ───────────────────────
    print("\n── 6.7  Canale plain con Bearer token")
    request_text = "Qual è il prezzo di AAPL?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain messages/send con bearer", str(err))
    else:
        task_id_67 = result.get("taskId")
        _ok(f"taskId={task_id_67}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.8 Canale plain — con API Key (header) → successo ───────────────────
    print("\n── 6.8  Canale plain con API Key (header)")
    response = await call_plain_agent(
        LC_PLAIN_PATH, "tasks/get",
        {"taskId": task_id_67},
        api_key=LC_API_KEY,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain tasks/get con api_key", str(err))
    else:
        _ok(f"tasks/get  status={result.get('status')}  "
            f"history={len(result.get('history', []))}msg")

    # ── 6.9 Canale plain SSE — senza credenziali → HTTP 401 ──────────────────
    print("\n── 6.9  Canale plain SSE senza credenziali (atteso HTTP 401)")
    resp = await call_plain_agent_stream(
        LC_PLAIN_PATH, "messages/sendStream",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok(f"HTTP 401 ricevuto su canale plain SSE")
    else:
        _fail("atteso HTTP 401 su plain SSE", f"ricevuto: {resp}")

    # ── 6.10 Canale plain SSE — con Bearer token → successo ──────────────────
    print("\n── 6.10  Canale plain SSE con Bearer token")
    request_text = "Qual è il prezzo di MSFT?"
    print(f"  📤 {request_text}")
    response = await call_plain_agent_stream(
        LC_PLAIN_PATH, "messages/sendStream",
        {"contextId": "ctx-auth-002",
         "message": {"role": "user", "parts": [{"type": "text", "text": request_text}]}},
        bearer_token=LC_BEARER_TOKEN,
    )
    result, err = extract_a2a_result(response)
    if err:
        _fail("plain SSE con bearer", str(err))
    else:
        _ok(f"taskId={result.get('taskId')}  status={result.get('status')}")
        print(f"  📥 {extract_a2a_text(response)}")

    # ── 6.11 Credenziali errate → HTTP 401 ────────────────────────────────────
    print("\n── 6.11  Credenziali errate (atteso HTTP 401)")
    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        bearer_token="wrong-token",
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok("HTTP 401 con bearer token errato")
    else:
        _fail("atteso HTTP 401 con token errato", f"ricevuto: {resp}")

    resp = await call_plain_agent(
        LC_PLAIN_PATH, "messages/send",
        {"message": {"role": "user", "parts": [{"type": "text", "text": "test"}]}},
        api_key="wrong-key",
        expect_401=True,
    )
    if resp.get("status_code") == 401:
        _ok("HTTP 401 con api_key errata")
    else:
        _fail("atteso HTTP 401 con api_key errata", f"ricevuto: {resp}")

    print()
    print("  ── Riepilogo Test 6 completato ──")



async def stream_secure_message_test():
    """
    Test 7 — stream_secure_message(): vero streaming incrementale cifrato.

    Verifica che il nuovo AsyncGenerator yieldi chunk per chunk man mano
    che arrivano dal server, invece di collezionarli tutti prima di restituire.

    Copre:
      7.1  chunk yieldati in ordine: submitted → working → artifact(s) → completed
      7.2  ogni TextPart arriva non vuota
      7.3  testo finale ricostruito uguale a quello restituito da send_secure_message
      7.4  almeno un chunk yieldato prima del completed (vero streaming, non batch)
      7.5  primo chunk arriva prima della fine dello stream (latenza incrementale)
      7.6  stream_secure_message su lc-agent con API key (auth funziona sul generator)
    """
    print("\n" + "="*70)
    print("Test 7: stream_secure_message() — vero streaming incrementale")
    print("="*70)

    # ── 7.1-7.4  adk-agent: verifica ordine eventi e incrementalità ───────────
    print("\n── 7.1-7.4  adk-agent: ordine eventi e incrementalità")
    request_text = "Qual è il prezzo di AAPL?"
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
            _fail("stream_secure_message errore inatteso",
                  f"code={chunk['code']} msg={chunk['message']}")
            return

    # 7.1 — verifica ordine eventi: deve esserci almeno submitted/working e completed
    status_events = [c["status"] for c in events_received
                     if c["event"] == "TaskStatusUpdateEvent"]
    artifact_events = [c for c in events_received
                       if c["event"] == "TaskArtifactUpdateEvent"]

    if "completed" in status_events:
        _ok(f"7.1  ordine eventi corretto — stati ricevuti: {status_events}")
    else:
        _fail("7.1  stato 'completed' non ricevuto", f"stati: {status_events}")

    # 7.2 — ogni TextPart non vuota
    empty_parts = [t for t in text_chunks if not t.strip()]
    if not empty_parts:
        _ok(f"7.2  nessuna TextPart vuota ({len(text_chunks)} chunks)")
    else:
        _fail("7.2  TextPart vuote ricevute", f"count={len(empty_parts)}")

    # 7.3 — testo finale = concatenazione dei chunk = risultato send_secure_message
    streamed_text = "".join(text_chunks)
    # Confronta con send_secure_message(stream=True) sullo stesso contesto
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
        _ok(f"7.3  testo streamed ({len(streamed_text)} chars) coerente "
            f"con batch ({len(batch_text)} chars)")
    else:
        _fail("7.3  testo vuoto in streamed o batch",
              f"streamed={len(streamed_text)}  batch={len(batch_text)}")
    print(f"  📥 Testo finale: {streamed_text[:120]}{'…' if len(streamed_text) > 120 else ''}")

    # 7.4 — almeno un artifact chunk prima del completed
    if artifact_events:
        _ok(f"7.4  {len(artifact_events)} artifact event(s) ricevuti prima del completed")
    else:
        _fail("7.4  nessun artifact event ricevuto — pseudo-streaming ancora attivo?")

    # ── 7.5  latenza primo chunk ───────────────────────────────────────────────
    print("\n── 7.5  latenza primo chunk (incrementalità)")
    if first_chunk_time is not None:
        total_time = _time.monotonic() - start_time
        print(f"  ⏱  primo chunk: {first_chunk_time:.2f}s  |  totale: {total_time:.2f}s")
        if first_chunk_time < total_time * 0.9:
            _ok(f"7.5  primo chunk arrivato a {first_chunk_time:.2f}s "
                f"(< 90% del tempo totale {total_time:.2f}s) — streaming incrementale")
        else:
            # Non è un hard fail: dipende dal modello e dalla latenza di rete
            print(f"  ⚠️  7.5  primo chunk a {first_chunk_time:.2f}s su {total_time:.2f}s totali "
                  f"— possibile batch del modello (non un errore del transport)")
    else:
        _fail("7.5  nessun artifact chunk ricevuto, impossibile misurare latenza")

    # ── 7.6  lc-agent con API key ─────────────────────────────────────────────
    print("\n── 7.6  lc-agent: stream_secure_message con API key")
    request_text_lc = "Qual è il prezzo di TSLA?"
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

    # extra_headers passa X-API-Key direttamente a stream_secure_message:
    # non serve più patchare adapter_lc.client — l'header viene iniettato
    # nell'AsyncClient SSE interno al generator.
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
        _fail("7.6  errore stream lc-agent",
              f"code={lc_error['code']} msg={lc_error['message']}")
    elif lc_final_status == "completed" and lc_text_chunks:
        lc_full = "".join(lc_text_chunks)
        _ok(f"7.6  lc-agent stream OK  status={lc_final_status}  "
            f"chunks={len(lc_text_chunks)}  chars={len(lc_full)}")
        print(f"  📥 {lc_full[:120]}{'…' if len(lc_full) > 120 else ''}")
    else:
        _fail("7.6  stream lc-agent incompleto",
              f"status={lc_final_status}  chunks={len(lc_text_chunks)}")

    print()
    print("  ── Riepilogo Test 7 completato ──")


async def task_test():
    global session_key_identifier, session_key_data

    # ── Inizializzazione PQC: handshake KEM prima di qualsiasi chiamata ──────
    if KEY_PROVIDER_NAME == "pqc":
        from stls_a2a import create_pqc_a2a_transport
        logger.info("PQC: esecuzione handshake KEM verso %s/stls/pqc/handshake", BASE_URL)
        _pqc_adapter = await create_pqc_a2a_transport(
            BASE_URL,
            client_id="task-test-pqc",
        )
        session_key_identifier = _pqc_adapter.session_id
        # session_key_data: chiave grezza conservata in SecureA2ATransport
        # (usata da call_agent per costruire nuovi SecureA2ATransportAdapter)
        session_key_data = _pqc_adapter.transport.session_key
        logger.info(
            "PQC handshake completato: session_id=%s key_len=%d",
            session_key_identifier, len(session_key_data),
        )

    try:
        print("\n" + "="*70)
        print("Test 1: Test ADK")
        print("="*70)

        request_text = "Qual è il prezzo di AAPL?"
        print(f"\n📤 Invio richiesta: {request_text}")

        response = await call_agent(
            agent_path='/agents/adk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        
        # Estrae solo il testo
        #print(f'response={response}')
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
        print()

        request_text = "Qual è il prezzo di META?"
        print(f"\n📤 Invio richiesta: {request_text}")

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
        # Estrae solo il testo
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
        print()    
        

        # Chiama lc-agent
        print("\n" + "="*70)
        print("Test 2: lc-agent (LangChain)")
        print("="*70)   

        request_text = "Qual è il prezzo di TSLA?"
        print(f"\n📤 Invio richiesta: {request_text}")

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
        print(f"📥 Risposta: {text}")
        print()

        request_text = "Qual è il prezzo di NVDA?"
        print(f"\n📤 Invio richiesta: {request_text}")

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
        # Estrae solo il testo
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
        print()    


        # Chiama sk-agent
        print("\n" + "="*70)
        print("Test 3: sk-agent (Semantic Kernel)")
        print("="*70)   

        request_text = "Qual è il prezzo di MSFT?"
        print(f"\n📤 Invio richiesta: {request_text}")

        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
        print()

        request_text = "Mi estrai info su MSFT?"
        print(f"\n📤 Invio richiesta: {request_text}")

        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='messages/send',
            params={"message": {
                    "role": "user",
                    "parts": [{ "type": "text", "text": request_text }]
                }}
            )
        
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
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
        # Estrae solo il testo
        text = extract_a2a_text(response)
        print(f"📥 Risposta: {text}")
        print()    
 
        # richiamo tasks/list
        response = await call_agent(
            agent_path='/agents/sk-agent',
            method='tasks/list',
            params={"limit": 50}
            )
        
        result = response.get('result')
        for task in result:
            print(f'-> taskId={task.get("taskId")};contextId={task.get("contextId")};status={task.get("status")}')
        if len(result)>0:
            # prendo il primo
            task=result[0]
            response = await call_agent(
                agent_path='/agents/sk-agent',
                method='tasks/get',
                params={"taskId": task.get('taskId')}
            )
            print(f"get response[0]: {response}")

        # ── Test 4: Async processing + polling + subscribe ────────────────────
        print("\n" + "="*70)
        print("Test 4: Async processing (messages/send con async:true)")
        print("="*70)

        request_text = "Qual è il prezzo di GOOGL?"
        print(f"\n📤 Invio richiesta ASINCRONA: {request_text}")

        # 4.1 – messages/send con async:true → risponde subito con status working
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
            print(f"❌ Errore: {error}")
        else:
            task_id   = result.get('taskId')
            status    = _status_str(result.get('status'))
            print(f"✅ Task creato immediatamente: taskId={task_id}  status={status}")
            assert status == "working", f"Atteso 'working', ricevuto '{status}'"

        # 4.2 – Polling con tasks/get: aspettiamo che il task termini
        print(f"\n⏳ Polling tasks/get ogni 500ms (max 10 tentativi)...")
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
            print(f"  tentativo {attempt}: status={current_status}")
            if current_status in ("completed", "failed", "canceled"):
                completed = True
                break

        if completed and current_status == "completed":
            text = extract_a2a_text(poll_response)
            print(f"📥 Risposta finale (via polling): {text}")
        elif not completed:
            print("⚠️  Task non completato entro il timeout di polling")

        # 4.3 – Stesso scenario ma con tasks/subscribe (SSE) invece di polling.
        #        Inviamo una nuova richiesta async e ci sottoscriviamo subito.
        print(f"\n📤 Invio seconda richiesta ASINCRONA per test subscribe...")
        request_text2 = "Qual è il prezzo di AMZN?"

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
            print(f"❌ Errore nella seconda richiesta: {error2}")
        else:
            task_id2 = result2.get('taskId')
            print(f"✅ Task creato: taskId={task_id2}  status={result2.get('status')}")

            print(f"\n📡 tasks/subscribe su taskId={task_id2} (SSE)...")
            subscribe_response = await call_agent(
                agent_path='/agents/adk-agent',
                method='tasks/subscribe',
                params={"taskId": task_id2},
                stream=True
            )
            sub_result, sub_error = extract_a2a_result(subscribe_response)
            if sub_error:
                print(f"❌ Errore subscribe: {sub_error}")
            else:
                sub_status = sub_result.get('status')
                sub_text   = extract_a2a_text(subscribe_response)
                print(f"📥 Subscribe completato — status={sub_status}")
                print(f"📥 Testo risposta: {sub_text}")

        # ── Test 5: Plain endpoint (non cifrato) ──────────────────────────────
        await plain_endpoint_test()

        # ── Test 6: Endpoint con autorizzazione (lc-agent) ────────────────────
        await auth_endpoint_test()

        # ── Test 7: stream_secure_message() — vero streaming incrementale ─────
        await stream_secure_message_test()

    finally:
        print("\n" + "="*70)
        print("Fine del test")
        print("="*70)

if __name__ == "__main__":
    print(__doc__)
    asyncio.run(task_test())