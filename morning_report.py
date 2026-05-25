#!/usr/bin/env python3
"""
morning_report.py — Daily pre-market intelligence report

Runs at 7:15am EST (12:15 UTC), after watchdog.py (12:00 UTC).

For each watched ticker (TEM, RGTI, BBAI):
  1. Checks watchdog state for overnight SEC filings
  2. Pulls price movement + news via yfinance
  3. Generates written analysis via Claude API (claude-opus-4-7)
  4. Compiles into reports/morning_report_YYYY-MM-DD.pdf
  5. Pushes the PDF to the repo

Usage
-----
  python morning_report.py                   # full run, all tickers
  python morning_report.py --ticker TEM      # single ticker
  python morning_report.py --no-push         # build PDF, skip git push
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import yfinance as yf
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether,
)

# ── Config ────────────────────────────────────────────────────────────────────
TICKERS = {
    "TEM":  {"name": "Tempus AI",         "cik": "0001717115"},
    "RGTI": {"name": "Rigetti Computing",  "cik": "0001838359"},
    "BBAI": {"name": "BigBear.ai",         "cik": "0001836981"},
}

REPO_DIR     = Path(__file__).parent.resolve()
STATE_FILE   = REPO_DIR / ".watchdog_state.json"
REPORTS_DIR  = REPO_DIR / "reports"
LOG_FILE     = REPO_DIR / "morning_report.log"
BRANCH       = "claude/agent-tools-edgar-setup-PimAK"
GIT_REMOTE   = "origin"
MODEL        = "claude-opus-4-7"

# ── Colour palette ────────────────────────────────────────────────────────────
C_NAVY  = HexColor("#0A2342")
C_BLUE  = HexColor("#1F5C99")
C_GREEN = HexColor("#1E8449")
C_RED   = HexColor("#C0392B")
C_LGREY = HexColor("#F5F7FA")
C_MGREY = HexColor("#CCCCCC")
C_DGREY = HexColor("#555555")
C_WHITE = colors.white
C_BLACK = colors.black

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("morning_report")


# ════════════════════════════════════════════════════════════════════════════
#  DATA COLLECTION
# ════════════════════════════════════════════════════════════════════════════

def load_watchdog_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def get_overnight_filings(ticker: str, state: dict) -> list[dict]:
    """Return any filings recorded by watchdog in the last 36 hours."""
    ts      = state.get(ticker, {})
    filings = ts.get("last_filings", [])
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=36)
    recent  = []
    for f in filings:
        try:
            d = datetime.fromisoformat(f.get("date", "")[:10]).replace(tzinfo=timezone.utc)
            if d >= cutoff:
                recent.append(f)
        except (ValueError, TypeError):
            pass
    return recent


def _parse_news_item(item: dict) -> dict:
    """Normalise a yfinance news item (handles both old and new API formats)."""
    content = item.get("content", {})
    if isinstance(content, dict) and content.get("title"):
        title = content["title"]
        summary = content.get("summary", "")[:200]
        pub   = (content.get("provider") or {}).get("displayName", "Unknown")
        raw_t = content.get("pubDate", "")
        t_str = raw_t[:16].replace("T", " ") if raw_t else ""
    else:
        title   = item.get("title") or item.get("headline") or "(no title)"
        summary = ""
        pub     = item.get("publisher", "Unknown")
        raw_t   = item.get("providerPublishTime", 0)
        if isinstance(raw_t, (int, float)) and raw_t > 1e9:
            t_str = datetime.fromtimestamp(raw_t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        else:
            t_str = str(raw_t)[:16] if raw_t else ""
    return {"title": title, "summary": summary, "publisher": pub, "time": t_str}


def get_price_data(ticker: str) -> dict:
    """Fetch current price metrics and 5-day history via yfinance."""
    try:
        yt   = yf.Ticker(ticker)
        info = yt.info or {}
        hist = yt.history(period="5d")

        price      = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        prev_close = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or 0)
        chg_pct    = (price - prev_close) / prev_close * 100 if prev_close else 0.0

        price_history = []
        if not hist.empty:
            for idx, row in hist.tail(5).iterrows():
                price_history.append({
                    "date":  idx.strftime("%Y-%m-%d"),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]),
                })

        return {
            "price":         round(price, 2),
            "prev_close":    round(prev_close, 2),
            "change_pct":    round(chg_pct, 2),
            "day_high":      round(float(info.get("dayHigh") or info.get("regularMarketDayHigh") or price), 2),
            "day_low":       round(float(info.get("dayLow") or info.get("regularMarketDayLow") or price), 2),
            "wk52_high":     round(float(info.get("fiftyTwoWeekHigh") or 0), 2),
            "wk52_low":      round(float(info.get("fiftyTwoWeekLow") or 0), 2),
            "market_cap":    int(info.get("marketCap") or 0),
            "volume":        int(info.get("volume") or info.get("regularMarketVolume") or 0),
            "avg_volume":    int(info.get("averageVolume") or 0),
            "pe_ratio":      info.get("trailingPE"),
            "beta":          info.get("beta"),
            "short_name":    info.get("shortName", ticker),
            "price_history": price_history,
        }
    except Exception as exc:
        log.warning("yfinance error for %s: %s", ticker, exc)
        return {
            "price": 0.0, "prev_close": 0.0, "change_pct": 0.0,
            "day_high": 0.0, "day_low": 0.0, "wk52_high": 0.0, "wk52_low": 0.0,
            "market_cap": 0, "volume": 0, "avg_volume": 0,
            "pe_ratio": None, "beta": None, "short_name": ticker,
            "price_history": [],
        }


def get_news(ticker: str, max_items: int = 8) -> list[dict]:
    """Fetch and normalise recent news headlines via yfinance."""
    try:
        raw = yf.Ticker(ticker).news or []
        return [_parse_news_item(item) for item in raw[:max_items]]
    except Exception as exc:
        log.warning("News fetch error for %s: %s", ticker, exc)
        return []


def fmt_cap(val: int) -> str:
    if val >= 1e12: return f"${val/1e12:.2f}T"
    if val >= 1e9:  return f"${val/1e9:.2f}B"
    if val >= 1e6:  return f"${val/1e6:.0f}M"
    return f"${val:,}" if val else "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CLAUDE ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

SECTIONS = ["OVERNIGHT_SUMMARY", "INVESTMENT_THESIS", "TECHNICAL_OUTLOOK", "CORRELATIONS"]

def generate_analysis(
    ticker: str,
    company_name: str,
    price_data: dict,
    news: list[dict],
    filings: list[dict],
    client: anthropic.Anthropic,
) -> dict[str, str]:
    """Return a dict of {section_key: text} from a single Claude call."""

    price  = price_data["price"]
    chg    = price_data["change_pct"]
    cap    = fmt_cap(price_data["market_cap"])
    hi52   = price_data["wk52_high"]
    lo52   = price_data["wk52_low"]
    beta   = price_data.get("beta")

    history_lines = "\n".join(
        f"  {h['date']}: ${h['close']} (vol {h['volume']:,})"
        for h in price_data.get("price_history", [])
    ) or "  (no history)"

    news_lines = "\n".join(
        f"  [{n['time']}] {n['publisher']}: {n['title']}"
        + (f"\n    {n['summary']}" if n.get("summary") else "")
        for n in news[:6]
    ) or "  No news available."

    filing_lines = "\n".join(
        f"  🚨 {f['form']} filed {f['date']}: {f.get('title','')}"
        for f in filings
    ) or "  No new SEC filings in the last 36 hours."

    prompt = f"""You are a professional equity analyst writing a pre-market morning intelligence brief.
