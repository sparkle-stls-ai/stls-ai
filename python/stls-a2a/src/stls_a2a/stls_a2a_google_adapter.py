from typing import Dict, Any, Optional, List,AsyncIterator
from .stls_a2a_task import TextPart, FilePart, DataPart, Part, FileInline, FileReference
import asyncio

from .stls_a2a_adapters_model import AgentAdapter,AgentRequest,AgentResponse

import logging
logger = logging.getLogger(__name__)


# ============================================================================
# ADAPTER FOR GOOGLE ADK
# ============================================================================
from google.genai import types

class GoogleADKAdapter(AgentAdapter):
    """
    Adapter for Google ADK Agent with InMemoryRunner

    Supports the real Google ADK API, which uses:
    - InMemoryRunner to run the agent
    - session_service to manage sessions
    - run_async for event streaming

    Usage example:
        from google.genai.adk import LlmAgent

        agent = LlmAgent(model='gemini-2.0-flash-exp')
        adapter = GoogleADKAdapter(agent, app_name='my_app')
    """

    def __init__(
        self,
        agent,
        app_name: str = 'a2a_agent',
        runner_class = None,
        session_config: Optional[Dict[str, Any]] = None
    ):
        """
        Initializes the adapter for Google ADK

        Args:
            agent: Instance of a google.genai.adk Agent (LlmAgent, etc.)
            app_name: Application name for sessions
            runner_class: Runner class (default: InMemoryRunner if available)
            session_config: Optional configuration for sessions
        """
        self.agent = agent
        self.app_name = app_name
        self.session_config = session_config or {}
        self._closed = False
        
        # Imports InMemoryRunner if not provided
        if runner_class is None:
            try:
                from google.adk.runners import InMemoryRunner 
                self.runner_class = InMemoryRunner
            except ImportError as exc:
                raise ImportError(
                    "Cannot import InMemoryRunner. "
                    "Make sure google-genai is installed: pip install google-genai"
                ) from exc
        else:
            self.runner_class = runner_class
        
        # Creates the runner
        #print(f"Initializing GoogleADKAdapter with agent: {agent.__class__.__name__}, app_name: {app_name}")
        self.runner = self.runner_class(agent=self.agent, app_name=self.app_name)
        self.session_service = self.runner.session_service
    
    async def close(self):
        """
        Closes the runner and releases resources

        Avoids async cleanup errors by correctly managing
        the lifecycle of the runner and its internal components.
        """
        if self._closed:
            return

        try:
            # Closes the runner if it has a close method
            if hasattr(self.runner, 'close'):
                if asyncio.iscoroutinefunction(self.runner.close):
                    await self.runner.close()
                else:
                    self.runner.close()

            # Closes the session service if it has a close method
            if hasattr(self.session_service, 'close'):
                if asyncio.iscoroutinefunction(self.session_service.close):
                    await self.session_service.close()
                else:
                    self.session_service.close()

            # Closes the agent if it has a close method
            if hasattr(self.agent, 'close'):
                if asyncio.iscoroutinefunction(self.agent.close):
                    await self.agent.close()
                else:
                    self.agent.close()

        except AttributeError as e:
            # Ignores AttributeError related to _async_httpx_client
            # This is a known bug in google.genai's cleanup
            if '_async_httpx_client' not in str(e):
                raise

        except Exception as e:
            # Log but don't propagate cleanup errors
            import warnings
            warnings.warn(f"Error during GoogleADKAdapter cleanup: {e}", RuntimeWarning)

        finally:
            self._closed = True

    async def __aenter__(self):
        """Context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - closes automatically"""
        await self.close()
        return False
    
    async def _to_genai_parts(self, parts: list[Part]) -> list:
        """
        Converts typed A2A Parts → Google GenAI types.Part.

        Handles:
          TextPart                             → types.Part(text=...)
          FilePart (FileInline)                → types.Part.from_bytes(...)
          FilePart (FileReference, gs:// or yt) → types.Part.from_uri(...)
          FilePart (FileReference, generic URL) → downloads and from_bytes(...)
          DataPart                             → types.Part.from_text(json)
        """
        import base64
        import json
        genai_parts = []
 
        for part in parts:
 
            if isinstance(part, TextPart):
                genai_parts.append(types.Part(text=part.text))
 
            elif isinstance(part, FilePart):
                if isinstance(part.file, FileInline):
                    raw = base64.b64decode(part.file.data)
                    _fname = getattr(part.file, "name", None)
                    if _fname:
                        # Propagates the original name as display_name on the Blob.
                        # The coordinator callback reads it via:
                        #   getattr(part.inline_data, "display_name", None)
                        # If Blob doesn't support display_name (old SDK), it falls
                        # back silently.
                        try:
                            genai_parts.append(types.Part(
                                inline_data=types.Blob(
                                    data=raw,
                                    mime_type=part.file.mimeType,
                                    display_name=_fname,
                                )
                            ))
                        except Exception:
                            genai_parts.append(
                                types.Part.from_bytes(data=raw, mime_type=part.file.mimeType)
                            )
                    else:
                        genai_parts.append(
                            types.Part.from_bytes(data=raw, mime_type=part.file.mimeType)
                        )
                elif isinstance(part.file, FileReference):
                    uri = part.file.uri
                    if uri.startswith("gs://") or "youtube.com" in uri:
                        genai_parts.append(
                            types.Part.from_uri(file_uri=uri, mime_type=part.file.mimeType)
                        )
                    else:
                        import httpx
                        # Explicit timeout: the httpx default (5s) would fail
                        # on remote file downloads even just moderately large.
                        _dl_timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)
                        async with httpx.AsyncClient(timeout=_dl_timeout) as client:
                            response = await client.get(uri)
                            response.raise_for_status()
                        genai_parts.append(
                            types.Part.from_bytes(data=response.content, mime_type=part.file.mimeType)
                        )
 
            elif isinstance(part, DataPart):
                genai_parts.append(
                    types.Part.from_text(json.dumps(part.data, ensure_ascii=False))
                )
 
        return genai_parts
    
    def _build_legacy_parts(self,text: str, files: list | None) -> list:
            """
            files: list of dicts with keys 'data' (base64 str/bytes)
                   and 'mimeType' (e.g. 'application/pdf', 'image/png')
            """
            parts = [types.Part(text=text)] if text else []
            for f in (files or []):
                parts.append(
                    types.Part(inline_data={
                            'data':f['data'],
                            'mimeType':f['mimeType'],   
                            })
                    )
            return parts
    
    # converts A2A Parts → Google GenAI types.Part
    def _from_genai_parts(self, genai_parts: list) -> list[Part]:
        """
        Converts Google GenAI types.Part → typed A2A Parts.

        Handles:
          part.text              → TextPart
          part.inline_data       → FilePart (FileInline, base64)
          part.file_data         → FilePart (FileReference, URI)
          part.function_response → DataPart (tool call result)
          part.function_call     → DataPart (tool request, rare in output)
        """
        import base64
        parts: list[Part] = []
 
        for part in genai_parts:
 
            if part.text is not None:
                parts.append(TextPart(text=part.text, mimeType="text/plain"))
 
            elif part.inline_data is not None:
                parts.append(FilePart(
                    file=FileInline(
                        mimeType=part.inline_data.mime_type,
                        data=base64.b64encode(part.inline_data.data).decode("utf-8")
                    )
                ))
 
            elif part.file_data is not None:
                parts.append(FilePart(
                    file=FileReference(
                        mimeType=part.file_data.mime_type,
                        uri=part.file_data.file_uri
                    )
                ))
 
            elif part.function_response is not None:
                parts.append(DataPart(
                    data={
                        "name":     part.function_response.name,
                        "response": dict(part.function_response.response or {}),
                    },
                    mimeType="application/json"
                ))
 
            elif part.function_call is not None:
                parts.append(DataPart(
                    data={
                        "name": part.function_call.name,
                        "args": dict(part.function_call.args or {}),
                    },
                    mimeType="application/json"
                ))
 
        return parts

    async def _inject_history_to_adk_session(
        self,
        session_id: str,
        user_id: str,
        history: list[dict],
    ) -> None:
        """
        Injects the STLS history into the just-created ADK session.

        Called only when the session is new (process restarted / new
        Cloud Run instance) and the TaskStore already has messages for that
        contextId. This way, even in multi-instance scenarios the ADK
        agent receives the full conversational context.

        Strategy:
          1. Tries to use session_service.append_event() if available (public ADK API).
          2. Fallback: directly accesses session.events (internal API, more fragile).
          3. If neither works, logs a warning and proceeds without memory
             (graceful degradation — the agent responds without past history).

        History format:
            [{"role": "user"|"agent", "parts": [{"type": "text", "text": "..."}]}, ...]
        """
        if not history:
            return

        try:
            # Retrieves the just-created session
            session = await self.session_service.get_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
            )
            if session is None:
                logger.warning(
                    "_inject_history_to_adk_session: session %s not found after create",
                    session_id,
                )
                return

            for msg in history:
                # role: "user" → "user", "agent" → "model" (Gemini convention)
                role = "user" if msg.get("role") == "user" else "model"
                text = " ".join(
                    p.get("text", "")
                    for p in msg.get("parts", [])
                    if p.get("type") == "text" and p.get("text")
                )
                if not text:
                    continue

                content = types.Content(role=role, parts=[types.Part(text=text)])

                # ── Strategy 1: public append_event API ──────────────────
                if hasattr(self.session_service, "append_event"):
                    try:
                        from google.adk.events import Event
                        event = Event(
                            author=role,
                            content=content,
                        )
                        await self.session_service.append_event(
                            session=session, event=event
                        )
                        continue
                    except Exception as e:
                        logger.debug("append_event failed (%s), trying direct access", e)

                # ── Strategy 2: direct access to session.events ───────────
                if hasattr(session, "events") and isinstance(session.events, list):
                    try:
                        # Creates a minimal event compatible with the ADK structure
                        from google.adk.events import Event
                        event = Event(author=role, content=content)
                        session.events.append(event)
                        continue
                    except Exception as e:
                        logger.debug("Direct events append failed (%s)", e)

                # ── No strategy available ──────────────────────────
                logger.warning(
                    "_inject_history_to_adk_session: unable to inject message "
                    "(role=%s, text=%.60s...). ADK will respond without history.", role, text
                )
                break  # pointless to continue if the API isn't accessible

            logger.debug(
                "_inject_history_to_adk_session: injected %d messages into session %s",
                len(history), session_id,
            )

        except Exception as e:
            # Graceful degradation: the agent responds without past history
            logger.warning(
                "_inject_history_to_adk_session: error during history injection "
                "(session=%s): %s — the agent will respond without context.", session_id, e
            )

    async def invoke(self, request: AgentRequest) -> AgentResponse:
        """
        Invokes the Google ADK agent using InMemoryRunner

        Supports:
        - 'generate': Standard text generation
        - 'chat': Conversation with multiple messages
        - 'invoke': Alias for generate

        Handles event streaming and extracts the final response.
        """
        try:
            # Imports types to create Content
            try:
                from google.genai import types
            except ImportError:
                return AgentResponse(
                    error={
                        'code': -32603,
                        'message': 'Cannot import google.genai.types'
                    },
                    id=request.id
                )


            # Prepares the message based on the method
            if request.method in ('messages/send', 'message/send'):
                # standard message
                raw_parts = request.params.get("message", {}).get("parts", [{}])
                #logger.debug(f'raw_parts:{raw_parts}')
                new_message=new_message = types.Content(
                    role="user",
                    parts=await self._to_genai_parts(self._build_parts(raw_parts))    
                )
                logger.debug(f'New Message:{new_message}')

            elif request.method in ['generate', 'invoke']:
                # Extract prompt/message
                prompt = request.params.get('prompt') or request.params.get('message') or request.params.get('input')
                
                if not prompt:
                    return AgentResponse(
                        error={
                            'code': -32602,
                            'message': 'Missing required parameter: prompt/message/input'
                        },
                        id=request.id
                    )
                
                files = request.params.get('files')
                new_message = types.Content(
                    role="user",
                    parts=self._build_legacy_parts(prompt, files)         
                )
            
            elif request.method == 'chat':
                # For chat, use the last message (the runner manages history via session)
                messages = request.params.get('messages', [])

                if not messages:
                    return AgentResponse(
                        error={
                            'code': -32602,
                            'message': 'Missing required parameter: messages'
                        },
                        id=request.id
                    )

                # Take the last message
                last_msg = messages[-1]
                content = last_msg.get('content', '')

                files = last_msg.get('files')                 
                new_message = types.Content(
                    role="user",
                    parts=self._build_legacy_parts(content, files)    
                )
                
            else:
                return AgentResponse(
                    error={
                        'code': -32601,
                        'message': f'Method not supported: {request.method}. Supported: generate, invoke, chat'
                    },
                    id=request.id
                )
            
            # Runs the agent with run_async and collects events
            response_text = None
            response_parts = None
            error_details = None

            # Uses contextId as session_id to leverage ADK's native memory
            # (InMemoryRunner keeps ChatHistory per session). If the client doesn't
            # send contextId, a one-shot UUID is generated (no multi-turn memory).
            import uuid
            context = request.context or {}
            session_id = (
                context.get('session_id')
                or request.params.get('contextId')
                or request.params.get('message', {}).get('contextId')
                or str(uuid.uuid4())
            )
            user_id = context.get('user_id', 'default_user')

            # Creates the session. If it already exists (same instance, same contextId),
            # create_session raises an exception → we catch it and continue.
            # In that case the ADK history is already in-session → nothing to inject.

            # Builds the ADK session's initial state.
            # user_id and profile (from message.metadata, propagated via context) are
            # exposed as session.state["user_id"] / session.state["profile"] and are
            # accessible to tools via tool_context.state["profile"]["tenant"], etc.
            profile = context.get("profile")
            session_state: Dict[str, Any] = {"user_id": user_id}
            if profile and isinstance(profile, dict):
                session_state["profile"] = profile

            session_is_new = False
            try:
                await self.session_service.create_session(
                    app_name=self.app_name,
                    user_id=user_id,
                    session_id=session_id,
                    state=session_state,
                )
                session_is_new = True
            except Exception:
                pass  # session already present

            # ALWAYS updates the internal pre-run state — for both new and
            # existing sessions. The reason: state_delta in run_async is applied
            # via append_event AFTER the InvocationContext has already been built,
            # so tool_context.state doesn't see the updated values.
            # By directly updating sessions[app][user][session_id].state
            # BEFORE run_async, we guarantee that ic.session.state (and thus
            # tool_context.state) reflects the current user_id and profile.
            try:
                _ss = self.session_service
                _store = (
                    getattr(_ss, "sessions", {})
                    .get(self.app_name, {})
                    .get(user_id, {})
                    .get(session_id)
                )
                if _store is not None and hasattr(_store, "state"):
                    _store.state.update(session_state)
                    logger.debug(
                        "GoogleADKAdapter: session.state updated pre-run "
                        "for session_id=%s keys=%s",
                        session_id, list(session_state.keys())
                    )
            except Exception as _upd_err:
                logger.debug(
                    "GoogleADKAdapter: unable to update session.state "
                    "pre-run for session_id=%s: %s", session_id, _upd_err
                )

            # Injects the STLS history into the ADK session only if:
            #   - the session was just created (process restarted / new instance)
            #   - history exists in the context (multi-turn conversation)
            # This way both the same-instance path (ADK session) and the
            # cross-instance path (STLS TaskStore → ADK session) work correctly.
            history_msgs = request.params.get('history', [])
            if session_is_new and history_msgs:
                await self._inject_history_to_adk_session(
                    session_id=session_id,
                    user_id=user_id,
                    history=history_msgs,
                )
            
            async for event in self.runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message,
            ):
                # Checks whether it's the final response.
                # With tool-use (FunctionTool), ADK emits multiple is_final_response events:
                # one for the function call and one for the final text response.
                # We only collect parts when they contain actual text,
                # and only break once valid content has been found.
                if event.is_final_response() and event.content:
                    if hasattr(event.content, 'parts') and event.content.parts:
                        # Verifies that at least one part has useful content
                        has_text = any(
                            getattr(p, 'text', None)
                            for p in event.content.parts
                        )
                        if has_text:
                            response_parts = event.content.parts
                            break  # ✅ Break only on the final text response
                        # else: it's an intermediate event (function call), continue

                # Checks whether there's an error
                elif hasattr(event, 'is_error') and event.is_error():
                    error_details = {
                        'code': getattr(event, 'error_code', -32603),
                        'message': str(getattr(event, 'error_details', 'Unknown error'))
                    }
                    break
            
            # Returns response or error
            if error_details:
                return AgentResponse(
                    error=error_details,
                    id=request.id
                )
            
            if response_parts:
                result={}
                if request.method not in ('messages/send','message/send'):
                   response_text=response_parts[0].text.strip()
                   result={'text': response_text}
                else:
                   # standard response, so return the parts
                   result={'parts': self._from_genai_parts(response_parts)}
                      
                return AgentResponse(
                    result=result,
                    id=request.id,
                    metadata={
                        'framework': 'google-adk',
                        'session_id': session_id,
                        'user_id': user_id
                    }
                )
                  
            # If we didn't receive either a response or an error
            return AgentResponse(
                error={
                    'code': -32603,
                    'message': 'No response received from agent'
                },
                id=request.id
            )
        
        except Exception as e:
            import traceback
            logger.error(f"Error invoking Google ADK agent: {e}")
            return AgentResponse(
                error={
                    'code': -32603,
                    'message': f'Internal error: {str(e)}',
                    'traceback': traceback.format_exc()
                },
                id=request.id
            )
    
    async def _invoke_stream(self, request: AgentRequest) -> AsyncIterator[Dict[str, Any]]:
        """
        Native streaming via runner.run_async().

        Each yield contains all the A2A Parts of the ADK event, not just the text:
            {
                "parts":      list[Part],   # TextPart | FilePart | DataPart
                "append":     bool,
                "last_chunk": bool,
            }

        Part types handled per event:
          part.text             → TextPart  (plain text or markdown)
          part.inline_data      → FilePart  FileInline (base64 image/audio/PDF)
          part.file_data        → FilePart  FileReference (GCS / YouTube URI)
          part.function_response→ DataPart  (tool call result)
          part.function_call    → DataPart  (tool request, rare in output)

        Events with no useful parts (e.g. ADK internal routing events) are
        skipped without interrupting the stream.
        """
        import uuid
        context    = request.context or {}
        session_id = (
            context.get('session_id')
            or request.params.get('contextId')
            or request.params.get('message', {}).get('contextId')
            or str(uuid.uuid4())
        )
        user_id    = context.get('user_id', 'default_user')
        print(f"Session ID: {session_id}, User ID: {user_id}")
        
        raw_parts   = request.params.get("message", {}).get("parts", [{}])
        new_message = types.Content(
            role="user",
            parts=await self._to_genai_parts(self._build_parts(raw_parts))
        )
 
        # Creates the session — idempotent. Tracks whether it's new to decide
        # whether to inject the STLS history (cross-instance / post-restart).

        # Builds the session's initial state (stream path):
        # user_id and profile are exposed as session.state to tools.
        profile = context.get("profile")
        session_state: Dict[str, Any] = {"user_id": user_id}
        if profile and isinstance(profile, dict):
            session_state["profile"] = profile

        session_is_new = False
        try:
            await self.session_service.create_session(
                app_name=self.app_name,
                user_id=user_id,
                session_id=session_id,
                state=session_state,
            )
            session_is_new = True
        except Exception:
            pass  # session already present

        # Updates state pre-run — same logic as invoke(), see the comment there.
        try:
            _ss = self.session_service
            _store = (
                getattr(_ss, "sessions", {})
                .get(self.app_name, {})
                .get(user_id, {})
                .get(session_id)
            )
            if _store is not None and hasattr(_store, "state"):
                _store.state.update(session_state)
                logger.debug(
                    "GoogleADKAdapter: session.state updated pre-run "
                    "(stream) for session_id=%s keys=%s",
                    session_id, list(session_state.keys())
                )
        except Exception as _upd_err:
            logger.debug(
                "GoogleADKAdapter: unable to update session.state "
                "pre-run (stream) for session_id=%s: %s", session_id, _upd_err
            )

        history_msgs = request.params.get('history', [])
        if session_is_new and history_msgs:
            await self._inject_history_to_adk_session(
                session_id=session_id,
                user_id=user_id,
                history=history_msgs,
            )
 
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
        ):
            if not (event.content and event.content.parts):
                continue
 
            is_final  = event.is_final_response()

            # Converts all of the event's parts into typed A2A Parts.
            # _from_genai_parts handles: text, inline_data, file_data,
            # function_response, function_call → no part is dropped.
            a2a_parts = self._from_genai_parts(event.content.parts)

            if a2a_parts:
                # A single yield per event: the middleware receives the
                # full chunk and inserts it into a TaskArtifactUpdateEvent
                # with artifact.parts = a2a_parts (not hardcoded to type:text).
                yield {
                    "parts":      a2a_parts,
                    "append":     True,
                    "last_chunk": is_final,
                }
 
            if is_final:
                break


    def get_capabilities(self) -> Dict[str, Any]:
        """Returns the ADK agent's capabilities"""
        caps = {
            'framework': 'google-adk',
            'supported_methods': ['generate', 'invoke', 'chat', 
                                  'messages/send', 'message/send','messages/sendStream','message/sendStream'],
            'app_name': self.app_name,
            'uses_runner': True,
            'uses_sessions': True
        }
        
        # Adds agent information if available
        if hasattr(self.agent, 'name'):
            caps['agent_name'] = self.agent.name

        if hasattr(self.agent, 'model'):
            caps['model'] = self.agent.model
        elif hasattr(self.agent, '_model'):
            caps['model'] = self.agent._model

        # Adds tool info if available
        if hasattr(self.agent, 'tools') and self.agent.tools:
            caps['tools'] = self._get_tools_info()

        return caps

    def _get_tools_info(self) -> List[Dict[str, Any]]:
        """Extracts information about available tools"""
        if not hasattr(self.agent, 'tools') or not self.agent.tools:
            return []
        
        tools_info = []
        for tool in self.agent.tools:
            tool_info = {
                'name': tool.name if hasattr(tool, 'name') else str(tool)
            }
            if hasattr(tool, 'description'):
                tool_info['description'] = tool.description
            tools_info.append(tool_info)
        
        return tools_info