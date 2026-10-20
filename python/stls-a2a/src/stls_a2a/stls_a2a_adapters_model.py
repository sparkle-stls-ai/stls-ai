from abc import ABC, abstractmethod
import asyncio
import os
from pydantic import BaseModel, ConfigDict
from typing import Dict, Any, Optional, AsyncIterator
from .stls_a2a_task import task_store, TaskState, TaskStatus, Message, Artifact, TextPart, FilePart, DataPart, Part


import logging
logger = logging.getLogger(__name__)

# ── Standard JSON-RPC error codes + A2A extensions ──────────────────────
# Standard JSON-RPC 2.0 (§9.5 A2A spec / RFC 8259)
ERR_PARSE_ERROR         = -32700   # body isn't valid JSON
ERR_INVALID_REQUEST     = -32600   # invalid JSON-RPC structure (missing method, etc.)
ERR_METHOD_NOT_FOUND    = -32601   # method not registered
ERR_INVALID_PARAMS      = -32602   # required parameters missing or malformed
ERR_INTERNAL_ERROR      = -32603   # internal server error

# Custom A2A extensions (range -32000 / -32099 reserved for apps)
ERR_TASK_NOT_FOUND               = -32001
ERR_TASK_NOT_CANCELABLE          = -32002   # task in a terminal state, not cancelable
ERR_PUSH_NOTIFICATION_NOT_SUPPORTED = -32003  # push notifications not implemented
ERR_SESSION_EXPIRED              = -32004
"""
Unknown or expired STLS session on the server side.

Distinct from ERR_INTERNAL_ERROR: tells the client that the correct
action is to redo the handshake and retry, not a permanent application
error. For providers without deterministic key regeneration (PQC: the
session_key is born from an ephemeral KEM and isn't derivable from the
session_id alone) it's the ONLY available signal — a cache miss is
always irreversible, never a transient error worth retrying without a
handshake.
"""

# Terminal states: a task in one of these states can't be canceled
_TERMINAL_STATES = {TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELED}


def _task_dict(task) -> dict:
    """
    Serializes a Task including both "id" (required by the a2a-python SDK)
    and "taskId" (used by internal STLS code and the tests).
    Both fields point to the same UUID value.
    """
    d = task.model_dump(by_alias=True)   # emits "id" (A2A spec alias)
    d["taskId"] = d.get("id")             # backward-compat for STLS code
    # Normalizes status: ensures it's always {"state": "..."}
    status = d.get("status")
    if isinstance(status, str):
        d["status"] = {"state": status}
    return d


# Maximum number of conversation turns to retrieve for memory.
# Each turn = 1 previous Task (user + agent message).
# Configurable via env: MEMORY_MAX_TURNS (default 3).
_MEMORY_MAX_TURNS: int = max(1, int(os.getenv("MEMORY_MAX_TURNS", "3")))


def _extract_history(task) -> list[dict]:
    """
    Serializes a single Task's history as a list of A2A-friendly messages.

    Output format:
        [{"role": "user"|"agent", "parts": [{"type": "text", "text": "..."}]}, ...]

    Returns an empty list if task is None or history is empty.
    """
    if not task:
        return []
    messages: list[dict] = []
    for msg in (task.history or []):
        parts: list[dict] = []
        for p in msg.parts:
            if hasattr(p, "model_dump"):
                parts.append(p.model_dump(by_alias=True))
            elif isinstance(p, dict):
                parts.append(p)
            else:
                parts.append({"type": "text", "text": str(p), "mimeType": "text/plain"})
        messages.append({"role": msg.role, "parts": parts})
    return messages


