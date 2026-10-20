import os
from typing import Any,Optional
from dotenv import load_dotenv
from fastapi import FastAPI

from .stls_a2a_card_model import (
    AgentSkill,
    AgentCard,
    AgentCapabilities,
    AgentExtension,
    HTTPSecurityScheme,
    SecuritySchemeType,
    StlsKeyExchangeSecurityScheme)

import logging
logger = logging.getLogger(__name__)
load_dotenv()

KEY_PROVIDER_NAME= os.getenv("KEY_PROVIDER_NAME")

def _build_stls_extension(secure_url: str | None = None) -> AgentExtension:
    """
    STLS extension declared in the public card. The provider (base/
    arqit_py/pqc) stays visible — it's legitimate information for
    interoperability (a client may want to agree on which key
    exchange mechanism to support). What no longer appears
    is the "Sparkle SKA" branding (Arqit product name) — wrong even
    just semantically for the pqc case, which has no relation to
    Arqit (X25519 + ML-KEM-768 is an independent mechanism).
    """
    params: dict = {
        "encryption": "AES-256-GCM",
        "keyProvider": KEY_PROVIDER_NAME or "base",
        "hmac": "HMAC-SHA256",
    }
    if KEY_PROVIDER_NAME == "pqc":
        params["keyDerivation"] = "HKDF-SHA256+X25519+ML-KEM-768"
        params["handshakeEndpoint"] = "/stls/pqc/handshake"
    else:
        params["keyDerivation"] = "HKDF-Expand-SHA256"
    if secure_url:
        params["endpoint"] = secure_url
    return AgentExtension(
        uri="https://stls-ai.com/extensions/secure-transport/v1",
        required=True,
        description="AES-256-GCM application-layer encryption with quantum-safe key exchange (STLS)",
        params=params,
    )

def _get_security_scheme() -> SecuritySchemeType:
    """
    Like _build_stls_extension: the provider stays visible in
    key_provider/description, only the "Sparkle SKA" branding has been
    removed (it was also semantically wrong for pqc — see
    StlsKeyExchangeSecurityScheme).
    """
    match KEY_PROVIDER_NAME:
        case "base" | "arqit_py":
            return StlsKeyExchangeSecurityScheme(
                key_provider=KEY_PROVIDER_NAME,
                description=f"Quantum-safe key exchange ({KEY_PROVIDER_NAME})",
            )
        case "pqc":
            return StlsKeyExchangeSecurityScheme(
                key_provider="pqc",
                description="Quantum-safe key exchange via X25519 + ML-KEM-768 (NIST FIPS 203)",
            )
        case "bearer":
            return HTTPSecurityScheme(
                scheme="bearer",
                bearer_format="JWT",
                description="Bearer token authentication"
            )
        case _:
            return HTTPSecurityScheme(scheme="bearer")

def _collect_modes(skills: list[AgentSkill]) -> tuple[list[str], list[str]]:
    """Aggregates inputModes/outputModes from all skills, deduplicated."""
    inputs: set[str] = set()
    outputs: set[str] = set()
    for s in skills:
        inputs.update(s.input_modes)
        outputs.update(s.output_modes)
    return sorted(inputs) or ["text"], sorted(outputs) or ["text"]        


def _tool_identity(tool: Any) -> tuple[str, str]:
    """
    Extracts (name, description) from a tool, whatever shape it takes.

    Covers two cases:
      - An already-wrapped object (BaseTool/FunctionTool) with .name/.description
        — common case when the framework has already wrapped it.
      - A raw Python function — Agent(tools=[my_function]) isn't always
        re-wrapped before being exposed on agent.tools, depending on the
        ADK version: getattr(tool, "name", str(tool)) on a raw function
        doesn't find "name" and falls back to str(tool), producing
        "<function my_function at 0x7f...>" in the public card — useless
        (it's not even a stable identifier: the memory address
        changes on every process).

    Fallback for the name: .name → .__name__ (raw function) → str(tool).
    Fallback for the description: .description → first line of the
    docstring (.__doc__) → empty string.
    """
    name = getattr(tool, "name", None)
    if not name:
        name = getattr(tool, "__name__", None)
    if not name:
        name = str(tool)

    description = getattr(tool, "description", None)
    if not description:
        doc = getattr(tool, "__doc__", None)
        description = doc.strip().splitlines()[0] if doc else ""

    return name, description


# ── Builder by agent type ────────────────────────────────────────────────
def _card_from_adk(agent: Any, url: str | None = None, 
                   secure_url: str | None = None, description: str | None = None) -> AgentCard:
    """Google ADK agent (BaseLlmAgent / BaseAgent)."""
    skills = []
    for tool in getattr(agent, "tools", []):
        tool_name, tool_description = _tool_identity(tool)
        skills.append(AgentSkill(
            id=tool_name,
            name=tool_name,
            description=tool_description,
            tags=getattr(tool, "tags", []),
        ))
    input_modes, output_modes = _collect_modes(skills)    
    return AgentCard(
        name=agent.name,
        description=description or getattr(agent, "description", f"ADK agent: {agent.name}"),
        url=url,
        secure_url=secure_url,
        skills=skills,
        default_input_modes=input_modes,
        default_output_modes=output_modes,
        capabilities=AgentCapabilities(streaming=False),
        security_scheme=_get_security_scheme(),
        extensions=[_build_stls_extension(secure_url)],                               
    )



