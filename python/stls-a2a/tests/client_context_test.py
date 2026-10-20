"""
client_context_test.py
-----------------------
Client di test per la verifica della propagazione del contesto utente.

Chiama agente A in due modalità:
  1. Via canale cifrato STLS  (SecureA2ATransportAdapter)
  2. Via canale plain A2A     (httpx JSON-RPC standard)

In entrambi i casi verifica che agent_b abbia ricevuto user_id e profile
propagati da A tramite ContextualRemoteA2aAgent.

Avvio (dopo aver avviato server B e server A):
  python client_context_test.py

Variabili d'ambiente:
  BASE_URL_A    URL del server A (default: http://localhost:8001)
  API_KEY_A     API key per chiamare A (default: test-key-agent-a)
  KEY_PROVIDER_NAME  Provider chiavi STLS (default: base)
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Dict, Optional

import httpx

from stls_a2a import (
    KeySessionProvider,
    KeySessionProviderFactory,
    SecureA2ATransportAdapter,
    extract_a2a_text,
    extract_a2a_result,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="% Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configurazione ────────────────────────────────────────────────────────────

import os

BASE_URL_A  = os.getenv("BASE_URL_A",  "http://localhost:8011").rstrip("/")
API_KEY_A   = os.getenv("API_KEY_A",   "test-key-agent-a")

AGENT_A_SECURE_PATH = "/agents/agent-a"           # → /agents/agent-a/secure-a2a
AGENT_A_PLAIN_PATH  = "/agents/agent-a/a2a"       # plain endpoint

# Contesto utente da propagare ad agent_b
TEST_USER_ID = "mario.rossi@example.com"
TEST_PROFILE = {"tenant": "tim-sparkle", "role": "admin", "department": "IT"}

# ── Setup STLS session ────────────────────────────────────────────────────────

stls_port = int(os.getenv("STLS_PORT_A", "8091"))
key_session_provider: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.CLIENT,
    port=stls_port,
    host="localhost",
)

session_key_identifier = str(
    KeySessionProviderFactory().resolve_if_awaitable(
        key_session_provider.getSessionKeyIdentifier()
    )
)
session_key_data = bytes(
    KeySessionProviderFactory().resolve_if_awaitable(
        key_session_provider.getSessionKeyData(session_key_identifier)
    )
)

logger.info("Session key identifier: %s", session_key_identifier)
logger.info("Session key length: %d bytes", len(session_key_data))

# ── Helper output ─────────────────────────────────────────────────────────────

def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")

def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌ {label}{': ' + detail if detail else ''}")

def _info(label: str, detail: str = "") -> None:
    print(f"  ℹ️  {label}{': ' + detail if detail else ''}")


def _extract_context_from_response(response: dict) -> tuple[str, Any]:
    """
    Estrae user_id e profile dalla risposta di agent_b.
    agent_b risponde con un JSON come testo, es.:
      {"user_id": "mario.rossi@example.com", "profile": {"tenant": "..."}}
    """
    text = extract_a2a_text(response) or ""
    try:
        # Prova a parsare il JSON nella risposta
        # agent_b potrebbe avvolgere il JSON in testo libero
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(text[start:end])
            return data.get("user_id", ""), data.get("profile", "")
    except Exception:
        pass
    return text, ""


def _verify_context(
    label: str,
    user_id_received: str,
    profile_received: Any,
) -> bool:
    """Verifica che user_id e profile ricevuti corrispondano a quelli inviati."""
    ok = True

    if user_id_received == TEST_USER_ID:
        _ok(f"{label} — user_id propagato", user_id_received)
    else:
        _fail(
            f"{label} — user_id NON propagato",
            f"atteso={TEST_USER_ID!r}  ricevuto={user_id_received!r}",
        )
        ok = False

    # Il tool restituisce profile come JSON string — prova a parsarlo
    if isinstance(profile_received, str):
        try:
            import json as _json
            profile_received = _json.loads(profile_received)
        except Exception:
            pass

    if isinstance(profile_received, dict):
        tenant_ok = profile_received.get("tenant") == TEST_PROFILE["tenant"]
        role_ok   = profile_received.get("role")   == TEST_PROFILE["role"]
        if tenant_ok and role_ok:
            _ok(f"{label} — profile propagato", str(profile_received))
        else:
            _fail(
                f"{label} — profile parziale o errato",
                f"atteso={TEST_PROFILE}  ricevuto={profile_received}",
            )
            ok = False
    elif str(profile_received).strip() in ("", "not set", "null", "None"):
        _fail(f"{label} — profile NON propagato", f"ricevuto={profile_received!r}")
        ok = False
    else:
        _info(f"{label} — profile ricevuto (non dict)", str(profile_received)[:120])

    return ok


# ── Test 1: canale cifrato STLS ───────────────────────────────────────────────

async def test_secure_channel() -> bool:
    """
    Test 1 — Canale cifrato STLS (SecureA2ATransportAdapter).

    Invia la richiesta ad A con user_id e profile in message.metadata.
    A propaga il contesto a B tramite ContextualRemoteA2aAgent.
    Verifica che B risponda con i valori corretti.
    """
    print("\n── Test 1: Canale cifrato STLS ──────────────────────────────────────")

    adapter = SecureA2ATransportAdapter(
        session_key=session_key_data,
        session_id=session_key_identifier,
        base_url=BASE_URL_A + AGENT_A_SECURE_PATH,
    )
    # Inietta API key
    await adapter.client.aclose()
    adapter.client = httpx.AsyncClient(
        timeout=60.0,
        headers={"X-API-Key": API_KEY_A},
    )

    message = {
        "jsonrpc": "2.0",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "diagnose context"}],
                # user_id e profile inviati in message.metadata
                # (sorgente priorità 1 lato server STLS)
                "metadata": {
                    "user_id": TEST_USER_ID,
                    "profile": TEST_PROFILE,
                },
            }
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (cifrato) con user_id={TEST_USER_ID!r}")
    try:
        response = await adapter.send_secure_message(message, stream=False)
        result, err = extract_a2a_result(response)
        if err:
            _fail("Errore nella risposta", str(err))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Risposta grezza di agent_b: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)
        return _verify_context("Canale cifrato", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Eccezione durante la chiamata cifrata", str(e))
        logger.exception("Dettaglio eccezione test_secure_channel")
        return False


# ── Test 2: canale plain ──────────────────────────────────────────────────────

async def test_plain_channel() -> bool:
    """
    Test 2 — Canale plain A2A (httpx JSON-RPC standard).

    Stesso scenario ma senza cifratura STLS.
    user_id e profile in message.metadata → propagati via params.metadata
    di ContextualRemoteA2aAgent → arrivano a B.
    """
    print("\n── Test 2: Canale plain A2A ─────────────────────────────────────────")

    url = f"{BASE_URL_A}{AGENT_A_PLAIN_PATH}"
    payload = {
        "jsonrpc": "2.0",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "diagnose context"}],
                "metadata": {
                    "user_id": TEST_USER_ID,
                    "profile": TEST_PROFILE,
                },
            }
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (plain) con user_id={TEST_USER_ID!r}")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": API_KEY_A,
                },
            )
        resp.raise_for_status()
        response = resp.json()

        if response.get("error"):
            _fail("Errore JSON-RPC", str(response["error"]))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Risposta grezza di agent_b: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)
        return _verify_context("Canale plain", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Eccezione durante la chiamata plain", str(e))
        logger.exception("Dettaglio eccezione test_plain_channel")
        return False


# ── Test 3: canale plain SSE (streaming) ──────────────────────────────────────

async def test_plain_stream_channel() -> bool:
    """
    Test 3 — Canale plain SSE (messages/sendStream).

    Verifica la propagazione sul percorso streaming plain.
    """
    print("\n── Test 3: Canale plain SSE (streaming) ─────────────────────────────")

    url = f"{BASE_URL_A}{AGENT_A_PLAIN_PATH}/stream"
    payload = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-context-test-stream-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "diagnose context"}],
                "metadata": {
                    "user_id": TEST_USER_ID,
                    "profile": TEST_PROFILE,
                },
            },
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (SSE plain) con user_id={TEST_USER_ID!r}")
    try:
        collected_parts: list[dict] = []
        last_status = ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept":       "text/event-stream",
                    "X-API-Key":    API_KEY_A,
                },
            ) as resp:
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
                            data    = json.loads(raw_data)
                            payload_inner = data.get("result", data)
                        except Exception:
                            continue
                        if event_type == "TaskArtifactUpdateEvent":
                            for part in payload_inner.get("artifact", {}).get("parts", []):
                                if part:
                                    collected_parts.append(part)
                        elif event_type == "TaskStatusUpdateEvent":
                            status = payload_inner.get("status", {})
                            last_status = (
                                status.get("state", status)
                                if isinstance(status, dict) else str(status)
                            )

        full_text = "".join(
            p["text"] for p in collected_parts
            if p.get("type") == "text" and p.get("text")
        )
        # Costruisce response compatibile con extract_a2a_text
        fake_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "status": last_status,
                "history": [{"role": "agent", "parts": collected_parts}],
            },
        }

        print(f"  📥 Risposta grezza di agent_b: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)
        return _verify_context("Canale plain SSE", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Eccezione durante la chiamata plain SSE", str(e))
        logger.exception("Dettaglio eccezione test_plain_stream_channel")
        return False


# ── Test 4: canale cifrato STLS streaming ─────────────────────────────────────

async def test_secure_stream_channel() -> bool:
    """
    Test 4 — Canale cifrato STLS streaming (stream_secure_message).

    Verifica la propagazione sul percorso SSE cifrato.
    """
    print("\n── Test 4: Canale cifrato STLS streaming ────────────────────────────")

    adapter = SecureA2ATransportAdapter(
        session_key=session_key_data,
        session_id=session_key_identifier,
        base_url=BASE_URL_A + AGENT_A_SECURE_PATH,
    )

    message = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-context-test-secure-stream-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "diagnose context"}],
                "metadata": {
                    "user_id": TEST_USER_ID,
                    "profile": TEST_PROFILE,
                },
            },
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (SSE cifrato) con user_id={TEST_USER_ID!r}")
    try:
        collected_parts: list[dict] = []
        last_status = ""

        async for chunk in adapter.stream_secure_message(
            message,
            extra_headers={"X-API-Key": API_KEY_A},
        ):
            event = chunk["event"]
            if event == "TaskArtifactUpdateEvent":
                collected_parts.extend(chunk.get("parts", []))
            elif event == "TaskStatusUpdateEvent":
                last_status = chunk.get("status", "")
            elif event == "error":
                _fail("Errore SSE cifrato", f"code={chunk['code']} msg={chunk['message']}")
                return False

        full_text = "".join(
            p["text"] for p in collected_parts
            if p.get("type") == "text" and p.get("text")
        )
        fake_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "status": last_status,
                "history": [{"role": "agent", "parts": collected_parts}],
            },
        }

        print(f"  📥 Risposta grezza di agent_b: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)
        return _verify_context("Canale cifrato SSE", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Eccezione durante la chiamata cifrata SSE", str(e))
        logger.exception("Dettaglio eccezione test_secure_stream_channel")
        return False


# ── Runner principale ─────────────────────────────────────────────────────────



# ── Test 5: header statici X-STLS-* — canale plain ───────────────────────────

# Valori speculari a STATIC_USER_ID / STATIC_PROFILE in agent_context_test_a.py
STATIC_USER_ID = "service-account-agent-a@tim-sparkle.com"
STATIC_PROFILE = {"tenant": "tim-sparkle", "role": "service", "department": "AI-Platform"}


async def test_static_header_plain() -> bool:
    """
    Test 5 — Header statici X-STLS-* sul canale plain.

    Il client chiama A chiedendo di usare agent_b_static.
    A ha un httpx_client configurato con X-STLS-User-Id e X-STLS-Profile
    statici verso B. Il server B li legge da _extract_http_context()
    e li propaga al context dell'adapter (priorità 3).
    """
    print("\n── Test 5: Header statici X-STLS-* — canale plain ──────────────────")

    url = f"{BASE_URL_A}{AGENT_A_PLAIN_PATH}"
    payload = {
        "jsonrpc": "2.0",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "check the STATIC context"}],
                # Nessun metadata qui: il contesto arriva solo dagli header
                # X-STLS-* configurati staticamente nell'httpx_client di A→B
            },
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (plain) — richiesta contesto STATICO")
    print(f"  ℹ️  Atteso: user_id={STATIC_USER_ID!r}")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url, json=payload,
                headers={"Content-Type": "application/json", "X-API-Key": API_KEY_A},
            )
        resp.raise_for_status()
        response = resp.json()
        if response.get("error"):
            _fail("Errore JSON-RPC", str(response["error"]))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Risposta grezza di agent_b_static: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)

        ok = True
        if user_id_rx == STATIC_USER_ID:
            _ok("Test 5 — user_id statico propagato", user_id_rx)
        else:
            _fail("Test 5 — user_id statico NON propagato",
                  f"atteso={STATIC_USER_ID!r}  ricevuto={user_id_rx!r}")
            ok = False

        if isinstance(profile_rx, str):
            try:
                import json as _json
                profile_rx = _json.loads(profile_rx)
            except Exception:
                pass
        if isinstance(profile_rx, dict) and profile_rx.get("role") == STATIC_PROFILE["role"]:
            _ok("Test 5 — profile statico propagato", str(profile_rx))
        else:
            _fail("Test 5 — profile statico NON propagato",
                  f"atteso={STATIC_PROFILE}  ricevuto={profile_rx}")
            ok = False
        return ok

    except Exception as e:
        _fail("Eccezione test 5", str(e))
        logger.exception("Dettaglio eccezione test_static_header_plain")
        return False


async def test_static_header_stream() -> bool:
    """
    Test 6 — Header statici X-STLS-* sul canale plain SSE.

    Stesso scenario del Test 5 ma sul percorso streaming.
    Verifica che _extract_http_context venga chiamato anche in
    plain_a2a_stream_handler e che http_context arrivi a
    _handle_messages_send_stream tramite _stls_http_context nei params.
    """
    print("\n── Test 6: Header statici X-STLS-* — canale plain SSE ──────────────")

    url = f"{BASE_URL_A}{AGENT_A_PLAIN_PATH}/stream"
    payload = {
        "jsonrpc": "2.0",
        "method": "messages/sendStream",
        "params": {
            "contextId": "ctx-static-header-stream-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "check the STATIC context"}],
            },
        },
        "id": 1,
    }

    print(f"  📤 Invio a agent_a (SSE plain) — richiesta contesto STATICO")
    print(f"  ℹ️  Atteso: user_id={STATIC_USER_ID!r}")
    try:
        collected_parts: list[dict] = []
        last_status = ""

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", url, json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept":       "text/event-stream",
                    "X-API-Key":    API_KEY_A,
                },
            ) as resp:
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
                            payload_inner = data.get("result", data)
                        except Exception:
                            continue
                        if event_type == "TaskArtifactUpdateEvent":
                            for part in payload_inner.get("artifact", {}).get("parts", []):
                                if part:
                                    collected_parts.append(part)
                        elif event_type == "TaskStatusUpdateEvent":
                            status = payload_inner.get("status", {})
                            last_status = (
                                status.get("state", status)
                                if isinstance(status, dict) else str(status)
                            )

        fake_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "status": last_status,
                "history": [{"role": "agent", "parts": collected_parts}],
            },
        }
        full_text = "".join(
            p["text"] for p in collected_parts
            if p.get("type") == "text" and p.get("text")
        )
        print(f"  📥 Risposta grezza di agent_b_static: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)

        ok = True
        if user_id_rx == STATIC_USER_ID:
            _ok("Test 6 — user_id statico propagato (SSE)", user_id_rx)
        else:
            _fail("Test 6 — user_id statico NON propagato (SSE)",
                  f"atteso={STATIC_USER_ID!r}  ricevuto={user_id_rx!r}")
            ok = False

        if isinstance(profile_rx, str):
            try:
                import json as _json
                profile_rx = _json.loads(profile_rx)
            except Exception:
                pass
        if isinstance(profile_rx, dict) and profile_rx.get("role") == STATIC_PROFILE["role"]:
            _ok("Test 6 — profile statico propagato (SSE)", str(profile_rx))
        else:
            _fail("Test 6 — profile statico NON propagato (SSE)",
                  f"atteso={STATIC_PROFILE}  ricevuto={profile_rx}")
            ok = False
        return ok

    except Exception as e:
        _fail("Eccezione test 6", str(e))
        logger.exception("Dettaglio eccezione test_static_header_stream")
        return False

async def main() -> None:
    print("\n" + "=" * 70)
    print("Context Propagation Test")
    print("Verifica propagazione user_id e profile: client → A → B")
    print("=" * 70)
    print(f"\n  BASE_URL_A   : {BASE_URL_A}")
    print(f"  TEST_USER_ID : {TEST_USER_ID}")
    print(f"  TEST_PROFILE : {TEST_PROFILE}")

    results: dict[str, bool] = {}

    results["1 — Cifrato STLS (sync)"]    = await test_secure_channel()
    results["2 — Plain A2A (sync)"]       = await test_plain_channel()
    results["3 — Plain SSE (stream)"]     = await test_plain_stream_channel()
    results["4 — Cifrato SSE (stream)"]   = await test_secure_stream_channel()
    results["5 — Header statici (plain)"] = await test_static_header_plain()
    results["6 — Header statici (SSE)"]   = await test_static_header_stream()

    # ── Riepilogo ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("Riepilogo")
    print("=" * 70)
    all_ok = True
    for label, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon}  Test {label}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("  🎉 Tutti i test superati — propagazione contesto funzionante.")
    else:
        print("  ⚠️  Alcuni test falliti — verificare i log sopra.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())