"""
a2a_protocol_conformance_test.py
---------------------------------
Regression suite for the A2A protocol conformance gaps tracked in
issue #14. Grows incrementally as each gap is addressed; currently
covers:

Gap #1 — JSON-RPC method names
  A2A 1.0 (spec §9.4) renamed the JSON-RPC methods to PascalCase
  (SendMessage, GetTask, CancelTask, ListTasks, SubscribeToTask,
  Create/Get/List/DeleteTaskPushNotificationConfig, SendStreamingMessage),
  replacing 0.3's slash-style names (messages/send, tasks/get, ...).
  stls_a2a_adapters.py normalizes the 1.0 names into their 0.3
  equivalents at a2a_to_agent_request() (sync path) and
  stls_a2a_integration.py's SSE_STREAM_METHODS/SSE_SEND_METHODS/
  SSE_SUBSCRIBE_METHODS recognize both spellings directly (SSE path).

Gap #2 — AgentCard schema
  AgentCard.supported_interfaces/security_schemes (stls_a2a_card_model.py)
  now hold the A2A 1.0-shaped array/map (spec §4.4.1), auto-derived from
  the pre-existing url/security_scheme fields via a model_validator so
  no _card_from_*() builder needs to change. The old fields (url,
  secureUrl, securityScheme) are kept exactly as before.

  Correction found while testing real SK/LangChain agents: security_schemes
  is only populated for OFFICIALLY-recognized scheme types
  (apiKey/http/oauth2/openIdConnect/mutualTLS) — our own
  StlsKeyExchangeSecurityScheme ("stls_key_exchange") isn't one of them,
  and a real a2a-sdk 0.3.26 client's strict AgentCard model hard-FAILED
  card resolution entirely when it saw that type there. Omitting the
  field for our own scheme (spec-legal, it's optional) is the only safe
  choice — there's no conformant representation of it in either 0.3 or
  1.0's closed SecurityScheme union.

Gap #3 — capability flag vs. actual behavior
  _card_from_adk/_card_from_langgraph/_card_generic (stls_a2a_utils.py)
  hardcoded capabilities.streaming=False even though every adapter
  genuinely supports SSE streaming (GoogleADKAdapter natively,
  everything else via the base AgentAdapter.stream() fallback). Now
  True for all three, matching what messages/sendStream actually does.

Gap #4 — generic card name
  _card_generic() (stls_a2a_utils.py) fell back to
  type(agent).__name__ for agents without a .name attribute (e.g. a
  plain function handler) — literally the string "function" for
  every such agent. Now tries the function's own __name__ first, same
  fallback chain _tool_identity() already used for tools.

Gaps #5/#6 — found by running a REAL a2a-sdk 1.3.0 Client against this
server (not just hand-rolled JSON-RPC), while building the second half
of issue #14 (testing against more complex agents):

Gap #5 — SendMessage result not oneof-wrapped
  A2A 1.0's SendMessageResponse is a oneof {task, message} (spec
  §9.4.1) — the real SDK raised "Response has neither task nor
  message" because our result had the Task's fields spread flat, with
  no "task" key. handle_request()'s messages/send branch now also
  nests the same fields under "task", alongside the flat shape kept
  for existing consumers (stls_a2a_client.py, ADK's RemoteA2aAgent).

Gap #6 — discriminator-less parts silently dropped
  The real SDK's proto-JSON mapping sends a text part as the bare
  {"text": "..."} — no "type"/"kind" key, since the oneof's selected
  field name is itself the discriminator. _build_parts() treated this
  as empty and dropped it; even where kept, _handle_messages_send
  passed adapters request.params verbatim, so an adapter's own
  type/kind check (e.g. financial_agent.py's) still missed it — the
  agent silently saw an empty message, no error at all. Fixed in two
  places: _build_parts() now infers the type from which known key
  (text/data/file) is present, and _handle_messages_send/
  _handle_messages_send_stream now substitute the already-normalized
  parts (always carrying both "type" and "kind") into what adapters
  actually receive, instead of the raw, as-received ones.

Gap #7 — SSE streaming events not oneof-wrapped, plus a state-enum
mismatch found while fixing it
  A2A 1.0's StreamResponse is a oneof {task, message, statusUpdate,
  artifactUpdate} (spec §4.2) — our SSE events were a flat dict with
  "kind": "status-update"/"artifact-update", so a real SDK Client got
  back an event with NONE of the oneof's fields set (reproduced
  identically across every framework: custom, LangChain/LangGraph,
  Semantic Kernel — confirmed general-purpose, not agent-specific).
  _make_status_payload/_make_artifact_payload (stls_a2a_integration.py)
  now also nest the same data under "statusUpdate"/"artifactUpdate",
  additive alongside the flat shape. Fixing this surfaced a second,
  related issue: A2A 1.0's TaskState proto enum uses TASK_STATE_* names
  (e.g. "TASK_STATE_COMPLETED"), not our lowercase values — a real
  client silently defaults status.state to TASK_STATE_UNSPECIFIED
  otherwise (protobuf's JSON unmarshal ignores unrecognized enum
  strings rather than erroring), which matters more than it sounds:
  the 1.0 TaskStatusUpdateEvent proto has no separate "final" field at
  all, so status.state is the ONLY signal a strict client has for
  detecting task completion in a stream. Fixed via
  to_task_state_proto_name() (stls_a2a_task.py), used only inside the
  new nested structures (here and in gap #5's "task" wrapper) — the
  pre-existing flat lowercase fields are untouched.

Gaps #8/#9/#10 — found by continuing to exercise GetTask/CancelTask/
ListTasks/SubscribeToTask with the real a2a-sdk 1.3.0 Client after
fixing #7, out of the same "are there any other gaps" review:

Gap #8 — Message.role not the proto enum name
  Same root cause as gap #7's TaskState issue, for Role: the proto enum
  is ROLE_USER/ROLE_AGENT (spec §4.1.3), not our lowercase "user"/
  "agent" — a real client sees ROLE_UNSPECIFIED for every history
  message otherwise and can't tell who said what. Fixed via
  to_role_proto_name() (stls_a2a_task.py), used only inside gap #5's
  "task" wrapper — same scope restriction as gap #7's fix. GetTask/
  CancelTask return a bare Task with no wrapper to add this into
  without risking 0.3 compat (both read the exact same object), so
  their role/state stay lowercase — a known, deliberate asymmetry, not
  an oversight.

Gap #9 — ListTasks result not wrapped (the one non-additive fix here)
  A2A 1.0's ListTasksResponse is {"tasks": [...], "pageSize": ...,
  "nextPageToken": ...} (spec §9.4.4) — our result was a bare array, so
  a real Client hard-failed ("unhashable type: 'dict'") trying to parse
  it, not just a soft degradation. Unlike every other fix in this
  suite, JSON can't be an array AND an object with a "tasks" key at
  once, so the old bare-array shape is genuinely gone, not kept
  alongside. Grep-verified the only in-repo consumer (tests/task_manual.py)
  before changing it, and updated it to match.

Gap #10 — GetTask/CancelTask/SubscribeToTask reject every real client
  Their proto request field for the task identifier is "id" (spec
  §9.4.3 example: {"id": "task-uuid", "historyLength": 10}), not
  "taskId" — a real Client sends exactly "id" and got a flat "taskId
  param is required" error for all three methods. Not an enum/shape
  nuance like #8/#9 — a complete functional break, the worst of this
  batch. Both names are accepted now.

Gaps #11/#12 — file parts, found checking "anything else?" one more
time (both directions of the same incoming/outgoing split as #6):

Gap #11 — incoming flat FilePart silently dropped
  A2A 1.0's Part proto has no "file" wrapper at all — FileInline/
  FileReference's fields (raw/url/filename/mediaType) sit directly on
  Part, part of its `content` oneof {text, raw, url, data}. Same
  symptom as gap #6: _build_parts() didn't recognize raw/url as file
  indicators and silently dropped the part, no error. Fixed by
  extending the same type-inference fallback gap #6 added.

Gap #12 — outgoing FilePart still only the old nested shape
  Even once accepted, a FilePart echoed back (e.g. in Task.history)
  was still serialized with only the nested {"file": {...}} shape — a
  real client's Part.WhichOneof('content') came back None, effectively
  empty. This is the outgoing-direction counterpart gap #6/#11 didn't
  cover (those only fixed incoming parsing). Fixed via a new
  model_serializer on FilePart (stls_a2a_task.py) that adds the flat
  raw/url/filename/mediaType fields alongside the nested "file" shape
  — a single change point, since every Task.history/artifact dump
  goes through this one model's serialization.

This suite verifies both ends of each gap's fix against a real FastAPI
app wired exactly as create_secure_a2a_endpoint() builds it, in-process
via httpx.ASGITransport (no server process, no network):

  1. 0.3 names / pre-existing card fields still behave exactly as
     before (backward-compat guard — this suite exists specifically
     because this must never regress).
  2. 1.0 names / card fields produce the spec-conformant shape.
  3. An unrecognized method name still gets -32601 (the alias map must
     not swallow real errors).
"""

