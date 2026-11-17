"""
Agent Adapters for A2A Secure Transport
========================================

Common interface and adapters for different AI/Agent frameworks,
enabling secure A2A communication independent of the framework.

Supported frameworks:
- Google ADK
- LangChain
- Semantic Kernel
- Custom agents

"""

from typing import Dict, Any, Optional

from .stls_a2a_adapters_model import AgentAdapter,AgentRequest,AgentResponse

from .stls_a2a_google_adapter import GoogleADKAdapter
from .stls_a2a_lc_adapter import LangChainAdapter
from .stls_a2a_sk_adapter import SemanticKernelAdapter
from .stls_a2a_custom_adapter import CustomFunctionAdapter

import logging
logger = logging.getLogger(__name__)


# ============================================================================
# FACTORY TO CREATE ADAPTERS
# ============================================================================

def create_adapter(
    agent,
    framework: str = 'auto',
    **kwargs
) -> AgentAdapter:
    """
    Factory to create the appropriate adapter

    Args:
        agent: Agent instance
        framework: 'google-adk', 'langchain', 'semantic-kernel', 'custom', or 'auto'
        **kwargs: Additional parameters for the adapter

    Returns:
        The appropriate AgentAdapter

    """
    if framework == 'auto':
        # Framework auto-detection
        agent_class = agent.__class__.__name__
        module = agent.__class__.__module__

        if 'google.adk' in module or 'genai.adk' in module:
            framework = 'google-adk'
        elif 'langchain' in module:
            framework = 'langchain'
        elif 'semantic_kernel' in module:
            framework = 'semantic-kernel'
        else:
            raise ValueError(
                f"Cannot auto-detect framework for {agent_class}. "
                "Please specify framework explicitly."
            )
    
    # Create the appropriate adapter
    if framework == 'google-adk':
        return GoogleADKAdapter(agent, **kwargs)
    elif framework == 'langchain':
        return LangChainAdapter(agent, **kwargs)
    elif framework == 'semantic-kernel':
        return SemanticKernelAdapter(agent, **kwargs)
    elif framework == 'custom':
        return CustomFunctionAdapter(agent, **kwargs)
    else:
        raise ValueError(f"Unknown framework: {framework}")


# ============================================================================
# UTILITIES FOR A2A <-> AgentRequest/Response CONVERSION
# ============================================================================

# A2A 1.0 (spec §9.4) renamed the JSON-RPC methods to PascalCase, shared
# across the JSON-RPC/gRPC/REST bindings — a breaking change from 0.3's
# slash-style names (`messages/send`, `tasks/get`, ...), which is what
# every dispatch point in this package still matches on internally.
#
# Rather than teaching every call site two spellings, 1.0 names are
# normalized here, at the single point where a raw JSON-RPC message
# becomes an AgentRequest, into their 0.3 equivalent. 0.3 clients are
# unaffected (their method names aren't in this map, so they pass
# through unchanged) — this is purely an additive input alias, not a
# replacement.
#
# SendStreamingMessage/SubscribeToTask are included for defensiveness
# (handle_request() has explicit branches that reject them with "requires
# SSE endpoint"), but in practice they never reach this function un-normalized:
# the SSE layer (SSE_STREAM_METHODS et al. in stls_a2a_integration.py) already
# recognizes both the 0.3 and 1.0 spellings before a2a_to_agent_request runs.
_A2A_1_0_METHOD_ALIASES: Dict[str, str] = {
    "SendMessage":                      "messages/send",
    "SendStreamingMessage":             "messages/sendStream",
    "GetTask":                          "tasks/get",
    "ListTasks":                        "tasks/list",
    "CancelTask":                       "tasks/cancel",
    "SubscribeToTask":                  "tasks/subscribe",
    "CreateTaskPushNotificationConfig": "tasks/pushNotificationConfig/set",
    "GetTaskPushNotificationConfig":    "tasks/pushNotificationConfig/get",
    "ListTaskPushNotificationConfigs":  "tasks/pushNotificationConfig/list",
    "DeleteTaskPushNotificationConfig": "tasks/pushNotificationConfig/delete",
}


def a2a_to_agent_request(a2a_message: Dict[str, Any]) -> AgentRequest:
    """
    Converts an A2A (JSON-RPC) message into an AgentRequest

    Args:
        a2a_message: A2A JSON-RPC message

    Returns:
        Standardized AgentRequest
    """
    method = a2a_message.get('method', 'invoke')
    method = _A2A_1_0_METHOD_ALIASES.get(method, method)
    return AgentRequest(
        method=method,
        params=a2a_message.get('params', {}),
        id=a2a_message.get('id'),
        context=a2a_message.get('context')
    )


def agent_response_to_a2a(response: Optional[AgentResponse]) -> Dict[str, Any]:
    """
    Converts an AgentResponse into an A2A (JSON-RPC) message.

    Handles the response=None case (adapter that doesn't explicitly return
    for unsupported methods) by returning a -32603 error instead of
    raising an AttributeError.
    """
    if response is None:
        logger.error(
            "agent_response_to_a2a: received None — adapter invoke() returned no value. "
            "Check that all code paths in the adapter return an AgentResponse."
        )
        return {
            'jsonrpc': '2.0',
            'error': {
                'code': -32603,
                'message': 'Internal error: adapter returned no response. '
                           'Possible missing else-branch for unsupported method.',
            },
            'id': None,
        }

    a2a_response: Dict[str, Any] = {
        'jsonrpc': '2.0',
        'id': response.id,
    }

    if response.error:
        a2a_response['error'] = response.error
    else:
        # The result is passed as-is: normalization into A2A format
        # (parts[]) already happens in CustomFunctionAdapter.invoke() for custom
        # agents, and in _handle_messages_send() for all the others.
        # DO NOT normalize here: Task dicts (tasks/get, tasks/cancel, messages/send)
        # don't have "parts" by design and must not be transformed.
        a2a_response['result'] = response.result

    if response.metadata:
        a2a_response['metadata'] = response.metadata

    return a2a_response