Be specific, data-driven, and forward-looking. Reference concrete numbers. Avoid filler phrases.
Today: {datetime.now(timezone.utc).strftime("%A, %B %d, %Y")} (pre-market)

═══════════════════════════════════════════════
TICKER: {ticker} — {company_name}
═══════════════════════════════════════════════
MARKET DATA:
  Price:      ${price:.2f}  ({chg:+.2f}%)
  Market cap: {cap}
  52-wk range: ${lo52} – ${hi52}  (currently at {((price - lo52) / (hi52 - lo52) * 100) if hi52 > lo52 else 0:.0f}% of range)
  Beta: {beta if beta else 'N/A'}

RECENT PRICE ACTION (5 sessions):
{history_lines}

OVERNIGHT NEWS:
{news_lines}

SEC FILINGS (last 36 hours):
{filing_lines}

Write exactly four sections using these labels (each on its own line, followed by content):

OVERNIGHT_SUMMARY:
Write 3–4 sentences. What happened specifically overnight/this morning? If no material catalyst, say so clearly and describe where the stock sits technically. Include any relevant pre-market observations implied by the data above.

INVESTMENT_THESIS:
Write 4–5 sentences. What is the core bull case for {ticker}? What specific revenue drivers, milestones, or catalysts could trigger a re-rating? What is the primary bear case risk? Be specific to {company_name}'s actual business model and competitive position.