def _card_from_langgraph(agent: Any, url: str | None = None,
                          secure_url: str | None = None, description: str | None = None) -> AgentCard:
    """LangGraph CompiledStateGraph (create_agent from langchain.agents >= 1.0)."""

    # ── Tools: agent.nodes["tools"].bound.tools_by_name ──────────────────────
    skills = []
    try:
        tools_by_name = agent.nodes["tools"].bound.tools_by_name
        for t in tools_by_name.values():
            skills.append(AgentSkill(
                id=t.name,
                name=t.name,
                description=getattr(t, "description", ""),
                tags=getattr(t, "tags", []),
            ))
    except Exception:
        pass

    input_modes, output_modes = _collect_modes(skills)

    # ── System prompt: from model node steps (path to be verified) ──────────────
    description = "LangGraph agent"
    try:
        steps = agent.nodes["model"].node.steps
        for step in steps:
            # looks in each step for attributes that contain the system prompt
            for attr in ["system_prompt", "messages", "bound"]:
                val = getattr(step, attr, None)
                if val and isinstance(val, str):
                    description = val
                    break
    except Exception:
        pass

    return AgentCard(
        name=getattr(agent, "name", "langgraph-agent"),
        description=description,
        url=url,
        secure_url=secure_url,
        skills=list(skills),
        default_input_modes=input_modes,
        default_output_modes=output_modes,
        capabilities=AgentCapabilities(streaming=False),
        security_scheme=_get_security_scheme(),
        extensions=[_build_stls_extension(secure_url)],                               
    )


def _card_generic(agent: Any, url: str | None = None,
                  secure_url: str | None = None, description: str | None = None) -> AgentCard:
    """Generic fallback: best-effort introspection."""
    name = getattr(agent, "name", type(agent).__name__)
    return AgentCard(
        name=name,
        description=description or getattr(agent, "description", f"Agent: {name}"),
        url=url,
        secure_url=secure_url,
        capabilities=AgentCapabilities(streaming=False),
        security_scheme=_get_security_scheme(),
        extensions=[_build_stls_extension(secure_url)],                                
    )

def _card_from_semantic_kernel(agent: Any, url: str | None = None, 
                               secure_url: str | None = None, description: str | None = None) -> AgentCard:
    """Semantic Kernel ChatCompletionAgent."""

    skills = []

    # tools live in the kernel's plugins
    try:
        kernel = getattr(agent, "kernel", None)
        if kernel:
            for plugin_name, plugin in kernel.plugins.items():
                for fn_name, fn in plugin.functions.items():
                    skills.append(AgentSkill(
                        id=f"{plugin_name}-{fn_name}",
                        name=fn_name,
                        description=getattr(fn, "description", ""),
                        tags=[plugin_name],
                        input_modes=["text"],
                        output_modes=["text"],
                    ))
    except Exception:
        pass

    input_modes, output_modes = _collect_modes(skills)

    return AgentCard(
        name=getattr(agent, "name", "sk-agent"),
        description=getattr(agent, "instructions", "Semantic Kernel agent"),
        url=url,
        secure_url=secure_url,
        skills=skills,
        default_input_modes=input_modes,
        default_output_modes=output_modes,
        capabilities=AgentCapabilities(streaming=True),
        security_scheme=_get_security_scheme(), 
        extensions=[_build_stls_extension(secure_url)],                               
    )


