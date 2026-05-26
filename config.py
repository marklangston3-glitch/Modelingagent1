#!/usr/bin/env python3
"""
config.py — Centralized configuration for Langston's Financial Intelligence system.

All scripts (morning_report.py, watchdog.py, build_dcf.py) import from here.
Never hardcode firm name, email, API keys, or ticker lists in individual scripts.
"""

import os
from pathlib import Path

# ── Firm Identity ─────────────────────────────────────────────────────────────
FIRM_NAME      = "Langston's"
FIRM_NAME_U    = FIRM_NAME.upper()                        # header band text
FIRM_NAME_FULL = "Langston's Financial Intelligence"
EMAIL          = "marklangston3@gmail.com"

# ── Email Recipients (all report and alert emails go to every address) ────────
RECIPIENTS: list[str] = [
    "marklangston3@gmail.com",
    "Langstonroy@aol.com",
]

# ── Repository ────────────────────────────────────────────────────────────────
REPO_DIR   = Path(__file__).parent.resolve()
BRANCH     = "claude/agent-tools-edgar-setup-PimAK"
GIT_REMOTE = "origin"

# ── Claude Model ──────────────────────────────────────────────────────────────
ANTHROPIC_MODEL = "claude-opus-4-7"


# ── API Key Accessors ─────────────────────────────────────────────────────────
def get_anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")
    return key


# ── Known CIKs (fallback when EDGAR lookup fails) ────────────────────────────
_KNOWN_CIKS: dict[str, str] = {
    "TEM":  "0001717115",   # Tempus AI
    "RGTI": "0001838359",   # Rigetti Computing
    "BBAI": "0001836981",   # BigBear.ai
}

# ── Known Company Metadata ────────────────────────────────────────────────────
_TICKER_META: dict[str, dict] = {
    "TEM":  {"name": "Tempus AI, Inc.",          "exch": "NASDAQ"},
    "RGTI": {"name": "Rigetti Computing, Inc.",   "exch": "NASDAQ"},
    "BBAI": {"name": "BigBear.ai Holdings, Inc.", "exch": "NYSE"},
}

# ── Tickers File ──────────────────────────────────────────────────────────────
TICKERS_FILE = REPO_DIR / "tickers.txt"


def load_tickers() -> dict[str, dict]:
    """
    Load tickers from tickers.txt and enrich with metadata.

    Returns:
        dict keyed by TICKER symbol:
        {
            "TEM": {"name": "Tempus AI, Inc.", "exch": "NASDAQ", "cik": "0001717115"},
            ...
        }

    Tickers.txt format:
        # comment lines are ignored
        TEM
        RGTI
        BBAI

    Falls back to _TICKER_META defaults if file is missing or empty.
    """
    tickers: dict[str, dict] = {}

    if TICKERS_FILE.exists():
        for raw_line in TICKERS_FILE.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # First whitespace-delimited token is the ticker symbol
            tk = line.split()[0].upper()
            meta = dict(_TICKER_META.get(tk, {"name": tk, "exch": "N/A"}))
            meta["cik"] = _KNOWN_CIKS.get(tk, "")
            tickers[tk] = meta

    if not tickers:
        # Fallback: use hard-coded defaults
        for tk, meta in _TICKER_META.items():
            tickers[tk] = dict(meta)
            tickers[tk]["cik"] = _KNOWN_CIKS.get(tk, "")

    return tickers


# ── Sector ETF Universe (used by morning_report.py) ──────────────────────────
SECTOR_ETFS: dict[str, str] = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLY":  "Cons. Disc.",
    "XLE":  "Energy",
    "XLI":  "Industrials",
    "XLU":  "Utilities",
    "XLP":  "Cons. Staples",
    "XLRE": "Real Estate",
    "XLB":  "Materials",
    "XLC":  "Comm. Svcs",
}

MACRO_RATE_TICKERS: dict[str, str] = {
    "^TNX":  "10Y Yield",
    "^FVX":  "5Y Yield",
    "^IRX":  "3M T-Bill",
    "^VIX":  "VIX",
    "GLD":   "Gold ETF",
    "^DXY":  "USD Index",
}