from __future__ import annotations

import json

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

async def test_0_3_names_unchanged():
    app = _build_app()
    async with _client(app) as client:
        send = await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        assert send.get("result", {}).get("status", {}).get("state") == "completed", send
        task_id = send["result"]["taskId"]

        get = await _rpc(client, "tasks/get", {"taskId": task_id})
        assert get.get("result", {}).get("id") == task_id, get

        lst = await _rpc(client, "tasks/list", {})
        # result is {"tasks": [...], "pageSize": ...} (spec §9.4.4), not
        # a bare array — the one non-additive fix in this suite (gap #9:
        # a real a2a-sdk 1.3.0 Client hard-failed trying to parse the old
        # bare-array shape; JSON can't be an array AND an object with a
        # "tasks" key at once, so there was no way to keep both).
        assert isinstance(lst.get("result"), dict), lst
        assert isinstance(lst["result"].get("tasks"), list), lst

        cancel = await _rpc(client, "tasks/cancel", {"taskId": task_id})
        assert cancel.get("error", {}).get("code") == -32002, cancel  # already completed

        push = await _rpc(client, "tasks/pushNotificationConfig/set", {})
        assert push.get("error", {}).get("code") == -32003, push  # unsupported


# ── Test 2: 1.0 names produce the same outcome as their 0.3 equivalent ────