TECHNICAL_OUTLOOK:
Write 3–4 sentences. Based on the 52-week range and recent price action, where could the stock go from ${price:.2f}? Identify specific price targets with reasoning (e.g. % to 52-wk high, key support/resistance levels). Characterise the current trend (momentum, mean reversion, consolidation, breakout potential).

CORRELATIONS:
Write 3–4 sentences. Name 3–5 specific publicly-traded stocks that historically correlate with {ticker} (by sector, theme, or business model) and explain why each is relevant. Then name 2 sectors being tailwind-beneficiaries of the trends driving {ticker}, and 1–2 sectors facing headwinds from those same trends."""

    log.info("  Calling Claude (%s) for %s analysis …", MODEL, ticker)
    msg = client.messages.create(
        model=MODEL,
        max_tokens=1400,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text

    # Parse sections by scanning for markers
    result = {}
    for i, label in enumerate(SECTIONS):
        marker = label + ":"
        start  = raw.find(marker)
        if start == -1:
            result[label] = "(Analysis unavailable.)"
            continue
        text_start = start + len(marker)
        end = len(raw)
        for other in SECTIONS[i + 1:]:
            pos = raw.find(other + ":", text_start)
            if 0 < pos < end:
                end = pos
        result[label] = raw[text_start:end].strip()

    return result


# ════════════════════════════════════════════════════════════════════════════
#  PDF BUILDER
# ════════════════════════════════════════════════════════════════════════════

def _make_styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "report_title": ParagraphStyle(
            "ReportTitle", parent=base["Normal"],
            fontSize=26, leading=30, fontName="Helvetica-Bold",
            textColor=C_WHITE, alignment=TA_CENTER,
        ),
        "report_date": ParagraphStyle(
            "ReportDate", parent=base["Normal"],
            fontSize=11, leading=14,
            textColor=HexColor("#A8C8F0"), alignment=TA_CENTER,
        ),
        "ticker_name": ParagraphStyle(
            "TickerName", parent=base["Normal"],
            fontSize=17, leading=21, fontName="Helvetica-Bold",
            textColor=C_WHITE,
        ),
        "ticker_price": ParagraphStyle(
            "TickerPrice", parent=base["Normal"],
            fontSize=15, leading=19, fontName="Helvetica-Bold",
            textColor=C_WHITE, alignment=TA_RIGHT,
        ),
        "section_hdr": ParagraphStyle(
            "SectionHdr", parent=base["Normal"],
            fontSize=9, leading=11, fontName="Helvetica-Bold",
            textColor=C_BLUE, spaceBefore=10, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["Normal"],
            fontSize=9.5, leading=14.5,
            textColor=C_BLACK, spaceAfter=5,
        ),
        "news_item": ParagraphStyle(
            "NewsItem", parent=base["Normal"],
            fontSize=8.5, leading=12,
            textColor=HexColor("#2c2c2c"),
            leftIndent=6, spaceAfter=4,
        ),
        "table_hdr": ParagraphStyle(
            "TableHdr", parent=base["Normal"],
            fontSize=8.5, fontName="Helvetica-Bold",
            textColor=C_WHITE, alignment=TA_CENTER,
        ),
        "table_cell": ParagraphStyle(
            "TableCell", parent=base["Normal"],
            fontSize=8.5, textColor=C_BLACK, alignment=TA_CENTER,
        ),
        "disclaimer": ParagraphStyle(
            "Disclaimer", parent=base["Normal"],
            fontSize=7, leading=9,
            textColor=HexColor("#999999"), alignment=TA_CENTER,
        ),
        "filing_alert": ParagraphStyle(
            "FilingAlert", parent=base["Normal"],
            fontSize=9, leading=13, fontName="Helvetica-Bold",
            textColor=C_WHITE, leftIndent=8,
        ),
    }


def _cover_page(elements: list, date_str: str, summaries: list[dict], st: dict):
    """Append cover page elements."""
    W = 7.0 * inch  # content width

    # ── Masthead ──────────────────────────────────────────────────────────────
    mast = Table(
        [[Paragraph("MORNING INTELLIGENCE REPORT", st["report_title"])],
         [Paragraph(date_str, st["report_date"])]],
        colWidths=[W],
    )
    mast.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), C_NAVY),
        ("TOPPADDING",    (0,0), (-1,0),  28),
        ("BOTTOMPADDING", (0,0), (-1,0),  6),
        ("TOPPADDING",    (0,1), (-1,1),  4),
        ("BOTTOMPADDING", (0,1), (-1,1),  28),
        ("LEFTPADDING",   (0,0), (-1,-1), 12),
        ("RIGHTPADDING",  (0,0), (-1,-1), 12),
    ]))
    elements.append(mast)
    elements.append(Spacer(1, 0.22 * inch))

    # ── Snapshot table ────────────────────────────────────────────────────────
    col_w = [0.70, 1.85, 0.90, 0.90, 1.05, 1.60]
    col_w = [w * inch for w in col_w]  # total ~7.0"

    rows = [["Ticker", "Company", "Price", "Change", "Mkt Cap", "Overnight Status"]]
    for s in summaries:
        chg = s["change_pct"]
        rows.append([
            s["ticker"],
            s["company"],
            f"${s['price']:.2f}",
            f"{'▲' if chg >= 0 else '▼'} {abs(chg):.2f}%",
            fmt_cap(s["market_cap"]),
            "🚨 New Filing" if s["has_filings"] else ("📰 News" if s["news_count"] else "— Quiet"),
        ])

    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    style_cmds = [
        ("FONTNAME",      (0,0),  (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),  (-1,-1), 9),
        ("BACKGROUND",    (0,0),  (-1,0),  C_NAVY),
        ("TEXTCOLOR",     (0,0),  (-1,0),  C_WHITE),
        ("ALIGN",         (0,0),  (-1,-1), "CENTER"),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),  (-1,-1), 7),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 7),
        ("GRID",          (0,0),  (-1,-1), 0.5, C_MGREY),
    ]
    for i in range(1, len(rows)):
        bg = C_LGREY if i % 2 == 1 else C_WHITE
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
        chg = summaries[i - 1]["change_pct"]
        col = C_GREEN if chg >= 0 else C_RED
        style_cmds.append(("TEXTCOLOR",  (3, i), (3, i), col))
        style_cmds.append(("FONTNAME",   (3, i), (3, i), "Helvetica-Bold"))

    tbl.setStyle(TableStyle(style_cmds))
    elements.append(tbl)
    elements.append(Spacer(1, 0.18 * inch))

    elements.append(Paragraph(
        "Auto-generated using SEC EDGAR filings, yfinance market data, and AI analysis (claude-opus-4-7). "
        "For informational purposes only — not investment advice.",
        st["disclaimer"],
    ))
    elements.append(PageBreak())


def _ticker_header(ticker: str, company: str, price_data: dict, st: dict) -> Table:
    """Navy header bar with ticker name left, price right."""
    price = price_data["price"]
    chg   = price_data["change_pct"]
    sign  = "▲" if chg >= 0 else "▼"
    chg_c = "#4EC44E" if chg >= 0 else "#FF6B6B"

    name_p  = Paragraph(f"{ticker}  &nbsp; <font size='11' color='#A8C8F0'>{company}</font>",
                        st["ticker_name"])
    price_p = Paragraph(
        f'<font size="18">${price:.2f}</font>  '
        f'<font color="{chg_c}">{sign}&nbsp;{abs(chg):.2f}%</font>',
        st["ticker_price"],
    )

    tbl = Table([[name_p, price_p]], colWidths=[4.0 * inch, 3.0 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), C_NAVY),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0), (0,0),   12),
        ("RIGHTPADDING",  (1,0), (1,0),   12),
        ("TOPPADDING",    (0,0), (-1,-1), 11),
        ("BOTTOMPADDING", (0,0), (-1,-1), 11),
    ]))
    return tbl


def _metrics_row(price_data: dict) -> Table:
    """Compact 2-row key metrics table."""
    p     = price_data
    beta  = f"{p['beta']:.2f}" if p.get("beta") else "N/A"
    vol   = f"{p['volume']:,}"   if p["volume"]     else "N/A"
    avol  = f"{p['avg_volume']:,}" if p["avg_volume"] else "N/A"

    col_w = [1.167 * inch] * 6   # 7.0" total
    data  = [
        ["52-Wk High", "52-Wk Low", "Day High", "Day Low", "Mkt Cap", "Beta"],
        [f"${p['wk52_high']:.2f}", f"${p['wk52_low']:.2f}",
         f"${p['day_high']:.2f}",  f"${p['day_low']:.2f}",
         fmt_cap(p["market_cap"]), beta],
        ["Volume", "Avg Volume", "Prev Close", "", "", ""],
        [vol, avol, f"${p['prev_close']:.2f}", "", "", ""],
    ]

    tbl = Table(data, colWidths=col_w)
    hdr_rows = [0, 2]
    tbl.setStyle(TableStyle(
        [
            ("FONTSIZE",      (0,0), (-1,-1), 8),
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("GRID",          (0,0), (-1,-1), 0.5, C_MGREY),
        ]
        + [("BACKGROUND",  (0,r), (-1,r), C_BLUE)   for r in hdr_rows]
        + [("TEXTCOLOR",   (0,r), (-1,r), C_WHITE)  for r in hdr_rows]
        + [("FONTNAME",    (0,r), (-1,r), "Helvetica-Bold") for r in hdr_rows]
        + [("BACKGROUND",  (0,r), (-1,r), C_LGREY)  for r in [1, 3]]
    ))
    return tbl


def _filing_alert(filings: list[dict], st: dict) -> Table:
    """Red alert block listing overnight SEC filings."""
    lines = "<br/>".join(
        f"🚨  NEW {f['form']} FILING  ·  {f['date']}  ·  {f.get('title','')}"
        for f in filings
    )
    tbl = Table([[Paragraph(lines, st["filing_alert"])]], colWidths=[7.0 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), HexColor("#7B0000")),
        ("TOPPADDING",    (0,0), (-1,-1), 9),
        ("BOTTOMPADDING", (0,0), (-1,-1), 9),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
    ]))
    return tbl


def _news_block(news: list[dict], st: dict) -> list:
    if not news:
        return [Paragraph("No news items available.", st["body"])]
    items = []
    for n in news[:6]:
        meta = f' &nbsp; <font color="#888888" size="8">{n["publisher"]}  ·  {n["time"]}</font>'
        items.append(Paragraph(f'<b>{n["title"]}</b>{meta}', st["news_item"]))
    return items


def _ticker_section(elements: list, ticker: str, company: str,
                    price_data: dict, news: list, filings: list,
                    analysis: dict, st: dict):
    """Append one ticker's full analysis section."""

    # Header + metrics (keep together if possible)
    block = [
        _ticker_header(ticker, company, price_data, st),
        Spacer(1, 0.07 * inch),
        _metrics_row(price_data),
        Spacer(1, 0.10 * inch),
    ]
    if filings:
        block.append(_filing_alert(filings, st))
        block.append(Spacer(1, 0.08 * inch))

    elements.append(KeepTogether(block))

    # Analysis sections
    section_map = [
        ("OVERNIGHT_SUMMARY", "OVERNIGHT ACTIVITY"),
        ("INVESTMENT_THESIS",  "INVESTMENT THESIS & FORWARD ANALYSIS"),
        ("TECHNICAL_OUTLOOK",  "TECHNICAL & FUNDAMENTAL OUTLOOK"),
        ("CORRELATIONS",       "CORRELATED STOCKS & SECTOR IMPLICATIONS"),
    ]
    for key, label in section_map:
        text = analysis.get(key, "(Analysis unavailable.)")
        elements.append(KeepTogether([
            Paragraph(label, st["section_hdr"]),
            Paragraph(text, st["body"]),
        ]))

    # News
    elements.append(HRFlowable(width="100%", thickness=0.5,
                                color=C_MGREY, spaceBefore=8, spaceAfter=5))
    elements.append(Paragraph("RECENT NEWS", st["section_hdr"]))
    elements.extend(_news_block(news, st))

    elements.append(PageBreak())


