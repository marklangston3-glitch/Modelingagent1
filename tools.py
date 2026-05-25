"""
tools.py – Claude tool definitions and dispatch for the EDGAR modeling agent.

Each tool is declared as an Anthropic-style tool schema and backed by a
Python handler function.  The `dispatch(name, inputs)` function routes a
tool call returned by the model to the correct handler.
"""

from __future__ import annotations

import json
import openpyxl
from io import BytesIO
from pathlib import Path

import edgar


# ---------------------------------------------------------------------------
# Tool schemas (passed to the Anthropic client)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_company_submissions",
        "description": (
            "Fetch SEC EDGAR submissions metadata for a company by CIK number. "
            "Returns company name, ticker, SIC code, and recent filing history."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cik": {
                    "type": "string",
                    "description": "The company's SEC CIK number (digits only, leading zeros optional).",
                },
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_recent_filings",
        "description": (
            "Return the most recent SEC filings of a given form type (e.g. 10-K, 10-Q, 8-K) "
            "for a company identified by CIK."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cik":       {"type": "string", "description": "SEC CIK number."},
                "form_type": {"type": "string", "description": "Filing form type, e.g. '10-K'."},
                "limit":     {"type": "integer", "description": "Max number of filings to return (default 5)."},
            },
            "required": ["cik"],
        },
    },
    {
        "name": "get_concept_values",
        "description": (
            "Retrieve the historical time-series values for a specific XBRL financial concept "
            "(e.g. 'Revenues', 'NetIncomeLoss', 'Assets') for a company identified by CIK."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cik":      {"type": "string",  "description": "SEC CIK number."},
                "concept":  {"type": "string",  "description": "XBRL concept name, e.g. 'Revenues'."},
                "taxonomy": {"type": "string",  "description": "Taxonomy namespace (default: 'us-gaap')."},
            },
            "required": ["cik", "concept"],
        },
    },
    {
        "name": "search_company_by_name",
        "description": "Search SEC EDGAR for companies whose name contains the given string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Partial or full company name to search for."},
                "hits": {"type": "integer", "description": "Max number of results (default 10)."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "export_to_excel",
        "description": (
            "Write a list of records (list of dicts) to an Excel (.xlsx) file. "
            "Returns the file path of the saved workbook."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "records":    {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "List of dicts to write as rows. Keys become column headers.",
                },
                "sheet_name": {"type": "string",  "description": "Worksheet name (default: 'Sheet1')."},
                "file_path":  {"type": "string",  "description": "Output file path (default: 'output.xlsx')."},
            },
            "required": ["records"],
        },
    },
]


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def handle_get_company_submissions(cik: str) -> dict:
    data = edgar.get_company_submissions(cik)
    # Trim down to a readable summary so we don't flood the context
    return {
        "cik":         data.get("cik"),
        "name":        data.get("name"),
        "ticker":      data.get("tickers"),
        "sic":         data.get("sic"),
        "sicDescription": data.get("sicDescription"),
        "stateOfIncorporation": data.get("stateOfIncorporation"),
        "fiscalYearEnd": data.get("fiscalYearEnd"),
    }


def handle_get_recent_filings(cik: str, form_type: str = "10-K", limit: int = 5) -> list[dict]:
    return edgar.get_recent_filings(cik, form_type=form_type, limit=limit)


def handle_get_concept_values(cik: str, concept: str, taxonomy: str = "us-gaap") -> list[dict]:
    rows = edgar.get_concept_values(cik, concept, taxonomy=taxonomy)
    # Return only annual (10-K) rows, sorted newest first, capped at 20
    annual = [r for r in rows if r.get("form") in ("10-K", "10-K/A")]
    annual.sort(key=lambda r: r.get("end", ""), reverse=True)
    return annual[:20]


def handle_search_company_by_name(name: str, hits: int = 10) -> list[dict]:
    return edgar.search_company_by_name(name, hits=hits)


def handle_export_to_excel(
    records: list[dict],
    sheet_name: str = "Sheet1",
    file_path: str = "output.xlsx",
) -> dict:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name

    if not records:
        wb.save(file_path)
        return {"file_path": file_path, "rows_written": 0}

    headers = list(records[0].keys())
    ws.append(headers)
    for record in records:
        ws.append([record.get(h) for h in headers])

    # Auto-fit column widths (approximate)
    for col in ws.columns:
        max_len = max((len(str(cell.value or "")) for cell in col), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 60)

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return {"file_path": str(path.resolve()), "rows_written": len(records)}


# ---------------------------------------------------------------------------
# Dispatch router
# ---------------------------------------------------------------------------

_HANDLERS = {
    "get_company_submissions": lambda i: handle_get_company_submissions(**i),
    "get_recent_filings":      lambda i: handle_get_recent_filings(**i),
    "get_concept_values":      lambda i: handle_get_concept_values(**i),
    "search_company_by_name":  lambda i: handle_search_company_by_name(**i),
    "export_to_excel":         lambda i: handle_export_to_excel(**i),
}


def dispatch(tool_name: str, tool_inputs: dict) -> str:
    """
    Execute the named tool with the given inputs.

    Returns a JSON string suitable for sending back as a tool_result block.
    """
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return json.dumps({"error": f"Unknown tool: {tool_name}"})
    try:
        result = handler(tool_inputs)
        return json.dumps(result, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})
