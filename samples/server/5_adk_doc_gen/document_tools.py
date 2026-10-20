"""
document_tools.py
-----------------------
Google ADK FunctionTools for on-demand generation of documents
(docx, xlsx, md, pdf) and charts (png, html, pdf) compatible with STLS A2A.

Dependencies: python-docx, openpyxl, matplotlib, plotly, reportlab.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime

from google.adk.tools import ToolContext
from google.genai import types

logger = logging.getLogger(__name__)

_PENDING_KEY = "temp:_pending_doc_artifacts"


# ══════════════════════════════════════════════════════════════════════════════
# DOCUMENT BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_docx(title: str, sections: list[dict]) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(11)

    h = doc.add_heading(title, level=0)
    h.runs[0].font.color.rgb = RGBColor(0x1F, 0x49, 0x7D)

    ts = doc.add_paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    ts.runs[0].font.size = Pt(9)
    ts.runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    doc.add_paragraph()

    for section in sections:
        heading_text = section.get("heading", "")
        content      = section.get("content", "")
        table_data   = section.get("table")

        if heading_text:
            sh = doc.add_heading(heading_text, level=2)
            sh.runs[0].font.color.rgb = RGBColor(0x2E, 0x74, 0xB5)

        if isinstance(content, str) and content.strip():
            doc.add_paragraph(content)
        elif isinstance(content, list):
            for item in content:
                doc.add_paragraph(str(item), style="List Bullet")

        if table_data:
            headers: list[str] = table_data.get("headers", [])
            rows: list[list]   = table_data.get("rows", [])
            if headers:
                table = doc.add_table(rows=1 + len(rows), cols=len(headers))
                table.style = "Light Grid Accent 1"
                for i, h_text in enumerate(headers):
                    cell = table.rows[0].cells[i]
                    cell.text = str(h_text)
                    cell.paragraphs[0].runs[0].bold = True
                for r_idx, row_data in enumerate(rows):
                    for c_idx, val in enumerate(row_data):
                        if c_idx < len(headers):
                            table.rows[r_idx + 1].cells[c_idx].text = str(val)

        doc.add_paragraph()

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_xlsx(title: str, sheets: list[dict]) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    hdr_font  = Font(name="Arial", bold=True, color="FFFFFF", size=11)
    hdr_fill  = PatternFill("solid", fgColor="1F497D")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_font = Font(name="Arial", size=10)
    thin      = Side(style="thin", color="CCCCCC")
    brd       = Border(left=thin, right=thin, top=thin, bottom=thin)

    for sheet_def in sheets:
        name    = sheet_def.get("name", "Sheet")[:31]
        headers = sheet_def.get("headers", [])
        rows    = sheet_def.get("rows", [])
        ws      = wb.create_sheet(title=name)

        ws.cell(row=1, column=1, value=title).font = Font(name="Arial", bold=True, size=13, color="1F497D")
        ws.cell(row=2, column=1, value=(
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )).font = Font(name="Arial", size=9, color="888888")

        for col_idx, h in enumerate(headers, 1):
            cell = ws.cell(row=4, column=col_idx, value=str(h))
            cell.font, cell.fill, cell.alignment = hdr_font, hdr_fill, hdr_align

        for r_idx, row_data in enumerate(rows, 5):
            for c_idx, val in enumerate(row_data, 1):
                if c_idx <= len(headers):
                    cell = ws.cell(row=r_idx, column=c_idx, value=val)
                    cell.font, cell.border = data_font, brd

        for col_idx, h in enumerate(headers, 1):
            max_len = max(
                [len(str(h))] +
                [len(str(r[col_idx - 1])) for r in rows if col_idx - 1 < len(r)]
            )
            ws.column_dimensions[
                ws.cell(row=4, column=col_idx).column_letter
            ].width = min(max_len + 4, 50)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _build_md(title: str, sections: list[dict]) -> bytes:
    lines = [
        f"# {title}", "",
        f"_Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}_", "",
    ]
    for section in sections:
        heading_text = section.get("heading", "")
        content      = section.get("content", "")
        table_data   = section.get("table")

        if heading_text:
            lines += [f"## {heading_text}", ""]
        if isinstance(content, str) and content.strip():
            lines += [content, ""]
        elif isinstance(content, list):
            lines += [f"- {item}" for item in content] + [""]
        if table_data:
            headers = table_data.get("headers", [])
            rows    = table_data.get("rows", [])
            if headers:
                lines.append("| " + " | ".join(str(h) for h in headers) + " |")
                lines.append("| " + " | ".join("---" for _ in headers) + " |")
                for row_data in rows:
                    cells = [str(v) for v in row_data]
                    while len(cells) < len(headers):
                        cells.append("")
                    lines.append("| " + " | ".join(cells[:len(headers)]) + " |")
                lines.append("")
    return "\n".join(lines).encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# CHART BUILDERS
# ══════════════════════════════════════════════════════════════════════════════

def _apply_matplotlib_chart(ax, chart_type: str, categories: list, series: list[dict]) -> None:
    """Draws the chart on the given matplotlib axis."""
    import numpy as np

    if chart_type == "pie":
        s = series[0] if series else {}
        ax.pie(s.get("values", []), labels=categories, autopct="%1.1f%%", startangle=90)
        ax.axis("equal")
        return

    n_series = max(len(series), 1)

    if chart_type == "bar":
        x = np.arange(len(categories))
        width = 0.8 / n_series
        for i, s in enumerate(series):
            offset = (i - (n_series - 1) / 2) * width
            ax.bar(x + offset, s.get("values", []), width, label=s.get("name", ""))
        ax.set_xticks(x)
        ax.set_xticklabels(categories, rotation=45, ha="right")

    elif chart_type in ("line", "area"):
        for s in series:
            y = s.get("values", [])
            ax.plot(categories, y, marker="o", label=s.get("name", ""))
            if chart_type == "area":
                ax.fill_between(range(len(y)), y, alpha=0.2)
        ax.set_xticks(range(len(categories)))
        ax.set_xticklabels(categories, rotation=45, ha="right")

    elif chart_type == "scatter":
        for s in series:
            ax.scatter(
                s.get("x", s.get("values", [])),
                s.get("y", []),
                label=s.get("name", ""),
            )

    if len(series) > 1 or (series and series[0].get("name")):
        ax.legend()


def _build_chart_png(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    x_label: str = "",
    y_label: str = "",
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("white")
    _apply_matplotlib_chart(ax, chart_type, categories, series)
    ax.set_title(title, fontsize=14, fontweight="bold", pad=15)
    if x_label:
        ax.set_xlabel(x_label, fontsize=11)
    if y_label:
        ax.set_ylabel(y_label, fontsize=11)
    plt.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _build_chart_html(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    x_label: str = "",
    y_label: str = "",
) -> bytes:
    import plotly.graph_objects as go

    fig = go.Figure()

    if chart_type == "pie" and series:
        s = series[0]
        fig.add_trace(go.Pie(labels=categories, values=s.get("values", []), name=s.get("name", ""), hole=0.3))
    elif chart_type == "bar":
        for s in series:
            fig.add_trace(go.Bar(x=categories, y=s.get("values", []), name=s.get("name", "")))
        fig.update_layout(barmode="group")
    elif chart_type == "line":
        for s in series:
            fig.add_trace(go.Scatter(x=categories, y=s.get("values", []), mode="lines+markers", name=s.get("name", "")))
    elif chart_type == "area":
        for s in series:
            fig.add_trace(go.Scatter(x=categories, y=s.get("values", []), fill="tozeroy", mode="lines", name=s.get("name", "")))
    elif chart_type == "scatter":
        for s in series:
            fig.add_trace(go.Scatter(x=s.get("x", s.get("values", [])), y=s.get("y", []), mode="markers", name=s.get("name", "")))

    fig.update_layout(
        title=dict(text=title, font=dict(size=18)),
        xaxis_title=x_label,
        yaxis_title=y_label,
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=-0.3),
        margin=dict(t=80, b=80),
    )
    return fig.to_html(include_plotlyjs="cdn", full_html=True).encode("utf-8")


def _build_chart_pdf(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    x_label: str = "",
    y_label: str = "",
) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    fig, ax = plt.subplots(figsize=(10, 6))
    fig.patch.set_facecolor("white")
    _apply_matplotlib_chart(ax, chart_type, categories, series)
    ax.set_title(title, fontsize=14, fontweight="bold", pad=15)
    if x_label:
        ax.set_xlabel(x_label, fontsize=11)
    if y_label:
        ax.set_ylabel(y_label, fontsize=11)
    fig.text(0.99, 0.01, f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
             ha="right", va="bottom", fontsize=8, color="gray")
    plt.tight_layout()
    buf = io.BytesIO()
    with PdfPages(buf) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# PDF REPORT BUILDER (text + charts on the same page)
# ══════════════════════════════════════════════════════════════════════════════

def _build_pdf_report(
    title: str,
    sections: list[dict],
    charts: list[dict],
) -> bytes:
    """
    Generates a PDF that combines text/table sections (reportlab) and
    charts embedded as PNG images (matplotlib).

    charts parameter — list of charts to include in the document:
      [
        {
          "title":      "Tesla Revenue",      # chart title (optional)
          "chart_type": "bar",                # bar | line | area | pie | scatter
          "categories": ["2021","2022",...],  # X axis or pie slices
          "series":     [{"name":"Revenue","values":[53.82,81.46,...]}],
          "x_label":    "Year",               # optional
          "y_label":    "Billion USD",        # optional
          "position":   "after_section_2"     # optional: after which section to insert
                                              # the chart (0-based). Default: at the end.
        }
      ]
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer,
        Table, TableStyle, Image, HRFlowable, KeepTogether,
    )
    from reportlab.lib.enums import TA_LEFT

    # ── Styles ─────────────────────────────────────────────────────────────
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "PdfTitle",
        parent=styles["Title"],
        fontSize=20,
        textColor=colors.HexColor("#1F497D"),
        spaceAfter=6,
    )
    meta_style = ParagraphStyle(
        "PdfMeta",
        parent=styles["Normal"],
        fontSize=9,
        textColor=colors.HexColor("#888888"),
        spaceAfter=16,
    )
    h2_style = ParagraphStyle(
        "PdfH2",
        parent=styles["Heading2"],
        fontSize=13,
        textColor=colors.HexColor("#2E74B5"),
        spaceBefore=14,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "PdfBody",
        parent=styles["Normal"],
        fontSize=11,
        leading=16,
        spaceAfter=6,
    )
    bullet_style = ParagraphStyle(
        "PdfBullet",
        parent=styles["Normal"],
        fontSize=11,
        leading=16,
        spaceAfter=4,
        leftIndent=18,
    )
    chart_caption_style = ParagraphStyle(
        "PdfCaption",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#444444"),
        alignment=1,   # center
        spaceAfter=4,
        spaceBefore=6,
    )

    # ── Helper: generates a chart PNG and returns a flowable Image ────────
    def _chart_flowable(chart_def: dict, width_cm: float = 15, height_cm: float = 9):
        png = _build_chart_png(
            title      = chart_def.get("title", ""),
            chart_type = chart_def.get("chart_type", "bar"),
            categories = chart_def.get("categories", []),
            series     = chart_def.get("series", []),
            x_label    = chart_def.get("x_label", ""),
            y_label    = chart_def.get("y_label", ""),
        )
        img_buf = io.BytesIO(png)
        return Image(img_buf, width=width_cm * cm, height=height_cm * cm)

    # ── Helper: builds the reportlab table ─────────────────────────────────
    def _rl_table(table_data: dict):
        headers = table_data.get("headers", [])
        rows    = table_data.get("rows", [])
        if not headers:
            return None
        data = [[str(h) for h in headers]] + [[str(v) for v in row] for row in rows]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  colors.HexColor("#1F497D")),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, 0),  11),
            ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
            ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
            ("FONTSIZE",      (0, 1), (-1, -1), 10),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2F7")]),
            ("GRID",          (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
            ("TOPPADDING",    (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING",   (0, 0), (-1, -1), 8),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
        ]))
        return t

    # ── Index charts by position ──────────────────────────────────────────
    # A chart with position=N is inserted right after section N (0-based).
    # Charts without a position (or with an invalid one) go at the end.
    charts_by_position: dict[int | None, list[dict]] = {}
    for chart_def in charts:
        pos = chart_def.get("position")
        if isinstance(pos, int):
            charts_by_position.setdefault(pos, []).append(chart_def)
        else:
            charts_by_position.setdefault(None, []).append(chart_def)

    def _append_charts_for_pos(story: list, pos):
        for cd in charts_by_position.get(pos, []):
            caption = cd.get("title", "")
            flowables = []
            if caption:
                flowables.append(Paragraph(caption, chart_caption_style))
            flowables.append(_chart_flowable(cd))
            flowables.append(Spacer(1, 12))
            story.append(KeepTogether(flowables))

    # ── Builds the reportlab story ─────────────────────────────────────────
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        rightMargin=2 * cm, leftMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=title,
    )

    story: list = []

    story.append(Paragraph(title, title_style))
    story.append(Paragraph(
        f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        meta_style,
    ))
    story.append(HRFlowable(
        width="100%", thickness=1,
        color=colors.HexColor("#1F497D"),
        spaceAfter=14,
    ))

    for idx, section in enumerate(sections):
        heading_text = section.get("heading", "")
        content      = section.get("content", "")
        table_data   = section.get("table")

        block = []
        if heading_text:
            block.append(Paragraph(heading_text, h2_style))
        if isinstance(content, str) and content.strip():
            block.append(Paragraph(content, body_style))
        elif isinstance(content, list):
            for item in content:
                block.append(Paragraph(f"• {str(item)}", bullet_style))
        if table_data:
            rl_t = _rl_table(table_data)
            if rl_t:
                block.append(Spacer(1, 6))
                block.append(rl_t)
                block.append(Spacer(1, 10))

        if block:
            story.append(KeepTogether(block))

        # Charts positioned after this section
        _append_charts_for_pos(story, idx)

    # Charts without a specific position → at the end
    _append_charts_for_pos(story, None)

    doc.build(story)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _save_and_track(
    tool_context: ToolContext,
    data: bytes,
    filename: str,
    mime_type: str,
) -> dict:
    part    = types.Part.from_bytes(data=data, mime_type=mime_type)
    version = await tool_context.save_artifact(filename=filename, artifact=part)

    pending: list[str] = list(tool_context.state.get(_PENDING_KEY, []))
    if filename not in pending:
        pending.append(filename)
    tool_context.state[_PENDING_KEY] = pending

    logger.info("Artifact saved: filename=%s version=%s size=%d", filename, version, len(data))
    return {"status": "ok", "filename": filename, "size_bytes": len(data), "version": version}


