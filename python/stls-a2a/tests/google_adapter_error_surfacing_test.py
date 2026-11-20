"""
google_adapter_error_surfacing_test.py
-----------------------------------------
Regression for a real bug found while testing issue #45, but general —
not identity-specific: GoogleADKAdapter's invoke() checked
`hasattr(event, 'is_error') and event.is_error()` to detect a failed
sub-agent call. `Event` (google.adk.events.event, extends
LlmResponse) has NO `is_error()` method — only `error_code`/
`error_message` fields — so that check was permanently False, dead
code. A RemoteA2aAgent sub-agent failure (e.g. a 401 from an
identity-protected endpoint) yields exactly such an Event
(error_message set, content=None); invoke() silently fell through to
"No response received from agent" (losing the real reason), and
_invoke_stream() — the path a real chat UI like sofia-client-secure
actually uses — dropped it with ZERO trace at all
(`if not (event.content and event.content.parts): continue`).

Same in-process style as identity_presentation_test.py: real
GoogleADKAdapter, real google-adk Agent/InMemoryRunner construction,
but runner.run_async() monkeypatched to a fake event sequence — no
real LLM call, no network, isolates exactly the fixed logic.
"""

import asyncio
import os
import sys

os.environ.setdefault("KEY_PROVIDER_NAME", "base")
os.environ.setdefault("TASK_STORE_BACKEND", "memory")
os.environ["AUTH_PG_DSN"] = ""

from google.adk.agents import Agent

from stls_a2a.stls_a2a_adapters_model import AgentRequest
from stls_a2a.stls_a2a_google_adapter import GoogleADKAdapter


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅ {label}{': ' + detail if detail else ''}")


def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌ {label}{': ' + detail if detail else ''}")


class _FakeContent:
    def __init__(self, parts):
        self.parts = parts


class _FakeTextPart:
    def __init__(self, text: str):
        self.text = text


class _FakeEvent:
    """Minimal stand-in for google.adk.events.Event — only the
    attributes GoogleADKAdapter actually reads."""

    def __init__(self, *, error_message=None, error_code=None, content=None, final=True):
        self.error_message = error_message
        self.error_code = error_code
        self.content = content
        self._final = final

    def is_final_response(self) -> bool:
        return self._final


def _build_adapter() -> GoogleADKAdapter:
    agent = Agent(
        name="test_agent", model="gemini-2.5-flash",
        description="test agent, never actually calls the model in this suite",
    )
    return GoogleADKAdapter(agent, app_name="google_adapter_error_test")


def _request(method: str = "messages/send") -> AgentRequest:
    return AgentRequest(
        method=method,
        params={"message": {"parts": [{"type": "text", "text": "hello"}]}},
        id=1,
    )


async def test_invoke_surfaces_sub_agent_error() -> bool:
    print("\n── Test 1: invoke() surfaces a sub-agent's error_message ──")
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(error_message="A2A request failed: 401", error_code=None)

    adapter.runner.run_async = _fake_run_async

    response = await adapter.invoke(_request())
    ok = True
    if response.error and "401" in str(response.error.get("message", "")):
        _ok("error surfaced", str(response.error))
    else:
        _fail("error not surfaced as expected", str(response.model_dump()))
        ok = False
    return ok


async def test_invoke_stream_surfaces_sub_agent_error() -> bool:
    print("\n── Test 2: _invoke_stream() surfaces a sub-agent's error_message ──")
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(error_message="A2A request failed: 401", error_code=None)

    adapter.runner.run_async = _fake_run_async

    chunks = []
    async for chunk in adapter._invoke_stream(_request("messages/sendStream")):
        chunks.append(chunk)

    ok = True
    if not chunks:
        _fail("stream produced nothing at all — the exact bug this regresses")
        ok = False
    else:
        text = chunks[0]["parts"][0]["text"]
        if "401" in text:
            _ok("error surfaced as a visible text part", text)
        else:
            _fail("first chunk doesn't contain the error", str(chunks))
            ok = False
    return ok


async def test_invoke_stream_normal_path_unaffected() -> bool:
    print("\n── Test 3: normal (non-error) events still stream correctly ──")
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(content=_FakeContent([_FakeTextPart("hello back")]), final=True)

    adapter.runner.run_async = _fake_run_async

    chunks = []
    async for chunk in adapter._invoke_stream(_request("messages/sendStream")):
        chunks.append(chunk)

    ok = True
    first_part = chunks[0]["parts"][0] if chunks and chunks[0]["parts"] else None
    text = getattr(first_part, "text", None) or (first_part.get("text") if isinstance(first_part, dict) else None)
    if text == "hello back":
        _ok("normal event still streamed correctly")
    else:
        _fail("normal event path regressed", str(chunks))
        ok = False
    return ok


async def main() -> None:
    print("\n" + "=" * 70)
    print("GoogleADKAdapter error surfacing (found while testing issue #45)")
    print("=" * 70)

    results: dict[str, bool] = {}
    results["1 — invoke() surfaces error"] = await test_invoke_surfaces_sub_agent_error()
    results["2 — _invoke_stream() surfaces error"] = await test_invoke_stream_surfaces_sub_agent_error()
    results["3 — normal stream path unaffected"] = await test_invoke_stream_normal_path_unaffected()

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
