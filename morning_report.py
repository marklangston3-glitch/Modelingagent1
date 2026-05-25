#!/usr/bin/env python3
"""
morning_report.py — Goldman Sachs equity-research-style Company Update PDFs.

Produces one single-page PDF per ticker (TEM, RGTI, BBAI):
  reports/{TICKER}_morning_report_{YYYY-MM-DD}.pdf

Layout (letter, 612×792 pt):
  Header  — GS navy band, company nameplate, rating/PT row, bold headline
  Body    — Left col 60% (What's Changed, Implications, Valuation, Risks)
             Right col 40% (Key Data table, Investment Profile chart, Price chart)
  Footer  — Analyst line, disclaimer

Usage:
  python morning_report.py                  # all tickers + push
  python morning_report.py --ticker TEM     # single ticker
  python morning_report.py --no-push        # build without pushing
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xe

import anthropic
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import yfinance as yf

from reportlab.lib.colors import HexColor, black, white, Color
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Frame, Paragraph, Spacer, Table, TableStyle, Image

# ════════════════════════════════════════════════════════════════════════════
#  CONFIG & CONSTANTS
# ════════════════════════════════════════════════════════════════════════════

TICKERS = {
    "TEM":  {"name": "Tempus AI, Inc.",          "exch": "NASDAQ"},
    "RGTI": {"name": "Rigetti Computing, Inc.",   "exch": "NASDAQ"},
    "BBAI": {"name": "BigBear.ai Holdings, Inc.", "exch": "NYSE"},
}

REPO_DIR    = Path(__file__).parent.resolve()
STATE_FILE  = REPO_DIR / ".watchdog_state.json"
REPORTS_DIR = REPO_DIR / "reports"
LOG_FILE    = REPO_DIR / "morning_report.log"
BRANCH      = "claude/agent-tools-edgar-setup-PimAK"
GIT_REMOTE  = "origin"
MODEL       = "claude-opus-4-7"

# ── GS Colour Palette ────────────────────────────────────────────────────────
GS_NAVY   = HexColor("#002F5F")
GS_BLUE   = HexColor("#0E4DA4")
GS_LGRAY  = HexColor("#EEF1F6")
GS_MGRAY  = HexColor("#B0BAC9")
GS_DGRAY  = HexColor("#4A5568")
GS_LINE   = HexColor("#CDD3DF")
GS_TEXT   = HexColor("#1A202C")
BUY_COL   = HexColor("#1A5276")
NEUT_COL  = HexColor("#4A5568")
SELL_COL  = HexColor("#7B241C")

# ── Page Layout Constants (all in pts, origin bottom-left) ──────────────────
PW, PH      = letter            # 612 × 792

# Header sections — top-down from y=756
HDR_BAND_TOP  = 756             # top of navy band
HDR_BAND_H    = 30
HDR_BAND_BOT  = 726             # = 756 - 30

HDR_COMP_TOP  = HDR_BAND_BOT    # company nameplate
HDR_COMP_H    = 52
HDR_COMP_BOT  = 674             # = 726 - 52

HDR_RAT_TOP   = HDR_COMP_BOT   # rating + price row
HDR_RAT_H     = 26
HDR_RAT_BOT   = 648             # = 674 - 26

HDR_HL_TOP    = HDR_RAT_BOT    # headline row
HDR_HL_H      = 28
HDR_HL_BOT    = 620             # = 648 - 28

HDR_DIV_Y     = 617             # thin navy divider

BODY_TOP      = 613             # body starts here
BODY_BOT      = 82              # body ends here (footer divider at 80)
BODY_H        = BODY_TOP - BODY_BOT   # 531

# Columns
LCOL_X   = 36
LCOL_W   = 316
RCOL_X   = 364                  # 36 + 316 + 12 gap
RCOL_W   = 212                  # 576 - 364

# Footer
FTR_DIV_Y = 80
FTR_BOT   = 36


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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    recent = []
    for f in state.get(ticker, {}).get("last_filings", []):
        try:
            d = datetime.fromisoformat(f.get("date", "")[:10]).replace(tzinfo=timezone.utc)
            if d >= cutoff:
                recent.append(f)
        except (ValueError, TypeError):
            pass
    return recent


def _parse_news_item(item: dict) -> dict:
    content = item.get("content", {})
    if isinstance(content, dict) and content.get("title"):
        title   = content["title"]
        summary = content.get("summary", "")[:180]
        pub     = (content.get("provider") or {}).get("displayName", "")
        raw_t   = content.get("pubDate", "")
        t_str   = raw_t[:16].replace("T", " ") if raw_t else ""
    else:
        title   = item.get("title") or item.get("headline") or "(no title)"
        summary = ""
        pub     = item.get("publisher", "")
        raw_t   = item.get("providerPublishTime", 0)
        t_str   = (datetime.fromtimestamp(raw_t, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
                   if isinstance(raw_t, (int, float)) and raw_t > 1e9 else "")
    return {"title": title, "summary": summary, "publisher": pub, "time": t_str}


def get_price_data(ticker: str) -> dict:
    try:
        yt      = yf.Ticker(ticker)
        info    = yt.info or {}
        hist5   = yt.history(period="5d")
        price   = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        prev_c  = float(info.get("previousClose") or info.get("regularMarketPreviousClose") or 0)
        chg_pct = (price - prev_c) / prev_c * 100 if prev_c else 0.0
        history = []
        for idx, row in hist5.tail(5).iterrows():
            history.append({"date": idx.strftime("%Y-%m-%d"),
                            "close": round(float(row["Close"]), 2),
                            "volume": int(row["Volume"])})
        return {
            "price":      round(price, 2),
            "prev_close": round(prev_c, 2),
            "change_pct": round(chg_pct, 2),
            "day_high":   round(float(info.get("dayHigh") or info.get("regularMarketDayHigh") or price), 2),
            "day_low":    round(float(info.get("dayLow") or info.get("regularMarketDayLow") or price), 2),
            "wk52_high":  round(float(info.get("fiftyTwoWeekHigh") or 0), 2),
            "wk52_low":   round(float(info.get("fiftyTwoWeekLow") or 0), 2),
            "market_cap": int(info.get("marketCap") or 0),
            "volume":     int(info.get("volume") or info.get("regularMarketVolume") or 0),
            "avg_volume": int(info.get("averageVolume") or 0),
            "short_name": info.get("shortName", ticker),
            "history":    history,
        }
    except Exception as exc:
        log.warning("yfinance error for %s: %s", ticker, exc)
        return {"price": 0, "prev_close": 0, "change_pct": 0,
                "day_high": 0, "day_low": 0, "wk52_high": 0, "wk52_low": 0,
                "market_cap": 0, "volume": 0, "avg_volume": 0,
                "short_name": ticker, "history": []}


def get_news(ticker: str, n: int = 8) -> list[dict]:
    try:
        return [_parse_news_item(i) for i in (yf.Ticker(ticker).news or [])[:n]]
    except Exception as exc:
        log.warning("News error %s: %s", ticker, exc)
        return []


def fmt_cap(v: int) -> str:
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,}" if v else "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CHART GENERATION
# ════════════════════════════════════════════════════════════════════════════

def make_price_chart(ticker: str, w_pt: float, h_pt: float) -> BytesIO | None:
    """12-month price performance vs S&P 500, normalised to 100."""
    try:
        h_stk = yf.Ticker(ticker).history(period="1y")
        h_sp  = yf.Ticker("^GSPC").history(period="1y")
        if h_stk.empty or h_sp.empty:
            return None
        common = h_stk.index.intersection(h_sp.index)
        if len(common) < 10:
            return None
        s = (h_stk.loc[common, "Close"] / h_stk.loc[common, "Close"].iloc[0]) * 100
        p = (h_sp.loc[common, "Close"]  / h_sp.loc[common, "Close"].iloc[0])  * 100

        fig, ax = plt.subplots(figsize=(w_pt / 72, h_pt / 72))
        ax.plot(s.index, s.values, color="#002F5F", lw=1.4, label=ticker,   zorder=3)
        ax.plot(p.index, p.values, color="#AAAAAA", lw=0.9, label="S&P 500",
                linestyle="--", zorder=2, alpha=0.85)
        ax.axhline(100, color="#E0E0E0", lw=0.5, zorder=1)

        ax.set_facecolor("white")
        fig.patch.set_facecolor("white")
        for sp in ["top", "right"]:
            ax.spines[sp].set_visible(False)
        for sp in ["left", "bottom"]:
            ax.spines[sp].set_color("#DDDDDD")
            ax.spines[sp].set_linewidth(0.4)

        ax.tick_params(axis="both", labelsize=5.5, colors="#777777", length=2, width=0.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.xticks(rotation=0, ha="center")
        ax.set_ylabel("Indexed (100 = 12M ago)", fontsize=5, color="#999999", labelpad=2)
        ax.grid(axis="y", ls="--", lw=0.3, color="#EEEEEE", zorder=0)
        leg = ax.legend(fontsize=5.5, loc="upper left", frameon=False,
                        handlelength=1.2, handletextpad=0.3, labelspacing=0.2)
        for t in leg.get_texts():
            t.set_color("#555555")

        plt.tight_layout(pad=0.35)
        buf = BytesIO()
        plt.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor="white")
        buf.seek(0)
        plt.close(fig)
        return buf
    except Exception as exc:
        log.warning("Price chart error %s: %s", ticker, exc)
        return None


def make_profile_chart(scores: dict, w_pt: float, h_pt: float) -> BytesIO:
    """Investment profile horizontal bar chart (GS style)."""
    labels = ["Growth", "Returns", "Multiple", "Volatility"]
    vals   = [max(1, min(10, scores.get(k, 5)))
               for k in ["growth_score", "returns_score", "multiple_score", "volatility_score"]]
    ys     = [3, 2, 1, 0]          # top-to-bottom order

    fig, ax = plt.subplots(figsize=(w_pt / 72, h_pt / 72))
    BAR_H = 0.22

    for y, v in zip(ys, vals):
        # grey background track
        ax.barh(y, 10, height=BAR_H, color="#DDE3EE", left=0, align="center", zorder=1)
        # GS navy fill
        ax.barh(y, v,  height=BAR_H, color="#002F5F", left=0, align="center", zorder=2)
        # white dot at score
        ax.plot(v, y, "o", color="white", ms=5.5,
                markeredgecolor="#002F5F", markeredgewidth=1.8, zorder=3)

    ax.set_yticks(ys)
    ax.set_yticklabels(labels, fontsize=6.5, color="#333333")
    ax.set_xticks([0, 5, 10])
    ax.set_xticklabels(["Low", "Mid", "High"], fontsize=5.5, color="#888888")
    ax.set_xlim(-0.3, 10.6)
    ax.set_ylim(-0.55, 3.65)

    for sp in ["top", "right", "left"]:
        ax.spines[sp].set_visible(False)
    ax.spines["bottom"].set_color("#DDDDDD")
    ax.spines["bottom"].set_linewidth(0.4)
    ax.tick_params(axis="x", length=2, width=0.4, colors="#888888")
    ax.tick_params(axis="y", length=0)
    ax.set_facecolor("white")
    fig.patch.set_facecolor("white")

    plt.tight_layout(pad=0.4)
    buf = BytesIO()
    plt.savefig(buf, format="png", dpi=160, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    plt.close(fig)
    return buf


# ════════════════════════════════════════════════════════════════════════════
#  CLAUDE ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

_JSON_DEFAULTS = {
    "rating": "Neutral", "price_target": None, "headline": "No Material Overnight Updates",
    "growth_score": 5, "returns_score": 5, "multiple_score": 5, "volatility_score": 5,
    "rev_yr1": "FY2026E", "rev_est1": None, "rev_yr2": "FY2027E", "rev_est2": None,
    "eps_est1": None, "eps_est2": None, "pe_fwd": None, "ev_ebitda_fwd": None,
}

def generate_analysis(ticker: str, company: str, price_data: dict,
                      news: list, filings: list, client: anthropic.Anthropic) -> dict:
    price  = price_data["price"]
    chg    = price_data["change_pct"]
    cap    = fmt_cap(price_data["market_cap"])
    hi52   = price_data["wk52_high"]
    lo52   = price_data["wk52_low"]
    pct_range = ((price - lo52) / (hi52 - lo52) * 100) if (hi52 > lo52) else 0

    hist_lines = "\n".join(
        f"  {h['date']}: ${h['close']} (vol {h['volume']:,})" for h in price_data.get("history", [])
    ) or "  (no history)"

    news_lines = "\n".join(
        f"  [{n['time']}] {n['publisher']}: {n['title']}" for n in news[:6]
    ) or "  No news available."

    filing_lines = "\n".join(
        f"  🚨 {f['form']} filed {f['date']}: {f.get('title','')}" for f in filings
    ) or "  No new filings in last 36 hours."

    prompt = f"""You are a Goldman Sachs equity research analyst writing a pre-market Company Update note.