async def _collect_history(
    context_id: str,
    exclude_task_id: str,
    max_turns: int = _MEMORY_MAX_TURNS,
) -> list[dict]:
    """
    Walks the ctx:prev chain collecting the history of the last `max_turns`
    tasks in the context, excluding the current task.

    Each "turn" corresponds to a previous Task and typically contains
    2 messages: the user message and the agent's response.

    Example with max_turns=3 and 5 total turns:
        Task5 (current, excluded)
        Task4 → included (turn N-1)
        Task3 → included (turn N-2)
        Task2 → included (turn N-3)
        Task1 → ignored (beyond max_turns)

    Returns:
        List of messages in chronological order (oldest to most recent),
        ready to be injected into request.params["history"].
    """
    collected: list[list[dict]] = []   # list of turns, most recent to oldest
    current_exclude = exclude_task_id

    for _ in range(max_turns):
        prev = await task_store.find_previous_by_context_id(
            context_id, exclude_task_id=current_exclude
        )
        if not prev:
            break
        turn_msgs = _extract_history(prev)
        if turn_msgs:
            collected.append(turn_msgs)
        current_exclude = prev.taskId

    # Reverses: turns were collected from most recent to oldest,
    # we put them back in chronological order for the agent's context.
    collected.reverse()

    # Flattens the list of turns into a list of messages
    messages: list[dict] = []
    for turn in collected:
        messages.extend(turn)

    logger.debug(
        "_collect_history: contextId=%s turns=%d msgs=%d max_turns=%d",
        context_id, len(collected), len(messages), max_turns,
    )
    return messages


# A2A methods that use the SSE channel (must not arrive on the sync endpoint)
_SSE_METHODS = {"messages/sendStream", "tasks/subscribe"}
# Standard A2A methods handled by handle_request (all others → legacy invoke)
_A2A_METHODS = {
    "messages/send", "messages/sendStream",
    "tasks/get", "tasks/cancel", "tasks/list",
    "tasks/pushNotificationConfig/set",
    "tasks/pushNotificationConfig/get",
}

class AgentRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    method: str
    params: Dict[str, Any]
    id: Optional[Any] = None
    context: Optional[Dict[str, Any]] = None

class AgentResponse(BaseModel):
    model_config = ConfigDict(exclude_none=True,populate_by_name=True)   
    result: Optional[Any] = None
    error: Optional[Dict[str, Any]] = None
    id: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None    


