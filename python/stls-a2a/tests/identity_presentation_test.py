"""
identity_presentation_test.py
-------------------------------
Regression suite for issue #15 gap #1: the DID/VC identity-presentation
hook added to SecureA2AHTTPMiddleware/create_secure_a2a_endpoint
(`identity_verifier=`). Same in-process ASGI style as
a2a_protocol_conformance_test.py (real FastAPI app, httpx.ASGITransport,
no network, no real infra) — kept as a separate file because it's a
different issue/concern, not a protocol-conformance gap.

This suite does NOT exercise real stls-identity crypto (Sentinel,
VerifiablePresentation) — that lives in
python/stls-identity/tests/test_request_verifier.py. Here
`identity_verifier` is a minimal fake duck-typed object, exactly the
shape stls-a2a itself expects (`async def verify_request(request, *,
challenge) -> result` with `.ok`/`.error`) — proving the hook's wiring
and off-by-default guarantee, independent of any particular identity
backend.

Covers:
  1. No identity_verifier configured (the default) → the identity check
     never fires, confirmed by the absence of ERR_IDENTITY_CHALLENGE_REQUIRED
     on a request that has no session at all either.
  2. identity_verifier configured, no X-STLS-Identity-Presentation header
     yet → 401 + ERR_IDENTITY_CHALLENGE_REQUIRED (-32005) + a challenge.
  3. Same session, header now present, fake verifier returns ok → the
     identity check passes (request proceeds past it — it then fails
     for an unrelated reason, since no real encrypted session exists in
     this suite; what matters is that it's no longer the identity error).
  4. Same flow, fake verifier returns not-ok → 401, distinct error code
     from the challenge-required one.
  5. STLSHttpxTransport's client-side retry-once (sync and SSE paths):
     a fake adapter/session-manager raises the challenge-required shape
     exactly once, identity_challenge_handler is called with the right
     challenge, and the retried call succeeds — mirrors
     _ERR_SESSION_EXPIRED's own retry-once test coverage, isolated from
     real crypto/network (that full round trip is the sample, not a
     unit test).
  6. Regression, found while building samples/server/19_identity_presentation
     (unrelated to identity itself — create_secure_a2a_endpoint() crashed
     for EVERY agent, not just identity ones): enable_plain_endpoint=False
     together with a non-None base_url used to raise TypeError
     ("can only concatenate str (not 'NoneType') to str") building the
     AgentCard's plain url. Tracked and fixed in this same issue/branch.
"""

import asyncio
import os
import sys
from typing import Any

os.environ.setdefault("KEY_PROVIDER_NAME", "base")
os.environ.setdefault("TASK_STORE_BACKEND", "memory")
os.environ["AUTH_PG_DSN"] = ""
# Force in-memory SessionCache regardless of a local .env pointing at
# Redis (python-dotenv's load_dotenv(), called at stls_a2a import time,
# won't override an already-set key) — this suite only exercises the
# pre-decryption identity-check hook, no real infra needed for it.
os.environ["SESSION_CACHE_BACKEND"] = "memory"

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
from stls_a2a.stls_a2a_adapters_model import ERR_IDENTITY_CHALLENGE_REQUIRED
from stls_a2a.stls_a2a_secure_remote import STLSHttpxTransport
from mock_agent import MockAgent

import logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌ {label}{': ' + detail if detail else ''}")


def _error_info(body: dict) -> tuple[Any, str]:
    """
    secure_a2a_handler returns two different error shapes depending on
    where a request fails: the JSON-RPC {'error': {'code', 'message'}}
    dict used by the identity/session-expired checks, or a plain
    {'error': '<str>'} blob for generic exceptions (e.g. a decryption
    failure past the identity check, expected in this suite since no
    real encrypted session exists). Normalizes both into (code, message).
    """
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code"), error.get("message", "")
    return None, str(error)


class FakeIdentityVerifier:
    """Minimal duck-typed stand-in for stls-identity's bridge — only
    what SecureA2AHTTPMiddleware._check_identity_async() actually calls."""

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


_KEY_SESSION_PROVIDER: KeySessionProvider = KeySessionProviderFactory().get_session_provider(
    session_type=KeySessionProvider.KeySessionProviderType.SERVER, port=0
)