Institutional quality: precise, data-referenced, concise. Today: {datetime.now(timezone.utc).strftime("%B %d, %Y")}.

TICKER: {ticker}  |  COMPANY: {company}
Price: ${price:.2f} ({chg:+.2f}%)  |  Mkt Cap: {cap}  |  52-Wk: ${lo52}–${hi52}  ({pct_range:.0f}% of range)

RECENT PRICE ACTION:
{hist_lines}

OVERNIGHT NEWS:
{news_lines}

SEC FILINGS (last 36 hrs):
{filing_lines}

OUTPUT EXACTLY this format — no extra text outside these markers:

===JSON_START===
{{
  "rating": "Buy",
  "price_target": 65.00,
  "headline": "{ticker}: [Concise event-driven headline; 10 words max; include 'Reiterate Buy/Neutral/Sell' if no major news]",
  "growth_score": 8,
  "returns_score": 4,
  "multiple_score": 7,
  "volatility_score": 7,
  "rev_yr1": "FY2026E",
  "rev_est1": 693,
  "rev_yr2": "FY2027E",
  "rev_est2": 940,
  "eps_est1": -2.10,
  "eps_est2": -1.50,
  "pe_fwd": null,
  "ev_ebitda_fwd": null
}}
===JSON_END===

===WHAT_CHANGED===
• [Specific bullet on overnight filing, price move, or news catalyst — be data-specific]
• [Second bullet if applicable; omit if only one item]
[OR if nothing material: No new filings or price-moving news overnight.]
===END_WHAT_CHANGED===

