from typing import Dict, Any
import asyncio
import json

from .stls_a2a_adapters_model import AgentAdapter,AgentRequest,AgentResponse
from .stls_a2a_task import TextPart, FilePart, DataPart, Part, FileInline, FileReference

import logging
logger = logging.getLogger(__name__)


# ============================================================================
# ADAPTER FOR LANGCHAIN
# ============================================================================

class LangChainAdapter(AgentAdapter):
    """
    Adapter for LangChain agents/chains

    Supports both LangChain Agent and Chain.
    """

    def __init__(self, agent_or_chain, chain_type: str = 'agent'):
        """
        Args:
            agent_or_chain: LangChain Agent or Chain
            chain_type: 'agent' or 'chain'
        """
        self.agent = agent_or_chain
        self.chain_type = chain_type

    def __is_langgraph_agent(self) -> bool:
        """
        Detects whether it's a LangGraph agent (CompiledGraph)
        or a plain LangChain LLM/chain.
        """
        # Checks the exact type — LangGraph agents are CompiledGraph or CompiledStateGraph
        class_name = type(self.agent).__name__
        if class_name in ('CompiledGraph', 'CompiledStateGraph'):
            return True

        # Checks the module — must be explicitly langgraph
        module = getattr(type(self.agent), '__module__', '') or ''
        if module.startswith('langgraph'):
            return True

        return False

    def __to_lc_input(self, parts: list[Part]) -> str | list:
        """
        Converts A2A Parts into LangChain input.
        - LangGraph: returns a list of HumanMessage-compatible dicts with multimodal content
        - Classic chain: returns a concatenated string
        """
        text_parts = []
        content_blocks = []   # for LangGraph multimodal

        for part in parts:
            if isinstance(part, TextPart):
                text_parts.append(part.text)
                content_blocks.append({"type": "text", "text": part.text})

            elif isinstance(part, FilePart):
                if isinstance(part.file, FileInline):
                    mime = part.file.mimeType
                    if mime.startswith("image/"):
                        # Inline image → multimodal for LangGraph
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{part.file.data}"
                            }
                        })
                    else:
                        # PDF or other files → descriptive text
                        text = f"[Attached file: {mime}, base64 data available]"
                        text_parts.append(text)
                        content_blocks.append({"type": "text", "text": text})

                elif isinstance(part.file, FileReference):
                    mime = part.file.mimeType
                    if mime.startswith("image/"):
                        content_blocks.append({
                            "type": "image_url",
                            "image_url": {"url": part.file.uri}
                        })
                    else:
                        text = f"[Remote file: {part.file.uri}, type: {mime}]"
                        text_parts.append(text)
                        content_blocks.append({"type": "text", "text": text})

            elif isinstance(part, DataPart):
                text = json.dumps(part.data, ensure_ascii=False)
                text_parts.append(text)
                content_blocks.append({"type": "text", "text": text})

        # LangGraph with multimodal → list of content blocks
        if self.__is_langgraph_agent() and any(b["type"] == "image_url" for b in content_blocks):
            return [{"role": "user", "content": content_blocks}]

        # LangGraph without images → standard messages format
        if self.__is_langgraph_agent():
            return {"messages": [{"role": "user", "content": " ".join(text_parts)}]}

        # Classic chain → string
        return " ".join(text_parts)

    def __history_to_lc_messages(self, history: list[dict]) -> list[dict]:
        """
        Converts A2A history (list[{role, parts}]) into LangChain/LangGraph messages.

        Output: list of dicts {"role": "user"|"assistant", "content": str}
        ready to be prepended to LangGraph's messages[] or used as a
        context prefix for classic chains.

        The "agent" role is mapped to "assistant" per LangChain convention.
        """
        lc_messages = []
        for msg in history:
            role = "assistant" if msg.get("role") == "agent" else "user"
            # Concatenates all TextParts of the message into a single string
            text = " ".join(
                p.get("text", "")
                for p in msg.get("parts", [])
                if p.get("type") == "text" and p.get("text")
            )
            if text:
                lc_messages.append({"role": role, "content": text})
        return lc_messages
    
    def __from_lc_output(self, result) -> list[Part]:
        """
        Converts LangChain output into A2A list[Part].
        """
        return self.__extract_parts(result)

    async def invoke(self, request: AgentRequest) -> AgentResponse:
        """Invokes a LangChain agent/chain"""
        try:
            # ── STANDARD A2A PATH ──────────────────────────────────────────
            if request.method in ('messages/send', 'message/send'):
                raw_parts = request.params.get("message", {}).get("parts", [{}])
                history   = request.params.get("history", [])   # injected by _handle_messages_send

                # 1. Parsing A2A Parts
                a2a_parts = self._build_parts(raw_parts)

                # 2. Conversion to LangChain input with previous history
                lc_input = self.__to_lc_input(a2a_parts)

                # 3. Prepend history to the current message (LangGraph only)
                if history and self.__is_langgraph_agent():
                    past = self.__history_to_lc_messages(history)
                    if isinstance(lc_input, dict) and "messages" in lc_input:
                        # Standard LangGraph format: {"messages": [...]}
                        lc_input = {"messages": past + lc_input["messages"]}
                    elif isinstance(lc_input, list):
                        lc_input = past + lc_input

                # 4. Invocation
                # Propagates user_id and profile as LangChain config["configurable"]
                # (standard for per-request state in LangGraph — accessible to nodes
                # via config["configurable"]["user_id"] / ["profile"]).
                # Classic chains receive the config but silently ignore it.
                _lc_ctx = request.context or {}
                _lc_configurable: Dict[str, Any] = {}
                if _lc_ctx.get("user_id"):
                    _lc_configurable["user_id"] = _lc_ctx["user_id"]
                if _lc_ctx.get("profile") is not None:
                    _lc_configurable["profile"] = _lc_ctx["profile"]
                _lc_config = {"configurable": _lc_configurable} if _lc_configurable else None

                if hasattr(self.agent, 'ainvoke'):
                    result = await self.agent.ainvoke(lc_input, config=_lc_config)
                else:
                    result = await asyncio.to_thread(
                        self.agent.invoke, lc_input, config=_lc_config
                    )

                # 5. Output conversion → list[Part]
                out_parts = self.__from_lc_output(result)

                return AgentResponse(
                    result={'parts': out_parts},
                    id=request.id,
                    metadata={'framework': 'langchain'}
                )

            # ── LEGACY PATH (unchanged) ────────────────────────────────────
            elif request.method in ('generate', 'invoke'):
            
                if request.method == 'generate' or request.method == 'invoke':
                    #print(f"[DEBUG] request.params = {request.params}")
                    #print(f"[DEBUG] type(request.params) = {type(request.params)}")

                    
                    input_data = request.params.get('input') or request.params.get('prompt')
                    #print(f"Invoking LangChain agent/chain with input: {input_data}")
                    
                    #print(f"[DEBUG] input_data = {input_data}")
                    #print(f"[DEBUG] type(input_data) = {type(input_data)}")

                    # Detects whether it's a LangGraph agent (create_react_agent, create_agent)
                    # or a classic LLM/chain
                    is_langgraph = self.__is_langgraph_agent()
                    print(f"[DEBUG] Detected LangGraph agent: {is_langgraph}")

                    if is_langgraph:
                        # LangGraph wants {"messages": [...]}
                        if isinstance(input_data, str):
                            agent_input = {"messages": [{"role": "user", "content": input_data}]}
                        elif isinstance(input_data, dict) and "messages" not in input_data:
                            agent_input = {"messages": [{"role": "user", "content": str(input_data)}]}
                        else:
                            agent_input = input_data
                    else:
                        # Classic LLM/chain: wants a string, PromptValue, or list of BaseMessages
                        # NEVER a generic dict
                        if isinstance(input_data, str):
                            agent_input = input_data
                        elif isinstance(input_data, dict):
                            # Extract the text from common keys
                            agent_input = (
                                input_data.get('input') or
                                input_data.get('prompt') or
                                input_data.get('text') or
                                input_data.get('content') or
                                str(input_data)
                            )
                        else:
                            agent_input = str(input_data)

                    # LangChain uses .ainvoke for async or .invoke for sync
                    if hasattr(self.agent, 'ainvoke'):
                        result = await self.agent.ainvoke(agent_input)
                    else:
                        result = await asyncio.to_thread(self.agent.invoke, agent_input)

                    # Extract output
                    output = self.__extract_output(result)
                    #print(f"LangChain agent/chain output: {output}")
                    
                    return AgentResponse(
                        #result={'output': output, 'raw': output},
                        result={'output': output},
                        id=request.id,
                        metadata={'framework': 'langchain'}
                    )
                
            else:
                return AgentResponse(
                    error={
                        'code': -32601,
                        'message': f'Method not supported: {request.method}'
                    },
                    id=request.id
                )
        
        except Exception as e:
            import traceback
            logger.error(f"Error invoking LangChain agent/chain: {e}")
            traceback.print_exc()
            return AgentResponse(
                error={
                    'code': -32603,
                    'message': f'LangChain error: {str(e)}'
                },
                id=request.id
            )
    
    
    def __extract_parts(self, result) -> list[Part]:
        """
        Extracts an A2A list[Part] from any LangChain/LangGraph output.
        Handles text, images, and multimodal content.
        """
        # Direct string
        if isinstance(result, str):
            return [TextPart(text=result, mimeType="text/plain")]

        # LangGraph: {"messages": [...]}
        if isinstance(result, dict) and "messages" in result:
            for msg in reversed(result["messages"]):
                msg_type = type(msg).__name__.lower()
                if 'tool' in msg_type or 'human' in msg_type:
                    continue

                content = getattr(msg, 'content', None)
                if not content:
                    continue

                # Multimodal content (list of blocks)
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append(TextPart(text=block["text"], mimeType="text/plain"))
                            elif block.get("type") == "image_url":
                                url = block.get("image_url", {}).get("url", "")
                                if url.startswith("data:"):
                                    # data URI → FileInline
                                    mime, b64 = url.split(";base64,")
                                    mime = mime.replace("data:", "")
                                    parts.append(FilePart(file=FileInline(mimeType=mime, data=b64)))
                                else:
                                    parts.append(FilePart(file=FileReference(mimeType="image/*", uri=url)))
                        elif isinstance(block, str):
                            parts.append(TextPart(text=block, mimeType="text/plain"))
                    if parts:
                        return parts

                # Simple string content
                if isinstance(content, str) and content.strip():
                    return [TextPart(text=content, mimeType="text/plain")]

        # Classic chains: common keys in the dict
        if isinstance(result, dict):
            for key in ['output', 'result', 'answer', 'text', 'response']:
                if result.get(key):
                    return [TextPart(text=str(result[key]), mimeType="text/plain")]

        # AIMessage / object with .content
        content = getattr(result, 'content', None)
        if content:
            if isinstance(content, list):
                # Multimodal from a LangChain object
                parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        parts.append(TextPart(text=block["text"], mimeType="text/plain"))
                if parts:
                    return parts
            return [TextPart(text=str(content), mimeType="text/plain")]

        return [TextPart(text=str(result), mimeType="text/plain")]


    def __extract_output(self, result) -> str:
        """
        Legacy wrapper — returns a string for compatibility
        with the generate/invoke path.
        """
        parts = self.__extract_parts(result)
        return " ".join(p.text for p in parts if isinstance(p, TextPart))
    
    
    async def _invoke_stream(self, request: AgentRequest):
        """
        Native streaming via astream().

        Each yield has the form:
            {"parts": list[dict], "append": bool, "last_chunk": bool}

        Behavior per agent type:
          LangGraph (CompiledStateGraph):
            - astream(input, stream_mode="messages") emits AIMessageChunk
              token by token. Falls back to base astream() if unsupported.
          Classic chain:
            - astream(input) emits strings or AIMessageChunk token by token.

        If astream is unavailable: fallback to ainvoke → single chunk.
        """
        raw_parts = request.params.get("message", {}).get("parts", [{}])
        history   = request.params.get("history", [])   # injected by _handle_messages_send_stream
        a2a_parts = self._build_parts(raw_parts)
        lc_input  = self.__to_lc_input(a2a_parts)

        # Prepend history to the current message (LangGraph only)
        if history and self.__is_langgraph_agent():
            past = self.__history_to_lc_messages(history)
            if isinstance(lc_input, dict) and "messages" in lc_input:
                lc_input = {"messages": past + lc_input["messages"]}
            elif isinstance(lc_input, list):
                lc_input = past + lc_input

        def _chunk_to_parts(chunk) -> list[dict]:
            """
            Normalizes an astream chunk into a list[dict] of A2A Parts.
            Handles: str, AIMessageChunk, dict with messages[], dict with content.
            """
            # Direct string (classic chain token-by-token)
            if isinstance(chunk, str):
                return [{"type": "text", "text": chunk, "mimeType": "text/plain"}]

            # LangGraph stream_mode="messages": (AIMessageChunk, metadata)
            if isinstance(chunk, tuple) and len(chunk) == 2:
                chunk, _ = chunk

            # AIMessageChunk / AIMessage with .content
            content = getattr(chunk, "content", None)
            if content is not None:
                if isinstance(content, str) and content:
                    return [{"type": "text", "text": content, "mimeType": "text/plain"}]
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text" and block.get("text"):
                                parts.append({"type": "text", "text": block["text"], "mimeType": "text/plain"})
                            elif block.get("type") == "image_url":
                                url = block.get("image_url", {}).get("url", "")
                                if url.startswith("data:"):
                                    mime, b64 = url.split(";base64,", 1)
                                    mime = mime.replace("data:", "")
                                    parts.append({"type": "file", "file": {"mimeType": mime, "bytes": b64}})
                                else:
                                    parts.append({"type": "file", "file": {"mimeType": "image/*", "uri": url}})
                        elif isinstance(block, str) and block:
                            parts.append({"type": "text", "text": block, "mimeType": "text/plain"})
                    return parts

            # Base LangGraph astream: {"messages": [...]} — take the last AI msg
            if isinstance(chunk, dict) and "messages" in chunk:
                lc_parts = self.__extract_parts(chunk)
                return [
                    p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
                    for p in lc_parts
                ]

            # Dict with output/text keys (final chain)
            if isinstance(chunk, dict):
                for key in ("output", "result", "answer", "text", "response"):
                    if chunk.get(key):
                        return [{"type": "text", "text": str(chunk[key]), "mimeType": "text/plain"}]

            return []

        # Per-request config with user_id / profile to propagate to astream/ainvoke.
        # LangGraph exposes config["configurable"] to the graph's nodes; classic chains
        # ignore it. None if there's no identity metadata in the context.
        _lc_ctx = request.context or {}
        _lc_configurable: Dict[str, Any] = {}
        if _lc_ctx.get("user_id"):
            _lc_configurable["user_id"] = _lc_ctx["user_id"]
        if _lc_ctx.get("profile") is not None:
            _lc_configurable["profile"] = _lc_ctx["profile"]
        _lc_config = {"configurable": _lc_configurable} if _lc_configurable else None

        # ── LangGraph streaming with stream_mode="messages" ───────────────────
        if self.__is_langgraph_agent() and hasattr(self.agent, "astream"):
            try:
                chunk_count = 0
                last_parts: list[dict] = []
                async for chunk in self.agent.astream(lc_input, stream_mode="messages",
                                                       config=_lc_config):
                    parts = _chunk_to_parts(chunk)
                    if not parts:
                        continue
                    last_parts = parts
                    chunk_count += 1
                    yield {"parts": parts, "append": True, "last_chunk": False}

                if chunk_count > 0:
                    yield {"parts": last_parts, "append": True, "last_chunk": True}
                    return
                # zero chunks → fallback below

            except (TypeError, NotImplementedError):
                pass  # stream_mode not supported → fallback to base astream

        # ── Classic chain streaming via astream ─────────────────────────────
        if hasattr(self.agent, "astream"):
            chunk_count = 0
            last_parts: list[dict] = []
            async for chunk in self.agent.astream(lc_input, config=_lc_config):
                parts = _chunk_to_parts(chunk)
                if not parts:
                    continue
                last_parts = parts
                chunk_count += 1
                yield {"parts": parts, "append": True, "last_chunk": False}

            if chunk_count > 0:
                yield {"parts": last_parts, "append": True, "last_chunk": True}
                return

        # ── Fallback: ainvoke → single final chunk ─────────────────────────
        if hasattr(self.agent, "ainvoke"):
            result = await self.agent.ainvoke(lc_input, config=_lc_config)
        else:
            result = await asyncio.to_thread(self.agent.invoke, lc_input, config=_lc_config)

        out_parts = self.__from_lc_output(result)
        serialized = [
            p.model_dump(by_alias=True) if hasattr(p, "model_dump") else p
            for p in out_parts
        ]
        yield {"parts": serialized, "append": False, "last_chunk": True}

    def get_capabilities(self) -> Dict[str, Any]:
        """LangChain capabilities"""
        return {
            'framework': 'langchain',
            'type': self.chain_type,
            'supported_methods': ['generate', 'invoke', 
                                  'messages/send','message/send','messages/sendStream','message/sendStream']
        }