# ══════════════════════════════════════════════════════════════════════════════
# ADK FUNCTION TOOLS — DOCUMENTS
# ══════════════════════════════════════════════════════════════════════════════

async def generate_docx_report(
    title: str,
    sections: list[dict],
    tool_context: ToolContext,
) -> dict:
    """
    Generates a Word report (.docx) and attaches it automatically to the response.

    Args:
        title: Document title.
        sections: [{"heading": str, "content": str|list[str], "table": {"headers":[], "rows":[[]]}}]
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_docx(title, sections)
        filename = f"{title.replace(' ', '_')[:60]}.docx"
        mime     = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        return await _save_and_track(tool_context, data, filename, mime)
    except Exception as exc:
        logger.exception("generate_docx_report failed")
        return {"status": "error", "message": str(exc)}


async def generate_xlsx_report(
    title: str,
    sheets: list[dict],
    tool_context: ToolContext,
) -> dict:
    """
    Generates an Excel report (.xlsx) and attaches it automatically to the response.

    Args:
        title: Workbook title.
        sheets: [{"name": str, "headers": [...], "rows": [[...]]}]
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_xlsx(title, sheets)
        filename = f"{title.replace(' ', '_')[:60]}.xlsx"
        mime     = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return await _save_and_track(tool_context, data, filename, mime)
    except Exception as exc:
        logger.exception("generate_xlsx_report failed")
        return {"status": "error", "message": str(exc)}


