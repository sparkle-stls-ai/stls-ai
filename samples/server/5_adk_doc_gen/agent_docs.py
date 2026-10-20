"""
agent_docs.py
-------------
Agent with specialized sub-agents and tools for on-demand generation
of documents (docx, xlsx, md, pdf) and charts (png, html, pdf).

MIME type and Gemini compatibility (input history):
  ✅ image/png, application/pdf  → accepted, no stripping
  ❌ docx, xlsx, text/html       → not accepted, stripped from history
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse
from google.adk.tools import AgentTool
from google.genai import types
from google.adk.tools import google_search

from document_tools import (
    _PENDING_KEY,
    generate_docx_report,
    generate_md_report,
    generate_xlsx_report,
    generate_pdf_report,
    generate_chart_png,
    generate_chart_html,
    generate_chart_pdf,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s-%(levelname)-8s-%(name)s-%(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

# LLM model passed in externally (AGENT_MODEL environment variable).
# The default keeps the model used so far if the variable is not set.
DEFAULT_AGENT_MODEL = "gemini-3.5-flash"
AGENT_MODEL = (os.getenv("AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()
logger.info(f"--- Agent model: {AGENT_MODEL} ---")


_UNSUPPORTED_MIME_TYPES = frozenset({
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/html",
})


# ══════════════════════════════════════════════════════════════════════════════
# CALLBACKS
# ══════════════════════════════════════════════════════════════════════════════

def strip_binary_parts(
    callback_context: CallbackContext,
    llm_request: LlmRequest,
) -> Optional[LlmResponse]:
    """Removes Parts with MIME types unsupported by Gemini from the history."""
    if not llm_request.contents:
        return None
    removed = 0
    for content in llm_request.contents:
        if not content.parts:
            continue
        original_len = len(content.parts)
        content.parts = [
            p for p in content.parts
            if not (p.inline_data is not None and p.inline_data.mime_type in _UNSUPPORTED_MIME_TYPES)
        ]
        removed += original_len - len(content.parts)
    if removed:
        logger.debug("strip_binary_parts: removed %d Parts from history", removed)
    return None


async def inject_document_artifacts(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Injects the generated artifacts as Part(inline_data) into the final response."""
    if llm_response.content and llm_response.content.parts:
        if any(p.function_call is not None for p in llm_response.content.parts):
            return None

    pending: list[str] = list(callback_context.state.get(_PENDING_KEY, []))
    if not pending:
        return None

    extra_parts: list[types.Part] = []
    for filename in pending:
        try:
            artifact = await callback_context.load_artifact(filename)
            if artifact is not None:
                extra_parts.append(artifact)
                logger.info("Injecting artifact into response: %s", filename)
            else:
                logger.warning("Artifact not found: %s", filename)
        except Exception as exc:
            logger.error("Failed to load artifact %s: %s", filename, exc)

    if not extra_parts:
        return None

    callback_context.state[_PENDING_KEY] = []
    existing_parts = list(llm_response.content.parts) if llm_response.content else []
    new_content    = types.Content(role="model", parts=existing_parts + extra_parts)
    return LlmResponse(content=new_content)


# ══════════════════════════════════════════════════════════════════════════════
# SUB-AGENTS
# ══════════════════════════════════════════════════════════════════════════════

search_agent = Agent(
    model=AGENT_MODEL,
    name="search_agent",
    description="Agent for searching content on the internet",
    instruction=(
        "You are an agent specialized in search. "
        "Your goal is to search for content on the internet "
        "based on the user's request."
    ),
    tools=[google_search],
)


# ══════════════════════════════════════════════════════════════════════════════
# ROOT AGENT
# ══════════════════════════════════════════════════════════════════════════════

root_agent = Agent(
    name="docs_agent",
    model=AGENT_MODEL,
    description="Agent for generating documents and charts from content found on the internet.",
    instruction=(
        "## CRITICAL RULE — No text before tool calls\n"
        "NEVER generate introductory or planning text before calling the tools.\n"
        "Examples of FORBIDDEN text: 'Sure, I'll search right away...', 'First...', "
        "'I'll start by searching...', 'Proceeding to search...'.\n"
        "Call the appropriate tool DIRECTLY with no preliminary text whatsoever.\n"
        "Response text must be produced ONLY at the end, after all tools have completed.\n\n"

        "## Goal\n"
        "Search for content on the internet and generate documents and charts based on it.\n"
        "Use the 'search_agent' agent tool to perform internet searches.\n\n"

        "## Available tools\n\n"

        "### Documents\n"
        "- 'generate_docx_report' — Word (.docx)\n"
        "- 'generate_xlsx_report' — Excel (.xlsx)\n"
        "- 'generate_md_report'   — Markdown (.md)\n"
        "- 'generate_pdf_report'  — PDF with text + embedded charts (use this\n"
        "  when the user wants a complete report with text and charts together)\n\n"

        "### Standalone charts\n"
        "- 'generate_chart_png'   — static PNG\n"
        "- 'generate_chart_html'  — interactive Plotly HTML (ideal for web)\n"
        "- 'generate_chart_pdf'   — PDF with just the chart\n\n"

        "### When to use generate_pdf_report vs standalone charts\n"
        "- Wants text + charts together → 'generate_pdf_report'\n"
        "- Wants only the chart          → 'generate_chart_png/html/pdf'\n"
        "- Wants tabular data            → 'generate_xlsx_report'\n"
        "- You can generate multiple files in the same response (e.g. xlsx + html)\n\n"

        "### sections structure (docx, md, pdf_report)\n"
        "  [{\"heading\": \"Title\", \"content\": \"text\" or [\"bullet1\",\"bullet2\"],\n"
        "    \"table\": {\"headers\": [...], \"rows\": [[...]]}}]\n\n"

        "### sheets structure (xlsx)\n"
        "  [{\"name\": \"SheetName\", \"headers\": [...], \"rows\": [[...]]}]\n\n"

        "### charts structure (only for generate_pdf_report)\n"
        "  [{\n"
        "    \"title\": \"Chart title\",\n"
        "    \"chart_type\": \"bar\",           # bar|line|area|pie|scatter\n"
        "    \"categories\": [\"2021\",\"2022\",...],\n"
        "    \"series\": [{\"name\": \"Revenue\", \"values\": [53.82, 81.46, ...]}],\n"
        "    \"x_label\": \"Year\",             # optional\n"
        "    \"y_label\": \"Billion USD\",      # optional\n"
        "    \"position\": 1                   # optional: inserts the chart after\n"
        "                                     # section N (0-based). Omit to append at the end.\n"
        "  }]\n\n"

        "### series structure (standalone charts)\n"
        "  Bar/line/area/pie: [{\"name\": \"Series A\", \"values\": [v1, v2, ...]}]\n"
        "  Scatter:           [{\"name\": \"Series A\", \"x\": [...], \"y\": [...]}]\n\n"

        "IMPORTANT: Never pass 'tool_context' — it is injected automatically by ADK."
    ),
    tools=[
        AgentTool(agent=search_agent),
        generate_docx_report,
        generate_xlsx_report,
        generate_md_report,
        generate_pdf_report,
        generate_chart_png,
        generate_chart_html,
        generate_chart_pdf,
    ],
    before_model_callback=strip_binary_parts,
    after_model_callback=inject_document_artifacts,
)