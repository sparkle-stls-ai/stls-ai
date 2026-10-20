from typing import Dict, Any, Optional

from .stls_a2a_adapters_model import AgentAdapter,AgentRequest,AgentResponse

import logging
logger = logging.getLogger(__name__)



# ============================================================================
# GENERIC ADAPTER FOR CUSTOM AGENTS
# ============================================================================

class CustomFunctionAdapter(AgentAdapter):
    """
    Generic adapter for custom functions.

    Allows wrapping any async function as an A2A agent.
    The result returned by the function is normalized into A2A format
    (parts[]) so that stls_a2a_task._handle_messages_send
    doesn't raise a KeyError: 'parts'.
    """

    def __init__(
        self,
        invoke_func: callable,
        capabilities: Optional[Dict[str, Any]] = None,
        name: str = 'custom_agent'
    ):
        """
        Args:
            invoke_func: Async function that accepts an AgentRequest and returns an AgentResponse
            capabilities: Agent capabilities
            name: Agent name
        """
        self.invoke_func = invoke_func
        self._capabilities = capabilities or {
            'framework': 'custom',
            'name': name
        }
        self.name = name

    async def invoke(self, request: AgentRequest) -> AgentResponse:
        """Delegates to the custom invoke_func and normalizes the result into A2A format."""
        try:
            response = await self.invoke_func(request)
            if response is None:
                raise ValueError("invoke_func returned None")
            # Normalize result into A2A format with parts[]
            if response.result is not None and not response.error:
                response = AgentResponse(
                    result=_normalize_result(response.result),
                    id=response.id,
                    metadata=response.metadata if hasattr(response, "metadata") else None,
                )
            return response
        except Exception as e:
            logger.error("CustomFunctionAdapter.invoke() error: %s", e)
            # Preserve the error code if the agent included it in the message
            # (e.g. ValueError("Method 'x' not found (-32601)") → -32601)
            import re as _re
            m = _re.search(r"\((-3[0-9]{4})\)", str(e))
            err_code = int(m.group(1)) if m else -32603
            return AgentResponse(
                error={'code': err_code, 'message': str(e)},
                id=request.id,
            )

    def get_capabilities(self) -> Dict[str, Any]:
        return self._capabilities


# ---------------------------------------------------------------------------
# Helper: normalizes any result into A2A format with parts[]
# ---------------------------------------------------------------------------

def _normalize_result(result: Any) -> Dict[str, Any]:
    """
    Converts the return value of a custom function into the A2A format
    expected by stls_a2a_task._handle_messages_send:

        {"parts": [{"type": "text", "text": "...", "mimeType": "text/plain"}], ...}

    Cases handled:
        str / int / float  → TextPart
        dict with "parts"  → passthrough
        dict without "parts" → serialized as JSON in a TextPart
        list               → each element as a separate TextPart
        AgentResponse      → extracted recursively
    """
    if result is None:
        return {"parts": []}

    # Already in correct A2A format
    if isinstance(result, dict) and "parts" in result:
        return result

    # Scalar → TextPart
    if isinstance(result, (str, int, float, bool)):
        return {
            "parts": [{
                "type": "text",
                "text": str(result),
                "mimeType": "text/plain",
            }]
        }

    # Dict without "parts" → serialize as JSON in a TextPart
    if isinstance(result, dict):
        import json as _json
        text = result.get("text") or result.get("output") or result.get("message") or _json.dumps(result, ensure_ascii=False)
        return {
            "parts": [{"type": "text", "text": str(text), "mimeType": "text/plain"}],
            **{k: v for k, v in result.items() if k not in ("text", "output", "message", "parts")},
        }

    # List → each element as a TextPart
    if isinstance(result, list):
        parts = []
        for item in result:
            if isinstance(item, dict) and item.get("type"):
                parts.append(item)   # already an A2A Part
            else:
                parts.append({"type": "text", "text": str(item), "mimeType": "text/plain"})
        return {"parts": parts}

    # Generic fallback
    return {"parts": [{"type": "text", "text": str(result), "mimeType": "text/plain"}]}