async def generate_md_report(
    title: str,
    sections: list[dict],
    tool_context: ToolContext,
) -> dict:
    """
    Generates a Markdown report (.md) and attaches it automatically to the response.

    Args:
        title: Document title.
        sections: Same format as generate_docx_report.
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_md(title, sections)
        filename = f"{title.replace(' ', '_')[:60]}.md"
        return await _save_and_track(tool_context, data, filename, "text/markdown")
    except Exception as exc:
        logger.exception("generate_md_report failed")
        return {"status": "error", "message": str(exc)}


async def generate_pdf_report(
    title: str,
    sections: list[dict],
    charts: list[dict],
    tool_context: ToolContext,
) -> dict:
    """
    Generates a PDF report that combines text/table sections and embedded charts.
    Uses reportlab for the text layout and matplotlib for the charts.

    Args:
        title: Document title.
        sections: List of text/table sections:
            [{"heading": str, "content": str|list[str], "table": {"headers":[], "rows":[[]]}}]
        charts: List of charts to include:
            [{
                "title":      "Chart title",       # optional
                "chart_type": "bar",               # bar|line|area|pie|scatter
                "categories": ["2021","2022",...], # X axis or pie slices
                "series":     [{"name":"...", "values":[...]}],
                "x_label":    "Year",              # optional
                "y_label":    "Billion USD",       # optional
                "position":   1                    # optional: inserts the chart
                                                   # after section N (0-based).
                                                   # Omit to append at the end.
            }]
        tool_context: Injected automatically by ADK.

    Returns:
        {"status": "ok", "filename": "...", "size_bytes": N, "version": N}
    """
    try:
        data     = _build_pdf_report(title, sections, charts)
        filename = f"{title.replace(' ', '_')[:60]}.pdf"
        return await _save_and_track(tool_context, data, filename, "application/pdf")
    except Exception as exc:
        logger.exception("generate_pdf_report failed")
        return {"status": "error", "message": str(exc)}


# ══════════════════════════════════════════════════════════════════════════════
# ADK FUNCTION TOOLS — STANDALONE CHARTS
# ══════════════════════════════════════════════════════════════════════════════

async def generate_chart_png(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    tool_context: ToolContext,
    x_label: str = "",
    y_label: str = "",
) -> dict:
    """
    Generates a static PNG chart (matplotlib).

    Args:
        title: Chart title.
        chart_type: "bar" | "line" | "area" | "pie" | "scatter"
        categories: X axis labels (or pie slices).
        series: [{"name": "Series A", "values": [v1, v2, ...]}]
                For scatter: [{"name": "...", "x": [...], "y": [...]}]
        x_label, y_label: Axis labels (optional).
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_chart_png(title, chart_type, categories, series, x_label, y_label)
        filename = f"{title.replace(' ', '_')[:55]}.png"
        return await _save_and_track(tool_context, data, filename, "image/png")
    except Exception as exc:
        logger.exception("generate_chart_png failed")
        return {"status": "error", "message": str(exc)}