def _build_app(identity_verifier=None, api_key_manager=None) -> FastAPI:
    app = FastAPI(title="identity presentation test app")
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
        enable_plain_endpoint=False,
        api_key_manager=api_key_manager,
        identity_verifier=identity_verifier,
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_off_by_default() -> bool:
    print("\n── Test 1: no identity_verifier configured — off by default ──")
    app = _build_app(identity_verifier=None)
    ok = True
    async with _client(app) as client:
        resp = await client.post(
            "/secure-a2a",
            json={"session_id": "unknown-session", "ciphertext": "irrelevant"},
            headers={"X-A2A-Session-ID": "unknown-session"},
        )
        body = resp.json()
        code, _ = _error_info(body)
        if code == ERR_IDENTITY_CHALLENGE_REQUIRED:
            _fail("identity check fired with no identity_verifier configured", str(body))
            ok = False
        else:
            _ok("identity check never fired", f"got unrelated error code {code} instead")
    return ok


async def test_challenge_required() -> bool:
    print("\n── Test 2: identity_verifier configured, no presentation header ──")
    verifier = FakeIdentityVerifier(ok=True)
    app = _build_app(identity_verifier=verifier)
    ok = True
    async with _client(app) as client:
        resp = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-A", "ciphertext": "irrelevant"},
            headers={"X-A2A-Session-ID": "sess-A"},
        )
        body = resp.json()
        error = body.get("error", {})
        if resp.status_code == 401 and error.get("code") == ERR_IDENTITY_CHALLENGE_REQUIRED and error.get("challenge"):
            _ok("challenge required", f"challenge={error['challenge'][:12]}...")
        else:
            _fail("challenge required", f"status={resp.status_code} body={body}")
            ok = False
        if verifier.calls:
            _fail("verifier called before a presentation was ever sent", str(verifier.calls))
            ok = False
        else:
            _ok("verifier not called yet", "challenge issuance doesn't need the verifier")
    return ok


async def test_accepted_presentation_passes_identity_check() -> bool:
    print("\n── Test 3: presentation header present, verifier accepts ──")
    verifier = FakeIdentityVerifier(ok=True)
    app = _build_app(identity_verifier=verifier)
    ok = True
    async with _client(app) as client:
        # First call: get the challenge for this session.
        resp1 = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-B", "ciphertext": "irrelevant"},
            headers={"X-A2A-Session-ID": "sess-B"},
        )
        challenge = resp1.json()["error"]["challenge"]

        # Second call: present (a fake) VP header — the fake verifier
        # accepts unconditionally, regardless of its content.
        resp2 = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-B", "ciphertext": "irrelevant"},
            headers={
                "X-A2A-Session-ID": "sess-B",
                "X-STLS-Identity-Presentation": "ZmFrZS12cA==",
            },
        )
        body2 = resp2.json()
        code2, message2 = _error_info(body2)
        if code2 == ERR_IDENTITY_CHALLENGE_REQUIRED:
            _fail("identity check still blocking after an accepted presentation", str(body2))
            ok = False
        elif message2 == "Unauthorized: invalid identity presentation":
            _fail("identity check rejected an accepted presentation", str(body2))
            ok = False
        else:
            _ok(
                "identity check passed",
                f"proceeded to unrelated error (no real session/crypto in this suite): {message2}",
            )
        if verifier.calls == [challenge]:
            _ok("verifier called once with the right challenge")
        else:
            _fail("verifier not called with the expected challenge", f"calls={verifier.calls} expected=[{challenge}]")
            ok = False
    return ok


async def test_rejected_presentation_is_denied() -> bool:
    print("\n── Test 4: presentation header present, verifier rejects ──")
    verifier = FakeIdentityVerifier(ok=False)
    app = _build_app(identity_verifier=verifier)
    ok = True
    async with _client(app) as client:
        resp1 = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-C", "ciphertext": "irrelevant"},
            headers={"X-A2A-Session-ID": "sess-C"},
        )
        _ = resp1.json()["error"]["challenge"]

        resp2 = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-C", "ciphertext": "irrelevant"},
            headers={
                "X-A2A-Session-ID": "sess-C",
                "X-STLS-Identity-Presentation": "dGFtcGVyZWQ=",
            },
        )
        body2 = resp2.json()
        error2 = body2.get("error", {})
        if resp2.status_code == 401 and error2.get("code") != ERR_IDENTITY_CHALLENGE_REQUIRED and "identity" in error2.get("message", ""):
            _ok("rejected presentation denied", error2.get("message"))
        else:
            _fail("rejected presentation not denied as expected", f"status={resp2.status_code} body={body2}")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Regression: _check_auth_async's refactor must not change the