def _card_from_llm(model: Any, url: str | None = None, 
                   secure_url: str | None = None, description: str | None = None) -> AgentCard:
    """Builder for raw LLM models (ChatOpenAI, ChatAnthropic, etc.)."""

    # tries to extract the model name
    model_name = (
        getattr(model, "model_name", None) or
        getattr(model, "model", None) or
        type(model).__name__
    )

    # tries to extract the provider from the class
    provider_name = type(model).__module__.split(".")[0]  # e.g. "langchain_openai"

    return AgentCard(
        name=model_name,
        description=description or f"LLM model: {model_name}",
        url=url,
        provider={"name": provider_name, "model": model_name},
        skills=[],  # raw LLM has no tools
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
        ),
        security_scheme=_get_security_scheme(),  
        secure_url=secure_url,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def to_a2a(agent: Any, url: str | None = None,
           secure_url: str | None = None,description: str | None = None) -> AgentCard:
    """
    Converts any supported agent into its A2A Agent Card.

    Args:
        agent: agent instance (SparkleAgent, ADK, LangChain, generic)
        url:   base URL where the agent is reachable (e.g. "https://my-agent.example.com")
        secure_url: URL of the secure A2A endpoint (e.g. "https://my-agent.example.com/secure-a2a")
        description: textual description of the agent (optional, otherwise an attempt is made to extract it from the agent)

    Returns:
        AgentCard ready to be served on GET /.well-known/agent.json
    """
    if not url and not secure_url:
        raise ValueError("At least one of url and secure_url must be set.")
    try:
        from google.adk.agents import BaseAgent
        if isinstance(agent, BaseAgent):
            return _card_from_adk(agent, url, secure_url, description)
    except ImportError:
        pass

   # LangGraph CompiledStateGraph (langchain.agents.create_agent >= 1.0)
    try:
        from langgraph.graph.state import CompiledStateGraph
        if isinstance(agent, CompiledStateGraph):
            return _card_from_langgraph(agent, url, secure_url, description)
    except ImportError:
        pass

    # Semantic Kernel
    try:
        from semantic_kernel.agents import ChatCompletionAgent
        if isinstance(agent, ChatCompletionAgent):
            return _card_from_semantic_kernel(agent, url, secure_url, description)
    except ImportError:
        pass

    # Raw LLM (ChatOpenAI, ChatAnthropic, ChatGoogleGenerativeAI, etc.)
    try:
        from langchain_core.language_models import BaseChatModel
        if isinstance(agent, BaseChatModel):
            return _card_from_llm(agent, url, secure_url, description)
    except ImportError:
        pass

    # generic fallback
    return _card_generic(agent, url, secure_url, description)

# ── Extract a2a ───────────────────────────────────────────────────────────────
def extract_a2a_result(response: dict) -> tuple[Optional[dict], Optional[dict]]:
    """
    Extracts result or error from a standard A2A response.

    Returns:
        (result, None)  ← success
        (None, error)   ← error
    """
    if error := response.get('error'):
        return None, error

    result = response.get('result')
    if not result:
        return None, {'code': -32603, 'message': 'Empty result'}

    return result, None


def extract_a2a_text(response: dict) -> Optional[str]:
    """
    Extracts the agent's response text from the last message in history.
    """
    result, error = extract_a2a_result(response)
    if error:
        raise ValueError(f"A2A error {error['code']}: {error['message']}")

    history = result.get('history', [])
    agent_messages = [m for m in history if m['role'] == 'agent']
    if not agent_messages:
        return None

    parts = agent_messages[-1].get('parts', [])
    text_parts = [p['text'] for p in parts if p.get('type') == 'text' and p.get('text')]
    if not text_parts:
        return None
    # Uses '' as the separator: TextParts of the same message are
    # fragments of the same text (token-level streaming) or sections
    # with no logical separator — the newline already goes in the text if needed.
    return ''.join(text_parts)


def extract_a2a_artifacts(response: dict) -> list[dict]:
    """
    Extracts all artifacts from the response.
    """
    result, error = extract_a2a_result(response)
    if error:
        raise ValueError(f"A2A error {error['code']}: {error['message']}")

    return result.get('artifacts', [])

from contextlib import asynccontextmanager
from .stls_a2a_task import task_store

def start_task_store_cleanup(app: FastAPI):
    """
    Hooks the TaskStore cleanup into the FastAPI app's lifespan.

    The cleanup is only started if TASK_STORE_CLEANUP_ENABLED=true (default).
    Set TASK_STORE_CLEANUP_ENABLED=false on agents that shouldn't
    run the Redis index cleanup (useful in production to centralize it
    on a subset of instances).

    Preserves the existing lifespan_context (if present) by chaining it
    with ours: first runs the outer context, then the internal cleanup.
    """
    CLEANUP_INTERVAL = int(os.getenv("TASK_STORE_CLEANUP_INTERVAL", "120"))
    # Default true: backward compatibility (whoever doesn't set the var gets
    # the previous behavior with cleanup enabled)
    cleanup_enabled_raw = os.getenv("TASK_STORE_CLEANUP_ENABLED", "true").strip().lower()
    cleanup_enabled = cleanup_enabled_raw in ("true", "1", "yes")

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI):
        if existing_lifespan is not None:
            async with existing_lifespan(a):
                if cleanup_enabled:
                    task_store.start_cleanup(interval_seconds=CLEANUP_INTERVAL)
                else:
                    logger.info(
                        "TaskStore cleanup disabled on this instance "
                        "(TASK_STORE_CLEANUP_ENABLED=false)"
                    )
                yield
                if cleanup_enabled:
                    task_store.stop_cleanup()
        else:
            if cleanup_enabled:
                task_store.start_cleanup(interval_seconds=CLEANUP_INTERVAL)
            else:
                logger.info(
                    "TaskStore cleanup disabled on this instance "
                    "(TASK_STORE_CLEANUP_ENABLED=false)"
                )
            yield
            if cleanup_enabled:
                task_store.stop_cleanup()

    app.router.lifespan_context = lifespan