async def generate_chart_html(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    tool_context: ToolContext,
    x_label: str = "",
    y_label: str = "",
) -> dict:
    """
    Generates an interactive HTML/Plotly chart (zoom, tooltip, filters).
    Ideal for web applications.

    Args:
        title: Chart title.
        chart_type: "bar" | "line" | "area" | "pie" | "scatter"
        categories: X axis labels (or pie slices).
        series: [{"name": "Series A", "values": [v1, v2, ...]}]
                For scatter: [{"name": "...", "x": [...], "y": [...]}]
        x_label, y_label: Axis labels (optional).
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_chart_html(title, chart_type, categories, series, x_label, y_label)
        filename = f"{title.replace(' ', '_')[:55]}.html"
        return await _save_and_track(tool_context, data, filename, "text/html")
    except Exception as exc:
        logger.exception("generate_chart_html failed")
        return {"status": "error", "message": str(exc)}


async def generate_chart_pdf(
    title: str,
    chart_type: str,
    categories: list[str],
    series: list[dict],
    tool_context: ToolContext,
    x_label: str = "",
    y_label: str = "",
) -> dict:
    """
    Generates a standalone PDF chart (matplotlib). Ideal for printable reports.

    Args:
        title: Chart title.
        chart_type: "bar" | "line" | "area" | "pie" | "scatter"
        categories: X axis labels (or pie slices).
        series: [{"name": "Series A", "values": [v1, v2, ...]}]
                For scatter: [{"name": "...", "x": [...], "y": [...]}]
        x_label, y_label: Axis labels (optional).
        tool_context: Injected automatically by ADK.
    """
    try:
        data     = _build_chart_pdf(title, chart_type, categories, series, x_label, y_label)
        filename = f"{title.replace(' ', '_')[:55]}.pdf"
        return await _save_and_track(tool_context, data, filename, "application/pdf")
    except Exception as exc:
        logger.exception("generate_chart_pdf failed")
        return {"status": "error", "message": str(exc)}