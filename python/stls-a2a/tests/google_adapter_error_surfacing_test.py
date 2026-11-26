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

from google.adk.agents import Agent

from stls_a2a.stls_a2a_adapters_model import AgentRequest
from stls_a2a.stls_a2a_google_adapter import GoogleADKAdapter


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


async def test_invoke_surfaces_sub_agent_error():
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(error_message="A2A request failed: 401", error_code=None)

    adapter.runner.run_async = _fake_run_async

    response = await adapter.invoke(_request())

    assert response.error is not None, response.model_dump()
    assert "401" in str(response.error.get("message", "")), response.model_dump()


async def test_invoke_stream_surfaces_sub_agent_error():
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(error_message="A2A request failed: 401", error_code=None)

    adapter.runner.run_async = _fake_run_async

    chunks = []
    async for chunk in adapter._invoke_stream(_request("messages/sendStream")):
        chunks.append(chunk)

    assert chunks, "stream produced nothing at all — the exact bug this regresses"
    text = chunks[0]["parts"][0]["text"]
    assert "401" in text, chunks


async def test_invoke_stream_normal_path_unaffected():
    adapter = _build_adapter()

    async def _fake_run_async(**_kwargs):
        yield _FakeEvent(content=_FakeContent([_FakeTextPart("hello back")]), final=True)

    adapter.runner.run_async = _fake_run_async

    chunks = []
    async for chunk in adapter._invoke_stream(_request("messages/sendStream")):
        chunks.append(chunk)

    first_part = chunks[0]["parts"][0] if chunks and chunks[0]["parts"] else None
    text = getattr(first_part, "text", None) or (first_part.get("text") if isinstance(first_part, dict) else None)
    assert text == "hello back", chunks