async def test_1_0_names_match_0_3():
    app = _build_app()
    async with _client(app) as client:
        send = await _rpc(client, "SendMessage", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        assert send.get("result", {}).get("status", {}).get("state") == "completed", send
        task_id = send["result"]["taskId"]

        get = await _rpc(client, "GetTask", {"taskId": task_id})
        assert get.get("result", {}).get("id") == task_id, get

        lst = await _rpc(client, "ListTasks", {})
        assert isinstance(lst.get("result"), dict), lst
        assert isinstance(lst["result"].get("tasks"), list), lst

        cancel = await _rpc(client, "CancelTask", {"taskId": task_id})
        assert cancel.get("error", {}).get("code") == -32002, cancel

        for method in (
            "CreateTaskPushNotificationConfig",
            "GetTaskPushNotificationConfig",
            "ListTaskPushNotificationConfigs",
            "DeleteTaskPushNotificationConfig",
        ):
            resp = await _rpc(client, method, {})
            # -32003 (unsupported), not -32601 (unknown method) — the
            # alias map must recognize these names even though they're
            # never actually implemented.
            assert resp.get("error", {}).get("code") == -32003, (method, resp)


# ── Test 3: SSE — 1.0 streaming names match their 0.3 equivalent ──────────

async def test_1_0_sse_names():
    app = _build_app()
    async with _client(app) as client:
        events = await _sse_events(
            client, "SendStreamingMessage",
            {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}, "expression": "add(2,3)"},
        )
        states = [e["result"]["status"]["state"] for e in events if e["result"]["kind"] == "status-update"]
        assert states == ["submitted", "working", "completed"], states
        task_id = next(e["result"]["taskId"] for e in events)

        sub_events = await _sse_events(client, "SubscribeToTask", {"taskId": task_id})
        sub_states = [e["result"]["status"]["state"] for e in sub_events if e["result"]["kind"] == "status-update"]
        assert sub_states == ["completed"], sub_states  # already-completed task -> immediate


# ── Test 4: unknown methods are still rejected (no over-broad aliasing) ───

async def test_unknown_method_still_rejected():
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "TotallyMadeUpMethod", {})
        assert resp.get("error", {}).get("code") == -32601, resp