class AgentAdapter(ABC):
    """
    Common interface for AI/Agent framework adapters

    Every framework implements this interface to enable uniform
    communication via A2A Secure Transport.
    """

    @abstractmethod
    async def invoke(self, request: AgentRequest) -> AgentResponse:
        """
        Invokes the agent with a standardized request

        Args:
            request: Request with method, parameters, and context

        Returns:
            The agent's response
        """
        pass


    @abstractmethod
    def get_capabilities(self) -> Dict[str, Any]:
        """
        Returns the agent's capabilities

        Returns:
            Dict with information on supported methods, tools, etc.
        """
        pass

    async def _invoke_stream(self, request: AgentRequest) -> AsyncIterator[Dict[str, Any]]:
        """
        Async generator — yields raw chunks from the underlying adapter.

        Each chunk has the form:
            {"parts": list[dict], "append": bool, "last_chunk": bool}

        Default: falls back to invoke() → single final chunk.
        Overridden in GoogleADKAdapter for true native chunk-by-chunk streaming.
        LangChain and SemanticKernel use this fallback.
        """
        response = await self.invoke(request)
        parts: list = []
        if response.result:
            r = response.result
            if isinstance(r, dict):
                if r.get("parts"):
                    # typed A2A parts (messages/send with _from_genai_parts)
                    parts = [
                        p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                        for p in r["parts"]
                    ]
                else:
                    # flat text from legacy generate/invoke/chat
                    text = r.get("text") or r.get("output") or str(r)
                    parts = [{"type": "text", "text": text, "mimeType": "text/plain"}]
            else:
                parts = [{"type": "text", "text": str(r), "mimeType": "text/plain"}]
        else:
            parts = [{"type": "text", "text": "stream not implemented", "mimeType": "text/plain"}]
        yield {"parts": parts, "append": False, "last_chunk": True}
 
    async def stream(self, params: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """
        SSE entry point — manages Task lifecycle and delegates chunks to _invoke_stream().
        Called by process_secure_stream() in the middleware.

        Every emitted chunk includes taskId and contextId so that
        the middleware can build SSE events without knowing about the Task.
        """
        request = AgentRequest(method="messages/sendStream", params=params, id=None)
        async for chunk in self._handle_messages_send_stream(request, rpc_id=None):
            yield chunk
 
    async def _handle_messages_send_stream(
        self, request: AgentRequest, rpc_id
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Streaming version of _handle_messages_send():
        - If the middleware pre-created the Task (via _stls_task_id in params), retrieves it
          from the TaskStore to ensure the SSE events' task_id matches the one
          the client can call tasks/get on.
        - If _stls_task_id is absent (direct call), creates a new Task.
        - Sets WORKING, yields chunks from _invoke_stream() enriched with taskId/contextId,
          saves the artifact, and sets COMPLETED/FAILED at the end of the stream.
        """
        # ── Extract the task_id pre-created by the middleware, if present ─────────
        pre_created_task_id = request.params.get("_stls_task_id")

        # Remove the internal param before passing params to the underlying adapter
        clean_params = {k: v for k, v in request.params.items() if k != "_stls_task_id"}

        # Bug fix: checks all 3 possible sources of contextId
        # (top-level params, inside message object, STLS context) — same order
        # as _handle_messages_send for consistency.
        context_id = (
            clean_params.get("contextId")
            or clean_params.get("message", {}).get("contextId")
            or (request.context.get("contextId") if request.context else None)
        )
        msg_parts = clean_params.get("message", {}).get("parts", [{}])
        parts = self._build_parts(msg_parts)

        if pre_created_task_id:
            # Task already in the TaskStore — retrieve it and update its state
            task = await task_store.get(pre_created_task_id)
            if task is None:
                # Safety fallback: the task has expired or wasn't found
                logger.warning(
                    "Stream: pre-created task %s not found in TaskStore, creating new",
                    pre_created_task_id,
                )
                task = await task_store.create(context_id=context_id)
        else:
            # Direct call without middleware: creates a new Task
            task = await task_store.create(context_id=context_id)

        # Bug fix: uses task.contextId (correctly set by the middleware
        # or by create()) as the authoritative source — context_id extracted
        # from params could be None if the middleware used different logic.
        effective_context_id = task.contextId or context_id

        # ── Context memory (stream path) ──────────────────────────────────────
        # Walks the ctx:prev chain to collect the last MEMORY_MAX_TURNS
        # conversation turns, excluding the current task.
        history_messages: list[dict] = []
        if effective_context_id:
            history_messages = await _collect_history(
                context_id=effective_context_id,
                exclude_task_id=task.taskId,
            )

        # clean_request built AFTER the history lookup,
        # so params["history"] is available to all adapters.

        # ── User identity from message.metadata (stream path) ───────────────────
        # Same logic as _handle_messages_send: extracts user_id and profile from
        # the message metadata and propagates them into the context for all adapters.
        # Extracts user_id and profile with decreasing priority:
        #   1. message.metadata     — direct STLS client (highest priority)
        #   2. params.metadata      — RemoteA2aAgent's a2a_request_meta_provider (ADK)
        #   3. _stls_http_context   — X-STLS-* headers injected by the middleware
        #   4. request.context      — final fallback
        _msg_meta: Dict[str, Any]    = clean_params.get("message", {}).get("metadata") or {}
        _params_meta: Dict[str, Any] = clean_params.get("metadata") or {}
        _http_ctx: Dict[str, Any]    = clean_params.pop("_stls_http_context", None) or {}
        _meta_uid: Optional[str]  = (
            _msg_meta.get("user_id")
            or _params_meta.get("user_id")
            or _http_ctx.get("user_id")
        )
        _meta_prof = (
            _msg_meta.get("profile")      if _msg_meta.get("profile")    is not None else
            _params_meta.get("profile")   if _params_meta.get("profile") is not None else
            _http_ctx.get("profile")
        )
        _ctx: Dict[str, Any]      = dict(request.context or {})
        if _meta_uid:
            _ctx["user_id"] = _meta_uid
        if _meta_prof is not None:
            _ctx["profile"] = _meta_prof

        clean_request = AgentRequest(
            method=request.method,
            params={**clean_params, "history": history_messages},
            id=request.id,
            context=_ctx,
        )

        await task_store.append_message(task.taskId, Message(role="user", parts=parts))
        await task_store.update_status(task.taskId, TaskState.WORKING)
        logger.debug(
            f"Stream task {task.taskId} {'reused' if pre_created_task_id else 'created'}, "
            f"context_id={effective_context_id}, history_msgs={len(history_messages)}"
        )
 
        collected_parts: list = []   # accumulates all Parts from all chunks
        try:
            async for chunk in self._invoke_stream(clean_request):
                # chunk["parts"] is an A2A list[dict] (TextPart/FilePart/DataPart)
                # produced by _from_genai_parts or by the base fallback.
                chunk_parts = [
                    p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                    for p in chunk.get("parts", [])
                ]
                if not chunk_parts:
                    continue  # internal ADK event with no useful content

                collected_parts.extend(chunk_parts)

                # Enriches the chunk with taskId/contextId for the SSE middleware
                yield {
                    "parts":      chunk_parts,
                    "append":     chunk.get("append", True),
                    "last_chunk": chunk.get("last_chunk", False),
                    "taskId":     task.taskId,
                    "contextId":  task.contextId,
                }

            # End of stream: saves the artifact with all collected Parts
            # Splits Parts by type: only TextPart goes into the Message/Artifact
            # (FilePart and DataPart have no text field to concatenate)
            typed_parts: list[Part] = []
            for p in collected_parts:
                if isinstance(p, dict):
                    t = p.get("type")
                    if t == "text":
                        typed_parts.append(TextPart(text=p.get("text", ""), mimeType=p.get("mimeType", "text/plain")))
                    elif t == "file":
                        typed_parts.append(FilePart(file=p.get("file", {})))
                    elif t == "data":
                        typed_parts.append(DataPart(data=p.get("data", {}), mimeType=p.get("mimeType", "application/json")))
                else:
                    typed_parts.append(p)  # already a typed Part

            if not typed_parts:
                typed_parts = [TextPart(text="", mimeType="text/plain")]

            # Consolidates consecutive TextParts into a single string.
            # Streaming produces N token-level TextParts (" The", " price", …):
            # saving them separately would cause a \n join in extract_a2a_text.
            consolidated: list[Part] = []
            text_buf: list[str] = []
            for p in typed_parts:
                if isinstance(p, TextPart):
                    text_buf.append(p.text)
                else:
                    # flushes the text buffer before the non-text part
                    if text_buf:
                        consolidated.append(TextPart(text="".join(text_buf), mimeType="text/plain"))
                        text_buf = []
                    consolidated.append(p)
            if text_buf:
                consolidated.append(TextPart(text="".join(text_buf), mimeType="text/plain"))
            typed_parts = consolidated

            await task_store.append_message(task.taskId, Message(role="agent", parts=typed_parts))
            await task_store.append_artifact(task.taskId, Artifact(name="response", parts=typed_parts))
            await task_store.update_status(task.taskId, TaskState.COMPLETED)
            logger.debug(f"Stream task {task.taskId} completed")
 
        except Exception:
            await task_store.update_status(task.taskId, TaskState.FAILED)
            logger.exception(f"Error streaming task {task.taskId}")
            raise  # re-raises → process_secure_stream() emits a failed event

    def _error(self, rpc_id: Any, code: int, message: str) -> AgentResponse:
        """Helper for building a JSON-RPC error AgentResponse."""
        return AgentResponse(error={"code": code, "message": message}, id=rpc_id)

    async def handle_request(self, request: AgentRequest) -> AgentResponse:
        logger.debug(f"handle_request from request:{request}")
        try:
            method  = request.method
            params  = request.params or {}
            rpc_id  = request.id
            logger.debug(f"method:{method}")

            # ── -32600: method is required ──────────────────────────────────
            if not method:
                return self._error(rpc_id, ERR_INVALID_REQUEST,
                                   "Invalid request: 'method' field is required")

            # ── messages/send ────────────────────────────────────────────────
            if method in ("messages/send","message/send"):
                return await self._handle_messages_send(request, rpc_id)

            # ── messages/sendStream (SSE) ────────────────────────────────────
            elif method in ("messages/sendStream", "message/sendStream"):
                return self._error(rpc_id, ERR_METHOD_NOT_FOUND,
                                   "messages/sendStream requires SSE endpoint: "
                                   "POST /secure-a2a/stream")

            # ── tasks/get ────────────────────────────────────────────────────
            elif method in ("tasks/get", "task/get"):
                task_id    = params.get("taskId")
                context_id = params.get("contextId")

                if not task_id:
                    return self._error(rpc_id, ERR_INVALID_PARAMS,
                                       "tasks/get: 'taskId' param is required")

                task = await task_store.get(task_id)
                if not task:
                    return self._error(rpc_id, ERR_TASK_NOT_FOUND, "Task not found")

                # §3.4.1 MUST: contextId/taskId mismatch
                if context_id and task.contextId != context_id:
                    logger.warning(
                        "tasks/get mismatch: taskId=%s belongs to contextId=%s, "
                        "requested contextId=%s", task_id, task.contextId, context_id
                    )
                    return self._error(rpc_id, ERR_TASK_NOT_FOUND,
                                       "Task not found: taskId/contextId mismatch")

                return AgentResponse(result=_task_dict(task), id=rpc_id)

            # ── tasks/cancel ─────────────────────────────────────────────────
            elif method in ("tasks/cancel", "task/cancel"):
                task_id    = params.get("taskId")
                context_id = params.get("contextId")

                if not task_id:
                    return self._error(rpc_id, ERR_INVALID_PARAMS,
                                       "tasks/cancel: 'taskId' param is required")

                task = await task_store.get(task_id)
                if not task:
                    return self._error(rpc_id, ERR_TASK_NOT_FOUND, "Task not found")

                # §3.4.1 MUST: contextId/taskId mismatch
                if context_id and task.contextId != context_id:
                    logger.warning(
                        "tasks/cancel mismatch: taskId=%s belongs to contextId=%s, "
                        "requested contextId=%s", task_id, task.contextId, context_id
                    )
                    return self._error(rpc_id, ERR_TASK_NOT_FOUND,
                                       "Task not found: taskId/contextId mismatch")

                # -32002: task already in a terminal state, not cancelable
                if task.status.state in _TERMINAL_STATES:
                    logger.debug(
                        "tasks/cancel rejected: task %s is in terminal state %s",
                        task_id, task.status.state
                    )
                    return self._error(rpc_id, ERR_TASK_NOT_CANCELABLE,
                                       f"Task cannot be canceled: current status is '{task.status.state}'")

                task = await task_store.cancel(task_id)
                return AgentResponse(result=_task_dict(task), id=rpc_id)

            # ── tasks/list ───────────────────────────────────────────────────
            elif method in ("tasks/list", "task/list"):
                tasks = await task_store.list_tasks(
                    limit=params.get("limit", 50),
                    cursor=params.get("cursor"),
                )
                return AgentResponse(
                    result=[_task_dict(t) for t in tasks],
                    id=rpc_id,
                )

            # ── push notification (not implemented) ─────────────────────────
            elif method in ("tasks/pushNotificationConfig/set", "task/pushNotificationConfig/set",
                            "tasks/pushNotificationConfig/get", "task/pushNotificationConfig/get"):
                return self._error(rpc_id, ERR_PUSH_NOTIFICATION_NOT_SUPPORTED,
                                   "Push notifications are not supported by this agent")

            # ── tasks/subscribe (SSE) ─────────────────────────────────────────
            elif method in ("tasks/subscribe", "task/subscribe"):
                return self._error(rpc_id, ERR_METHOD_NOT_FOUND,
                                   "tasks/subscribe requires SSE endpoint: "
                                   "POST /secure-a2a/stream")

            # ── Legacy proprietary methods (calculate, generate, invoke, chat, …) ──
            # All standard A2A methods are handled above.
            # Any other method is delegated to the adapter as a proprietary
            # call — it's the adapter's responsibility to return an
            # appropriate error if the method isn't supported.
            else:
                return await self.invoke(request)

        except Exception as e:
            return AgentResponse(
                error={"code": ERR_INTERNAL_ERROR, "message": str(e)},
                id=request.id,
            )

    def _build_parts(self, raw_parts: list) -> list[Part]:
        parts: list[Part] = []
        for part in raw_parts:
            if not isinstance(part, dict):
                continue
            # Normalizes "kind" → "type" without mutating the original dict.
            # Some libraries (Google ADK RemoteA2aAgent, a2a-python SDK) send
            # {"kind": "text", ...} instead of {"type": "text", ...}.
            # Others send both fields — in that case "type" takes priority.
            # We always work on a local copy so as not to corrupt the shared
            # object received from the adapter (e.g. a dict reused across calls).
            part_type = part.get("type") or part.get("kind")
            if not part_type:
                # Skips elements that have neither "type" nor "kind"
                # (e.g. empty dict {} used as a default when params.message.parts is absent)
                continue
            if part_type == "text":
                parts.append(TextPart(
                    text=part.get("text", ""),
                    mimeType=part.get("mimeType", "text/plain"),
                ))
            elif part_type == "file":
                # Normalizes the "file" sub-object: the SDK sends "bytes" instead of "data"
                # (FileInline.data = base64 string; the SDK calls the same field "bytes").
                # Copies the sub-dict before modifying it to avoid in-place mutation.
                file_obj = dict(part.get("file", {}))
                if "bytes" in file_obj and "data" not in file_obj:
                    file_obj["data"] = file_obj.pop("bytes")
                parts.append(FilePart(type="file", file=file_obj))
            elif part_type == "data":
                parts.append(DataPart(**{
                    k: v for k, v in part.items()
                    if k not in ("kind", "type")
                }))
        return parts

    async def _run_in_background(self, task_id: str, request: AgentRequest) -> None:
        """
        Runs the adapter in the background after messages/send has already
        responded with the task in WORKING state.

        Called via asyncio.create_task() — doesn't block the caller.
        On completion, sets COMPLETED (with history and artifact) or FAILED.
        This lets tasks/get and tasks/subscribe observe the progress.
        """
        logger.debug(f"Background task {task_id} started")
        try:
            agent_response = await self.invoke(request)
            if agent_response.error:
                logger.warning(
                    f"Background task {task_id}: adapter returned error "
                    f"{agent_response.error}, setting FAILED"
                )
                await task_store.update_status(task_id, TaskState.FAILED)
                return

            # Normalizes the result into A2A format {parts: [...]} before accessing "parts".
            # Some adapters (custom, legacy LangChain) return flat text, a dict without
            # "parts", or None — accessing result["parts"] without a guard causes a KeyError and
            # leaves the task in WORKING state indefinitely until the TTL.
            from .stls_a2a_custom_adapter import _normalize_result
            normalized = _normalize_result(agent_response.result)
            result_parts = normalized.get("parts", [])

            await task_store.append_message(
                task_id,
                Message(role="agent", parts=result_parts),
            )
            await task_store.append_artifact(
                task_id,
                Artifact(name="response", parts=result_parts),
            )
            await task_store.update_status(task_id, TaskState.COMPLETED)
            logger.debug(f"Background task {task_id} completed")

        except Exception:
            await task_store.update_status(task_id, TaskState.FAILED)
            logger.exception(f"Background task {task_id} failed with exception")

    async def _handle_messages_send(self, request: AgentRequest, rpc_id) -> AgentResponse:
        context_id = (
            request.params.get("contextId")                           # 1. standard A2A
            or request.params.get("message", {}).get("contextId")     # 2. inside message
            or (request.context.get("contextId") if request.context else None)  # 3. STLS fallback
        )
        logger.debug(f"Handling messages/send with context_id: {context_id}")
        msg_parts = request.params.get("message", {}).get("parts", [{}])

        # ── Idempotency §3.3.1 ───────────────────────────────────────────────
        client_task_id: Optional[str] = request.params.get("taskId")
        if client_task_id:
            existing = await task_store.get(client_task_id)
            if existing:
                logger.debug(
                    f"Idempotent hit: taskId={client_task_id} already in the TaskStore "
                    f"(status={existing.status}), returning directly without reprocessing"
                )
                return AgentResponse(result=_task_dict(existing), id=rpc_id)

        # ── Context memory ───────────────────────────────────────────────────
        # Retrieves history BEFORE create(): at this point
        # find_by_context_id still points to the last completed task of the
        # context, not the one we're about to create.
        history_messages: list[dict] = []
        if context_id:
            # find_by_context_id returns the context's most recent task
            # (turn N-1, since the new task hasn't been created yet).
            # _collect_history walks the ctx:prev chain to collect previous
            # turns (N-2, N-3, ...) up to MEMORY_MAX_TURNS-1 steps,
            # then we append N-1 at the end to get the correct chronological order.
            prev_task = await task_store.find_by_context_id(context_id)
            if prev_task:
                older = await _collect_history(
                    context_id=context_id,
                    exclude_task_id=prev_task.taskId,
                    max_turns=max(0, _MEMORY_MAX_TURNS - 1),
                )
                history_messages = older + _extract_history(prev_task)

        # Creates the task (with the client-assigned taskId if present, otherwise a generated UUID)
        task = await task_store.create(context_id=context_id, task_id=client_task_id)
        logger.debug(f"Created task with ID: {task.taskId} and context_id: {context_id}")

        parts = self._build_parts(msg_parts)
        await task_store.append_message(task.taskId, Message(role="user", parts=parts))
        await task_store.update_status(task.taskId, TaskState.WORKING)

        # Enriches the request with the history before passing it to the adapter.
        # Each adapter reads request.params["history"] to populate its own
        # native mechanism: ChatHistory (SK), messages[] (LC/LangGraph), session (ADK).

        # ── User identity from message.metadata ─────────────────────────────────
        # Extracts user_id and profile from the A2A message's application metadata
        # and propagates them into the context, making them available to all adapters.
        #
        # message.metadata.user_id identifies the end user (e.g. "mario.rossi");
        # it takes priority over any generic user_id already in the context (e.g. set
        # by the auth middleware with the A2A service account, not the end user).
        #
        # message.metadata.profile (e.g. {"role": "agent", "tenant": "acme"})
        # gives adapters the tenant/role context to customize
        # behavior:
        #   - ADK  → ends up in session.state, accessible to tools via tool_context.state
        #   - LC   → propagated as config["configurable"], standard LangGraph per-request
        #   - SK   → injected as an initial system message in ChatHistory
        # Extracts user_id and profile with decreasing priority:
        #   1. message.metadata  — direct STLS client (highest priority)
        #   2. params.metadata   — RemoteA2aAgent's a2a_request_meta_provider (ADK)
        #   3. request.context   — propagated by the middleware (lowest priority)
        _msg_meta: Dict[str, Any]    = request.params.get("message", {}).get("metadata") or {}
        _params_meta: Dict[str, Any] = request.params.get("metadata") or {}
        _meta_uid: Optional[str]  = (
            _msg_meta.get("user_id")
            or _params_meta.get("user_id")
        )
        _meta_prof = (
            _msg_meta.get("profile")
            if _msg_meta.get("profile") is not None
            else _params_meta.get("profile")
        )
        _ctx: Dict[str, Any]      = dict(request.context or {})
        if _meta_uid:
            _ctx["user_id"] = _meta_uid
        if _meta_prof is not None:
            _ctx["profile"] = _meta_prof

        enriched_request = AgentRequest(
            method=request.method,
            params={**request.params, "history": history_messages},
            id=request.id,
            context=_ctx,
        )

        # ── Async processing §3.3.3 ──────────────────────────────────────────
        # If the client passes "async": true in params, processing is
        # delegated to a background asyncio.Task and the response is returned
        # immediately with the task in WORKING state.
        #
        # IMPORTANT — race condition fix:
        # task_store.get() isn't called after create_task() because the background
        # task might already have completed before the get(), returning 'completed'
        # instead of 'working'. The task object already in hand is used directly,
        # updating the status field in-place before serializing.
        if request.params.get("async", False):
            asyncio.create_task(
                self._run_in_background(task.taskId, enriched_request),
                name=f"stls_bg_{task.taskId[:8]}",
            )
            logger.debug(
                f"Async task {task.taskId} scheduled in background, "
                f"returning WORKING to client immediately"
            )
            # Forces the status field to WORKING in the local snapshot — doesn't
            # re-read from the TaskStore to avoid the race with the just-started background task.
            task.status = TaskStatus(state=TaskState.WORKING)
            return AgentResponse(result=_task_dict(task), id=rpc_id)

        # ── Synchronous path (default) ───────────────────────────────────────
        try:
            agent_response = await self.invoke(enriched_request)
            logger.debug(f"Agent response for task {task.taskId}: {agent_response}")
            if agent_response.error:
                return agent_response

            await task_store.append_message(
                task.taskId,
                Message(role="agent", parts=agent_response.result["parts"]),
            )
            await task_store.append_artifact(
                task.taskId,
                Artifact(name="response", parts=agent_response.result["parts"]),
            )
            await task_store.update_status(task.taskId, TaskState.COMPLETED)

        except Exception as e:
            await task_store.update_status(task.taskId, TaskState.FAILED)
            return AgentResponse(
                error={"code": -32603, "message": str(e)},
                id=rpc_id,
            )

        task = await task_store.get(task.taskId)
        agent_response.result = _task_dict(task)
        logger.debug(f"Agent response new for task {task.taskId}: {agent_response}")
        return agent_response