import json
import inspect
from typing import Dict, Any

from .stls_a2a_adapters_model import AgentAdapter,AgentRequest,AgentResponse
from .stls_a2a_task import TextPart, FilePart, DataPart, Part, FileInline, FileReference

import logging
logger = logging.getLogger(__name__)


# ============================================================================
# ADAPTER FOR SEMANTIC KERNEL
# ============================================================================

from semantic_kernel.contents import ChatHistory
from semantic_kernel.functions.kernel_arguments import KernelArguments
from semantic_kernel.agents import ChatCompletionAgent

class SemanticKernelAdapter(AgentAdapter):
    """
    Adapter for Semantic Kernel.

    Supports two modes:
    - ChatCompletionAgent: agent with tools, history, and instructions
    - Kernel function: direct invocation of a function/plugin
    """

    def __init__(self, agent_or_kernel, function_name: str = None):
        """
        Args:
            agent_or_kernel: ChatCompletionAgent or Kernel
            function_name: only for direct Kernel, e.g. 'PluginName.FunctionName'
        """
        self.agent_or_kernel = agent_or_kernel
        self.function_name = function_name
        self._mode = self._detect_mode()

    def _detect_mode(self) -> str:
        """Detects whether it's a ChatCompletionAgent or a direct Kernel"""
        if isinstance(self.agent_or_kernel, ChatCompletionAgent):
            return 'chat_agent'
        # Checks the class name to avoid circular imports
        class_name = type(self.agent_or_kernel).__name__
        if 'Agent' in class_name:
            return 'chat_agent'
        return 'kernel_function'

    def __to_sk_input(self, parts: list[Part]) -> str:
        """
        Converts A2A Parts into textual input for Semantic Kernel.
        SK has no native multimodal support — files are described as text.
        """
        chunks = []

        for part in parts:
            if isinstance(part, TextPart):
                chunks.append(part.text)

            elif isinstance(part, FilePart):
                if isinstance(part.file, FileInline):
                    chunks.append(f"[Attached file: {part.file.mimeType}, base64 available]")
                elif isinstance(part.file, FileReference):
                    chunks.append(f"[Remote file: {part.file.uri}, type: {part.file.mimeType}]")

            elif isinstance(part, DataPart):
                chunks.append(json.dumps(part.data, ensure_ascii=False))

        return " ".join(chunks)

    def __extract_parts(self, result) -> list[Part]:
        """
        Converts raw SK output into A2A list[Part].
        Handles ChatMessageContent, FunctionResult, and string.
        """
        # Direct string
        if isinstance(result, str):
            return [TextPart(text=result, mimeType="text/plain")]

        # ChatMessageContent — has .items with TextContent, ImageContent, etc.
        items = getattr(result, 'items', None)
        if items:
            parts = []
            for item in items:
                class_name = type(item).__name__

                if class_name == 'TextContent':
                    text = str(getattr(item, 'text', '') or getattr(item, 'content', ''))
                    if text:
                        parts.append(TextPart(text=text, mimeType="text/plain"))

                elif class_name == 'ImageContent':
                    uri = getattr(item, 'uri', None)
                    data = getattr(item, 'data', None)
                    mime = getattr(item, 'mime_type', 'image/png') or 'image/png'
                    if data:
                        import base64
                        b64 = base64.b64encode(data).decode('utf-8')
                        parts.append(FilePart(file=FileInline(mimeType=mime, data=b64)))
                    elif uri:
                        parts.append(FilePart(file=FileReference(mimeType=mime, uri=str(uri))))

            if parts:
                return parts

        # FunctionResult — has .value
        value = getattr(result, 'value', None)
        if value is not None:
            return [TextPart(text=str(value), mimeType="text/plain")]

        # .content as fallback (ChatMessageContent without items)
        content = getattr(result, 'content', None)
        if content:
            return [TextPart(text=str(content), mimeType="text/plain")]

        return [TextPart(text=str(result), mimeType="text/plain")]

    def __from_sk_output(self, result) -> list[Part]:
        """Converts SK output into A2A list[Part]."""
        return self.__extract_parts(result)

    def __to_str_output(self, result) -> str:
        """Legacy wrapper — returns a string for the generate/invoke path."""
        parts = self.__extract_parts(result)
        return " ".join(p.text for p in parts if isinstance(p, TextPart))
    
    async def _invoke_chat_agent(
        self,
        input_text: str,
        history: list[dict] | None = None,
        user_id: str | None = None,
        profile: dict | None = None,
    ) -> str:
        """
        Invokes a ChatCompletionAgent with ChatHistory.

        If history contains previous messages (injected by _handle_messages_send),
        they are prepended to the ChatHistory before adding the current message,
        giving the agent memory of the entire conversation.

        If user_id or profile are present (propagated from message.metadata via context),
        they are injected as an initial system message in the ChatHistory, making them
        visible to the LLM for personalization (tenant, role, audit trail).
        The system message precedes the history → it takes priority over conversational context.
        """
        chat_history = ChatHistory()

        # Injects user context as a system message if available.
        # It is placed before the history and the current message,
        # so the LLM always receives it regardless of the history's length.
        if user_id or (profile and isinstance(profile, dict)):
            ctx_parts: list[str] = []
            if user_id:
                ctx_parts.append(f"user_id={user_id}")
            if profile and isinstance(profile, dict):
                for k, v in profile.items():
                    ctx_parts.append(f"{k}={v}")
            chat_history.add_system_message(
                "User context: " + "; ".join(ctx_parts)
            )

        # Populates with the previous history
        for msg in (history or []):
            role = msg.get("role", "user")
            text = " ".join(
                p.get("text", "")
                for p in msg.get("parts", [])
                if p.get("type") == "text" and p.get("text")
            )
            if not text:
                continue
            if role == "agent":
                chat_history.add_assistant_message(text)
            else:
                chat_history.add_user_message(text)

        # Adds the current message
        chat_history.add_user_message(input_text)
        response = await self.agent_or_kernel.get_response(messages=chat_history)
        return str(response.content) if response.content else str(response)   
    
    async def invoke(self, request: AgentRequest) -> AgentResponse:
        try:
            logger.warning(f"SemanticKernelAdapter received request: method={request.method}, params={request.params}")
            # ── STANDARD A2A PATH ──────────────────────────────────────────
            if request.method in ("messages/send", "message/send"):
                raw_parts = request.params.get("message", {}).get("parts", [{}])
                history   = request.params.get("history", [])   # injected by _handle_messages_send
                logger.warning(f"Extracted raw parts: {raw_parts}")

                # 1. Parsing A2A Parts
                a2a_parts = self._build_parts(raw_parts)
                logger.warning(f"Extracted A2A parts: {a2a_parts}")

                # 2. Conversion to SK input
                input_text = self.__to_sk_input(a2a_parts)

                # 3. Invocation via detected mode (with history for chat_agent)
                _sk_ctx  = request.context or {}
                _sk_uid  = _sk_ctx.get("user_id")
                _sk_prof = _sk_ctx.get("profile")
                if self._mode == 'chat_agent':
                    output = await self._invoke_chat_agent(
                        input_text, history=history,
                        user_id=_sk_uid, profile=_sk_prof,
                    )
                else:
                    output = await self._invoke_kernel_function(input_text)

                # 4. Output conversion → list[Part]
                out_parts = self.__from_sk_output(output)

                return AgentResponse(
                    result={'parts': out_parts},
                    id=request.id,
                    metadata={
                        'framework': 'semantic-kernel',
                        'mode': self._mode
                    }
                )

            # ── LEGACY PATH (unchanged) ──────────────────────────────────
            elif request.method in ('generate', 'invoke'):

                input_text = request.params.get('input') or request.params.get('prompt')

                if self._mode == 'chat_agent':
                    output = await self._invoke_chat_agent(input_text)
                else:
                    output = await self._invoke_kernel_function(input_text)

                return AgentResponse(
                    result={'output': self.__to_str_output(output)},  # ← string wrapper
                    id=request.id,
                    metadata={'framework': 'semantic-kernel', 'mode': self._mode}
                )
        
        except Exception as e:
            import traceback
            traceback.print_exc()
            return AgentResponse(
                error={'code': -32603, 'message': f'Semantic Kernel error: {str(e)}'},
                id=request.id
            )
    
    # _invoke_chat_agent defined above with history support
    
    async def _invoke_kernel_function(self, input_text: str) -> str:
        """
        Directly invokes a Kernel function.
        Supports both coroutine and async generator (streaming).
        """
        kernel = self.agent_or_kernel
        arguments = KernelArguments(input=input_text)
        
        if self.function_name:
            if '.' in self.function_name:
                plugin_name, fn_name = self.function_name.split('.', 1)
                raw = kernel.invoke(
                    plugin_name=plugin_name,
                    function_name=fn_name,
                    arguments=arguments
                )
            else:
                raw = kernel.invoke(
                    function_name=self.function_name,
                    arguments=arguments
                )
        else:
            raw = kernel.invoke(arguments=arguments)
        
        # kernel.invoke() can be either a coroutine or an async generator
        if inspect.isasyncgen(raw):
            chunks = []
            async for chunk in raw:
                content = getattr(chunk, 'value', None) or str(chunk)
                chunks.append(content)
            return ''.join(chunks)

        elif inspect.isawaitable(raw):
            result = await raw
            # FunctionResult has .value with the clean text
            return str(getattr(result, 'value', None) or result)
        
        else:
            return str(raw)
    
    
    async def _invoke_stream(self, request: AgentRequest):
        """
        Native streaming for Semantic Kernel.

        Each yield has the form:
            {"parts": list[dict], "append": bool, "last_chunk": bool}

        Behavior per mode:
          chat_agent (ChatCompletionAgent):
            - get_streaming_response(messages=history) → AsyncGenerator[ChatMessageContent]
              Each chunk contains items with partial TextContent.
          kernel_function:
            - _invoke_kernel_function_stream() → async generator of FunctionResult/str.

        If streaming is unavailable: fallback to invoke() → single chunk.
        """
        raw_parts  = request.params.get("message", {}).get("parts", [{}])
        history_msgs = request.params.get("history", [])   # injected by _handle_messages_send_stream
        a2a_parts  = self._build_parts(raw_parts)
        input_text = self.__to_sk_input(a2a_parts)

        def _sk_chunk_to_parts(chunk) -> list[dict]:
            """
            Normalizes an SK chunk into a list[dict] of A2A Parts.
            Handles: ChatMessageContent (items), FunctionResult, str.
            """
            # Direct string
            if isinstance(chunk, str) and chunk:
                return [{"type": "text", "text": chunk, "mimeType": "text/plain"}]

            # ChatMessageContent with .items (TextContent, ImageContent)
            items = getattr(chunk, "items", None)
            if items:
                parts = []
                for item in items:
                    class_name = type(item).__name__
                    if class_name == "TextContent":
                        text = str(getattr(item, "text", "") or getattr(item, "content", ""))
                        if text:
                            parts.append({"type": "text", "text": text, "mimeType": "text/plain"})
                    elif class_name == "ImageContent":
                        uri  = getattr(item, "uri",  None)
                        data = getattr(item, "data", None)
                        mime = getattr(item, "mime_type", "image/png") or "image/png"
                        if data:
                            import base64
                            b64 = base64.b64encode(data).decode("utf-8")
                            parts.append({"type": "file", "file": {"mimeType": mime, "bytes": b64}})
                        elif uri:
                            parts.append({"type": "file", "file": {"mimeType": mime, "uri": str(uri)}})
                if parts:
                    return parts

            # FunctionResult → .value
            value = getattr(chunk, "value", None)
            if value is not None:
                return [{"type": "text", "text": str(value), "mimeType": "text/plain"}]

            # .content as fallback (ChatMessageContent without items)
            content = getattr(chunk, "content", None)
            if content:
                return [{"type": "text", "text": str(content), "mimeType": "text/plain"}]

            return []

        # ── ChatCompletionAgent: get_streaming_response() ─────────────────────
        _sk_ctx  = request.context or {}
        _sk_uid  = _sk_ctx.get("user_id")
        _sk_prof = _sk_ctx.get("profile")

        if self._mode == "chat_agent" and hasattr(self.agent_or_kernel, "get_streaming_response"):
            try:
                history = ChatHistory()
                # Injects user context as a system message (stream path)
                if _sk_uid or (_sk_prof and isinstance(_sk_prof, dict)):
                    ctx_parts: list[str] = []
                    if _sk_uid:
                        ctx_parts.append(f"user_id={_sk_uid}")
                    if _sk_prof and isinstance(_sk_prof, dict):
                        for k, v in _sk_prof.items():
                            ctx_parts.append(f"{k}={v}")
                    history.add_system_message("User context: " + "; ".join(ctx_parts))
                # Prepends the previous conversation history
                for msg in (history_msgs or []):
                    role = msg.get("role", "user")
                    text = " ".join(
                        p.get("text", "")
                        for p in msg.get("parts", [])
                        if p.get("type") == "text" and p.get("text")
                    )
                    if text:
                        if role == "agent":
                            history.add_assistant_message(text)
                        else:
                            history.add_user_message(text)
                history.add_user_message(input_text)

                chunk_count = 0
                last_parts: list[dict] = []
                async for chunk in self.agent_or_kernel.get_streaming_response(messages=history):
                    parts = _sk_chunk_to_parts(chunk)
                    if not parts:
                        continue
                    last_parts = parts
                    chunk_count += 1
                    yield {"parts": parts, "append": True, "last_chunk": False}

                if chunk_count > 0:
                    yield {"parts": last_parts, "append": True, "last_chunk": True}
                    return
                # zero chunks → fallback below

            except (TypeError, NotImplementedError, AttributeError):
                pass  # get_streaming_response not available in this SK version

        # ── Kernel function: async generator ─────────────────────────────────
        if self._mode == "kernel_function":
            try:
                kernel    = self.agent_or_kernel
                arguments = KernelArguments(input=input_text)

                if self.function_name and "." in self.function_name:
                    plugin_name, fn_name = self.function_name.split(".", 1)
                    raw = kernel.invoke_stream(
                        plugin_name=plugin_name,
                        function_name=fn_name,
                        arguments=arguments
                    )
                elif self.function_name:
                    raw = kernel.invoke_stream(
                        function_name=self.function_name,
                        arguments=arguments
                    )
                else:
                    raw = kernel.invoke_stream(arguments=arguments)

                if inspect.isasyncgen(raw):
                    chunk_count = 0
                    last_parts: list[dict] = []
                    async for chunk in raw:
                        parts = _sk_chunk_to_parts(chunk)
                        if not parts:
                            continue
                        last_parts = parts
                        chunk_count += 1
                        yield {"parts": parts, "append": True, "last_chunk": False}

                    if chunk_count > 0:
                        yield {"parts": last_parts, "append": True, "last_chunk": True}
                        return

            except (TypeError, NotImplementedError, AttributeError):
                pass  # invoke_stream not available

        # ── Fallback: invoke() → single final chunk ─────────────────────────
        # Forces messages/send: invoke() doesn't handle messages/sendStream
        # and would return None, causing an AttributeError on response.result.
        fallback_request = AgentRequest(
            method="messages/send",
            params=request.params,
            id=request.id,
            context=request.context,
        )
        response = await self.invoke(fallback_request)
        parts: list = []
        if response is not None and response.result:
            r = response.result
            if isinstance(r, dict) and r.get("parts"):
                parts = [
                    p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                    for p in r["parts"]
                ]
            else:
                text = r.get("text") or r.get("output") or str(r) if isinstance(r, dict) else str(r)
                parts = [{"type": "text", "text": text, "mimeType": "text/plain"}]
        else:
            parts = [{"type": "text", "text": "stream not implemented", "mimeType": "text/plain"}]
        yield {"parts": parts, "append": False, "last_chunk": True}

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            'framework': 'semantic-kernel',
            'mode': self._mode,
            'function_name': self.function_name,
            'supported_methods': ['generate', 'invoke', 
                                  'messages/send', 'message/send','messages/sendStream','message/sendStream']
        }