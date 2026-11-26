"""
task_store_test.py
-------------------
Unit tests for the in-memory `TaskStore` (stls_a2a_task.py) — the
backend every test in this suite runs against (TASK_STORE_BACKEND=memory,
forced in conftest.py), and the piece every manual integration script
in this directory (task_manual.py, the KM/key-provider scripts, ...)
otherwise only exercises indirectly, through full HTTP round trips
against a real server.

No infra of any kind: pure in-process asyncio + dict. `RedisTaskStore`
is intentionally out of scope here (needs a real Redis — see issue #57
for a possible future infra-backed CI layer).

Also covers the small free functions this module added for A2A 1.0
conformance (issue #14): to_task_state_proto_name/to_role_proto_name,
and FilePart's additive flat-field serialization (gap #12).
"""

import pytest

from stls_a2a.stls_a2a_task import (
    FileInline,
    FilePart,
    Message,
    TaskState,
    TaskStore,
    to_role_proto_name,
    to_task_state_proto_name,
)


@pytest.fixture
def store() -> TaskStore:
    return TaskStore()


async def test_create_assigns_task_id_and_defaults(store: TaskStore):
    task = await store.create()
    assert task.taskId
    assert task.status.state == TaskState.SUBMITTED
    assert task.history == []
    assert task.artifacts == []


async def test_create_honors_explicit_ids(store: TaskStore):
    task = await store.create(context_id="ctx-1", task_id="task-1")
    assert task.taskId == "task-1"
    assert task.contextId == "ctx-1"


async def test_get_roundtrips_a_created_task(store: TaskStore):
    created = await store.create()
    fetched = await store.get(created.taskId)
    assert fetched is created


async def test_get_unknown_task_returns_none(store: TaskStore):
    assert await store.get("does-not-exist") is None


async def test_update_status_changes_state_and_bumps_updated_at(store: TaskStore):
    task = await store.create()
    before = task.updatedAt
    updated = await store.update_status(task.taskId, TaskState.WORKING)
    assert updated.status.state == TaskState.WORKING
    assert updated.updatedAt >= before


async def test_update_status_on_unknown_task_returns_none(store: TaskStore):
    assert await store.update_status("nope", TaskState.WORKING) is None


async def test_append_message_grows_history_in_order(store: TaskStore):
    task = await store.create()
    await store.append_message(task.taskId, Message(role="user", parts=[]))
    await store.append_message(task.taskId, Message(role="agent", parts=[]))
    fetched = await store.get(task.taskId)
    assert [m.role for m in fetched.history] == ["user", "agent"]


async def test_append_artifact_grows_artifacts(store: TaskStore):
    from stls_a2a.stls_a2a_task import Artifact

    task = await store.create()
    await store.append_artifact(task.taskId, Artifact(parts=[]))
    fetched = await store.get(task.taskId)
    assert len(fetched.artifacts) == 1


async def test_cancel_moves_task_to_canceled(store: TaskStore):
    task = await store.create()
    canceled = await store.cancel(task.taskId)
    assert canceled.status.state == TaskState.CANCELED


async def test_list_tasks_respects_limit_and_cursor(store: TaskStore):
    ids = [f"t{i}" for i in range(5)]
    for tid in ids:
        await store.create(task_id=tid)

    first_page = await store.list_tasks(limit=2)
    assert len(first_page) == 2

    all_tasks = await store.list_tasks(limit=50)
    assert {t.taskId for t in all_tasks} == set(ids)

    # Cursor is a taskId; the next page starts right after it.
    cursor_id = all_tasks[1].taskId
    after_cursor = await store.list_tasks(limit=50, cursor=cursor_id)
    assert cursor_id not in {t.taskId for t in after_cursor}


async def test_find_by_context_id_returns_most_recent(store: TaskStore):
    older = await store.create(context_id="ctx-shared", task_id="older")
    await store.update_status(older.taskId, TaskState.WORKING)  # bumps updatedAt
    newer = await store.create(context_id="ctx-shared", task_id="newer")
    await store.update_status(newer.taskId, TaskState.WORKING)

    found = await store.find_by_context_id("ctx-shared")
    assert found.taskId == "newer"


async def test_find_by_context_id_unknown_context_returns_none(store: TaskStore):
    assert await store.find_by_context_id("no-such-context") is None


async def test_find_previous_by_context_id_excludes_given_task(store: TaskStore):
    await store.create(context_id="ctx-2", task_id="first")
    await store.create(context_id="ctx-2", task_id="second")

    previous = await store.find_previous_by_context_id("ctx-2", exclude_task_id="second")
    assert previous.taskId == "first"


async def test_touch_bumps_updated_at_without_changing_state(store: TaskStore):
    task = await store.create()
    before = task.updatedAt
    await store.touch(task.taskId)
    fetched = await store.get(task.taskId)
    assert fetched.status.state == TaskState.SUBMITTED  # unchanged
    assert fetched.updatedAt >= before


# ── A2A 1.0 proto-name helpers (issue #14, gaps #7/#8) ────────────────────

@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (TaskState.SUBMITTED, "TASK_STATE_SUBMITTED"),
        (TaskState.COMPLETED, "TASK_STATE_COMPLETED"),
        ("canceled", "TASK_STATE_CANCELED"),  # plain string also accepted
    ],
)
def test_to_task_state_proto_name_known_values(state, expected):
    assert to_task_state_proto_name(state) == expected


def test_to_task_state_proto_name_unknown_value_falls_back_to_unspecified():
    assert to_task_state_proto_name("not-a-real-state") == "TASK_STATE_UNSPECIFIED"


@pytest.mark.parametrize(
    ("role", "expected"),
    [("user", "ROLE_USER"), ("agent", "ROLE_AGENT")],
)
def test_to_role_proto_name_known_values(role, expected):
    assert to_role_proto_name(role) == expected


def test_to_role_proto_name_unknown_value_falls_back_to_unspecified():
    assert to_role_proto_name("narrator") == "ROLE_UNSPECIFIED"


# ── FilePart's additive flat-field serialization (gap #12) ───────────────

def test_file_part_serializes_both_nested_and_flat_shapes():
    # by_alias=True matches how the real wire path serializes parts
    # (every p.model_dump(...) call across the codebase passes it) —
    # FileInline.data's alias is "bytes".
    part = FilePart(file=FileInline(mimeType="text/plain", bytes="aGVsbG8=", name="a.txt"))
    dumped = part.model_dump(by_alias=True)

    assert dumped["file"]["bytes"] == "aGVsbG8="  # nested shape, unchanged
    assert dumped["raw"] == "aGVsbG8="  # flat shape, additive (gap #12)
    assert dumped["mediaType"] == "text/plain"
    assert dumped["filename"] == "a.txt"