def build_pdf(date_str: str, all_data: list[dict]) -> Path:
    """Assemble the full report PDF and return its path."""
    REPORTS_DIR.mkdir(exist_ok=True)
    fname   = f"morning_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.pdf"
    out     = REPORTS_DIR / fname

    doc = SimpleDocTemplate(
        str(out), pagesize=letter,
        leftMargin=0.75*inch, rightMargin=0.75*inch,
        topMargin=0.55*inch,  bottomMargin=0.55*inch,
    )
    st       = _make_styles()
    elements = []

    # Cover
    summaries = [
        {
            "ticker":      d["ticker"],
            "company":     d["company"],
            "price":       d["price_data"]["price"],
            "change_pct":  d["price_data"]["change_pct"],
            "market_cap":  d["price_data"]["market_cap"],
            "has_filings": bool(d["filings"]),
            "news_count":  len(d["news"]),
        }
        for d in all_data
    ]
    _cover_page(elements, date_str, summaries, st)

    # Per-ticker
    for d in all_data:
        _ticker_section(
            elements, d["ticker"], d["company"],
            d["price_data"], d["news"], d["filings"],
            d["analysis"], st,
        )

    doc.build(elements)
    log.info("PDF built: %s", out)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  GIT PUSH
# ════════════════════════════════════════════════════════════════════════════