# api_key_manager-success path when identity_verifier is None (the
# original code returned None immediately on api_key_manager success;
# it now falls through to the identity check instead, which must still
# be a no-op when unconfigured — nothing else in this suite or in
# a2a_protocol_conformance_test.py configures api_key_manager, so this
# closes a real gap, not a redundant check).
# ---------------------------------------------------------------------------

class _FakeApiKeyManager:
    async def validate_request(self, request):
        class _Result:
            ok = True
        return _Result()


async def test_api_key_manager_success_still_falls_through_cleanly() -> bool:
    print("\n── Test: api_key_manager success + no identity_verifier — unchanged ──")
    app = _build_app(identity_verifier=None, api_key_manager=_FakeApiKeyManager())
    ok = True
    async with _client(app) as client:
        resp = await client.post(
            "/secure-a2a",
            json={"session_id": "sess-km", "ciphertext": "irrelevant"},
            headers={"X-A2A-Session-ID": "sess-km"},
        )
        code, _ = _error_info(resp.json())
        if code == ERR_IDENTITY_CHALLENGE_REQUIRED:
            _fail("identity check fired even though identity_verifier is None", resp.text)
            ok = False
        else:
            _ok("api_key_manager success path unaffected", f"got unrelated error code {code} instead")
    return ok


# ---------------------------------------------------------------------------
# STLSHttpxTransport client-side retry-once (sync + SSE)
# ---------------------------------------------------------------------------