# ── Test 5: AgentCard — old fields unchanged, new 1.0 fields correct ──────

async def test_agent_card_schema():
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/.well-known/agent-card.json")
        resp.raise_for_status()
        card = resp.json()

        # Backward compat: pre-existing fields untouched.
        assert card.get("url") and card.get("secureUrl"), card

        old_scheme = card.get("securityScheme")
        assert isinstance(old_scheme, dict) and "type" in old_scheme, card

        # New A2A 1.0 fields: present, correctly shaped, and derived from
        # the same values as the fields above (not independently wrong).
        interfaces = card.get("supportedInterfaces")
        assert isinstance(interfaces, list) and interfaces, card
        assert interfaces[0].get("url") == card.get("url"), card
        assert interfaces[0].get("protocolBinding") == "JSONRPC", card

        # securitySchemes is a CLOSED union in both 0.3 and 1.0
        # (apiKey/http/oauth2/openIdConnect/mutualTLS — no custom
        # variant). Our default scheme (stls_key_exchange) isn't one of
        # those, so it must be OMITTED here, not populated — the real
        # a2a-sdk 0.3.26 client's AgentCard model hard-fails card
        # resolution entirely if it sees an unrecognized type in this
        # field (confirmed against a real client, issue #14). The
        # positive case — an official-type scheme DOES get populated —
        # is checked directly below, since this test app's scheme is
        # always the non-standard one.
        schemes = card.get("securitySchemes")
        assert old_scheme.get("type") == "stls_key_exchange", old_scheme
        assert schemes == {}, schemes

        # Positive case: an OFFICIAL scheme type must still populate
        # securitySchemes (the gating is type-specific, not "never").
        # Checked directly against the model — changing KEY_PROVIDER_NAME
        # per-test isn't possible, it's read once at module import time.
        from stls_a2a.stls_a2a_card_model import AgentCard, HTTPSecurityScheme
        official_card = AgentCard(
            name="x", description="x", url="http://x/a2a",
            security_scheme=HTTPSecurityScheme(scheme="bearer"),
        )
        assert official_card.security_schemes.get("http") is not None, official_card.security_schemes


# ── Test 6: capability flag matches actual behavior (gap #3) ─────────────

async def test_streaming_capability_matches_reality():
    """
    _card_from_adk/_card_from_langgraph/_card_generic used to hardcode
    capabilities.streaming=False even though every adapter (native
    override in GoogleADKAdapter, or the base AgentAdapter.stream()
    fallback for everything else) genuinely supports SSE streaming.
    This doesn't just check the flag's value — it re-exercises the
    SSE path (same as test_1_0_sse_names) and confirms the card's
    claim and the actual behavior agree, for the 'custom' framework
    this test app uses (_card_generic's code path).
    """
    app = _build_app()
    async with _client(app) as client:
        card = (await client.get("/.well-known/agent-card.json")).json()
        assert card.get("capabilities", {}).get("streaming") is True, card

        events = await _sse_events(
            client, "messages/sendStream",
            {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}, "expression": "add(2,3)"},
        )
        states = [e["result"]["status"]["state"] for e in events if e["result"]["kind"] == "status-update"]
        assert states == ["submitted", "working", "completed"], states


# ── Test 7: generic card name isn't the useless "function" (gap #4) ──────

async def test_card_name_not_generic():
    """
    _card_generic() used to report type(agent).__name__ as a last
    resort, which for a plain function handler (this test app's
    calculator_handler, same as multi_agent_server.py's) is literally
    the string "function" for every such agent — not a useful or even
    distinguishing identifier. Now falls back to the function's own
    __name__ first.
    """
    app = _build_app()
    async with _client(app) as client:
        card = (await client.get("/.well-known/agent-card.json")).json()
        name = card.get("name")
        assert name and name != "function", name


# ── Test 8: SendMessage result is oneof-wrapped (gap #5) ─────────────────