def git_push_report(pdf_path: Path) -> bool:
    rel = str(pdf_path.relative_to(REPO_DIR))
    cmds = [
        ["git", "add", rel],
        ["git", "commit", "-m",
         f"Morning report {pdf_path.stem}\n\n"
         "Auto-generated daily pre-market intelligence report.\n\n"
         "https://claude.ai/code/session_014hesikAtm8zzGNsXbYWmGV"],
        ["git", "push", "-u", GIT_REMOTE, BRANCH],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            log.error("git failed: %s\n%s", " ".join(cmd), r.stderr)
            return False
        log.info("$ %s → %s", " ".join(cmd), r.stdout.strip() or r.stderr.strip())
    return True


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Generate daily morning intelligence report PDF")
    parser.add_argument("--no-push", action="store_true",
                        help="Build PDF but skip git push")
    parser.add_argument("--ticker", metavar="T",
                        help="Run for a single ticker only (default: all)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — aborting.")
        sys.exit(1)

    client     = anthropic.Anthropic(api_key=api_key)
    now_utc    = datetime.now(timezone.utc)
    date_str   = now_utc.strftime("%A, %B %d, %Y")
    watch_list = [args.ticker.upper()] if args.ticker else list(TICKERS.keys())

    log.info("=" * 70)
    log.info("Morning report started: %s", now_utc.isoformat())
    log.info("Tickers: %s", ", ".join(watch_list))

    state    = load_watchdog_state()
    all_data = []

    for ticker in watch_list:
        if ticker not in TICKERS:
            log.warning("Unknown ticker %s — skipping.", ticker)
            continue

        company = TICKERS[ticker]["name"]
        log.info("─── %s (%s) ───", ticker, company)

        price_data = get_price_data(ticker)
        log.info("  $%.2f  %+.2f%%  cap=%s",
                 price_data["price"], price_data["change_pct"],
                 fmt_cap(price_data["market_cap"]))

        news    = get_news(ticker)
        log.info("  %d news items", len(news))

        filings = get_overnight_filings(ticker, state)
        log.info("  %d overnight filing(s)", len(filings))

        analysis = generate_analysis(ticker, company, price_data, news, filings, client)

        all_data.append({
            "ticker":     ticker,
            "company":    company,
            "price_data": price_data,
            "news":       news,
            "filings":    filings,
            "analysis":   analysis,
        })

    if not all_data:
        log.error("No data collected — exiting.")
        sys.exit(1)

    pdf_path = build_pdf(date_str, all_data)

    if not args.no_push:
        if git_push_report(pdf_path):
            log.info("Report pushed to %s:%s", GIT_REMOTE, BRANCH)
        else:
            log.warning("Push failed — PDF saved locally at %s", pdf_path)

    log.info("Morning report complete: %s", pdf_path.name)
    log.info("=" * 70)


if __name__ == "__main__":
    main()