def _challenge_required_error(url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    resp = httpx.Response(
        401,
        request=req,
        json={
            "jsonrpc": "2.0",
            "error": {
                "code": ERR_IDENTITY_CHALLENGE_REQUIRED,
                "message": "Identity presentation required",
                "challenge": "chal-xyz",
            },
            "id": None,
        },
    )
    return httpx.HTTPStatusError("401", request=req, response=resp)


class _FakeAdapter:
    """Stands in for SecureA2ATransportAdapter: fails with the
    challenge-required shape exactly `fail_times` times, then succeeds
    — isolates STLSHttpxTransport's retry orchestration from real
    crypto/network (covered elsewhere: Steps 2-4's tests, and the
    sample for the genuine end-to-end round trip)."""

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.sync_calls = 0
        self.stream_calls = 0
        self.sync_headers_seen: list[dict] = []
        self.stream_headers_seen: list[dict] = []

    async def send_secure_message(self, payload, extra_headers=None):
        self.sync_calls += 1
        self.sync_headers_seen.append(dict(extra_headers or {}))
        if self.sync_calls <= self.fail_times:
            raise _challenge_required_error("http://test/secure-a2a")
        return {"jsonrpc": "2.0", "result": {"ok": True}, "id": payload.get("id")}

    async def stream_secure_message(self, payload, extra_headers=None):
        self.stream_calls += 1
        self.stream_headers_seen.append(dict(extra_headers or {}))
        if self.stream_calls <= self.fail_times:
            raise _challenge_required_error("http://test/secure-a2a/stream")
        yield {
            "event": "TaskStatusUpdateEvent",
            "taskId": "t1", "contextId": "c1", "status": "completed", "final": True,
        }


class _FakeSessionManager:
    def __init__(self, adapter: _FakeAdapter):
        self._adapter = adapter
        self.invalidate_calls: list[str] = []

    async def get_adapter(self, base_url: str):
        return self._adapter

    def invalidate(self, base_url: str) -> None:
        self.invalidate_calls.append(base_url)


async def test_client_retries_once_on_identity_challenge_sync() -> bool:
    print("\n── Test 5a: STLSHttpxTransport retries once on challenge (sync) ──")
    adapter = _FakeAdapter(fail_times=1)
    manager = _FakeSessionManager(adapter)
    seen_challenges: list[str] = []

    def handler(challenge: str) -> dict[str, str]:
        seen_challenges.append(challenge)
        return {"X-STLS-Identity-Presentation": "ZmFrZS12cA=="}

    transport = STLSHttpxTransport(manager, identity_challenge_handler=handler)
    request = httpx.Request(
        "POST", "http://test/secure-a2a",
        json={"jsonrpc": "2.0", "method": "messages/send", "params": {}, "id": 1},
    )

    resp = await transport.handle_async_request(request)
    ok = True
    if resp.status_code == 200 and seen_challenges == ["chal-xyz"] and adapter.sync_calls == 2:
        _ok("retried exactly once with the right challenge", f"calls={adapter.sync_calls}")
    else:
        _fail(
            "retry did not happen as expected",
            f"status={resp.status_code} challenges={seen_challenges} calls={adapter.sync_calls}",
        )
        ok = False
    if "X-STLS-Identity-Presentation" in adapter.sync_headers_seen[-1]:
        _ok("retried request carried the presentation header")
    else:
        _fail("retried request missing the presentation header", str(adapter.sync_headers_seen))
        ok = False
    return ok


async def test_client_retries_once_on_identity_challenge_stream() -> bool:
    print("\n── Test 5b: STLSHttpxTransport retries once on challenge (SSE) ──")
    adapter = _FakeAdapter(fail_times=1)
    manager = _FakeSessionManager(adapter)
    seen_challenges: list[str] = []

    def handler(challenge: str) -> dict[str, str]:
        seen_challenges.append(challenge)
        return {"X-STLS-Identity-Presentation": "ZmFrZS12cA=="}

    transport = STLSHttpxTransport(manager, identity_challenge_handler=handler)
    request = httpx.Request(
        "POST", "http://test/secure-a2a",
        json={"jsonrpc": "2.0", "method": "messages/sendStream", "params": {}, "id": 1},
        headers={"Accept": "text/event-stream"},
    )

    resp = await transport.handle_async_request(request)
    ok = True
    if resp.status_code == 200 and seen_challenges == ["chal-xyz"] and adapter.stream_calls == 2:
        _ok("retried exactly once with the right challenge", f"calls={adapter.stream_calls}")
    else:
        _fail(
            "retry did not happen as expected",
            f"status={resp.status_code} challenges={seen_challenges} calls={adapter.stream_calls}",
        )
        ok = False
    if "X-STLS-Identity-Presentation" in adapter.stream_headers_seen[-1]:
        _ok("retried stream carried the presentation header")
    else:
        _fail("retried stream missing the presentation header", str(adapter.stream_headers_seen))
        ok = False
    return ok


# ---------------------------------------------------------------------------
# Regression: enable_plain_endpoint=False + base_url used to crash
# ---------------------------------------------------------------------------

async def test_enable_plain_endpoint_false_with_base_url_does_not_crash() -> bool:
    print("\n── Test 6: enable_plain_endpoint=False + base_url (unrelated regression) ──")
    ok = True
    try:
        app = FastAPI(title="regression test app")
        start_task_store_cleanup(app=app)

        async def handler(request):
            return AgentResponse(result={}, id=request.id)

        path = "/agents/no-plain/secure-a2a"
        create_secure_a2a_endpoint(
            app=app,
            agent=handler,
            key_session_provider=_KEY_SESSION_PROVIDER,
            path=path,
            base_url="http://localhost:9999",
            framework="custom",
            enable_plain_endpoint=False,
        )
    except TypeError as exc:
        _fail("create_secure_a2a_endpoint crashed", str(exc))
        return False

    card = app._a2a_agent_registry[path]
    if card.url is None and card.secure_url == "http://localhost:9999/agents/no-plain/secure-a2a":
        _ok("no crash, card.url is None, secure_url set correctly")
    else:
        _fail("card built with unexpected url/secure_url", f"url={card.url!r} secure_url={card.secure_url!r}")
        ok = False
    return ok


async def main() -> None:
    print("\n" + "=" * 70)
    print("Identity Presentation hook (issue #15, gap #1)")
    print("=" * 70)

    results: dict[str, bool] = {}
    results["1 — off by default"]            = await test_off_by_default()
    results["2 — challenge required"]        = await test_challenge_required()
    results["3 — accepted presentation"]     = await test_accepted_presentation_passes_identity_check()
    results["4 — rejected presentation"]      = await test_rejected_presentation_is_denied()
    results["5a — client retry (sync)"]       = await test_client_retries_once_on_identity_challenge_sync()
    results["5b — client retry (SSE)"]        = await test_client_retries_once_on_identity_challenge_stream()
    results["6 — plain-endpoint-off regression"] = await test_enable_plain_endpoint_false_with_base_url_does_not_crash()
    results["7 — api_key_manager success unaffected"] = await test_api_key_manager_success_still_falls_through_cleanly()

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
