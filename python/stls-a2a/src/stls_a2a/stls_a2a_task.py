from pydantic import BaseModel, Field, ConfigDict
from pydantic.alias_generators import to_camel
from enum import Enum
from typing import Union, Optional, Literal
from datetime import datetime, timezone
from abc import ABC, abstractmethod
import uuid
import asyncio
import time
import os
import redis.asyncio as aioredis  # mandatory dependency: pip install redis>=5.0

import logging
logger = logging.getLogger(__name__)

class TaskState(str, Enum):
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"

# ── TextPart ──────────────────────────────────────────
class TextPart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: Literal["text"] = "text"
    kind: Literal["text"] = "text"   # ADK RemoteA2aAgent compatibility (mirrors type)
    text: str
    mimeType: Optional[str] = "text/plain"


# ── FilePart ──────────────────────────────────────────
class FileInline(BaseModel):
    """File sent as inline base64."""
    model_config = ConfigDict(populate_by_name=True)
    mimeType: str
    data: str = Field(alias="bytes")
    name: Optional[str] = None   # original file name (e.g. "report.xlsx")


class FileReference(BaseModel):
    """File referenced via URL."""
    model_config = ConfigDict(populate_by_name=True)
    mimeType: str
    uri: str


class FilePart(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    type: Literal["file"] = "file"
    kind: Literal["file"] = "file"   # ADK RemoteA2aAgent compatibility (mirrors type)
    file: Union[FileInline, FileReference]


# ── DataPart ──────────────────────────────────────────
class DataPart(BaseModel):
    """Arbitrary structured JSON."""
    model_config = ConfigDict(populate_by_name=True)
    type: Literal["data"] = "data"
    kind: Literal["data"] = "data"   # ADK RemoteA2aAgent compatibility (mirrors type)
    data: dict
    mimeType: Optional[str] = "application/json"


# ── Part = discriminated union ──────────────────────────
Part = Union[TextPart, FilePart, DataPart]


# ── Message ───────────────────────────────────────────
class Message(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    messageId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: Literal["user", "agent"]
    parts: list[Part]
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Artifact(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    artifactId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: Optional[str] = None
    parts: list[Part] = []
    createdAt: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ── TaskStatus wrapper (spec A2A §4.1.3) ─────────────────────────────────
class TaskStatus(BaseModel):
    """Object wrapper for the task's state."""
    model_config = ConfigDict(populate_by_name=True)
    state: TaskState
    message: Optional[Message] = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Task(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    taskId: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        alias="id",
    )
    contextId: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: TaskStatus = Field(
        default_factory=lambda: TaskStatus(state=TaskState.SUBMITTED)
    )
    history: list[Message] = []
    artifacts: list[Artifact] = []
    createdAt: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updatedAt: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class TaskStatusUpdateEvent(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    task_id: str
    context_id: str
    status: TaskState
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    final: bool = False


class TaskArtifactUpdateEvent(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    task_id: str
    context_id: str
    artifact: Artifact
    append: bool = False
    last_chunk: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# TaskStoreBase — common interface (ABC)
# ═══════════════════════════════════════════════════════════════════════════

class TaskStoreBase(ABC):
    """
    Abstract interface for the TaskStore.
    Both implementations (in-memory and Redis) must honor
    this contract — the rest of the codebase only depends on TaskStoreBase.
    """

    @abstractmethod
    async def create(
        self,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        """Creates and persists a new Task."""
        ...

    @abstractmethod
    async def get(self, task_id: str) -> Optional[Task]:
        """Retrieves a Task by taskId. Returns None if it doesn't exist."""
        ...

    @abstractmethod
    async def update_status(self, task_id: str, status: TaskState) -> Optional[Task]:
        """Updates the state (wrapped in TaskStatus) of an existing Task."""
        ...

    @abstractmethod
    async def append_message(self, task_id: str, message: Message) -> Optional[Task]:
        """Adds a Message to the Task's history."""
        ...

    @abstractmethod
    async def append_artifact(self, task_id: str, artifact: Artifact) -> Optional[Task]:
        """Adds an Artifact to the Task."""
        ...

    @abstractmethod
    async def cancel(self, task_id: str) -> Optional[Task]:
        """Moves the Task into the CANCELED state."""
        ...

    @abstractmethod
    async def list_tasks(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> list[Task]:
        """Lists Tasks with cursor-based pagination."""
        ...

    @abstractmethod
    async def find_by_context_id(self, context_id: str) -> Optional[Task]:
        """
        Finds the most recent Task associated with a contextId.
        Used by the input-required pattern (§3.4.3) to resume a suspended task.
        """
        ...

    async def touch(self, task_id: str) -> None:
        """
        Renews updatedAt (and the TTL, for backends that support it) of a
        Task without modifying its state, history, or artifacts.

        Used by the SSE heartbeat (v0.1.4) to keep a Task alive while
        the adapter is blocked in a long-running delegation: without the touch, a
        job longer than the TTL would be removed by cleanup while still running
        and the post-completion tasks/get would return Task not found.

        Default no-op (not abstract) for backward compatibility with any
        external implementations of TaskStoreBase.
        """
        return None

    async def find_previous_by_context_id(
        self, context_id: str, exclude_task_id: str
    ) -> Optional[Task]:
        """
        Returns the most recent Task for contextId EXCLUDING exclude_task_id.

        Used by the memory layer to retrieve the previous conversation's
        history when the new task has already been created (stream path with
        _stls_task_id pre-injected by the middleware).

        Default: calls find_by_context_id and filters it out if it matches exclude_task_id.
        Subclasses can do better (Redis uses stls:ctx:prev: for O(1)).
        """
        task = await self.find_by_context_id(context_id)
        if task and task.taskId != exclude_task_id:
            return task
        return None

    def start_cleanup(self, interval_seconds: int = 120) -> None:
        """Starts the expired-task cleanup loop (no-op for backends with native TTL)."""

    def stop_cleanup(self) -> None:
        """Stops the cleanup loop (no-op for backends with native TTL)."""


# ═══════════════════════════════════════════════════════════════════════════
# TaskStore — in-memory implementation (unchanged, now inherits from Base)
# ═══════════════════════════════════════════════════════════════════════════

class TaskStore(TaskStoreBase):
    """Thread-safe in-memory store for A2A Tasks."""

    def __init__(self, ttl_seconds: int = 900):
        self._tasks: dict[str, Task] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._ttl = ttl_seconds
        self._cleanup_task: Optional[asyncio.Task] = None

    def _get_lock(self) -> asyncio.Lock:
        """Creates the lock lazily on first use, when the event loop is active."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def start_cleanup(self, interval_seconds: int = 120) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(interval_seconds),
            name="task_store_cleanup",
        )
        logger.info(
            f"TaskStore cleanup started — TTL: {self._ttl}s, interval: {interval_seconds}s"
        )

    def stop_cleanup(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("TaskStore cleanup stopped")

    async def _cleanup_loop(self, interval_seconds: int) -> None:
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                removed = await self._cleanup_expired()
                if removed:
                    logger.info(f"TaskStore cleanup: removed {removed} expired tasks")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"TaskStore cleanup error: {e}", exc_info=True)

    async def _cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc)
        expired = []
        async with self._get_lock():
            for task_id, task in self._tasks.items():
                updated_at = datetime.fromisoformat(task.updatedAt)
                if (now - updated_at).total_seconds() > self._ttl:
                    expired.append(task_id)
            for task_id in expired:
                del self._tasks[task_id]
                logger.debug(f"Task {task_id} removed (expired)")
        return len(expired)

    async def create(
        self,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        task = Task()
        if context_id:
            task.contextId = context_id
        if task_id:
            task.taskId = task_id
        async with self._get_lock():
            self._tasks[task.taskId] = task
        return task

    async def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    async def update_status(self, task_id: str, status: TaskState) -> Optional[Task]:
        async with self._get_lock():
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus(state=status)
                task.updatedAt = datetime.now(timezone.utc).isoformat()
        return task

    async def touch(self, task_id: str) -> None:
        """Renews the Task's updatedAt (SSE heartbeat) with no other changes."""
        async with self._get_lock():
            task = self._tasks.get(task_id)
            if task:
                task.updatedAt = datetime.now(timezone.utc).isoformat()

    async def append_message(self, task_id: str, message: Message) -> Optional[Task]:
        async with self._get_lock():
            task = self._tasks.get(task_id)
            if task:
                task.history.append(message)
                task.updatedAt = datetime.now(timezone.utc).isoformat()
        return task

    async def append_artifact(self, task_id: str, artifact: Artifact) -> Optional[Task]:
        async with self._get_lock():
            task = self._tasks.get(task_id)
            if task:
                task.artifacts.append(artifact)
                task.updatedAt = datetime.now(timezone.utc).isoformat()
        return task

    async def cancel(self, task_id: str) -> Optional[Task]:
        return await self.update_status(task_id, TaskState.CANCELED)

    async def list_tasks(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> list[Task]:
        async with self._get_lock():
            tasks = list(self._tasks.values())
        if cursor:
            ids = [t.taskId for t in tasks]
            if cursor in ids:
                tasks = tasks[ids.index(cursor) + 1:]
        return tasks[:limit]

    async def find_by_context_id(self, context_id: str) -> Optional[Task]:
        """Finds the most recent Task for contextId (O(n) — acceptable in-memory)."""
        async with self._get_lock():
            candidates = [
                t for t in self._tasks.values() if t.contextId == context_id
            ]
        if not candidates:
            return None
        # Returns the most recent by updatedAt
        return max(candidates, key=lambda t: t.updatedAt)

    async def find_previous_by_context_id(
        self, context_id: str, exclude_task_id: str
    ) -> Optional[Task]:
        """Returns the most recent Task for contextId excluding exclude_task_id."""
        async with self._get_lock():
            candidates = [
                t for t in self._tasks.values()
                if t.contextId == context_id and t.taskId != exclude_task_id
            ]
        if not candidates:
            return None
        return max(candidates, key=lambda t: t.updatedAt)


# ═══════════════════════════════════════════════════════════════════════════
# RedisTaskStore — optional Redis implementation
# ═══════════════════════════════════════════════════════════════════════════
#
# Optional dependency: pip install redis[asyncio]
#
# Redis keys used:
#   stls:task:{task_id}        → full Task JSON           (with EXPIRE = ttl)
#   stls:ctx:{context_id}      → most recent task_id       (with EXPIRE = ttl)
#   stls:index                 → Sorted Set {task_id: timestamp_score}
#                                 used by list_tasks for O(log n) pagination
#
# The append_message / append_artifact operations use a Lua script
# to guarantee read-modify-write atomicity without an explicit transaction.
# ═══════════════════════════════════════════════════════════════════════════

# KEYS[1] = task_key   KEYS[2] = index_key
# ARGV[1] = message JSON   ARGV[2] = updatedAt ISO   ARGV[3] = ttl (int str)
# ARGV[4] = expiry_score (float str)   ARGV[5] = task_id
_LUA_APPEND_MESSAGE = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local task = cjson.decode(raw)
local msg  = cjson.decode(ARGV[1])
table.insert(task['history'], msg)
task['updatedAt'] = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(task))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[4]), ARGV[5])
return cjson.encode(task)
"""

# KEYS[1] = task_key   KEYS[2] = index_key
# ARGV[1] = artifact JSON   ARGV[2] = updatedAt ISO   ARGV[3] = ttl (int str)
# ARGV[4] = expiry_score (float str)   ARGV[5] = task_id
_LUA_APPEND_ARTIFACT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local task = cjson.decode(raw)
local art  = cjson.decode(ARGV[1])
table.insert(task['artifacts'], art)
task['updatedAt'] = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(task))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[4]), ARGV[5])
return cjson.encode(task)
"""

# KEYS[1] = task_key   KEYS[2] = index_key
# ARGV[1] = status JSON   ARGV[2] = updatedAt ISO   ARGV[3] = ttl (int str)
# ARGV[4] = expiry_score (float str)   ARGV[5] = task_id
_LUA_UPDATE_STATUS = """
local raw = redis.call('GET', KEYS[1])
if not raw then return nil end
local task   = cjson.decode(raw)
local status = cjson.decode(ARGV[1])
task['status']    = status
task['updatedAt'] = ARGV[2]
redis.call('SET', KEYS[1], cjson.encode(task))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[4]), ARGV[5])
return cjson.encode(task)
"""

# Distributed lock release — deletes the key only if the value matches
# the instance_id of whoever acquired the lock (no race condition).
# KEYS[1] = lock_key   ARGV[1] = instance_id
_LUA_RELEASE_LOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class RedisTaskStore(TaskStoreBase):
    """
    TaskStore backed by Redis.

    Configuration via environment variables (or constructor parameters):
        REDIS_URL                default: redis://localhost:6379/0
        REDIS_KEY_PREFIX         default: stls
        TASK_STORE_TTL_SECONDS   default: 900

    Score of the stls:index sorted set = expiry timestamp (time.time() + ttl).
    Guarantees alignment between stls:task:* keys (EXPIRE) and the index (cleanup by score).

    Distributed cleanup:
        Before running ZREMRANGEBYSCORE, the loop acquires a Redis lock
        (SET NX EX) identified by the instance's instance_id. In a
        multi-instance cluster only one agent at a time runs the cleanup.
        Lock key: {prefix}:cleanup:lock
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "stls",
        ttl_seconds: int = 900,
    ):
        self._redis_url = redis_url
        self._prefix = key_prefix
        self._ttl = ttl_seconds
        self._client: Optional[aioredis.Redis] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        # Unique ID for this instance — used as the distributed lock's value
        self._instance_id = str(uuid.uuid4())

        # Lua scripts registered on first use
        self._script_append_msg    = None
        self._script_append_art    = None
        self._script_update_status = None
        self._script_release_lock  = None

    # ── Redis keys ──────────────────────────────────────────────────────

    def _task_key(self, task_id: str) -> str:
        return f"{self._prefix}:task:{task_id}"

    def _ctx_key(self, context_id: str) -> str:
        return f"{self._prefix}:ctx:{context_id}"

    def _index_key(self) -> str:
        return f"{self._prefix}:index"

    def _lock_key(self) -> str:
        """Key of the distributed lock for cleaning up the index."""
        return f"{self._prefix}:cleanup:lock"

    def _expiry_score(self) -> float:
        """Score to use in the sorted set = the task's Unix expiry timestamp."""
        return time.time() + self._ttl

    # ── lazy connection ──────────────────────────────────────────────────

    async def _get_client(self):
        if self._client is None:
            self._client = await aioredis.from_url(
                self._redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            logger.info(f"RedisTaskStore connected to {self._redis_url}")
        return self._client

    # ── Lua script registration ──────────────────────────────────────────

    async def _get_scripts(self):
        """Registers the Lua scripts on Redis (only once per connection)."""
        if self._script_append_msg is None:
            client = await self._get_client()
            self._script_append_msg    = client.register_script(_LUA_APPEND_MESSAGE)
            self._script_append_art    = client.register_script(_LUA_APPEND_ARTIFACT)
            self._script_update_status = client.register_script(_LUA_UPDATE_STATUS)
            self._script_release_lock  = client.register_script(_LUA_RELEASE_LOCK)
        return (
            self._script_append_msg,
            self._script_append_art,
            self._script_update_status,
        )

    # ── distributed lock ─────────────────────────────────────────────────

    async def _acquire_lock(self, client, lock_ttl: int) -> bool:
        """
        Acquires the distributed lock for cleanup.
        Uses SET NX EX — atomic, no race condition.

        Args:
            lock_ttl: Lock TTL in seconds. Must be > the cleanup's duration.

        Returns:
            True if the lock was acquired, False if already held by another instance.
        """
        acquired = await client.set(
            self._lock_key(),
            self._instance_id,
            nx=True,       # SET only if Not eXists
            ex=lock_ttl,   # auto-expire in case of a crash
        )
        return acquired is not None

    async def _release_lock(self, client) -> bool:
        """
        Releases the distributed lock only if it belongs to this instance.
        Uses a Lua script for atomicity (conditional GET + DEL).

        Returns:
            True if the lock was released, False if it wasn't ours.
        """
        if self._script_release_lock is None:
            await self._get_scripts()
        result = await self._script_release_lock(
            keys=[self._lock_key()],
            args=[self._instance_id],
        )
        return bool(result)

    # ── serialization / deserialization ───────────────────────────────

    @staticmethod
    def _serialize(task: Task) -> str:
        return task.model_dump_json(by_alias=True)

    @staticmethod
    def _deserialize(raw: str) -> Task:
        # cjson (Lua) serializes empty lists as {} instead of [].
        # We normalize before passing to Pydantic to avoid
        # "Input should be a valid array [type=list_type]".
        import json as _json
        data = _json.loads(raw)
        for field in ("history", "artifacts"):
            if isinstance(data.get(field), dict):
                data[field] = []
        # Also normalizes the parts inside history and artifacts
        for msg in data.get("history", []):
            if isinstance(msg.get("parts"), dict):
                msg["parts"] = []
        for art in data.get("artifacts", []):
            if isinstance(art.get("parts"), dict):
                art["parts"] = []
        return Task.model_validate(_json.loads(_json.dumps(data)))

    # ── helper: saves the task and updates the indexes ──────────────────────────────

    async def _save(self, client, task: Task) -> None:
        """
        Persists the task and updates the two secondary indexes in an atomic pipeline.
        Score = expiry timestamp: enables filtering and cleanup by score on stls:index.
        ZREMRANGEBYSCORE removes expired entries on every write (self-cleaning).

        Before overwriting stls:ctx:{contextId}, reads the current pointer
        and saves it to stls:ctx:prev:{contextId} — lets find_previous_by_context_id
        retrieve the previous conversation's history in O(1) without a scan.
        """
        raw = self._serialize(task)
        expiry_score = self._expiry_score()
        ctx_key = self._ctx_key(task.contextId)

        # Reads the current pointer before overwriting it (single operation, outside the pipeline)
        old_task_id = await client.get(ctx_key)

        pipe = client.pipeline(transaction=True)
        pipe.set(self._task_key(task.taskId), raw, ex=self._ttl)
        # If a different previous task exists, archive it as "prev"
        if old_task_id and old_task_id != task.taskId:
            prev_key = f"{self._prefix}:ctx:prev:{task.contextId}"
            pipe.set(prev_key, old_task_id, ex=self._ttl)
        pipe.set(ctx_key, task.taskId, ex=self._ttl)
        pipe.zadd(self._index_key(), {task.taskId: expiry_score})
        # Self-cleaning: removes already-expired entries in O(log n)
        pipe.zremrangebyscore(self._index_key(), "-inf", time.time())
        await pipe.execute()

    # ── TaskStoreBase interface ───────────────────────────────────────────

    async def create(
        self,
        context_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Task:
        task = Task()
        if context_id:
            task.contextId = context_id
        if task_id:
            task.taskId = task_id
        client = await self._get_client()
        await self._save(client, task)
        logger.debug(f"RedisTaskStore: Task {task.taskId} created")
        return task

    async def get(self, task_id: str) -> Optional[Task]:
        client = await self._get_client()
        raw = await client.get(self._task_key(task_id))
        if raw is None:
            return None
        return self._deserialize(raw)

    async def update_status(self, task_id: str, status: TaskState) -> Optional[Task]:
        _, _, script_status = await self._get_scripts()
        new_status = TaskStatus(state=status)
        now = datetime.now(timezone.utc).isoformat()
        expiry_score = self._expiry_score()
        result = await script_status(
            keys=[self._task_key(task_id), self._index_key()],
            args=[
                new_status.model_dump_json(),
                now,
                str(self._ttl),
                str(expiry_score),
                task_id,
            ],
        )
        if result is None:
            return None
        task = self._deserialize(result)
        logger.debug(f"RedisTaskStore: Task {task_id} → {status.value}")
        return task

    async def touch(self, task_id: str) -> None:
        """
        Renews the Task's TTL (SSE heartbeat) without rewriting the payload.

        Deliberately does NOT do get-modify-save: rewriting the whole task
        would overwrite any concurrent updates applied via Lua scripts
        between the read and the write. It only renews EXPIRE on the key
        and the expiry score in the index — on Redis expiry is governed
        by the native TTL, not by updatedAt, so this is sufficient and race-free.
        """
        client = await self._get_client()
        # EXPIRE returns 0 if the key no longer exists: in that case
        # we don't re-insert the score into the index (would avoid orphan entries).
        exists = await client.expire(self._task_key(task_id), self._ttl)
        if exists:
            await client.zadd(self._index_key(), {task_id: self._expiry_score()})

    async def append_message(self, task_id: str, message: Message) -> Optional[Task]:
        script_msg, _, _ = await self._get_scripts()
        now = datetime.now(timezone.utc).isoformat()
        expiry_score = self._expiry_score()
        result = await script_msg(
            keys=[self._task_key(task_id), self._index_key()],
            args=[
                message.model_dump_json(),
                now,
                str(self._ttl),
                str(expiry_score),
                task_id,
            ],
        )
        if result is None:
            return None
        return self._deserialize(result)

    async def append_artifact(self, task_id: str, artifact: Artifact) -> Optional[Task]:
        _, script_art, _ = await self._get_scripts()
        now = datetime.now(timezone.utc).isoformat()
        expiry_score = self._expiry_score()
        result = await script_art(
            keys=[self._task_key(task_id), self._index_key()],
            args=[
                artifact.model_dump_json(),
                now,
                str(self._ttl),
                str(expiry_score),
                task_id,
            ],
        )
        if result is None:
            return None
        return self._deserialize(result)

    async def cancel(self, task_id: str) -> Optional[Task]:
        return await self.update_status(task_id, TaskState.CANCELED)

    async def list_tasks(
        self, limit: int = 50, cursor: Optional[str] = None
    ) -> list[Task]:
        """
        Pagination via a Sorted Set filtered by score >= now (non-expired tasks only).
        cursor = last taskId of the previous page.
        """
        client = await self._get_client()
        now = time.time()

        if cursor:
            cursor_score = await client.zscore(self._index_key(), cursor)
            if cursor_score is None:
                return []
            raw_ids = await client.zrangebyscore(
                self._index_key(),
                min=f"({cursor_score}",
                max="+inf",
                start=0,
                num=limit,
            )
        else:
            # Only entries with score >= now (not expired)
            raw_ids = await client.zrangebyscore(
                self._index_key(),
                min=now,
                max="+inf",
                start=0,
                num=limit,
            )

        if not raw_ids:
            return []

        pipe = client.pipeline()
        for tid in raw_ids:
            pipe.get(self._task_key(tid))
        raws = await pipe.execute()

        tasks = []
        for raw in raws:
            if raw:
                try:
                    tasks.append(self._deserialize(raw))
                except Exception as e:
                    logger.warning(f"RedisTaskStore: deserialization failed: {e}")
        return tasks

    async def find_by_context_id(self, context_id: str) -> Optional[Task]:
        """
        O(1) lookup via the secondary index stls:ctx:{context_id}.
        Used by the input-required pattern (§3.4.3).
        """
        client = await self._get_client()
        task_id = await client.get(self._ctx_key(context_id))
        if not task_id:
            return None
        return await self.get(task_id)

    async def find_previous_by_context_id(
        self, context_id: str, exclude_task_id: str
    ) -> Optional[Task]:
        """
        O(1) lookup via the index stls:ctx:prev:{context_id}.
        Returns the previous task in the context (saved by _save before overwriting).
        """
        client = await self._get_client()
        prev_key = f"{self._prefix}:ctx:prev:{context_id}"
        task_id = await client.get(prev_key)
        if not task_id or task_id == exclude_task_id:
            return None
        return await self.get(task_id)

    # ── index cleanup ─────────────────────────────────────────────────────

    async def cleanup_index(self) -> int:
        """
        Removes expired entries (score <= now) from stls:index.
        O(log n + k) on Redis. Safe to call at any time.

        Returns:
            Number of entries removed.
        """
        client = await self._get_client()
        removed = await client.zremrangebyscore(self._index_key(), "-inf", time.time())
        logger.info(f"RedisTaskStore.cleanup_index: {removed} expired entries removed")
        return removed

    # ── lifecycle: distributed-lock cleanup loop ──────────────────────────

    def start_cleanup(self, interval_seconds: int = 120) -> None:
        """
        Starts the periodic loop that removes expired entries from stls:index.
        Before each run, acquires a distributed Redis lock:
        in a multi-instance cluster only one agent at a time runs the cleanup.
        """
        if self._cleanup_task and not self._cleanup_task.done():
            return
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(interval_seconds),
            name="redis_index_cleanup",
        )
        logger.info(
            f"RedisTaskStore: cleanup loop started — "
            f"instance={self._instance_id[:8]}, "
            f"task TTL={self._ttl}s, interval={interval_seconds}s"
        )

    def stop_cleanup(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info(
                f"RedisTaskStore: cleanup loop stopped "
                f"(instance={self._instance_id[:8]})"
            )

    async def _cleanup_loop(self, interval_seconds: int) -> None:
        # Lock TTL = interval + safety buffer
        # Prevents orphan locks in case of a crash during cleanup
        lock_ttl = interval_seconds + 30
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                client = await self._get_client()

                acquired = await self._acquire_lock(client, lock_ttl)
                if not acquired:
                    logger.debug(
                        f"RedisTaskStore cleanup: lock already held by another instance, skipping "
                        f"(instance={self._instance_id[:8]})"
                    )
                    continue

                try:
                    removed = await self.cleanup_index()
                    if removed:
                        logger.info(
                            f"RedisTaskStore cleanup: {removed} entries removed from stls:index "
                            f"(instance={self._instance_id[:8]})"
                        )
                finally:
                    await self._release_lock(client)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"RedisTaskStore cleanup error (instance={self._instance_id[:8]}): {e}",
                    exc_info=True,
                )

    async def close(self) -> None:
        """Closes the Redis connection (call on app shutdown)."""
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("RedisTaskStore: connection closed")


# ═══════════════════════════════════════════════════════════════════════════
# Factory — backend selection via env var
# ═══════════════════════════════════════════════════════════════════════════
#
# Environment variables:
#   TASK_STORE_BACKEND      "memory" (default) | "redis"
#   TASK_STORE_TTL_SECONDS  default: 900
#   REDIS_URL               default: redis://localhost:6379/0
#   REDIS_KEY_PREFIX        default: stls
#
# ═══════════════════════════════════════════════════════════════════════════

def create_task_store() -> TaskStoreBase:
    """
    Factory that instantiates the correct TaskStore based on TASK_STORE_BACKEND.

    Typical use in stls_a2a_integration.py:
        from stls_a2a_task import task_store
    The singleton is already created here — no other module should call the factory.
    """
    # Guarantees the log is visible even when called before
    # uvicorn/FastAPI configures logging (e.g. at import time).
    if not logging.root.handlers:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s-%(levelname)-7s -%(name)s-%(message)s",
        )

    backend = os.getenv("TASK_STORE_BACKEND", "memory").lower().strip()
    ttl = int(os.getenv("TASK_STORE_TTL_SECONDS", "900"))

    if backend == "redis":
        redis_url    = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        key_prefix   = os.getenv("REDIS_KEY_PREFIX", "stls")
        store = RedisTaskStore(
            redis_url=redis_url,
            key_prefix=key_prefix,
            ttl_seconds=ttl,
        )
        logger.info(
            f"TaskStore backend: Redis — url={redis_url}, "
            f"prefix={key_prefix}, ttl={ttl}s"
        )
    else:
        store = TaskStore(ttl_seconds=ttl)
        logger.info(f"TaskStore backend: in-memory — ttl={ttl}s")

    return store


# Singleton usato dal resto del progetto
task_store: TaskStoreBase = create_task_store()