"""
client_context_test.py
-----------------------
Test client for verifying user context propagation.

Calls agent A in two modes:
  1. Via the encrypted STLS channel  (SecureA2ATransportAdapter)
  2. Via the plain A2A channel       (standard httpx JSON-RPC)

In both cases, verifies that agent_b received the user_id and profile
propagated by A via ContextualRemoteA2aAgent.

Startup (after starting server B and server A):
  python client_context_test.py

Environment variables:
  BASE_URL_A    URL of server A (default: http://localhost:8001)
  API_KEY_A     API key to call A (default: test-key-agent-a)
  KEY_PROVIDER_NAME  STLS key provider (default: base)
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

# ── Configuration ──────────────────────────────────────────────────────────────

import os

BASE_URL_A  = os.getenv("BASE_URL_A",  "http://localhost:8011").rstrip("/")
API_KEY_A   = os.getenv("API_KEY_A",   "test-key-agent-a")

AGENT_A_SECURE_PATH = "/agents/agent-a"           # → /agents/agent-a/secure-a2a
AGENT_A_PLAIN_PATH  = "/agents/agent-a/a2a"       # plain endpoint

# User context to propagate to agent_b
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
    Extracts user_id and profile from agent_b's response.
    agent_b replies with JSON as text, e.g.:
      {"user_id": "mario.rossi@example.com", "profile": {"tenant": "..."}}
    """
    text = extract_a2a_text(response) or ""
    try:
        # Try to parse the JSON in the response
        # agent_b might wrap the JSON in free text
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
    """Verifies that the received user_id and profile match what was sent."""
    ok = True

    if user_id_received == TEST_USER_ID:
        _ok(f"{label} — user_id propagated", user_id_received)
    else:
        _fail(
            f"{label} — user_id NOT propagated",
            f"expected={TEST_USER_ID!r}  received={user_id_received!r}",
        )
        ok = False

    # The tool returns profile as a JSON string — try to parse it
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
            _ok(f"{label} — profile propagated", str(profile_received))
        else:
            _fail(
                f"{label} — profile partial or wrong",
                f"expected={TEST_PROFILE}  received={profile_received}",
            )
            ok = False
    elif str(profile_received).strip() in ("", "not set", "null", "None"):
        _fail(f"{label} — profile NOT propagated", f"received={profile_received!r}")
        ok = False
    else:
        _info(f"{label} — profile received (not a dict)", str(profile_received)[:120])

    return ok


# ── Test 1: encrypted STLS channel ─────────────────────────────────────────────