async def test_send_message_result_is_task_wrapped():
    """
    A2A 1.0's SendMessageResponse is a oneof {task, message} (spec
    §9.4.1) — a strict proto-JSON client expects the result wrapped
    under a "task" key, not the Task's fields spread flat at the top
    level. Discovered by running a REAL a2a-sdk 1.3.0 Client against
    this server: it raised "Response has neither task nor message"
    before this fix. Checked here with a plain httpx/JSON assertion
    (no a2a-sdk dependency in this venv) so the regression is caught
    without needing the real SDK installed.

    The flat shape is kept alongside — existing consumers
    (stls_a2a_client.py, ADK's RemoteA2aAgent) read it directly and
    must keep working unchanged.
    """
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        result = resp.get("result", {})
        task_id = result.get("taskId")

        assert "task" in result and result["task"].get("id") == task_id, result
        assert result.get("id") == task_id and result.get("status"), result


# ── Test 9: a part with neither "type" nor "kind" reaches the agent ──────

async def test_discriminatorless_part_is_not_dropped():
    """
    A2A 1.0's real wire format (a2a-sdk>=1.0's proto-JSON mapping of
    Part's oneof) sends a text part as the bare {"text": "..."}  — no
    "type"/"kind" discriminator key at all, since the oneof's selected
    field name is itself the only discriminator. Before this fix,
    _build_parts() silently dropped such parts (treating them as
    empty), and even where kept, _handle_messages_send passed adapters
    request.params verbatim — so an adapter's own type/kind check
    (e.g. financial_agent.py's) still missed it. Discovered the same
    way as Test 8: a real a2a-sdk 1.3.0 client got a generic fallback
    reply instead of an actual answer.

    Verified here against the calculator fixture, whose own extraction
    (mock_agent.py) checks `p.get("type") == "text"` — strict, no
    "kind" fallback of its own — so this only passes if
    _handle_messages_send is handing the adapter an already-normalized
    part (both "type" and "kind" set), not the bare one received.
    """
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "messages/send", {
            "message": {"parts": [{"text": "add(2,3)"}]},  # bare — no type/kind
        })
        artifacts = resp.get("result", {}).get("artifacts", [])
        text = "".join(
            p.get("text", "") for a in artifacts for p in a.get("parts", [])
            if p.get("type") == "text"
        )
        assert '"result": 5' in text and '"status": "success"' in text, text


# ── Test 10: SSE events are oneof-wrapped with correct state names ───────

async def test_sse_events_are_oneof_wrapped():
    """
    A2A 1.0's StreamResponse is a oneof {task, message, statusUpdate,
    artifactUpdate} (spec §4.2). Checks both halves of gap #7's fix:
    the wrapper key is present (a real client got back fields-free
    events before this fix), and status.state inside it uses the
    proto's TASK_STATE_* enum names (a real client otherwise silently
    sees TASK_STATE_UNSPECIFIED for every event — protobuf's JSON
    unmarshal ignores unrecognized enum strings rather than erroring).
    The flat, pre-existing shape (kind/taskId/status.state lowercase)
    must stay exactly as it was, for 0.3/existing SSE consumers.
    """
    app = _build_app()
    async with _client(app) as client:
        events = await _sse_events(
            client, "messages/sendStream",
            {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}, "expression": "add(2,3)"},
        )

        status_events = [e["result"] for e in events if e["result"]["kind"] == "status-update"]
        artifact_events = [e["result"] for e in events if e["result"]["kind"] == "artifact-update"]

        assert status_events and all(e.get("status", {}).get("state") for e in status_events), status_events

        proto_states = [e.get("statusUpdate", {}).get("status", {}).get("state") for e in status_events]
        assert proto_states and all(s and s.startswith("TASK_STATE_") for s in proto_states), proto_states
        assert proto_states[-1] == "TASK_STATE_COMPLETED", proto_states

        assert artifact_events and all("artifactUpdate" in e for e in artifact_events), artifact_events


# ── Test 11: history message.role uses the proto enum name (gap #8) ──────

async def test_history_role_proto_name():
    """
    A2A 1.0's Role proto enum is ROLE_USER/ROLE_AGENT (spec §4.1.3), not
    our lowercase "user"/"agent" — a real client otherwise sees
    ROLE_UNSPECIFIED for every history message and can't tell which came
    from whom. Checked only inside the new "task" wrapper (gap #5) — the
    flat history array alongside it keeps the original lowercase values
    for 0.3/existing consumers.
    """
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        result = resp.get("result", {})

        flat_roles = [m.get("role") for m in result.get("history", [])]
        assert flat_roles == ["user", "agent"], flat_roles

        proto_roles = [m.get("role") for m in result.get("task", {}).get("history", [])]
        assert proto_roles == ["ROLE_USER", "ROLE_AGENT"], proto_roles