===IMPLICATIONS===
[2–3 sentences. What does this mean for the investment thesis, model, or near-term catalysts? Be specific about {ticker}'s business.]
===END_IMPLICATIONS===

===VALUATION===
[2–3 sentences. Current valuation methodology; reference P/S, EV/Revenue, or EV/EBITDA as appropriate for {ticker}'s stage. State upside/downside to your PT. Note key multiple expansion or contraction driver.]
===END_VALUATION===

===KEY_RISKS===
• [Specific risk 1]
• [Specific risk 2]
• [Specific risk 3]
===END_KEY_RISKS===

SCORING GUIDE for Investment Profile (1–10):
  growth_score: revenue CAGR trajectory (10 = hypergrowth >100%/yr)
  returns_score: current FCF/EBITDA margins (10 = highly profitable; 1 = deeply negative)
  multiple_score: valuation richness (10 = extremely expensive vs peers; 1 = deep value)
  volatility_score: stock price volatility / binary outcome risk (10 = extreme)"""

    log.info("  Calling Claude (%s) for %s …", MODEL, ticker)
    msg = client.messages.create(
        model=MODEL, max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text

    # ── Parse JSON block ──────────────────────────────────────────────────────
    parsed = dict(_JSON_DEFAULTS)
    try:
        js_start = raw.find("===JSON_START===") + len("===JSON_START===")
        js_end   = raw.find("===JSON_END===")
        if js_start > 0 and js_end > js_start:
            parsed.update(json.loads(raw[js_start:js_end].strip()))
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("  JSON parse error: %s", e)

    # ── Parse text sections ───────────────────────────────────────────────────
    def _section(marker_open, marker_close):
        s = raw.find(marker_open)
        e = raw.find(marker_close)
        if s == -1 or e == -1:
            return ""
        return raw[s + len(marker_open):e].strip()

    parsed["what_changed"] = _section("===WHAT_CHANGED===",   "===END_WHAT_CHANGED===")
    parsed["implications"] = _section("===IMPLICATIONS===",    "===END_IMPLICATIONS===")
    parsed["valuation"]    = _section("===VALUATION===",       "===END_VALUATION===")
    parsed["key_risks"]    = _section("===KEY_RISKS===",       "===END_KEY_RISKS===")
    return parsed


# ════════════════════════════════════════════════════════════════════════════
#  PARAGRAPH STYLES
# ════════════════════════════════════════════════════════════════════════════

def _styles() -> dict:
    return {
        "sec_hdr": ParagraphStyle(
            "SecHdr", fontName="Helvetica-Bold", fontSize=6.5, leading=8,
            textColor=GS_NAVY, spaceBefore=0, spaceAfter=0,
        ),
        "body": ParagraphStyle(
            "Body", fontName="Helvetica", fontSize=8.5, leading=12.5,
            textColor=GS_TEXT, spaceAfter=0, alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName="Helvetica", fontSize=8.5, leading=12,
            textColor=GS_TEXT, leftIndent=10, firstLineIndent=-8, spaceAfter=1,
        ),
        "tbl_hdr": ParagraphStyle(
            "TblHdr", fontName="Helvetica-Bold", fontSize=7.5, textColor=white,
            alignment=TA_LEFT,
        ),
        "tbl_lbl": ParagraphStyle(
            "TblLbl", fontName="Helvetica", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_LEFT,
        ),
        "tbl_val": ParagraphStyle(
            "TblVal", fontName="Helvetica-Bold", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_RIGHT,
        ),
        "chart_cap": ParagraphStyle(
            "ChartCap", fontName="Helvetica", fontSize=6, textColor=GS_DGRAY,
            alignment=TA_CENTER,
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
#  SECTION HEADER HELPER — full-width colored band
# ════════════════════════════════════════════════════════════════════════════

def _sec_hdr_table(title: str, col_w: float, st: dict) -> Table:
    p = Paragraph(title.upper(), st["sec_hdr"])
    t = Table([[p]], colWidths=[col_w])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), GS_LGRAY),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LEFTPADDING",   (0,0), (-1,-1), 5),
        ("RIGHTPADDING",  (0,0), (-1,-1), 5),
    ]))
    return t


# ════════════════════════════════════════════════════════════════════════════
#  LEFT COLUMN STORY
# ════════════════════════════════════════════════════════════════════════════

def _bullets(text: str, style) -> list:
    """Convert newline-separated bullet text to Paragraph list."""
    items = []
    for line in text.split("\n"):
        line = line.strip().lstrip("•‒–—-").strip()
        if line:
            items.append(Paragraph(f"• {xe(line)}", style))
    return items or [Paragraph("• No material updates.", style)]


def build_left_story(analysis: dict, news: list, filings: list, st: dict) -> list:
    W = LCOL_W
    story = [Spacer(1, 3)]

    # ── What's Changed ────────────────────────────────────────────────────────
    story.append(_sec_hdr_table("What's Changed", W, st))
    story.append(Spacer(1, 4))

    if filings:
        for f in filings:
            story.append(Paragraph(
                f'• <b>NEW {xe(f["form"])} FILING</b> — {xe(f.get("date",""))}  '
                f'{xe(f.get("title",""))}',
                st["bullet"],
            ))
        story.append(Spacer(1, 2))

    wc_text = analysis.get("what_changed", "")
    story.extend(_bullets(wc_text, st["bullet"]))
    story.append(Spacer(1, 8))

    # ── Implications ──────────────────────────────────────────────────────────
    story.append(_sec_hdr_table("Implications", W, st))
    story.append(Spacer(1, 4))
    impl = analysis.get("implications", "").strip()
    story.append(Paragraph(xe(impl) if impl else "No implications to report.", st["body"]))
    story.append(Spacer(1, 8))

    # ── Valuation ─────────────────────────────────────────────────────────────
    story.append(_sec_hdr_table("Valuation", W, st))
    story.append(Spacer(1, 4))
    val = analysis.get("valuation", "").strip()
    story.append(Paragraph(xe(val) if val else "See key data table.", st["body"]))
    story.append(Spacer(1, 8))

    # ── Key Risks ─────────────────────────────────────────────────────────────
    story.append(_sec_hdr_table("Key Risks", W, st))
    story.append(Spacer(1, 4))
    risk_text = analysis.get("key_risks", "")
    story.extend(_bullets(risk_text, st["bullet"]))
    story.append(Spacer(1, 10))

    # ── Recent News ───────────────────────────────────────────────────────────
    story.append(_sec_hdr_table("Recent News", W, st))
    story.append(Spacer(1, 4))
    news_style = ParagraphStyle(
        "NS", fontName="Helvetica", fontSize=7.5, leading=11,
        textColor=GS_TEXT, spaceAfter=3, leftIndent=0,
    )
    meta_color = "#666666"
    for n in news[:5]:
        title = xe(n["title"][:90] + ("…" if len(n["title"]) > 90 else ""))
        pub   = xe(n["publisher"])
        tm    = xe(n["time"][:16])
        story.append(Paragraph(
            f'<b>{title}</b>  '
            f'<font color="{meta_color}" size="7">{pub} · {tm}</font>',
            news_style,
        ))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  RIGHT COLUMN STORY
# ════════════════════════════════════════════════════════════════════════════

def _key_data_table(analysis: dict, price_data: dict, st: dict) -> Table:
    """Build the GS-style Key Data table."""
    a      = analysis
    pd_    = price_data
    price  = pd_["price"]
    pt     = a.get("price_target")
    upside = ((pt - price) / price * 100) if pt else None

    def _fmt_eps(v):
        if v is None: return "N/M"
        return f"(${abs(v):.2f})" if v < 0 else f"${v:.2f}"

    def _fmt_rev(v):
        if v is None: return "—"
        return f"${v:,.0f}M" if v >= 1 else f"${v*1000:.0f}K"

    rows = [
        # (label, value)
        ("Price (USD)",                  f"${price:.2f}"),
        ("12M Price Target",             f"${pt:.2f}" if pt else "—"),
        ("Upside / (Downside)",          f"{upside:+.1f}%" if upside is not None else "—"),
        ("Market Cap",                   fmt_cap(pd_["market_cap"])),
        (f"{a.get('rev_yr1','FY2026E')} Revenue",   _fmt_rev(a.get("rev_est1"))),
        (f"{a.get('rev_yr2','FY2027E')} Revenue",   _fmt_rev(a.get("rev_est2"))),
        (f"{a.get('rev_yr1','FY2026E')} EPS",       _fmt_eps(a.get("eps_est1"))),
        (f"{a.get('rev_yr2','FY2027E')} EPS",       _fmt_eps(a.get("eps_est2"))),
        ("Fwd P/E",                      f"{a['pe_fwd']:.1f}x" if a.get("pe_fwd") else "N/M"),
    ]

    LW = RCOL_W * 0.64
    VW = RCOL_W * 0.36

    # Header row
    data = [[Paragraph("Key Data", st["tbl_hdr"]), ""]]
    # Data rows
    for lbl, val in rows:
        data.append([Paragraph(xe(lbl), st["tbl_lbl"]),
                     Paragraph(xe(val), st["tbl_val"])])

    tbl_style = [
        # Header
        ("BACKGROUND",    (0,0),  (-1,0),  GS_NAVY),
        ("SPAN",          (0,0),  (-1,0)),
        ("TOPPADDING",    (0,0),  (-1,0),  4),
        ("BOTTOMPADDING", (0,0),  (-1,0),  4),
        ("LEFTPADDING",   (0,0),  (0,0),   6),
        # Data rows
        ("FONTSIZE",      (0,1),  (-1,-1), 7.5),
        ("TOPPADDING",    (0,1),  (-1,-1), 3),
        ("BOTTOMPADDING", (0,1),  (-1,-1), 3),
        ("LEFTPADDING",   (0,1),  (0,-1),  6),
        ("RIGHTPADDING",  (-1,1), (-1,-1), 6),
        ("LINEBELOW",     (0,0),  (-1,-1), 0.4, GS_LINE),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
    ]
    # Alternating row backgrounds
    for i in range(1, len(data)):
        bg = GS_LGRAY if i % 2 == 0 else white
        tbl_style.append(("BACKGROUND", (0, i), (-1, i), bg))
    # Upside row — color by sign
    upside_row = 3   # index of upside row (0-based header=0, price=1, pt=2, upside=3)
    if upside is not None:
        uc = HexColor("#1A5276") if upside >= 0 else HexColor("#7B241C")
        tbl_style.append(("TEXTCOLOR", (1, upside_row), (1, upside_row), uc))
        tbl_style.append(("FONTNAME",  (1, upside_row), (1, upside_row), "Helvetica-Bold"))

    t = Table(data, colWidths=[LW, VW])
    t.setStyle(TableStyle(tbl_style))
    return t


def build_right_story(analysis: dict, price_data: dict, charts: dict, st: dict) -> list:
    story = [Spacer(1, 3)]

    # ── Key Data ──────────────────────────────────────────────────────────────
    story.append(_key_data_table(analysis, price_data, st))
    story.append(Spacer(1, 10))

    # ── Investment Profile ────────────────────────────────────────────────────
    story.append(_sec_hdr_table("Investment Profile", RCOL_W, st))
    story.append(Spacer(1, 4))
    prof_buf = charts.get("profile")
    if prof_buf:
        story.append(Image(prof_buf, width=RCOL_W, height=88))
    story.append(Spacer(1, 8))

    # ── Price Performance ─────────────────────────────────────────────────────
    story.append(_sec_hdr_table("12-Month Price Performance vs. S&P 500", RCOL_W, st))
    story.append(Spacer(1, 3))
    price_buf = charts.get("price")
    if price_buf:
        # Available height = BODY_H minus what key data + profile took
        # Estimated usage: key_data ~150, spacers ~21, profile hdr+chart ~108 = ~279
        # Remaining ≈ 531 - 279 = 252pt; use 210 to leave some bottom padding
        story.append(Image(price_buf, width=RCOL_W, height=200))
    else:
        story.append(Paragraph("Price history unavailable.", st["body"]))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS HEADER
# ════════════════════════════════════════════════════════════════════════════

def _rating_badge(c, x: float, y: float, rating: str):
    """Draw filled rating badge (Buy/Neutral/Sell) at position x,y (bottom-left)."""
    col = {"Buy": BUY_COL, "Neutral": NEUT_COL, "Sell": SELL_COL}.get(rating, NEUT_COL)
    W, H = 30, 12
    c.setFillColor(col)
    c.roundRect(x, y, W, H, radius=1.5, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 6.5)
    c.setFillColor(white)
    tw = stringWidth(rating, "Helvetica-Bold", 6.5)
    c.drawString(x + (W - tw) / 2, y + 3.5, rating)


def draw_header(c, ticker: str, company: str, exch: str,
                price_data: dict, analysis: dict):
    """Draw fixed header elements directly on canvas."""
    rating   = analysis.get("rating", "Neutral")
    pt       = analysis.get("price_target")
    price    = price_data["price"]
    chg      = price_data["change_pct"]
    hi52     = price_data["wk52_high"]
    lo52     = price_data["wk52_low"]
    headline = analysis.get("headline", "No Material Overnight Updates")
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # ── Navy top band ─────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, HDR_BAND_BOT, PW, HDR_BAND_H, fill=1, stroke=0)

    # Date (left)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(white)
    c.drawString(36, HDR_BAND_BOT + 18, date_str)

    # Sub-label row
    c.setFont("Helvetica", 6.5)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, HDR_BAND_BOT + 7, "EQUITY RESEARCH  |  COMPANY UPDATE")

    # GS wordmark (right)
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(white)
    gw = stringWidth("GOLDMAN SACHS", "Helvetica-Bold", 8.5)
    c.drawString(576 - gw, HDR_BAND_BOT + 18, "GOLDMAN SACHS")
    # Gold accent line under GS name
    c.setStrokeColor(HexColor("#C9A84C"))
    c.setLineWidth(1.2)
    c.line(576 - gw, HDR_BAND_BOT + 15, 576, HDR_BAND_BOT + 15)

    # ── Company nameplate (white) ─────────────────────────────────────────────
    c.setFillColor(white)
    c.rect(0, HDR_COMP_BOT, PW, HDR_COMP_H, fill=1, stroke=0)

    # Company name
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(GS_TEXT)
    c.drawString(36, HDR_COMP_BOT + HDR_COMP_H - 22, company)

    # Ticker + exchange badge
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(GS_BLUE)
    c.drawString(36, HDR_COMP_BOT + HDR_COMP_H - 37, ticker)

    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_DGRAY)
    tkw = stringWidth(ticker, "Helvetica-Bold", 10)
    c.drawString(36 + tkw + 8, HDR_COMP_BOT + HDR_COMP_H - 36.5, f"  {exch}")

    # Horizontal separator inside nameplate
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.5)
    c.line(36, HDR_COMP_BOT + 14, 576, HDR_COMP_BOT + 14)

    # ── Rating + prices row ───────────────────────────────────────────────────
    c.setFillColor(GS_LGRAY)
    c.rect(0, HDR_RAT_BOT, PW, HDR_RAT_H, fill=1, stroke=0)

    row_mid = HDR_RAT_BOT + HDR_RAT_H / 2

    # Rating badge
    _rating_badge(c, 36, row_mid - 6, rating)

    # Price + change
    chg_col = HexColor("#1A5276") if chg >= 0 else HexColor("#7B241C")
    sign    = "▲" if chg >= 0 else "▼"
    c.setFont("Helvetica-Bold", 9.5)
    c.setFillColor(GS_TEXT)
    c.drawString(76, row_mid - 3.5, f"${price:.2f}")
    pw_ = stringWidth(f"${price:.2f}", "Helvetica-Bold", 9.5)
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(chg_col)
    c.drawString(76 + pw_ + 4, row_mid - 3, f"{sign} {abs(chg):.2f}%")

    # Price target
    if pt:
        c.setFont("Helvetica", 8)
        c.setFillColor(GS_DGRAY)
        c.drawString(170, row_mid + 4, "12M Price Target")
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(GS_TEXT)
        c.drawString(170, row_mid - 5, f"${pt:.2f}")

    # 52-week range
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_DGRAY)
    c.drawString(260, row_mid + 4, "52-Week Range")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_TEXT)
    c.drawString(260, row_mid - 5, f"${lo52:.2f} – ${hi52:.2f}")

    # Market cap
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_DGRAY)
    c.drawString(370, row_mid + 4, "Market Cap")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_TEXT)
    c.drawString(370, row_mid - 5, fmt_cap(price_data["market_cap"]))

    # ── Headline ──────────────────────────────────────────────────────────────
    c.setFillColor(white)
    c.rect(0, HDR_HL_BOT, PW, HDR_HL_H, fill=1, stroke=0)

    # Bold headline text — wrap manually if needed
    c.setFont("Helvetica-Bold", 9.5)
    c.setFillColor(GS_TEXT)
    max_w = 540
    if stringWidth(headline, "Helvetica-Bold", 9.5) <= max_w:
        c.drawString(36, HDR_HL_BOT + HDR_HL_H - 13, headline)
    else:
        words = headline.split()
        line1 = ""
        for w in words:
            test = (line1 + " " + w).strip()
            if stringWidth(test, "Helvetica-Bold", 9.5) <= max_w:
                line1 = test
            else:
                break
        line2 = headline[len(line1):].strip()
        c.drawString(36, HDR_HL_BOT + HDR_HL_H - 10, line1)
        c.setFont("Helvetica-Bold", 9.5)
        c.drawString(36, HDR_HL_BOT + HDR_HL_H - 22, line2)

    # ── Divider ───────────────────────────────────────────────────────────────
    c.setStrokeColor(GS_NAVY)
    c.setLineWidth(0.8)
    c.line(36, HDR_DIV_Y, 576, HDR_DIV_Y)


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS FOOTER
# ════════════════════════════════════════════════════════════════════════════

def draw_footer(c, ticker: str):
    """Draw footer elements directly on canvas."""
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.5)
    c.line(36, FTR_DIV_Y, 576, FTR_DIV_Y)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(GS_DGRAY)
    c.drawString(36, FTR_DIV_Y - 11, "Equity Research Coverage")

    c.setFont("Helvetica", 6.5)
    c.setFillColor(GS_MGRAY)
    c.drawString(36, FTR_DIV_Y - 22, f"AI-Generated Pre-Market Brief  |  {now_str}")

    disc = ("This report is generated automatically using SEC EDGAR filings, market data (yfinance), and "
            "AI-generated analysis. It does not constitute investment advice. All figures are estimates. "
            "Past performance is not indicative of future results. Investors should conduct their own due diligence.")
    c.setFont("Helvetica", 5.5)
    c.setFillColor(GS_MGRAY)

    # Wrap disclaimer across 2 lines
    max_w = 540
    words = disc.split()
    line1 = ""
    for w in words:
        test = (line1 + " " + w).strip()
        if stringWidth(test, "Helvetica", 5.5) <= max_w:
            line1 = test
        else:
            break
    line2 = disc[len(line1):].strip()
    c.drawString(36, FTR_DIV_Y - 32, line1)
    c.drawString(36, FTR_DIV_Y - 40, line2)

    # Page number
    c.setFont("Helvetica", 6)
    c.setFillColor(GS_MGRAY)
    c.drawRightString(576, FTR_DIV_Y - 11, f"{ticker} | Company Update")


# ════════════════════════════════════════════════════════════════════════════
#  REPORT BUILDER
# ════════════════════════════════════════════════════════════════════════════

def build_ticker_pdf(
    ticker: str, company: str, exch: str,
    price_data: dict, analysis: dict,
    news: list, filings: list,
    charts: dict,
) -> Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out      = REPORTS_DIR / f"{ticker}_morning_report_{date_tag}.pdf"

    c = pdfcanvas.Canvas(str(out), pagesize=letter)
    c.setTitle(f"{ticker} — Company Update  {date_tag}")
    c.setAuthor("Goldman Sachs | Equity Research")
    c.setSubject(f"Company Update: {company}")

    # ── Fixed elements (canvas) ───────────────────────────────────────────────
    draw_header(c, ticker, company, exch, price_data, analysis)
    draw_footer(c, ticker)

    # ── Column divider ────────────────────────────────────────────────────────
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.4)
    c.line(RCOL_X - 6, BODY_TOP - 2, RCOL_X - 6, BODY_BOT + 2)

    # ── Left column (Frame) ───────────────────────────────────────────────────
    st = _styles()
    left_story = build_left_story(analysis, news, filings, st)
    left_frame = Frame(
        LCOL_X, BODY_BOT, LCOL_W, BODY_H,
        leftPadding=0, rightPadding=6, topPadding=0, bottomPadding=0,
        showBoundary=0,
    )
    left_frame.addFromList(left_story, c)

    # ── Right column (Frame) ──────────────────────────────────────────────────
    right_story = build_right_story(analysis, price_data, charts, st)
    right_frame = Frame(
        RCOL_X, BODY_BOT, RCOL_W, BODY_H,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        showBoundary=0,
    )
    right_frame.addFromList(right_story, c)

    c.save()
    log.info("  PDF: %s", out.name)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  GIT PUSH
# ════════════════════════════════════════════════════════════════════════════

def git_push_reports(pdf_paths: list[Path]) -> bool:
    rels = [str(p.relative_to(REPO_DIR)) for p in pdf_paths]
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmds = [
        ["git", "add"] + rels,
        ["git", "commit", "-m",
         f"Morning reports {date_tag} — {', '.join(p.stem for p in pdf_paths)}\n\n"
         "GS-style equity research Company Update PDFs.\n\n"
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
    parser = argparse.ArgumentParser(description="GS-style morning report PDFs")
    parser.add_argument("--no-push", action="store_true", help="Skip git push")
    parser.add_argument("--ticker", metavar="T", help="Single ticker (default: all)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — aborting.")
        sys.exit(1)

    client     = anthropic.Anthropic(api_key=api_key)
    now_utc    = datetime.now(timezone.utc)
    watch_list = [args.ticker.upper()] if args.ticker else list(TICKERS.keys())

    log.info("=" * 70)
    log.info("GS Morning Reports started: %s", now_utc.isoformat())
    log.info("Tickers: %s", ", ".join(watch_list))

    state    = load_watchdog_state()
    pdf_list = []

    for ticker in watch_list:
        if ticker not in TICKERS:
            log.warning("Unknown ticker %s — skipping.", ticker)
            continue

        meta    = TICKERS[ticker]
        company = meta["name"]
        exch    = meta["exch"]
        log.info("─── %s (%s) ───", ticker, company)

        price_data = get_price_data(ticker)
        log.info("  $%.2f  %+.2f%%  cap=%s",
                 price_data["price"], price_data["change_pct"],
                 fmt_cap(price_data["market_cap"]))

        news    = get_news(ticker, n=8)
        filings = get_overnight_filings(ticker, state)
        log.info("  news=%d  filings=%d", len(news), len(filings))

        analysis = generate_analysis(ticker, company, price_data, news, filings, client)
        log.info("  Rating=%s  PT=%s  Headline: %s",
                 analysis.get("rating"), analysis.get("price_target"),
                 analysis.get("headline","")[:60])

        # Generate charts
        log.info("  Generating charts …")
        charts = {
            "price":   make_price_chart(ticker, RCOL_W, 200),
            "profile": make_profile_chart(analysis, RCOL_W, 88),
        }

        pdf_path = build_ticker_pdf(
            ticker, company, exch,
            price_data, analysis, news, filings, charts,
        )
        pdf_list.append(pdf_path)

    if not pdf_list:
        log.error("No PDFs generated — exiting.")
        sys.exit(1)

    if not args.no_push:
        if git_push_reports(pdf_list):
            log.info("Reports pushed to %s:%s", GIT_REMOTE, BRANCH)
        else:
            log.warning("Push failed — PDFs saved locally in reports/")

    log.info("Done. Generated: %s", ", ".join(p.name for p in pdf_list))
    log.info("=" * 70)


if __name__ == "__main__":
    main()