async def test_secure_channel() -> bool:
    """
    Test 1 — Encrypted STLS channel (SecureA2ATransportAdapter).

    Sends the request to A with user_id and profile in message.metadata.
    A propagates the context to B via ContextualRemoteA2aAgent.
    Verifies that B responds with the correct values.
    """
    print("\n── Test 1: Encrypted STLS channel ───────────────────────────────────")

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
                # user_id and profile sent in message.metadata
                # (priority-1 source on the STLS server side)
                "metadata": {
                    "user_id": TEST_USER_ID,
                    "profile": TEST_PROFILE,
                },
            }
        },
        "id": 1,
    }

    print(f"  📤 Sending to agent_a (encrypted) with user_id={TEST_USER_ID!r}")
    try:
        response = await adapter.send_secure_message(message, stream=False)
        result, err = extract_a2a_result(response)
        if err:
            _fail("Error in response", str(err))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Raw response from agent_b: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)
        return _verify_context("Encrypted channel", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Exception during the encrypted call", str(e))
        logger.exception("Exception detail for test_secure_channel")
        return False


# ── Test 2: plain channel ──────────────────────────────────────────────────────

async def test_plain_channel() -> bool:
    """
    Test 2 — Plain A2A channel (standard httpx JSON-RPC).

    Same scenario but without STLS encryption.
    user_id and profile in message.metadata → propagated via
    ContextualRemoteA2aAgent's params.metadata → reach B.
    """
    print("\n── Test 2: Plain A2A channel ────────────────────────────────────────")

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

    print(f"  📤 Sending to agent_a (plain) with user_id={TEST_USER_ID!r}")
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
            _fail("JSON-RPC error", str(response["error"]))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Raw response from agent_b: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)
        return _verify_context("Plain channel", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Exception during the plain call", str(e))
        logger.exception("Exception detail for test_plain_channel")
        return False


# ── Test 3: plain SSE channel (streaming) ──────────────────────────────────────

async def test_plain_stream_channel() -> bool:
    """
    Test 3 — Plain SSE channel (messages/sendStream).

    Verifies propagation on the plain streaming path.
    """
    print("\n── Test 3: Plain SSE channel (streaming) ────────────────────────────")

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

    print(f"  📤 Sending to agent_a (plain SSE) with user_id={TEST_USER_ID!r}")
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
        # Builds a response compatible with extract_a2a_text
        fake_response = {
            "jsonrpc": "2.0", "id": 1,
            "result": {
                "status": last_status,
                "history": [{"role": "agent", "parts": collected_parts}],
            },
        }

        print(f"  📥 Raw response from agent_b: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)
        return _verify_context("Plain SSE channel", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Exception during the plain SSE call", str(e))
        logger.exception("Exception detail for test_plain_stream_channel")
        return False


# ── Test 4: encrypted STLS streaming channel ───────────────────────────────────

async def test_secure_stream_channel() -> bool:
    """
    Test 4 — Encrypted STLS streaming channel (stream_secure_message).

    Verifies propagation on the encrypted SSE path.
    """
    print("\n── Test 4: Encrypted STLS streaming channel ─────────────────────────")

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

    print(f"  📤 Sending to agent_a (encrypted SSE) with user_id={TEST_USER_ID!r}")
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
                _fail("Encrypted SSE error", f"code={chunk['code']} msg={chunk['message']}")
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

        print(f"  📥 Raw response from agent_b: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)
        return _verify_context("Encrypted SSE channel", user_id_rx, profile_rx)

    except Exception as e:
        _fail("Exception during the encrypted SSE call", str(e))
        logger.exception("Exception detail for test_secure_stream_channel")
        return False


# ── Main runner ─────────────────────────────────────────────────────────────



# ── Test 5: static X-STLS-* headers — plain channel ───────────────────────────

# Values mirroring STATIC_USER_ID / STATIC_PROFILE in agent_context_test_a.py
STATIC_USER_ID = "service-account-agent-a@tim-sparkle.com"
STATIC_PROFILE = {"tenant": "tim-sparkle", "role": "service", "department": "AI-Platform"}


async def test_static_header_plain() -> bool:
    """
    Test 5 — Static X-STLS-* headers on the plain channel.

    The client calls A asking it to use agent_b_static.
    A has an httpx_client configured with static X-STLS-User-Id and
    X-STLS-Profile headers towards B. Server B reads them from
    _extract_http_context() and propagates them to the adapter's
    context (priority 3).
    """
    print("\n── Test 5: Static X-STLS-* headers — plain channel ─────────────────")

    url = f"{BASE_URL_A}{AGENT_A_PLAIN_PATH}"
    payload = {
        "jsonrpc": "2.0",
        "method": "messages/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "check the STATIC context"}],
                # No metadata here: the context comes only from the
                # X-STLS-* headers statically configured in A's httpx_client towards B
            },
        },
        "id": 1,
    }

    print(f"  📤 Sending to agent_a (plain) — STATIC context request")
    print(f"  ℹ️  Expected: user_id={STATIC_USER_ID!r}")
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                url, json=payload,
                headers={"Content-Type": "application/json", "X-API-Key": API_KEY_A},
            )
        resp.raise_for_status()
        response = resp.json()
        if response.get("error"):
            _fail("JSON-RPC error", str(response["error"]))
            return False

        text = extract_a2a_text(response) or ""
        print(f"  📥 Raw response from agent_b_static: {text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(response)

        ok = True
        if user_id_rx == STATIC_USER_ID:
            _ok("Test 5 — static user_id propagated", user_id_rx)
        else:
            _fail("Test 5 — static user_id NOT propagated",
                  f"expected={STATIC_USER_ID!r}  received={user_id_rx!r}")
            ok = False

        if isinstance(profile_rx, str):
            try:
                import json as _json
                profile_rx = _json.loads(profile_rx)
            except Exception:
                pass
        if isinstance(profile_rx, dict) and profile_rx.get("role") == STATIC_PROFILE["role"]:
            _ok("Test 5 — static profile propagated", str(profile_rx))
        else:
            _fail("Test 5 — static profile NOT propagated",
                  f"expected={STATIC_PROFILE}  received={profile_rx}")
            ok = False
        return ok

    except Exception as e:
        _fail("Test 5 exception", str(e))
        logger.exception("Exception detail for test_static_header_plain")
        return False


async def test_static_header_stream() -> bool:
    """
    Test 6 — Static X-STLS-* headers on the plain SSE channel.

    Same scenario as Test 5 but on the streaming path.
    Verifies that _extract_http_context is also called in
    plain_a2a_stream_handler and that http_context reaches
    _handle_messages_send_stream via _stls_http_context in params.
    """
    print("\n── Test 6: Static X-STLS-* headers — plain SSE channel ─────────────")

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

    print(f"  📤 Sending to agent_a (plain SSE) — STATIC context request")
    print(f"  ℹ️  Expected: user_id={STATIC_USER_ID!r}")
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
        print(f"  📥 Raw response from agent_b_static: {full_text[:200]}")
        user_id_rx, profile_rx = _extract_context_from_response(fake_response)

        ok = True
        if user_id_rx == STATIC_USER_ID:
            _ok("Test 6 — static user_id propagated (SSE)", user_id_rx)
        else:
            _fail("Test 6 — static user_id NOT propagated (SSE)",
                  f"expected={STATIC_USER_ID!r}  received={user_id_rx!r}")
            ok = False

        if isinstance(profile_rx, str):
            try:
                import json as _json
                profile_rx = _json.loads(profile_rx)
            except Exception:
                pass
        if isinstance(profile_rx, dict) and profile_rx.get("role") == STATIC_PROFILE["role"]:
            _ok("Test 6 — static profile propagated (SSE)", str(profile_rx))
        else:
            _fail("Test 6 — static profile NOT propagated (SSE)",
                  f"expected={STATIC_PROFILE}  received={profile_rx}")
            ok = False
        return ok

    except Exception as e:
        _fail("Test 6 exception", str(e))
        logger.exception("Exception detail for test_static_header_stream")
        return False

async def main() -> None:
    print("\n" + "=" * 70)
    print("Context Propagation Test")
    print("Verifying user_id and profile propagation: client → A → B")
    print("=" * 70)
    print(f"\n  BASE_URL_A   : {BASE_URL_A}")
    print(f"  TEST_USER_ID : {TEST_USER_ID}")
    print(f"  TEST_PROFILE : {TEST_PROFILE}")

    results: dict[str, bool] = {}

    results["1 — Encrypted STLS (sync)"]  = await test_secure_channel()
    results["2 — Plain A2A (sync)"]       = await test_plain_channel()
    results["3 — Plain SSE (stream)"]     = await test_plain_stream_channel()
    results["4 — Encrypted SSE (stream)"] = await test_secure_stream_channel()
    results["5 — Static headers (plain)"] = await test_static_header_plain()
    results["6 — Static headers (SSE)"]   = await test_static_header_stream()

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    all_ok = True
    for label, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon}  Test {label}")
        if not ok:
            all_ok = False

    print()
    if all_ok:
        print("  🎉 All tests passed — context propagation working.")
    else:
        print("  ⚠️  Some tests failed — check the logs above.")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())