# ── Test 12: ListTasks result is wrapped, not a bare array (gap #9) ──────

async def test_list_tasks_result_is_wrapped():
    """
    A2A 1.0's ListTasksResponse is {"tasks": [...], "pageSize": ...,
    "nextPageToken": ...} (spec §9.4.4), not a bare array. A real
    a2a-sdk 1.3.0 Client hard-failed trying to parse the old bare-array
    shape ("unhashable type: 'dict'"). Unlike every other fix in this
    suite, this ISN'T purely additive — JSON can't be an array AND an
    object with a "tasks" key at once — so the old shape is genuinely
    gone; grep-verified the only in-repo consumer (tests/task_manual.py)
    before changing it, and updated it alongside this fix.
    """
    app = _build_app()
    async with _client(app) as client:
        await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        resp = await _rpc(client, "tasks/list", {"limit": 50})
        result = resp.get("result")

        assert isinstance(result, dict), result
        assert isinstance(result.get("tasks"), list), result
        assert "pageSize" in result, result


# ── Test 13: GetTask/CancelTask/SubscribeToTask accept "id" (gap #10) ────

async def test_task_methods_accept_id_param():
    """
    GetTaskRequest/CancelTaskRequest/SubscribeToTaskRequest's proto
    field for the task identifier is "id" (spec §9.4.3 example:
    {"id": "task-uuid", "historyLength": 10}), not "taskId". A real
    a2a-sdk 1.3.0 Client sends exactly "id" and got a flat "taskId
    param is required" error for all three methods before this fix —
    not an enum/shape nuance, a complete functional break. Both names
    are accepted now; "taskId" (0.3/existing consumers) is unaffected.
    """
    app = _build_app()
    async with _client(app) as client:
        send = await _rpc(client, "messages/send", {"message": {"parts": [{"type": "text", "text": "add(2,3)"}]}})
        task_id = send["result"]["taskId"]

        get = await _rpc(client, "GetTask", {"id": task_id})
        assert get.get("result", {}).get("id") == task_id, get

        sub_events = await _sse_events(client, "SubscribeToTask", {"id": task_id})
        assert sub_events

        cancel = await _rpc(client, "CancelTask", {"id": task_id})
        # Already completed -> correctly rejected, but that's a real
        # business-logic response, not the "id required" param error.
        assert cancel.get("error", {}).get("code") == -32002, cancel


# ── Test 14: file parts round-trip in A2A 1.0's flat shape (gaps #11/#12) ─

async def test_file_part_round_trips():
    """
    A2A 1.0's Part proto has no "file" wrapper at all — FileInline/
    FileReference's fields (raw/url/filename/mediaType) sit directly on
    Part, part of its `content` oneof {text, raw, url, data}.

    Incoming (gap #11): a file part sent this way used to be silently
    dropped by _build_parts() — same symptom as gap #6's text-part
    issue (an empty history, no error at all).

    Outgoing (gap #12): once accepted, the part was still being echoed
    back (e.g. in Task.history) using only the old nested "file" shape
    — a strict client's Part.WhichOneof('content') came back None,
    effectively empty. Fixed via FilePart's new model_serializer
    (stls_a2a_task.py), additive alongside the nested shape kept for
    0.3/existing consumers.
    """
    app = _build_app()
    async with _client(app) as client:
        resp = await _rpc(client, "messages/send", {
            "message": {"parts": [{"raw": "aGVsbG8gd29ybGQ=", "filename": "test.txt", "mediaType": "text/plain"}]},
        })
        history = resp.get("result", {}).get("history", [])
        user_parts = history[0].get("parts", []) if history else []

        assert user_parts and user_parts[0].get("type") == "file", user_parts  # gap #11

        part = user_parts[0]
        assert part.get("raw") == "aGVsbG8gd29ybGQ=", part  # gap #12
        assert part.get("mediaType") == "text/plain", part
        assert part.get("filename") == "test.txt", part

        assert isinstance(part.get("file"), dict), part  # nested shape unchanged
        assert part["file"].get("bytes") == "aGVsbG8gd29ybGQ=", part
