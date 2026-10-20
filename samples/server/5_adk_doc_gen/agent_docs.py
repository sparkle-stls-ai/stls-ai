"""
agent_docs.py
-------------
Agent con sub-agent specializzati e tool per la generazione on-demand
di documenti (docx, xlsx, md, pdf) e grafici (png, html, pdf).

MIME type e compatibilità Gemini (input history):
  ✅ image/png, application/pdf  → accettati, niente strip
  ❌ docx, xlsx, text/html       → non accettati, strippati dalla history
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

# Modello LLM passato dall'esterno (variabile d'ambiente AGENT_MODEL).
# Il default mantiene il modello usato finora se la variabile non e' impostata.
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
    """Rimuove dalla history i Part con MIME type non supportati da Gemini."""
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
        logger.debug("strip_binary_parts: rimossi %d Part dalla history", removed)
    return None


async def inject_document_artifacts(
    callback_context: CallbackContext,
    llm_response: LlmResponse,
) -> Optional[LlmResponse]:
    """Inietta gli artifact generati come Part(inline_data) nella risposta finale."""
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
    description="Agente per ricerche di contenuti su internet",
    instruction=(
        "Sei un agente specializzato nella ricerca. "
        "Il tuo obiettivo e' quello di cercare contenuti su internet "
        "in base alla richiesta dell'utente."
    ),
    tools=[google_search],
)


# ══════════════════════════════════════════════════════════════════════════════
# ROOT AGENT
# ══════════════════════════════════════════════════════════════════════════════

root_agent = Agent(
    name="docs_agent",
    model=AGENT_MODEL,
    description="Agente per la generazione di documenti e grafici con contenuti trovati su internet.",
    instruction=(
        "## REGOLA CRITICA — Nessun testo prima dei tool call\n"
        "NON generare MAI testo introduttivo o di pianificazione prima di chiamare i tool.\n"
        "Esempi di testo VIETATO: 'Certo, cerco subito...', 'Per prima cosa...', "
        "'Inizierò cercando...', 'Procedo a cercare...'.\n"
        "Chiama DIRETTAMENTE il tool appropriato senza nessun testo preliminare.\n"
        "Il testo di risposta va prodotto SOLO alla fine, dopo che tutti i tool hanno completato.\n\n"

        "## Obiettivo\n"
        "Cercare contenuti su internet e generare documenti e grafici basati su di essi.\n"
        "Usa il tool agente 'search_agent' per effettuare ricerche su internet.\n\n"

        "## Tool disponibili\n\n"

        "### Documenti\n"
        "- 'generate_docx_report' — Word (.docx)\n"
        "- 'generate_xlsx_report' — Excel (.xlsx)\n"
        "- 'generate_md_report'   — Markdown (.md)\n"
        "- 'generate_pdf_report'  — PDF con testo + grafici embedded (usa questo\n"
        "  quando l'utente vuole un report completo con testo e grafici insieme)\n\n"

        "### Grafici standalone\n"
        "- 'generate_chart_png'   — PNG statico\n"
        "- 'generate_chart_html'  — HTML interattivo Plotly (ideale per web)\n"
        "- 'generate_chart_pdf'   — PDF con solo il grafico\n\n"

        "### Quando usare generate_pdf_report vs grafici standalone\n"
        "- Vuole testo + grafici insieme → 'generate_pdf_report'\n"
        "- Vuole solo il grafico         → 'generate_chart_png/html/pdf'\n"
        "- Vuole dati tabellari          → 'generate_xlsx_report'\n"
        "- Puoi generare più file nella stessa risposta (es. xlsx + html)\n\n"

        "### Struttura sections (docx, md, pdf_report)\n"
        "  [{\"heading\": \"Titolo\", \"content\": \"testo\" o [\"bullet1\",\"bullet2\"],\n"
        "    \"table\": {\"headers\": [...], \"rows\": [[...]]}}]\n\n"

        "### Struttura sheets (xlsx)\n"
        "  [{\"name\": \"NomeFoglio\", \"headers\": [...], \"rows\": [[...]]}]\n\n"

        "### Struttura charts (solo per generate_pdf_report)\n"
        "  [{\n"
        "    \"title\": \"Titolo grafico\",\n"
        "    \"chart_type\": \"bar\",           # bar|line|area|pie|scatter\n"
        "    \"categories\": [\"2021\",\"2022\",...],\n"
        "    \"series\": [{\"name\": \"Ricavi\", \"values\": [53.82, 81.46, ...]}],\n"
        "    \"x_label\": \"Anno\",             # opzionale\n"
        "    \"y_label\": \"Miliardi USD\",     # opzionale\n"
        "    \"position\": 1                   # opzionale: inserisce il grafico dopo\n"
        "                                     # la sezione N (0-based). Ometti per coda.\n"
        "  }]\n\n"

        "### Struttura series (grafici standalone)\n"
        "  Bar/line/area/pie: [{\"name\": \"Serie A\", \"values\": [v1, v2, ...]}]\n"
        "  Scatter:           [{\"name\": \"Serie A\", \"x\": [...], \"y\": [...]}]\n\n"

        "IMPORTANTE: Non passare mai 'tool_context' — viene iniettato automaticamente da ADK."
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