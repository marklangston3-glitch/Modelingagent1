#!/usr/bin/env python3
"""
morning_report.py — Langston's Financial Intelligence daily equity research PDFs.

Produces:
  reports/morning_report_{YYYY-MM-DD}.pdf           — combined report with front page
  reports/{TICKER}_morning_report_{YYYY-MM-DD}.pdf  — per-ticker 2-page PDFs

Combined PDF structure:
  Page 1:         Front Page — Macro overview + Conviction ranking
  Pages 2–3:      TEM        — Page 1 (company update) + Page 2 (price outlook / strategy)
  Pages 4–5:      RGTI
  Pages 6–7:      BBAI

Usage:
  python morning_report.py                  # all tickers + push
  python morning_report.py --ticker TEM     # single ticker (no front page)
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

# Import centralized config
from config import (
    FIRM_NAME, FIRM_NAME_U, FIRM_NAME_FULL, EMAIL, RECIPIENTS,
    REPO_DIR, BRANCH, GIT_REMOTE,
    ANTHROPIC_MODEL, get_anthropic_key,
    SECTOR_ETFS, MACRO_RATE_TICKERS,
    load_tickers,
)

# Smart money intelligence modules
from options_flow          import get_options_flow
from insider_tracker       import get_insider_activity
from institutional_tracker import get_institutional_ownership
from prospect_finder       import find_prospects
from market_intelligence   import collect_market_intelligence

# ════════════════════════════════════════════════════════════════════════════
#  RUNTIME CONSTANTS
# ════════════════════════════════════════════════════════════════════════════

TICKERS     = load_tickers()   # dynamic from tickers.txt
STATE_FILE  = REPO_DIR / ".watchdog_state.json"
REPORTS_DIR = REPO_DIR / "reports"
LOG_FILE    = REPO_DIR / "morning_report.log"
MODEL       = ANTHROPIC_MODEL

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
BULL_COL  = HexColor("#1A5276")
BEAR_COL  = HexColor("#7B241C")
GOLD_COL  = HexColor("#C9A84C")

# ── Page Layout Constants (pts, origin bottom-left) ──────────────────────────
PW, PH      = letter            # 612 × 792

# Header sections — top-down from y=756
HDR_BAND_TOP  = 756
HDR_BAND_H    = 30
HDR_BAND_BOT  = 726

HDR_COMP_TOP  = HDR_BAND_BOT
HDR_COMP_H    = 52
HDR_COMP_BOT  = 674

HDR_RAT_TOP   = HDR_COMP_BOT
HDR_RAT_H     = 26
HDR_RAT_BOT   = 648

HDR_HL_TOP    = HDR_RAT_BOT
HDR_HL_H      = 28
HDR_HL_BOT    = 620

HDR_DIV_Y     = 617

BODY_TOP      = 613
BODY_BOT      = 82
BODY_H        = BODY_TOP - BODY_BOT   # 531

# Columns (Page 1 body)
LCOL_X   = 36
LCOL_W   = 316
RCOL_X   = 364
RCOL_W   = 212

# Footer
FTR_DIV_Y = 80
FTR_BOT   = 36

# Full content width (used by draw_prospects_page and page-2 layout)
FULL_W = 540  # 576 - 36 (right margin)


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


def get_macro_data() -> dict:
    """Pull sector ETF performance and macro rate data from yfinance."""
    log.info("  Fetching macro & sector ETF data …")
    sector_data: dict[str, dict] = {}
    for etf_tk, sector_name in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(etf_tk).history(period="1mo")
            if hist.empty:
                continue
            closes  = hist["Close"]
            price   = float(closes.iloc[-1])
            c_1d    = float(closes.iloc[-2]) if len(closes) >= 2 else price
            c_5d    = float(closes.iloc[-6]) if len(closes) >= 6 else price
            c_1m    = float(closes.iloc[0])
            sector_data[etf_tk] = {
                "name":   sector_name,
                "price":  round(price, 2),
                "chg_1d": round((price - c_1d) / c_1d * 100, 2) if c_1d else 0,
                "chg_5d": round((price - c_5d) / c_5d * 100, 2) if c_5d else 0,
                "chg_1m": round((price - c_1m) / c_1m * 100, 2) if c_1m else 0,
            }
        except Exception as exc:
            log.debug("Sector ETF error %s: %s", etf_tk, exc)

    rate_data: dict[str, dict] = {}
    for rt, name in MACRO_RATE_TICKERS.items():
        try:
            hist = yf.Ticker(rt).history(period="5d")
            if hist.empty:
                continue
            closes = hist["Close"]
            val  = float(closes.iloc[-1])
            prev = float(closes.iloc[-2]) if len(closes) >= 2 else val
            rate_data[rt] = {
                "name":  name,
                "value": round(val, 4 if "^" in rt and rt != "^VIX" else 2),
                "chg":   round(val - prev, 4),
            }
        except Exception as exc:
            log.debug("Rate data error %s: %s", rt, exc)

    return {"sectors": sector_data, "rates": rate_data}


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
    ys     = [3, 2, 1, 0]

    fig, ax = plt.subplots(figsize=(w_pt / 72, h_pt / 72))
    BAR_H = 0.22

    for y, v in zip(ys, vals):
        ax.barh(y, 10, height=BAR_H, color="#DDE3EE", left=0, align="center", zorder=1)
        ax.barh(y, v,  height=BAR_H, color="#002F5F", left=0, align="center", zorder=2)
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
#  CLAUDE ANALYSIS — EXTENDED
# ════════════════════════════════════════════════════════════════════════════

_JSON_DEFAULTS: dict = {
    "rating": "Neutral",
    "price_target": None,
    "headline": "No Material Overnight Updates",
    "growth_score": 5, "returns_score": 5, "multiple_score": 5, "volatility_score": 5,
    "rev_yr1": "FY2026E", "rev_est1": None,
    "rev_yr2": "FY2027E", "rev_est2": None,
    "eps_est1": None, "eps_est2": None,
    "pe_fwd": None, "ev_ebitda_fwd": None,
    "conviction_score": 5,
    "one_line_thesis": "Monitoring for catalyst.",
    "price_targets": {},
    "position_size": "1-2% of portfolio",
    "entry_point": "Current levels",
    "time_horizon": "12 months",
    "invalidation": "Significant revenue miss or macro deterioration",
    "hedge": "See key risks",
}


def generate_analysis(
    ticker: str, company: str, exch: str,
    price_data: dict, news: list, filings: list,
    macro_data: dict, client: anthropic.Anthropic,
    options_data: dict | None = None,
    insider_data: dict | None = None,
    institutional_data: dict | None = None,
) -> dict:
    price   = price_data["price"]
    chg     = price_data["change_pct"]
    cap     = fmt_cap(price_data["market_cap"])
    hi52    = price_data["wk52_high"]
    lo52    = price_data["wk52_low"]
    pct_rng = ((price - lo52) / (hi52 - lo52) * 100) if (hi52 > lo52) else 0
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    hist_lines = "\n".join(
        f"  {h['date']}: ${h['close']} (vol {h['volume']:,})"
        for h in price_data.get("history", [])
    ) or "  (no history)"

    news_lines = "\n".join(
        f"  [{n['time']}] {n['publisher']}: {n['title']}" for n in news[:6]
    ) or "  No news available."

    filing_lines = "\n".join(
        f"  🚨 {f['form']} filed {f['date']}: {f.get('title','')}" for f in filings
    ) or "  No new filings in last 36 hours."

    # Top sector movers for context
    sectors = macro_data.get("sectors", {})
    sector_lines = "\n".join(
        f"  {v['name']:14s} | {v['chg_1d']:+.2f}% (1D) | {v['chg_5d']:+.2f}% (5D)"
        for v in sorted(sectors.values(), key=lambda x: x["chg_1d"], reverse=True)[:6]
    ) or "  No sector data."

    prompt = f"""You are a senior equity research analyst at {FIRM_NAME} writing a pre-market Company Update note.
Institutional quality: precise, data-referenced, concise. Today: {date_str}.

TICKER: {ticker}  |  COMPANY: {company}  |  EXCHANGE: {exch}
Price: ${price:.2f} ({chg:+.2f}%)  |  Mkt Cap: {cap}  |  52-Wk: ${lo52}–${hi52}  ({pct_rng:.0f}% of range)

RECENT PRICE ACTION:
{hist_lines}

OVERNIGHT NEWS:
{news_lines}

SEC FILINGS (last 36 hrs):
{filing_lines}

TOP SECTOR ETF MOVERS (context):
{sector_lines}

SMART MONEY SIGNALS:
{_fmt_smart_money(options_data, insider_data, institutional_data)}

OUTPUT EXACTLY this format — no extra text outside these markers:

===JSON_START===
{{
  "rating": "Buy",
  "price_target": 65.00,
  "headline": "{ticker}: [Concise event-driven headline 10 words max; include 'Reiterate Buy/Neutral/Sell' if no major news]",
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
  "ev_ebitda_fwd": null,
  "conviction_score": 7,
  "one_line_thesis": "10-15 word thesis summarizing the core investment case",
  "price_targets": {{
    "1mo":  {{"bull": 55.00, "base": 48.00, "bear": 40.00}},
    "6mo":  {{"bull": 72.00, "base": 58.00, "bear": 38.00}},
    "1yr":  {{"bull": 90.00, "base": 65.00, "bear": 32.00}},
    "2yr":  {{"bull": 120.00, "base": 80.00, "bear": 28.00}},
    "5yr":  {{"bull": 200.00, "base": 120.00, "bear": 20.00}},
    "10yr": {{"bull": 400.00, "base": 200.00, "bear": 15.00}}
  }},
  "position_size": "3-5% of portfolio",
  "entry_point": "$45-50 on pullbacks",
  "time_horizon": "12-24 months",
  "invalidation": "Revenue miss >10% or loss of key health system partnership",
  "hedge": "Short comparable-stage AI name at richer multiple"
}}
===JSON_END===

===WHAT_CHANGED===
• [Specific bullet on overnight filing, price move, or news catalyst — be data-specific]
• [Second bullet if applicable; omit if only one item]
===END_WHAT_CHANGED===

===IMPLICATIONS===
[REQUIRED — 2–3 sentences. NEVER write "No implications to report" or any placeholder. Even when there are no new SEC filings, synthesize from the overnight news, recent price action, and sector context above to explain what the current market environment means for {ticker}'s investment thesis. Be specific to {ticker}'s business model and near-term drivers.]
===END_IMPLICATIONS===

===VALUATION===
[2–3 sentences. Current valuation methodology; reference P/S, EV/Revenue, or EV/EBITDA as appropriate for {ticker}'s stage. State upside/downside to your PT. Note key multiple expansion or contraction driver.]
===END_VALUATION===

===KEY_RISKS===
• [Specific risk 1]
• [Specific risk 2]
• [Specific risk 3]
===END_KEY_RISKS===

===PRICE_OUTLOOK_THESIS===
[2–3 sentences: what must go right for the bull case, what is the base case narrative, what catalyst triggers the bear case.]
===END_PRICE_OUTLOOK_THESIS===

===SECTOR_INTELLIGENCE===
HOT SECTORS: [sector], [sector]
COLD SECTORS: [sector], [sector]
ROTATION WATCH: [1–2 sentences on likely near-term sector rotation based on macro tailwinds]
BYPRODUCT PLAY: [1–2 sentences on secondary opportunities created by trends benefiting {ticker}'s space]
===END_SECTOR_INTELLIGENCE===

===OUTPERFORMERS===
• [TICKER] ([Company name]): [specific metric/reason showing this company is outpacing its sector]
• [TICKER] ([Company name]): [specific metric/reason]
{ticker} CONTEXT: [Is {ticker} pulling away from its peer group or lagging? What does relative performance say?]
===END_OUTPERFORMERS===

===INVESTMENT_STRATEGY===
CONVICTION: {{}}/10 — [one-sentence rationale grounded in the data above]
POSITION SIZE: [suggested % of portfolio and why this sizing fits the risk profile]
ENTRY POINTS: [specific price levels or technical/fundamental conditions for entry]
TIME HORIZON: [specific timeframe and the key catalyst expected within that window]
THESIS INVALIDATION: [specific, measurable data points that would trigger an exit]
HEDGING: [specific hedge instrument, pair trade, or options strategy]
===END_INVESTMENT_STRATEGY===

SCORING GUIDE for Investment Profile (1–10):
  growth_score: revenue CAGR trajectory (10 = hypergrowth >100%/yr)
  returns_score: current FCF/EBITDA margins (10 = highly profitable; 1 = deeply negative)
  multiple_score: valuation richness (10 = extremely expensive vs peers; 1 = deep value)
  volatility_score: stock price volatility / binary outcome risk (10 = extreme)
  conviction_score: overall analyst conviction 1–10 (10 = highest conviction Buy)"""

    log.info("  Calling Claude (%s) for %s …", MODEL, ticker)
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=2500,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as api_err:
        log.error("  Claude API error for %s: %s", ticker, api_err)
        return dict(_JSON_DEFAULTS)
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
    def _section(open_marker: str, close_marker: str) -> str:
        s = raw.find(open_marker)
        e = raw.find(close_marker)
        if s == -1 or e == -1:
            return ""
        return raw[s + len(open_marker):e].strip()

    parsed["what_changed"]          = _section("===WHAT_CHANGED===",          "===END_WHAT_CHANGED===")
    parsed["implications"]          = _section("===IMPLICATIONS===",           "===END_IMPLICATIONS===")
    parsed["valuation"]             = _section("===VALUATION===",              "===END_VALUATION===")
    parsed["key_risks"]             = _section("===KEY_RISKS===",              "===END_KEY_RISKS===")
    parsed["price_outlook_thesis"]  = _section("===PRICE_OUTLOOK_THESIS===",   "===END_PRICE_OUTLOOK_THESIS===")
    parsed["sector_intelligence"]   = _section("===SECTOR_INTELLIGENCE===",    "===END_SECTOR_INTELLIGENCE===")
    parsed["outperformers"]         = _section("===OUTPERFORMERS===",          "===END_OUTPERFORMERS===")
    parsed["investment_strategy"]   = _section("===INVESTMENT_STRATEGY===",    "===END_INVESTMENT_STRATEGY===")
    return parsed


def generate_macro_analysis(macro_data: dict, client: anthropic.Anthropic) -> str:
    """Single Claude call to generate a macro intelligence briefing."""
    sectors = macro_data.get("sectors", {})
    rates   = macro_data.get("rates", {})
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    sector_lines = "\n".join(
        f"  {v['name']:14s} | {v['price']:7.2f} | 1D: {v['chg_1d']:+.2f}% | 5D: {v['chg_5d']:+.2f}% | 1M: {v['chg_1m']:+.2f}%"
        for k, v in sorted(sectors.items(), key=lambda x: x[1]["chg_1d"], reverse=True)
    ) or "  No sector data available."

    rate_lines = "\n".join(
        f"  {v['name']:12s} | {v['value']:8.4f} | Δ {v['chg']:+.4f}"
        for k, v in rates.items()
    ) or "  No rate data available."

    prompt = f"""You are the Chief Macro Strategist at {FIRM_NAME}.
Today: {date_str}. Write a concise institutional macro briefing for AI/tech equity investors.

RATE ENVIRONMENT:
{rate_lines}

SECTOR ETF PERFORMANCE (1D | 5D | 1M):
{sector_lines}

OUTPUT EXACTLY this format — no extra text outside markers:

===MACRO_ANALYSIS===
• [Key rate/Fed observation — specific data point and implication]
• [Strongest sector trend and what is driving it]
• [Weakest sector / sector under pressure and why]
• [Risk flag or opportunity for AI/technology companies specifically]
• [One actionable macro conclusion for portfolio positioning today]
===END_MACRO_ANALYSIS==="""

    log.info("  Calling Claude for macro analysis …")
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        s = raw.find("===MACRO_ANALYSIS===")
        e = raw.find("===END_MACRO_ANALYSIS===")
        if s != -1 and e != -1:
            return raw[s + len("===MACRO_ANALYSIS==="):e].strip()
        return raw.strip()
    except Exception as exc:
        log.warning("Macro analysis call failed: %s", exc)
        return "• Macro data analysis unavailable."


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
        "body_sm": ParagraphStyle(
            "BodySm", fontName="Helvetica", fontSize=7.5, leading=11,
            textColor=GS_TEXT, spaceAfter=0, alignment=TA_JUSTIFY,
        ),
        "bullet": ParagraphStyle(
            "Bullet", fontName="Helvetica", fontSize=8.5, leading=12,
            textColor=GS_TEXT, leftIndent=10, firstLineIndent=-8, spaceAfter=1,
        ),
        "bullet_sm": ParagraphStyle(
            "BulletSm", fontName="Helvetica", fontSize=7.5, leading=11,
            textColor=GS_TEXT, leftIndent=10, firstLineIndent=-8, spaceAfter=1,
        ),
        "tbl_hdr": ParagraphStyle(
            "TblHdr", fontName="Helvetica-Bold", fontSize=7.5, textColor=white,
            alignment=TA_LEFT,
        ),
        "tbl_hdr_c": ParagraphStyle(
            "TblHdrC", fontName="Helvetica-Bold", fontSize=7.5, textColor=white,
            alignment=TA_CENTER,
        ),
        "tbl_lbl": ParagraphStyle(
            "TblLbl", fontName="Helvetica", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_LEFT,
        ),
        "tbl_val": ParagraphStyle(
            "TblVal", fontName="Helvetica-Bold", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_RIGHT,
        ),
        "tbl_bull": ParagraphStyle(
            "TblBull", fontName="Helvetica-Bold", fontSize=7.5, textColor=BULL_COL,
            alignment=TA_CENTER,
        ),
        "tbl_base": ParagraphStyle(
            "TblBase", fontName="Helvetica", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_CENTER,
        ),
        "tbl_bear": ParagraphStyle(
            "TblBear", fontName="Helvetica-Bold", fontSize=7.5, textColor=BEAR_COL,
            alignment=TA_CENTER,
        ),
        "chart_cap": ParagraphStyle(
            "ChartCap", fontName="Helvetica", fontSize=6, textColor=GS_DGRAY,
            alignment=TA_CENTER,
        ),
        "key_val": ParagraphStyle(
            "KV", fontName="Helvetica-Bold", fontSize=8.5, textColor=GS_NAVY,
            alignment=TA_LEFT,
        ),
        "label_sm": ParagraphStyle(
            "LblSm", fontName="Helvetica", fontSize=6.5, textColor=GS_DGRAY,
            alignment=TA_LEFT,
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _sec_hdr_table(title: str, col_w: float, st: dict) -> Table:
    """Full-width colored section header band."""
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


def _bullets(text: str, style) -> list:
    """Convert newline-separated bullet text to Paragraph list."""
    items = []
    for line in text.split("\n"):
        line = line.strip().lstrip("•‒–—-").strip()
        if line:
            items.append(Paragraph(f"• {xe(line)}", style))
    return items or [Paragraph("• No material updates.", style)]


def _sm_table(story: list, rows: list, col_w: float) -> None:
    """Append a compact 2-col label|value table to story (for smart money sections)."""
    lw = col_w * 0.42
    vw = col_w * 0.58
    val_style = ParagraphStyle(
        "SMV", fontName="Helvetica-Bold", fontSize=7.5, leading=11,
        textColor=GS_TEXT, alignment=TA_RIGHT,
    )
    lbl_style = ParagraphStyle(
        "SML", fontName="Helvetica", fontSize=7.5, leading=11,
        textColor=GS_DGRAY, alignment=TA_LEFT,
    )
    tbl_rows = [
        [Paragraph(xe(str(lbl)), lbl_style), Paragraph(str(val), val_style)]
        for lbl, val in rows
    ]
    tbl = Table(tbl_rows, colWidths=[lw, vw])
    tbl.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [white, GS_LGRAY]),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 4),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("GRID",          (0, 0), (-1, -1), 0.2, GS_LINE),
    ]))
    story.append(tbl)


def _fmt_smart_money(
    options_data: dict | None,
    insider_data: dict | None,
    institutional_data: dict | None,
) -> str:
    """Format smart money data as prompt context for Claude."""
    lines = []
    opt  = options_data  or {}
    ins  = insider_data  or {}
    inst = institutional_data or {}

    lines.append("OPTIONS FLOW (near-term, ~60 days):")
    lines.append(
        f"  Put/Call Ratio: {opt.get('put_call_ratio', 0):.2f}"
        f"  |  Calls: {opt.get('total_calls', 0):,}"
        f"  |  Puts: {opt.get('total_puts', 0):,}"
        f"  |  Signal: {opt.get('flow_signal', 'n/a').upper()}"
    )
    for u in opt.get("unusual", [])[:3]:
        lines.append(
            f"  UNUSUAL {u['type'].upper()} ${u['strike']} exp {u['expiry'][:7]}"
            f" — {u['volume']:,} contracts ({u['vol_oi_ratio']:.1f}x OI)"
            f" ${u['notional']:,} notional"
        )
    if not opt.get("unusual"):
        lines.append("  No unusual options activity.")

    lines.append("INSIDER TRANSACTIONS (last 30 days):")
    lines.append(
        f"  Net Signal: {ins.get('net_signal','n/a').upper()}"
        f"  |  Significant Buys: {ins.get('significant_buys', 0)}"
        f"  |  Cluster Selling: {ins.get('cluster_selling', False)}"
    )
    for t in ins.get("transactions", [])[:4]:
        lines.append(
            f"  {t['date']} {t['name']} ({t['title']}): "
            f"{t['type'].upper()} {t['shares']:,} @ ${t['price']:.2f}"
            f" = ${t['value']:,.0f}"
            + ("  <- SIGNIFICANT" if t.get("significant") else "")
        )
    if not ins.get("transactions"):
        lines.append("  No insider transactions in the past 30 days.")

    lines.append("INSTITUTIONAL OWNERSHIP:")
    lines.append(
        f"  Institutions: {inst.get('pct_institutional', 0):.1f}%"
        f"  |  Insiders: {inst.get('pct_insiders', 0):.1f}%"
        f"  |  Holders: {inst.get('holder_count', 0):,}"
        f"  |  Signal: {inst.get('smart_money_signal', 'n/a').upper()}"
    )
    for h in inst.get("top_holders", [])[:3]:
        lines.append(f"  {h['name']}: {h['pct_out']:.1f}% ({h['shares']:,} sh)")
    if not inst.get("top_holders"):
        lines.append("  No institutional holder data.")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 1 — LEFT COLUMN
# ════════════════════════════════════════════════════════════════════════════

def build_left_story(
    analysis: dict, news: list, filings: list, st: dict,
    options_data: dict | None = None,
    insider_data: dict | None = None,
    institutional_data: dict | None = None,
) -> list:
    W     = LCOL_W
    story = [Spacer(1, 3)]

    # What's Changed
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

    # Implications
    story.append(_sec_hdr_table("Implications", W, st))
    story.append(Spacer(1, 4))
    impl = analysis.get("implications", "").strip()
    if not impl:
        impl = "No new material overnight filings; the thesis is unchanged. Monitor price action and sector rotation for near-term entry signals."
    story.append(Paragraph(xe(impl), st["body"]))
    story.append(Spacer(1, 8))

    # Valuation
    story.append(_sec_hdr_table("Valuation", W, st))
    story.append(Spacer(1, 4))
    val = analysis.get("valuation", "").strip()
    story.append(Paragraph(xe(val) if val else "See key data table.", st["body"]))
    story.append(Spacer(1, 8))

    # Key Risks
    story.append(_sec_hdr_table("Key Risks", W, st))
    story.append(Spacer(1, 4))
    story.extend(_bullets(analysis.get("key_risks", ""), st["bullet"]))
    story.append(Spacer(1, 10))

    # Recent News
    story.append(_sec_hdr_table("Recent News", W, st))
    story.append(Spacer(1, 4))
    news_style = ParagraphStyle(
        "NS", fontName="Helvetica", fontSize=7.5, leading=11,
        textColor=GS_TEXT, spaceAfter=3,
    )
    for n in news[:5]:
        title = xe(n["title"][:90] + ("…" if len(n["title"]) > 90 else ""))
        pub   = xe(n["publisher"])
        tm    = xe(n["time"][:16])
        story.append(Paragraph(
            f'<b>{title}</b>  <font color="#666666" size="7">{pub} · {tm}</font>',
            news_style,
        ))

    # ── Options Intelligence ───────────────────────────────────────────────
    if options_data and not options_data.get("error"):
        story.append(Spacer(1, 8))
        story.append(_sec_hdr_table("Options Intelligence", W, st))
        story.append(Spacer(1, 3))
        opt = options_data
        sig = opt.get("flow_signal", "neutral")
        sig_col = "#1A5276" if sig == "bullish" else "#7B241C" if sig == "bearish" else "#4A5568"
        opt_rows: list = [
            ["Put/Call Ratio", f'{opt.get("put_call_ratio", 0):.2f}'],
            ["Call Vol / Put Vol",
             f'{opt.get("total_calls", 0):,} / {opt.get("total_puts", 0):,}'],
            ["Flow Signal",
             f'<font color="{sig_col}"><b>{sig.upper()}</b></font>'],
        ]
        unusual = opt.get("unusual", [])
        if unusual:
            u = unusual[0]
            opt_rows.append([
                "Largest Unusual",
                xe(f'{u["type"].upper()} ${u["strike"]} {u["expiry"][:7]} '
                   f'({u["vol_oi_ratio"]:.1f}x OI)'),
            ])
        _sm_table(story, opt_rows, W)
        if opt.get("summary"):
            story.append(Spacer(1, 2))
            story.append(Paragraph(xe(opt["summary"]), st["body_sm"]))

    # ── Insider Activity ───────────────────────────────────────────────────
    if insider_data and not insider_data.get("error"):
        story.append(Spacer(1, 8))
        story.append(_sec_hdr_table("Insider Activity  (30 Days)", W, st))
        story.append(Spacer(1, 3))
        ins = insider_data
        sig = ins.get("net_signal", "neutral")
        sig_col = "#1A5276" if sig == "bullish" else "#7B241C" if sig == "bearish" else "#4A5568"
        ins_rows: list = [
            ["Net Signal",
             f'<font color="{sig_col}"><b>{sig.upper()}</b></font>'],
            ["Significant Buys (>$100K)", str(ins.get("significant_buys", 0))],
            ["Cluster Selling",
             '<font color="#7B241C"><b>YES</b></font>'
             if ins.get("cluster_selling") else "No"],
            ["Transactions", str(len(ins.get("transactions", [])))],
        ]
        txns = ins.get("transactions", [])
        if txns:
            t = txns[0]
            ins_rows.append([
                f'{t["type"].upper()}  {t["date"][:10]}',
                xe(f'{t["name"]} ({t["title"][:14]}): ${t["value"]:,.0f}'),
            ])
        _sm_table(story, ins_rows, W)
        if ins.get("summary"):
            story.append(Spacer(1, 2))
            story.append(Paragraph(xe(ins["summary"]), st["body_sm"]))

    # ── Institutional Ownership ────────────────────────────────────────────
    if institutional_data and not institutional_data.get("error"):
        story.append(Spacer(1, 8))
        story.append(_sec_hdr_table("Institutional Ownership", W, st))
        story.append(Spacer(1, 3))
        inst = institutional_data
        sig = inst.get("smart_money_signal", "neutral")
        sig_col = "#1A5276" if sig == "bullish" else "#7B241C" if sig == "bearish" else "#4A5568"
        inst_rows: list = [
            ["Institutions",
             f'{inst.get("pct_institutional", 0):.1f}%'],
            ["Insiders",
             f'{inst.get("pct_insiders", 0):.1f}%'],
            ["# Holders", f'{inst.get("holder_count", 0):,}'],
            ["Smart Money",
             f'<font color="{sig_col}"><b>{sig.upper()}</b></font>'],
        ]
        for h in inst.get("top_holders", [])[:2]:
            inst_rows.append([
                xe(h["name"][:26]),
                f'{h["pct_out"]:.1f}%',
            ])
        _sm_table(story, inst_rows, W)
        if inst.get("summary"):
            story.append(Spacer(1, 2))
            story.append(Paragraph(xe(inst["summary"]), st["body_sm"]))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 1 — RIGHT COLUMN
# ════════════════════════════════════════════════════════════════════════════

def _key_data_table(analysis: dict, price_data: dict, st: dict) -> Table:
    a     = analysis
    pd_   = price_data
    price = pd_["price"]
    pt    = a.get("price_target")
    upside = ((pt - price) / price * 100) if pt else None

    def _fmt_eps(v):
        if v is None: return "N/M"
        return f"(${abs(v):.2f})" if v < 0 else f"${v:.2f}"

    def _fmt_rev(v):
        if v is None: return "—"
        return f"${v:,.0f}M" if v >= 1 else f"${v*1000:.0f}K"

    rows = [
        ("Price (USD)",                f"${price:.2f}"),
        ("12M Price Target",           f"${pt:.2f}" if pt else "—"),
        ("Upside / (Downside)",        f"{upside:+.1f}%" if upside is not None else "—"),
        ("Market Cap",                 fmt_cap(pd_["market_cap"])),
        (f"{a.get('rev_yr1','FY2026E')} Revenue", _fmt_rev(a.get("rev_est1"))),
        (f"{a.get('rev_yr2','FY2027E')} Revenue", _fmt_rev(a.get("rev_est2"))),
        (f"{a.get('rev_yr1','FY2026E')} EPS",     _fmt_eps(a.get("eps_est1"))),
        (f"{a.get('rev_yr2','FY2027E')} EPS",     _fmt_eps(a.get("eps_est2"))),
        ("Fwd P/E",                    f"{a['pe_fwd']:.1f}x" if a.get("pe_fwd") else "N/M"),
    ]

    LW = RCOL_W * 0.64
    VW = RCOL_W * 0.36
    data = [[Paragraph("Key Data", st["tbl_hdr"]), ""]]
    for lbl, val in rows:
        data.append([Paragraph(xe(lbl), st["tbl_lbl"]),
                     Paragraph(xe(val), st["tbl_val"])])

    tbl_style = [
        ("BACKGROUND",    (0,0),  (-1,0),  GS_NAVY),
        ("SPAN",          (0,0),  (-1,0)),
        ("TOPPADDING",    (0,0),  (-1,0),  4),
        ("BOTTOMPADDING", (0,0),  (-1,0),  4),
        ("LEFTPADDING",   (0,0),  (0,0),   6),
        ("FONTSIZE",      (0,1),  (-1,-1), 7.5),
        ("TOPPADDING",    (0,1),  (-1,-1), 3),
        ("BOTTOMPADDING", (0,1),  (-1,-1), 3),
        ("LEFTPADDING",   (0,1),  (0,-1),  6),
        ("RIGHTPADDING",  (-1,1), (-1,-1), 6),
        ("LINEBELOW",     (0,0),  (-1,-1), 0.4, GS_LINE),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
    ]
    for i in range(1, len(data)):
        tbl_style.append(("BACKGROUND", (0,i), (-1,i), GS_LGRAY if i % 2 == 0 else white))
    if upside is not None:
        uc = BULL_COL if upside >= 0 else BEAR_COL
        tbl_style.append(("TEXTCOLOR", (1,3), (1,3), uc))
        tbl_style.append(("FONTNAME",  (1,3), (1,3), "Helvetica-Bold"))

    t = Table(data, colWidths=[LW, VW])
    t.setStyle(TableStyle(tbl_style))
    return t


def build_right_story(analysis: dict, price_data: dict, charts: dict, st: dict) -> list:
    story = [Spacer(1, 3)]
    story.append(_key_data_table(analysis, price_data, st))
    story.append(Spacer(1, 10))

    story.append(_sec_hdr_table("Investment Profile", RCOL_W, st))
    story.append(Spacer(1, 4))
    if charts.get("profile"):
        story.append(Image(charts["profile"], width=RCOL_W, height=88))
    story.append(Spacer(1, 8))

    story.append(_sec_hdr_table("12-Month Price Performance vs. S&P 500", RCOL_W, st))
    story.append(Spacer(1, 3))
    if charts.get("price"):
        story.append(Image(charts["price"], width=RCOL_W, height=200))
    else:
        story.append(Paragraph("Price history unavailable.", st["body"]))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 2 — PRICE OUTLOOK TABLE
# ════════════════════════════════════════════════════════════════════════════

def build_price_outlook_table(analysis: dict, st: dict, col_w: float) -> Table:
    """6-timeframe × Bull/Base/Bear price target table."""
    price_targets = analysis.get("price_targets", {})
    timeframes    = [("1mo","1 Month"),("6mo","6 Months"),("1yr","1 Year"),
                     ("2yr","2 Years"),("5yr","5 Years"),("10yr","10 Years")]

    TW  = col_w * 0.20
    BLW = col_w * 0.265
    BSW = col_w * 0.265
    BRW = col_w * 0.265

    header = [
        Paragraph("TIMEFRAME",  st["tbl_hdr_c"]),
        Paragraph("BULL CASE",  st["tbl_hdr_c"]),
        Paragraph("BASE CASE",  st["tbl_hdr_c"]),
        Paragraph("BEAR CASE",  st["tbl_hdr_c"]),
    ]
    data = [header]

    for tf, label in timeframes:
        targets = price_targets.get(tf, {})
        bull    = targets.get("bull")
        base    = targets.get("base")
        bear    = targets.get("bear")
        data.append([
            Paragraph(label, st["tbl_lbl"]),
            Paragraph(f"${bull:.2f}" if bull else "—", st["tbl_bull"]),
            Paragraph(f"${base:.2f}" if base else "—", st["tbl_base"]),
            Paragraph(f"${bear:.2f}" if bear else "—", st["tbl_bear"]),
        ])

    tbl_style = [
        ("BACKGROUND",    (0,0), (-1,0),  GS_NAVY),
        ("TOPPADDING",    (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
        ("RIGHTPADDING",  (0,0), (-1,-1), 6),
        ("LINEBELOW",     (0,0), (-1,-1), 0.4, GS_LINE),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]
    for i in range(1, len(data)):
        bg = GS_LGRAY if i % 2 == 0 else white
        tbl_style.append(("BACKGROUND", (0,i), (-1,i), bg))

    t = Table(data, colWidths=[TW, BLW, BSW, BRW])
    t.setStyle(TableStyle(tbl_style))
    return t


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 2 — SECTOR INTELLIGENCE + OUTPERFORMERS (LEFT)
# ════════════════════════════════════════════════════════════════════════════

def build_page2_left_story(analysis: dict, st: dict, col_w: float) -> list:
    story = [Spacer(1, 3)]

    # Sector Intelligence
    story.append(_sec_hdr_table("Sector Intelligence", col_w, st))
    story.append(Spacer(1, 4))
    si_text = analysis.get("sector_intelligence", "").strip()
    if si_text:
        for line in si_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            # Lines like "HOT SECTORS: ..." or "ROTATION WATCH: ..."
            if ":" in line and line.split(":")[0].upper() == line.split(":")[0]:
                label, _, rest = line.partition(":")
                story.append(Paragraph(
                    f'<b><font color="#002F5F">{xe(label.strip())}:</font></b> {xe(rest.strip())}',
                    st["body_sm"],
                ))
                story.append(Spacer(1, 3))
            else:
                story.append(Paragraph(xe(line), st["body_sm"]))
                story.append(Spacer(1, 2))
    else:
        story.append(Paragraph("Sector data unavailable.", st["body_sm"]))
    story.append(Spacer(1, 10))

    # Outperformers
    story.append(_sec_hdr_table("Outperformers & Peer Context", col_w, st))
    story.append(Spacer(1, 4))
    op_text = analysis.get("outperformers", "").strip()
    if op_text:
        for line in op_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if line.startswith("•"):
                content = line.lstrip("•").strip()
                story.append(Paragraph(f"• {xe(content)}", st["bullet_sm"]))
            elif line.upper().startswith("CONTEXT") or "CONTEXT" in line[:20].upper():
                label, _, rest = line.partition(":")
                story.append(Spacer(1, 4))
                story.append(Paragraph(
                    f'<b><font color="#002F5F">{xe(label.strip())}:</font></b> {xe(rest.strip())}',
                    st["body_sm"],
                ))
            else:
                story.append(Paragraph(xe(line), st["body_sm"]))
    else:
        story.append(Paragraph("Sector peer analysis unavailable.", st["body_sm"]))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 2 — INVESTMENT STRATEGY (RIGHT)
# ════════════════════════════════════════════════════════════════════════════

def build_page2_right_story(analysis: dict, st: dict, col_w: float) -> list:
    story = [Spacer(1, 3)]
    story.append(_sec_hdr_table("Investment Strategy", col_w, st))
    story.append(Spacer(1, 4))

    # Conviction score badge area
    conv  = analysis.get("conviction_score", 5)
    conv  = max(1, min(10, int(conv)))
    story.append(Paragraph(
        f'<b><font color="#002F5F" size="14">{conv}/10</font></b> '
        f'<font color="#4A5568" size="8">Conviction Score</font>',
        ParagraphStyle("CVS", fontName="Helvetica-Bold", fontSize=12, leading=16,
                       textColor=GS_NAVY),
    ))
    story.append(Spacer(1, 6))

    # Strategy lines from Claude
    is_text = analysis.get("investment_strategy", "").strip()
    if is_text:
        for line in is_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                label, _, rest = line.partition(":")
                label_u = label.strip().upper()
                # Bold label for known keys
                known = {"CONVICTION","POSITION SIZE","ENTRY POINTS","TIME HORIZON",
                         "THESIS INVALIDATION","HEDGING","POSITION","ENTRY","HORIZON",
                         "INVALIDATION","HEDGE"}
                if any(k in label_u for k in known):
                    story.append(Paragraph(
                        f'<b><font color="#002F5F">{xe(label.strip())}:</font></b> {xe(rest.strip())}',
                        st["body_sm"],
                    ))
                    story.append(Spacer(1, 4))
                else:
                    story.append(Paragraph(xe(line), st["body_sm"]))
            else:
                story.append(Paragraph(xe(line), st["body_sm"]))
    else:
        # Fallback to JSON fields
        fields = [
            ("Position Size",      analysis.get("position_size", "—")),
            ("Entry Points",       analysis.get("entry_point",   "—")),
            ("Time Horizon",       analysis.get("time_horizon",  "—")),
            ("Thesis Invalidation",analysis.get("invalidation",  "—")),
            ("Hedging",            analysis.get("hedge",         "—")),
        ]
        for label, val in fields:
            story.append(Paragraph(
                f'<b><font color="#002F5F">{xe(label)}:</font></b> {xe(str(val))}',
                st["body_sm"],
            ))
            story.append(Spacer(1, 5))

    return story


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS DRAWING — SHARED HEADER / FOOTER
# ════════════════════════════════════════════════════════════════════════════

def _rating_badge(c, x: float, y: float, rating: str):
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
    rating   = analysis.get("rating", "Neutral")
    pt       = analysis.get("price_target")
    price    = price_data["price"]
    chg      = price_data["change_pct"]
    hi52     = price_data["wk52_high"]
    lo52     = price_data["wk52_low"]
    headline = analysis.get("headline", "No Material Overnight Updates")
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Navy top band
    c.setFillColor(GS_NAVY)
    c.rect(0, HDR_BAND_BOT, PW, HDR_BAND_H, fill=1, stroke=0)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(white)
    c.drawString(36, HDR_BAND_BOT + 18, date_str)
    c.setFont("Helvetica", 6.5)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, HDR_BAND_BOT + 7, "EQUITY RESEARCH  |  COMPANY UPDATE")
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(white)
    gw = stringWidth(FIRM_NAME_U, "Helvetica-Bold", 8.5)
    c.drawString(576 - gw, HDR_BAND_BOT + 18, FIRM_NAME_U)
    c.setStrokeColor(GOLD_COL)
    c.setLineWidth(1.2)
    c.line(576 - gw, HDR_BAND_BOT + 15, 576, HDR_BAND_BOT + 15)

    # Company nameplate
    c.setFillColor(white)
    c.rect(0, HDR_COMP_BOT, PW, HDR_COMP_H, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 15)
    c.setFillColor(GS_TEXT)
    c.drawString(36, HDR_COMP_BOT + HDR_COMP_H - 22, company)
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(GS_BLUE)
    c.drawString(36, HDR_COMP_BOT + HDR_COMP_H - 37, ticker)
    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_DGRAY)
    tkw = stringWidth(ticker, "Helvetica-Bold", 10)
    c.drawString(36 + tkw + 8, HDR_COMP_BOT + HDR_COMP_H - 36.5, f"  {exch}")
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.5)
    c.line(36, HDR_COMP_BOT + 14, 576, HDR_COMP_BOT + 14)

    # Rating row
    c.setFillColor(GS_LGRAY)
    c.rect(0, HDR_RAT_BOT, PW, HDR_RAT_H, fill=1, stroke=0)
    row_mid = HDR_RAT_BOT + HDR_RAT_H / 2
    _rating_badge(c, 36, row_mid - 6, rating)
    chg_col = BULL_COL if chg >= 0 else BEAR_COL
    sign    = "▲" if chg >= 0 else "▼"
    c.setFont("Helvetica-Bold", 9.5)
    c.setFillColor(GS_TEXT)
    c.drawString(76, row_mid - 3.5, f"${price:.2f}")
    pw_ = stringWidth(f"${price:.2f}", "Helvetica-Bold", 9.5)
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(chg_col)
    c.drawString(76 + pw_ + 4, row_mid - 3, f"{sign} {abs(chg):.2f}%")
    if pt:
        c.setFont("Helvetica", 8)
        c.setFillColor(GS_DGRAY)
        c.drawString(170, row_mid + 4, "12M Price Target")
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(GS_TEXT)
        c.drawString(170, row_mid - 5, f"${pt:.2f}")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_DGRAY)
    c.drawString(260, row_mid + 4, "52-Week Range")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_TEXT)
    c.drawString(260, row_mid - 5, f"${lo52:.2f} – ${hi52:.2f}")
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_DGRAY)
    c.drawString(370, row_mid + 4, "Market Cap")
    c.setFont("Helvetica", 8.5)
    c.setFillColor(GS_TEXT)
    c.drawString(370, row_mid - 5, fmt_cap(price_data["market_cap"]))

    # Headline
    c.setFillColor(white)
    c.rect(0, HDR_HL_BOT, PW, HDR_HL_H, fill=1, stroke=0)
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
        c.drawString(36, HDR_HL_BOT + HDR_HL_H - 22, line2)

    # Divider
    c.setStrokeColor(GS_NAVY)
    c.setLineWidth(0.8)
    c.line(36, HDR_DIV_Y, 576, HDR_DIV_Y)


def draw_footer(c, label: str):
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.5)
    c.line(36, FTR_DIV_Y, 576, FTR_DIV_Y)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(GS_DGRAY)
    c.drawString(36, FTR_DIV_Y - 11, f"{FIRM_NAME}  |  Equity Research Coverage")
    c.setFont("Helvetica", 6.5)
    c.setFillColor(GS_MGRAY)
    c.drawString(36, FTR_DIV_Y - 22, f"AI-Generated Pre-Market Brief  |  {now_str}")
    disc = (f"This report is prepared by {FIRM_NAME} using SEC EDGAR filings, market data (yfinance), and "
            "AI-generated analysis. It does not constitute investment advice. All figures are estimates. "
            "Past performance is not indicative of future results. Investors should conduct their own due diligence.")
    c.setFont("Helvetica", 5.5)
    c.setFillColor(GS_MGRAY)
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
    c.setFont("Helvetica", 6)
    c.setFillColor(GS_MGRAY)
    c.drawRightString(576, FTR_DIV_Y - 11, label)


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS DRAWING — PAGE 2 HEADER
# ════════════════════════════════════════════════════════════════════════════

def draw_page2_header(c, ticker: str, company: str, section_title: str):
    """Compact header for page 2 (price outlook / strategy page)."""
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Navy band
    c.setFillColor(GS_NAVY)
    c.rect(0, 752, PW, 36, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 11)
    c.setFillColor(white)
    c.drawString(36, 767, f"{ticker}  —  {section_title}")
    c.setFont("Helvetica", 7)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, 757, "EQUITY RESEARCH  |  EXTENDED ANALYSIS")
    c.setFont("Helvetica-Bold", 8.5)
    c.setFillColor(white)
    gw = stringWidth(FIRM_NAME_U, "Helvetica-Bold", 8.5)
    c.drawString(576 - gw, 767, FIRM_NAME_U)
    c.setStrokeColor(GOLD_COL)
    c.setLineWidth(1.2)
    c.line(576 - gw, 764, 576, 764)

    # Company sub-row
    c.setFillColor(GS_LGRAY)
    c.rect(0, 736, PW, 16, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 8)
    c.setFillColor(GS_BLUE)
    c.drawString(36, 741, ticker)
    c.setFont("Helvetica", 8)
    c.setFillColor(GS_DGRAY)
    tkw = stringWidth(ticker, "Helvetica-Bold", 8)
    c.drawString(36 + tkw + 6, 741, f" — {company}  |  {date_str}")

    # Divider
    c.setStrokeColor(GS_NAVY)
    c.setLineWidth(0.8)
    c.line(36, 734, 576, 734)


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS DRAWING — MARKET-WIDE INTELLIGENCE (PAGE 1)
# ════════════════════════════════════════════════════════════════════════════

def draw_market_intelligence_page(c, market_intel: dict):
    """Draw the Market-Wide Intelligence page (page 1 of 2) onto the current canvas."""
    st = _styles()
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, 740, PW, 52, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(white)
    c.drawString(36, 770, "MARKET-WIDE OPTIONS INTELLIGENCE")
    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, 757, "CROSS-SECTOR FLOW ANALYSIS  |  UNUSUAL ACTIVITY  |  CREDIT & ROTATION")
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(white)
    c.drawRightString(576, 770, FIRM_NAME_U)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawRightString(576, 757, date_str)

    # Gold accent line
    c.setStrokeColor(GOLD_COL)
    c.setLineWidth(2.0)
    c.line(0, 740, PW, 740)

    y_cursor = 730

    # ── Section 1: Sector Options Flow ────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "SECTOR OPTIONS FLOW  —  PUT/CALL RATIO RANKING")
    y_cursor -= 18

    sector_flow = market_intel.get("sector_flow", {})
    sectors = sector_flow.get("sectors", [])

    hdr_c = ParagraphStyle("SFHC", fontName="Helvetica-Bold", fontSize=6.5,
                           textColor=white, alignment=TA_CENTER)
    hdr_l = ParagraphStyle("SFHL", fontName="Helvetica-Bold", fontSize=6.5,
                           textColor=white, alignment=TA_LEFT)
    cell_c = ParagraphStyle("SFC", fontName="Helvetica", fontSize=6.5,
                            alignment=TA_CENTER)
    cell_l = ParagraphStyle("SFL", fontName="Helvetica", fontSize=6.5,
                            alignment=TA_LEFT)

    sf_rows = [[
        Paragraph("SECTOR", hdr_l),
        Paragraph("ETF", hdr_c),
        Paragraph("P/C RATIO", hdr_c),
        Paragraph("CALL VOL", hdr_c),
        Paragraph("PUT VOL", hdr_c),
        Paragraph("SIGNAL", hdr_c),
        Paragraph("UNUSUAL", hdr_c),
    ]]

    for s in sectors:
        sig = s.get("signal", "neutral")
        if sig == "bullish":
            sig_col = "#1A5276"
        elif sig == "bearish":
            sig_col = "#7B241C"
        else:
            sig_col = "#4A5568"
        unusual_str = "YES" if s.get("unusual_volume", False) else "—"
        sf_rows.append([
            Paragraph(xe(str(s.get("sector", ""))), cell_l),
            Paragraph(xe(str(s.get("etf", ""))), cell_c),
            Paragraph(f'{s.get("put_call_ratio", 0):.2f}', cell_c),
            Paragraph(f'{s.get("call_volume", 0):,.0f}', cell_c),
            Paragraph(f'{s.get("put_volume", 0):,.0f}', cell_c),
            Paragraph(f'<font color="{sig_col}"><b>{xe(sig.upper())}</b></font>', cell_c),
            Paragraph(unusual_str, cell_c),
        ])

    sf_cw = [100, 42, 62, 72, 72, 62, 56]
    sf_tbl = Table(sf_rows, colWidths=sf_cw)
    sf_style_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    for i in range(1, len(sf_rows)):
        sf_style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                              GS_LGRAY if i % 2 == 0 else white))
    sf_tbl.setStyle(TableStyle(sf_style_cmds))

    # Estimate table height: header + data rows
    sf_h = min(14 * len(sf_rows) + 4, 180)
    sf_frame = Frame(36, y_cursor - sf_h, FULL_W, sf_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    sf_frame.addFromList([sf_tbl], c)
    y_cursor -= sf_h + 6

    # ── Section 2: Unusual Market-Wide Activity ───────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "UNUSUAL MARKET-WIDE ACTIVITY  —  TOP 10")
    y_cursor -= 18

    unusual = market_intel.get("unusual_activity", [])[:10]
    ua_story = []
    ua_style = ParagraphStyle("UA", fontName="Helvetica", fontSize=7, leading=10,
                              textColor=GS_TEXT, leftIndent=8, firstLineIndent=-6,
                              spaceAfter=1)
    for item in unusual:
        ticker = item.get("ticker", "?")
        sector = item.get("sector", "")
        desc = item.get("description", item.get("activity", ""))
        impl = item.get("implication", "")
        line = f"<b>{xe(ticker)}</b> ({xe(sector)}): {xe(desc)}"
        if impl:
            line += f" — <i>{xe(impl)}</i>"
        ua_story.append(Paragraph(f"• {line}", ua_style))
    if not ua_story:
        ua_story.append(Paragraph("• No unusual activity detected.", ua_style))

    ua_h = min(len(ua_story) * 12 + 4, 130)
    ua_frame = Frame(36, y_cursor - ua_h, FULL_W, ua_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    ua_frame.addFromList(ua_story, c)
    y_cursor -= ua_h + 6

    # ── Section 3: Rotation Analysis ──────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "ROTATION ANALYSIS  —  AI-GENERATED ASSESSMENT")
    y_cursor -= 18

    rot_text = market_intel.get("rotation_analysis", "")
    rot_style = ParagraphStyle("RA", fontName="Helvetica", fontSize=7.5, leading=11,
                               textColor=GS_TEXT, alignment=TA_JUSTIFY)
    rot_story = []
    if rot_text:
        rot_story.append(Paragraph(xe(rot_text), rot_style))
    else:
        rot_story.append(Paragraph("Rotation analysis unavailable.", rot_style))

    rot_h = min(80, max(40, y_cursor - FTR_DIV_Y - 200))
    rot_frame = Frame(36, y_cursor - rot_h, FULL_W, rot_h,
                      leftPadding=4, rightPadding=4, topPadding=2, bottomPadding=0,
                      showBoundary=0)
    rot_frame.addFromList(rot_story, c)
    y_cursor -= rot_h + 6

    # ── Section 4: Credit Market Signals ──────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "CREDIT MARKET SIGNALS")
    y_cursor -= 18

    credit = market_intel.get("credit_signals", {})
    cr_rows = []
    cr_hdr_c = ParagraphStyle("CRHC", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_CENTER)
    cr_hdr_l = ParagraphStyle("CRHL", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_LEFT)
    cr_cell_c = ParagraphStyle("CRC", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_CENTER)
    cr_cell_l = ParagraphStyle("CRL", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_LEFT)

    cr_rows.append([
        Paragraph("ETF", cr_hdr_l),
        Paragraph("P/C RATIO", cr_hdr_c),
        Paragraph("SIGNAL", cr_hdr_c),
        Paragraph("1W CHG", cr_hdr_c),
        Paragraph("1M CHG", cr_hdr_c),
    ])

    for etf_key in ["HYG", "LQD"]:
        etf_data = credit.get(etf_key, credit.get(etf_key.lower(), {}))
        if etf_data:
            sig = etf_data.get("signal", "neutral")
            sig_col = "#1A5276" if sig == "bullish" else ("#7B241C" if sig == "bearish" else "#4A5568")
            cr_rows.append([
                Paragraph(f"<b>{etf_key}</b>", cr_cell_l),
                Paragraph(f'{etf_data.get("put_call_ratio", 0):.2f}', cr_cell_c),
                Paragraph(f'<font color="{sig_col}"><b>{xe(sig.upper())}</b></font>', cr_cell_c),
                Paragraph(f'{etf_data.get("1w_change", etf_data.get("change_1w", 0)):+.2f}', cr_cell_c),
                Paragraph(f'{etf_data.get("1m_change", etf_data.get("change_1m", 0)):+.2f}', cr_cell_c),
            ])

    if len(cr_rows) > 1:
        cr_cw = [60, 80, 80, 80, 80]
        cr_tbl = Table(cr_rows, colWidths=cr_cw)
        cr_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
            ("BACKGROUND",    (0, 1), (-1, 1), GS_LGRAY),
            ("BACKGROUND",    (0, 2), (-1, 2), white),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        cr_story = [cr_tbl]
    else:
        cr_story = [Paragraph("Credit signal data unavailable.", cr_cell_l)]

    # Overall risk signal
    risk_sig = credit.get("risk_signal", "neutral")
    risk_col = "#1A5276" if risk_sig in ("low_risk", "bullish") else (
        "#7B241C" if risk_sig in ("high_risk", "bearish") else "#4A5568")
    cr_story.append(Spacer(1, 3))
    cr_story.append(Paragraph(
        f'Overall Credit Risk Signal: <font color="{risk_col}"><b>{xe(risk_sig.upper())}</b></font>',
        ParagraphStyle("CRisk", fontName="Helvetica-Bold", fontSize=7, textColor=GS_TEXT)))

    cr_h = 56
    cr_frame = Frame(36, y_cursor - cr_h, FULL_W, cr_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    cr_frame.addFromList(cr_story, c)
    y_cursor -= cr_h + 6

    # ── Section 5: Short Interest & Squeeze Candidates ────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "SHORT INTEREST & SQUEEZE CANDIDATES")
    y_cursor -= 18

    squeeze = market_intel.get("short_interest", {}).get("squeeze_candidates", [])
    sq_hdr_c = ParagraphStyle("SQHC", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_CENTER)
    sq_hdr_l = ParagraphStyle("SQHL", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_LEFT)
    sq_cell_c = ParagraphStyle("SQC", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_CENTER)
    sq_cell_l = ParagraphStyle("SQL", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_LEFT)

    sq_rows = [[
        Paragraph("TICKER", sq_hdr_l),
        Paragraph("SHORT%", sq_hdr_c),
        Paragraph("CHANGE", sq_hdr_c),
        Paragraph("OPTIONS SIGNAL", sq_hdr_c),
    ]]
    for s in squeeze:
        sig = s.get("options_signal", "neutral")
        sig_col = "#1A5276" if sig == "bullish" else ("#7B241C" if sig == "bearish" else "#4A5568")
        sq_rows.append([
            Paragraph(f'<b>{xe(str(s.get("ticker", "")))}</b>', sq_cell_l),
            Paragraph(f'{s.get("short_pct", 0):.1f}%', sq_cell_c),
            Paragraph(f'{s.get("change_pct", 0):+.1f}%', sq_cell_c),
            Paragraph(f'<font color="{sig_col}"><b>{xe(sig.upper())}</b></font>', sq_cell_c),
        ])

    if len(sq_rows) > 1:
        sq_cw = [80, 80, 80, 120]
        sq_tbl = Table(sq_rows, colWidths=sq_cw)
        sq_style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(sq_rows)):
            sq_style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                                  GS_LGRAY if i % 2 == 0 else white))
        sq_tbl.setStyle(TableStyle(sq_style_cmds))
        sq_story = [sq_tbl]
    else:
        sq_story = [Paragraph("No squeeze candidates identified.", sq_cell_c)]

    sq_h = max(30, min(14 * len(sq_rows) + 4, y_cursor - FTR_DIV_Y - 10))
    sq_frame = Frame(36, y_cursor - sq_h, FULL_W, sq_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    sq_frame.addFromList(sq_story, c)

    # ── Footer ────────────────────────────────────────────────────────────────
    draw_footer(c, "Market-Wide Intelligence")


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS DRAWING — MARKET-WIDE INTELLIGENCE (PAGE 2)
# ════════════════════════════════════════════════════════════════════════════

def draw_market_intelligence_page2(c, market_intel: dict):
    """Draw Market-Wide Intelligence page 2: earnings, congress, macro, dark pool."""
    st = _styles()
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, 740, PW, 52, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 14)
    c.setFillColor(white)
    c.drawString(36, 770, "MARKET-WIDE OPTIONS INTELLIGENCE")
    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, 757, "EARNINGS  |  CONGRESSIONAL TRADES  |  MACRO EVENTS  |  DARK POOL")
    c.setFont("Helvetica-Bold", 9)
    c.setFillColor(white)
    c.drawRightString(576, 770, FIRM_NAME_U)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawRightString(576, 757, date_str)

    # Gold accent line
    c.setStrokeColor(GOLD_COL)
    c.setLineWidth(2.0)
    c.line(0, 740, PW, 740)

    y_cursor = 730

    # ── Section 1: Earnings Calendar ──────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "EARNINGS CALENDAR  —  OPTIONS IMPLIED MOVES")
    y_cursor -= 18

    earnings = market_intel.get("earnings_calendar", [])
    e_hdr_c = ParagraphStyle("EHC", fontName="Helvetica-Bold", fontSize=6.5,
                             textColor=white, alignment=TA_CENTER)
    e_hdr_l = ParagraphStyle("EHL", fontName="Helvetica-Bold", fontSize=6.5,
                             textColor=white, alignment=TA_LEFT)
    e_cell_c = ParagraphStyle("EC", fontName="Helvetica", fontSize=6.5,
                              alignment=TA_CENTER)
    e_cell_l = ParagraphStyle("EL", fontName="Helvetica", fontSize=6.5,
                              alignment=TA_LEFT)

    e_rows = [[
        Paragraph("TICKER", e_hdr_l),
        Paragraph("DATE", e_hdr_c),
        Paragraph("IMPLIED MOVE%", e_hdr_c),
        Paragraph("HIST AVG%", e_hdr_c),
        Paragraph("RICH/CHEAP", e_hdr_c),
    ]]
    for e in earnings:
        rc = e.get("rich_cheap", "")
        rc_col = "#7B241C" if rc.lower() == "rich" else (
            "#1A5276" if rc.lower() == "cheap" else "#4A5568")
        e_rows.append([
            Paragraph(f'<b>{xe(str(e.get("ticker", "")))}</b>', e_cell_l),
            Paragraph(xe(str(e.get("earnings_date", e.get("date", "")))), e_cell_c),
            Paragraph(f'{e.get("implied_move_pct", 0):.1f}%', e_cell_c),
            Paragraph(f'{e.get("hist_avg_move_pct", e.get("hist_avg_pct", 0)):.1f}%', e_cell_c),
            Paragraph(f'<font color="{rc_col}"><b>{xe(rc.upper() if rc else "—")}</b></font>', e_cell_c),
        ])

    if len(e_rows) > 1:
        e_cw = [70, 90, 100, 100, 90]
        e_tbl = Table(e_rows, colWidths=e_cw)
        e_style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(e_rows)):
            e_style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                                 GS_LGRAY if i % 2 == 0 else white))
        e_tbl.setStyle(TableStyle(e_style_cmds))
        e_story = [e_tbl]
    else:
        e_story = [Paragraph("No upcoming earnings in calendar.", e_cell_c)]

    e_h = min(14 * len(e_rows) + 4, 140)
    e_frame = Frame(36, y_cursor - e_h, FULL_W, e_h,
                    leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                    showBoundary=0)
    e_frame.addFromList(e_story, c)
    y_cursor -= e_h + 6

    # ── Section 2: Congressional Trades ───────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "CONGRESSIONAL TRADES  —  RECENT DISCLOSURES")
    y_cursor -= 18

    congress = market_intel.get("congressional_trades", [])
    ct_hdr_c = ParagraphStyle("CTHC", fontName="Helvetica-Bold", fontSize=6,
                              textColor=white, alignment=TA_CENTER)
    ct_hdr_l = ParagraphStyle("CTHL", fontName="Helvetica-Bold", fontSize=6,
                              textColor=white, alignment=TA_LEFT)
    ct_cell_c = ParagraphStyle("CTC", fontName="Helvetica", fontSize=6,
                               alignment=TA_CENTER)
    ct_cell_l = ParagraphStyle("CTL", fontName="Helvetica", fontSize=6,
                               alignment=TA_LEFT)

    ct_rows = [[
        Paragraph("MEMBER", ct_hdr_l),
        Paragraph("CHAMBER", ct_hdr_c),
        Paragraph("TICKER", ct_hdr_c),
        Paragraph("TYPE", ct_hdr_c),
        Paragraph("AMOUNT", ct_hdr_c),
        Paragraph("DATE", ct_hdr_c),
        Paragraph("SECTOR", ct_hdr_l),
    ]]
    for t in congress:
        tx_type = t.get("type", "")
        tx_col = "#1A5276" if tx_type.lower() == "purchase" else (
            "#7B241C" if tx_type.lower() == "sale" else "#4A5568")
        ct_rows.append([
            Paragraph(xe(str(t.get("member", ""))[:20]), ct_cell_l),
            Paragraph(xe(str(t.get("chamber", ""))), ct_cell_c),
            Paragraph(f'<b>{xe(str(t.get("ticker", "")))}</b>', ct_cell_c),
            Paragraph(f'<font color="{tx_col}"><b>{xe(tx_type.upper())}</b></font>', ct_cell_c),
            Paragraph(xe(str(t.get("amount", ""))), ct_cell_c),
            Paragraph(xe(str(t.get("date", ""))), ct_cell_c),
            Paragraph(xe(str(t.get("sector", ""))[:16]), ct_cell_l),
        ])

    if len(ct_rows) > 1:
        ct_cw = [90, 52, 50, 50, 72, 64, 74]
        ct_tbl = Table(ct_rows, colWidths=ct_cw)
        ct_style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 2),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 2),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(ct_rows)):
            ct_style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                                  GS_LGRAY if i % 2 == 0 else white))
        ct_tbl.setStyle(TableStyle(ct_style_cmds))
        ct_story = [ct_tbl]
    else:
        ct_story = [Paragraph("No recent congressional trades reported.", ct_cell_c)]

    ct_h = min(14 * len(ct_rows) + 4, 160)
    ct_frame = Frame(36, y_cursor - ct_h, FULL_W, ct_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    ct_frame.addFromList(ct_story, c)
    y_cursor -= ct_h + 6

    # ── Section 3: Macro Events ───────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "MACRO EVENTS  —  UPCOMING CALENDAR")
    y_cursor -= 18

    macro_events = market_intel.get("macro_events", {})
    events = macro_events.get("events", [])
    me_story = []
    me_style = ParagraphStyle("ME", fontName="Helvetica", fontSize=7, leading=10,
                              textColor=GS_TEXT, leftIndent=8, firstLineIndent=-6,
                              spaceAfter=1)

    IMP_COLORS = {"high": "#7B241C", "medium": "#C9A84C", "low": "#4A5568"}
    for ev in events:
        dt = ev.get("date", "")
        name = ev.get("event", ev.get("name", ""))
        imp = ev.get("importance", "medium")
        imp_col = IMP_COLORS.get(imp.lower(), "#4A5568")
        line = (f'<b>{xe(str(dt))}</b>  {xe(str(name))}  '
                f'<font color="{imp_col}"><b>[{xe(imp.upper())}]</b></font>')
        me_story.append(Paragraph(f"• {line}", me_style))

    rate_sens = macro_events.get("rate_sensitivity", "")
    if rate_sens:
        me_story.append(Spacer(1, 3))
        me_story.append(Paragraph(
            f'<i>Rate Sensitivity: {xe(str(rate_sens))}</i>',
            ParagraphStyle("RS", fontName="Helvetica", fontSize=6.5, leading=9,
                           textColor=GS_DGRAY)))
    if not me_story:
        me_story.append(Paragraph("• No macro events scheduled.", me_style))

    me_h = min(len(events) * 12 + 20, max(40, y_cursor - FTR_DIV_Y - 100))
    me_frame = Frame(36, y_cursor - me_h, FULL_W, me_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    me_frame.addFromList(me_story, c)
    y_cursor -= me_h + 6

    # ── Section 4: Dark Pool Signals ──────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(36, y_cursor - 14, FULL_W, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, y_cursor - 10, "DARK POOL SIGNALS  —  FLAGGED ACTIVITY")
    y_cursor -= 18

    dark_pool = market_intel.get("dark_pool", {})
    flagged = dark_pool.get("flagged", [])

    dp_hdr_c = ParagraphStyle("DPHC", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_CENTER)
    dp_hdr_l = ParagraphStyle("DPHL", fontName="Helvetica-Bold", fontSize=6.5,
                              textColor=white, alignment=TA_LEFT)
    dp_cell_c = ParagraphStyle("DPC", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_CENTER)
    dp_cell_l = ParagraphStyle("DPL", fontName="Helvetica", fontSize=6.5,
                               alignment=TA_LEFT)

    dp_rows = [[
        Paragraph("TICKER", dp_hdr_l),
        Paragraph("SECTOR", dp_hdr_l),
        Paragraph("VOL SPIKE", dp_hdr_c),
        Paragraph("PRICE MOVE", dp_hdr_c),
        Paragraph("BIAS", dp_hdr_c),
    ]]
    for f in flagged:
        bias = f.get("bias", "neutral")
        bias_col = "#1A5276" if bias == "bullish" else (
            "#7B241C" if bias == "bearish" else "#4A5568")
        dp_rows.append([
            Paragraph(f'<b>{xe(str(f.get("ticker", "")))}</b>', dp_cell_l),
            Paragraph(xe(str(f.get("sector", ""))), dp_cell_l),
            Paragraph(f'{f.get("volume_spike", f.get("vol_spike", 0)):.1f}x', dp_cell_c),
            Paragraph(f'{f.get("price_move", 0):+.1f}%', dp_cell_c),
            Paragraph(f'<font color="{bias_col}"><b>{xe(bias.upper())}</b></font>', dp_cell_c),
        ])

    if len(dp_rows) > 1:
        dp_cw = [70, 100, 90, 90, 90]
        dp_tbl = Table(dp_rows, colWidths=dp_cw)
        dp_style_cmds = [
            ("BACKGROUND",    (0, 0), (-1, 0), GS_NAVY),
            ("TOPPADDING",    (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING",   (0, 0), (-1, -1), 3),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -1), 0.3, GS_LINE),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]
        for i in range(1, len(dp_rows)):
            dp_style_cmds.append(("BACKGROUND", (0, i), (-1, i),
                                  GS_LGRAY if i % 2 == 0 else white))
        dp_tbl.setStyle(TableStyle(dp_style_cmds))
        dp_story = [dp_tbl]
    else:
        dp_story = [Paragraph("No dark pool flags detected.", dp_cell_c)]

    dp_h = max(30, min(14 * len(dp_rows) + 4, y_cursor - FTR_DIV_Y - 10))
    dp_frame = Frame(36, y_cursor - dp_h, FULL_W, dp_h,
                     leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                     showBoundary=0)
    dp_frame.addFromList(dp_story, c)

    # ── Footer ────────────────────────────────────────────────────────────────
    draw_footer(c, "Market-Wide Intelligence — Page 2")


# ════════════════════════════════════════════════════════════════════════════
#  CANVAS DRAWING — FRONT PAGE
# ════════════════════════════════════════════════════════════════════════════

def draw_front_page(c, all_ticker_data: list[dict], macro_data: dict, macro_text: str):
    """
    Draws the combined front page: macro overview + conviction ranking.

    all_ticker_data: list of {ticker, company, price_data, analysis} dicts
    """
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, 740, PW, 52, fill=1, stroke=0)

    c.setFont("Helvetica-Bold", 16)
    c.setFillColor(white)
    c.drawString(36, 772, FIRM_NAME_FULL.upper())
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawString(36, 759, "MORNING INTELLIGENCE BRIEF  |  EQUITY RESEARCH  |  DAILY ISSUE")
    c.setFont("Helvetica-Bold", 10)
    c.setFillColor(white)
    c.drawRightString(576, 772, date_str)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(HexColor("#A8C8F0"))
    c.drawRightString(576, 759, f"AI-Powered  |  {EMAIL}")

    # Gold accent line
    c.setStrokeColor(GOLD_COL)
    c.setLineWidth(2.0)
    c.line(0, 740, PW, 740)

    # ── Section: Macro Environment (two columns) ──────────────────────────────
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(GS_NAVY)
    c.rect(36, 718, 540, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, 723, "MACRO ENVIRONMENT")

    # Left: rate data table
    rates   = macro_data.get("rates", {})
    sectors = macro_data.get("sectors", {})

    _LEFT_X  = 36
    _LEFT_W  = 240
    _RIGHT_X = 300
    _RIGHT_W = 276

    # Rate data — draw as mini-table
    row_y = 706
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(GS_DGRAY)
    c.drawString(_LEFT_X, row_y, "INDICATOR")
    c.drawRightString(_LEFT_X + _LEFT_W * 0.6, row_y, "VALUE")
    c.drawRightString(_LEFT_X + _LEFT_W, row_y, "CHANGE")
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.4)
    c.line(_LEFT_X, row_y - 2, _LEFT_X + _LEFT_W, row_y - 2)

    row_y -= 12
    rate_items = list(rates.values())[:6]
    for i, v in enumerate(rate_items):
        bg = GS_LGRAY if i % 2 == 0 else white
        c.setFillColor(bg)
        c.rect(_LEFT_X, row_y - 2, _LEFT_W, 11, fill=1, stroke=0)
        c.setFont("Helvetica", 7)
        c.setFillColor(GS_TEXT)
        c.drawString(_LEFT_X + 2, row_y + 1, v["name"])
        c.drawRightString(_LEFT_X + _LEFT_W * 0.6, row_y + 1, f"{v['value']:.2f}")
        chg_col = BULL_COL if v["chg"] >= 0 else BEAR_COL
        c.setFillColor(chg_col)
        sign = "+" if v["chg"] >= 0 else ""
        c.drawRightString(_LEFT_X + _LEFT_W, row_y + 1, f"{sign}{v['chg']:.4f}")
        row_y -= 11

    # Right: sector ETF mini-table
    sec_y = 706
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(GS_DGRAY)
    c.drawString(_RIGHT_X, sec_y, "SECTOR ETF")
    c.drawString(_RIGHT_X + _RIGHT_W * 0.50, sec_y, "1D%")
    c.drawString(_RIGHT_X + _RIGHT_W * 0.66, sec_y, "5D%")
    c.drawString(_RIGHT_X + _RIGHT_W * 0.82, sec_y, "1M%")
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.4)
    c.line(_RIGHT_X, sec_y - 2, _RIGHT_X + _RIGHT_W, sec_y - 2)

    sec_y -= 12
    top_sectors = sorted(sectors.values(), key=lambda x: x["chg_1d"], reverse=True)[:11]
    for i, v in enumerate(top_sectors):
        bg = GS_LGRAY if i % 2 == 0 else white
        c.setFillColor(bg)
        c.rect(_RIGHT_X, sec_y - 2, _RIGHT_W, 11, fill=1, stroke=0)
        c.setFont("Helvetica", 7)
        c.setFillColor(GS_TEXT)
        c.drawString(_RIGHT_X + 2, sec_y + 1, v["name"])

        def _pct_color(pct, canvas):
            canvas.setFillColor(BULL_COL if pct >= 0 else BEAR_COL)
            return f"{pct:+.1f}%"

        for xoff, key in [(_RIGHT_W*0.50, "chg_1d"), (_RIGHT_W*0.66, "chg_5d"), (_RIGHT_W*0.82, "chg_1m")]:
            pct = v.get(key, 0)
            c.setFillColor(BULL_COL if pct >= 0 else BEAR_COL)
            c.drawString(_RIGHT_X + xoff, sec_y + 1, f"{pct:+.1f}%")
        sec_y -= 11

    # ── Divider ───────────────────────────────────────────────────────────────
    divider_y = min(row_y, sec_y) - 8
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.5)
    c.line(36, divider_y, 576, divider_y)

    # ── Macro Analysis from Claude ────────────────────────────────────────────
    macro_sec_y = divider_y - 4
    c.setFillColor(GS_NAVY)
    c.rect(36, macro_sec_y - 14, 540, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, macro_sec_y - 10, "MACRO INTELLIGENCE  —  AI-GENERATED BRIEFING")

    macro_frame_y = macro_sec_y - 20
    st = _styles()
    macro_story = []
    for line in macro_text.split("\n"):
        line = line.strip().lstrip("•‒–—-").strip()
        if line:
            macro_story.append(Paragraph(f"• {xe(line)}", st["bullet_sm"]))
    macro_story = macro_story or [Paragraph("• Macro analysis unavailable.", st["bullet_sm"])]

    macro_frame = Frame(
        36, FTR_DIV_Y + 140, 540, max(40, macro_sec_y - 20 - FTR_DIV_Y - 140),
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        showBoundary=0,
    )
    macro_frame.addFromList(macro_story[:], c)

    # ── Conviction Ranking Table ──────────────────────────────────────────────
    conv_top = FTR_DIV_Y + 132
    c.setFillColor(GS_NAVY)
    c.rect(36, conv_top - 14, 540, 14, fill=1, stroke=0)
    c.setFont("Helvetica-Bold", 7)
    c.setFillColor(white)
    c.drawString(41, conv_top - 10, "CONVICTION RANKING  —  ALL COVERED TICKERS")

    # Build conviction table data
    sorted_data = sorted(all_ticker_data, key=lambda x: x["analysis"].get("conviction_score", 0), reverse=True)
    conv_rows   = []
    hdr_p = ParagraphStyle("CH", fontName="Helvetica-Bold", fontSize=7, textColor=white, alignment=TA_CENTER)
    hdr_l = ParagraphStyle("CHL", fontName="Helvetica-Bold", fontSize=7, textColor=white, alignment=TA_LEFT)

    conv_rows.append([
        Paragraph("#",            hdr_p),
        Paragraph("TICKER",       hdr_p),
        Paragraph("COMPANY",      hdr_l),
        Paragraph("PRICE",        hdr_p),
        Paragraph("1D%",          hdr_p),
        Paragraph("RATING",       hdr_p),
        Paragraph("CONV.",        hdr_p),
        Paragraph("1Y BASE",      hdr_p),
        Paragraph("ONE-LINE THESIS", hdr_l),
    ])

    for rank, td in enumerate(sorted_data, 1):
        tk   = td["ticker"]
        an   = td["analysis"]
        pd_  = td["price_data"]
        conv = an.get("conviction_score", 5)
        chg  = pd_["change_pct"]
        rtg  = an.get("rating", "Neutral")
        thesis = an.get("one_line_thesis", "Monitoring for catalyst.")[:60]
        chg_col_hex = "#1A5276" if chg >= 0 else "#7B241C"
        rtg_col_hex = "#1A5276" if rtg == "Buy" else ("#7B241C" if rtg == "Sell" else "#4A5568")

        cell_p = ParagraphStyle("CR", fontName="Helvetica", fontSize=7, alignment=TA_CENTER)
        cell_l = ParagraphStyle("CRL", fontName="Helvetica", fontSize=7, alignment=TA_LEFT)
        cell_b = ParagraphStyle("CRB", fontName="Helvetica-Bold", fontSize=7, alignment=TA_CENTER)

        pt_1yr_base = an.get("price_targets", {}).get("1yr", {}).get("base")
        pt_str      = f"${pt_1yr_base:.2f}" if pt_1yr_base else "—"
        conv_rows.append([
            Paragraph(str(rank), cell_b),
            Paragraph(f'<font color="#0E4DA4"><b>{xe(tk)}</b></font>', cell_p),
            Paragraph(xe(td["company"])[:28], cell_l),
            Paragraph(f"${pd_['price']:.2f}", cell_p),
            Paragraph(f'<font color="{chg_col_hex}">{chg:+.2f}%</font>', cell_p),
            Paragraph(f'<font color="{rtg_col_hex}"><b>{xe(rtg)}</b></font>', cell_p),
            Paragraph(f'<font color="#002F5F"><b>{conv}/10</b></font>', cell_p),
            Paragraph(f'<font color="#1A5276"><b>{pt_str}</b></font>', cell_p),
            Paragraph(xe(thesis), cell_l),
        ])

    CW = [18, 36, 100, 44, 36, 36, 32, 44, 194]
    conv_tbl = Table(conv_rows, colWidths=CW)
    tbl_style = [
        ("BACKGROUND",    (0,0), (-1,0), GS_NAVY),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("LEFTPADDING",   (0,0), (-1,-1), 3),
        ("RIGHTPADDING",  (0,0), (-1,-1), 3),
        ("LINEBELOW",     (0,0), (-1,-1), 0.4, GS_LINE),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
    ]
    for i in range(1, len(conv_rows)):
        tbl_style.append(("BACKGROUND", (0,i), (-1,i), GS_LGRAY if i % 2 == 0 else white))
    conv_tbl.setStyle(TableStyle(tbl_style))

    tbl_story = [conv_tbl]
    tbl_frame = Frame(
        36, FTR_DIV_Y + 4, 540, 120,
        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        showBoundary=0,
    )
    tbl_frame.addFromList(tbl_story, c)

    # ── Footer ────────────────────────────────────────────────────────────────
    draw_footer(c, "Morning Intelligence Brief")


# ════════════════════════════════════════════════════════════════════════════
#  WATCHLIST SUGGESTIONS PAGE
# ════════════════════════════════════════════════════════════════════════════

def draw_prospects_page(
    c: pdfcanvas.Canvas,
    prospects: list[dict],
    date_tag: str,
) -> None:
    """Draw the Watchlist Suggestions page onto the current canvas page."""
    st = _styles()

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, PH - 56, PW, 56, fill=1, stroke=0)
    c.setFillColor(GOLD_COL)
    c.rect(0, PH - 59, PW, 3, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(36, PH - 24, FIRM_NAME_U + "  FINANCIAL INTELLIGENCE")
    c.setFont("Helvetica", 8)
    c.drawString(36, PH - 40, "WATCHLIST SUGGESTIONS  ·  DAILY PROSPECT RANKING")
    c.setFillColor(GS_MGRAY)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(PW - 36, PH - 40, date_tag)

    # ── Intro text ────────────────────────────────────────────────────────────
    intro_style = ParagraphStyle(
        "Intro", fontName="Helvetica", fontSize=8, leading=12,
        textColor=GS_DGRAY,
    )
    intro = Paragraph(
        "The following stocks were identified by screening a curated universe against the "
        "current watchlist's investment themes, today's macro environment, and smart-money "
        "signals. Scores reflect theme alignment, momentum, valuation, and institutional flow. "
        "These are research leads — not investment advice.",
        intro_style,
    )
    intro_frame = Frame(36, PH - 85, FULL_W, 26,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    intro_frame.addFromList([intro], c)

    # ── Prospect cards ────────────────────────────────────────────────────────
    y_cursor = PH - 92    # top of first card
    card_h   = 88         # pts per card
    card_gap = 5

    RATING_COLORS = {
        "Buy":   HexColor("#1A5276"),
        "Watch": HexColor("#7D6608"),
        "Avoid": HexColor("#7B241C"),
    }
    SIGNAL_COLS = {
        "bullish": HexColor("#1A5276"),
        "bearish": HexColor("#7B241C"),
        "neutral": GS_DGRAY,
    }

    body_style = ParagraphStyle(
        "PBody", fontName="Helvetica", fontSize=7.5, leading=11.5,
        textColor=GS_TEXT, alignment=TA_JUSTIFY,
    )
    sm_style = ParagraphStyle(
        "SM", fontName="Helvetica", fontSize=7, leading=10,
        textColor=GS_DGRAY,
    )

    for rank, p in enumerate(prospects[:5], 1):
        if y_cursor - card_h < 40:
            break

        # Card background
        c.setFillColor(GS_LGRAY if rank % 2 == 0 else white)
        c.rect(36, y_cursor - card_h, FULL_W, card_h, fill=1, stroke=0)
        # Left accent bar
        rating     = p.get("rating", "Watch")
        accent_col = RATING_COLORS.get(rating, GS_DGRAY)
        c.setFillColor(accent_col)
        c.rect(36, y_cursor - card_h, 4, card_h, fill=1, stroke=0)
        # Card border
        c.setStrokeColor(GS_LINE)
        c.setLineWidth(0.3)
        c.rect(36, y_cursor - card_h, FULL_W, card_h, fill=0, stroke=1)

        # ── Header row ────────────────────────────────────────────────────────
        header_y = y_cursor - 14
        # Rank badge
        c.setFillColor(GS_NAVY)
        c.circle(52, header_y + 2, 8, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(52, header_y - 1, str(rank))

        # Ticker
        c.setFillColor(GS_NAVY)
        c.setFont("Helvetica-Bold", 13)
        c.drawString(66, header_y, p.get("ticker", "—"))

        # Company + theme
        c.setFont("Helvetica", 8)
        c.setFillColor(GS_DGRAY)
        company_str = p.get("company", "")[:40]
        theme_str   = p.get("theme", "")
        c.drawString(110, header_y + 2, company_str)
        c.setFont("Helvetica", 7)
        c.drawString(110, header_y - 7, theme_str)

        # Score badge
        score = p.get("score", 0)
        c.setFillColor(GS_BLUE)
        c.roundRect(PW - 130, header_y - 8, 48, 18, 3, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(PW - 106, header_y - 1, f"{score:.1f}/10")

        # Rating badge
        c.setFillColor(accent_col)
        c.roundRect(PW - 76, header_y - 8, 40, 18, 3, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont("Helvetica-Bold", 8)
        c.drawCentredString(PW - 56, header_y - 1, rating.upper())

        # ── Thesis ────────────────────────────────────────────────────────────
        thesis_text = p.get("thesis", "")
        if thesis_text:
            thesis_frame = Frame(
                42, y_cursor - card_h + 28, FULL_W - 20, 34,
                leftPadding=4, rightPadding=4, topPadding=0, bottomPadding=0,
            )
            thesis_frame.addFromList(
                [Paragraph(xe(thesis_text), body_style)], c
            )

        # ── Metrics row ───────────────────────────────────────────────────────
        metrics_y = y_cursor - card_h + 18
        price  = p.get("price", 0)
        ret1m  = p.get("ret_1m", 0)
        cap    = p.get("market_cap", 0)
        ps     = p.get("ps_ratio", 0)
        opt_s  = p.get("options_signal", "neutral")
        ins_s  = p.get("insider_signal", "neutral")
        inst_s = p.get("institutional_signal", "neutral")

        cap_str = (f"${cap/1e9:.1f}B" if cap >= 1e9 else
                   f"${cap/1e6:.0f}M" if cap >= 1e6 else "—")
        chg_col = BULL_COL if ret1m >= 0 else BEAR_COL

        c.setFont("Helvetica", 7)
        c.setFillColor(GS_DGRAY)
        parts = [
            f"Price: ${price:.2f}",
            f"1M: {ret1m:+.1f}%",
            f"P/S: {ps:.1f}x",
            f"Cap: {cap_str}",
        ]
        c.drawString(44, metrics_y, "  ·  ".join(parts))

        # Signal badges (right side)
        sig_x = PW - 250
        for label, sig in [("OPT", opt_s), ("INS", ins_s), ("INST", inst_s)]:
            sig_col = SIGNAL_COLS.get(sig, GS_DGRAY)
            c.setFillColor(sig_col)
            c.setFont("Helvetica-Bold", 6)
            c.drawString(sig_x, metrics_y, f"{label}:{sig[:4].upper()}")
            sig_x += 62

        # ── Action line ───────────────────────────────────────────────────────
        action_text = p.get("action", "")
        if action_text:
            c.setFont("Helvetica-Bold", 7)
            c.setFillColor(accent_col)
            c.drawString(44, y_cursor - card_h + 7,
                         f"▶  {action_text[:90]}")

        y_cursor -= card_h + card_gap

    # ── Footer ────────────────────────────────────────────────────────────────
    c.setFillColor(GS_LGRAY)
    c.rect(0, 0, PW, 22, fill=1, stroke=0)
    c.setFillColor(GS_DGRAY)
    c.setFont("Helvetica", 6.5)
    c.drawString(36, 8,
                 f"{FIRM_NAME_FULL}  ·  Watchlist Suggestions  ·  {date_tag}"
                 f"  ·  AI-generated — not investment advice.")
    c.drawRightString(PW - 36, 8, "To add: edit tickers.txt and push")


# ════════════════════════════════════════════════════════════════════════════
#  MULTI-PAGE PDF BUILDER — COMBINED
# ════════════════════════════════════════════════════════════════════════════

def build_combined_pdf(
    all_ticker_data: list[dict],
    macro_data: dict,
    macro_text: str,
    prospects: list[dict] | None = None,
    market_intel: dict | None = None,
) -> Path:
    """Build the combined multi-page PDF with front page + per-ticker pages."""
    REPORTS_DIR.mkdir(exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out      = REPORTS_DIR / f"morning_report_{date_tag}.pdf"

    c = pdfcanvas.Canvas(str(out), pagesize=letter)
    c.setTitle(f"{FIRM_NAME} — Morning Intelligence Brief  {date_tag}")
    c.setAuthor(f"{FIRM_NAME} | Equity Research")
    c.setSubject("Daily Intelligence Brief")

    st = _styles()

    # ── Page 1: Front Page ────────────────────────────────────────────────────
    if len(all_ticker_data) > 1:
        draw_front_page(c, all_ticker_data, macro_data, macro_text)
        c.showPage()

    # ── Market-Wide Intelligence Pages ────────────────────────────────────────
    if market_intel:
        draw_market_intelligence_page(c, market_intel)
        c.showPage()
        draw_market_intelligence_page2(c, market_intel)
        c.showPage()

    # ── Per-Ticker Pages ──────────────────────────────────────────────────────
    for td in all_ticker_data:
        ticker    = td["ticker"]
        company   = td["company"]
        exch      = td["exch"]
        price_data= td["price_data"]
        analysis  = td["analysis"]
        news      = td["news"]
        filings   = td["filings"]
        charts    = td["charts"]

        # ─ Ticker Page 1: Company Update ─────────────────────────────────────
        draw_header(c, ticker, company, exch, price_data, analysis)
        draw_footer(c, f"{ticker} | Company Update — Page 1")
        # Column divider
        c.setStrokeColor(GS_LINE)
        c.setLineWidth(0.4)
        c.line(RCOL_X - 6, BODY_TOP - 2, RCOL_X - 6, BODY_BOT + 2)
        # Left column
        left_story  = build_left_story(
            analysis, news, filings, st,
            options_data=td.get("options"),
            insider_data=td.get("insider"),
            institutional_data=td.get("institutional"),
        )
        left_frame  = Frame(LCOL_X, BODY_BOT, LCOL_W, BODY_H,
                            leftPadding=0, rightPadding=6, topPadding=0, bottomPadding=0,
                            showBoundary=0)
        left_frame.addFromList(left_story, c)
        # Right column
        right_story = build_right_story(analysis, price_data, charts, st)
        right_frame = Frame(RCOL_X, BODY_BOT, RCOL_W, BODY_H,
                            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
                            showBoundary=0)
        right_frame.addFromList(right_story, c)
        c.showPage()

        # ─ Ticker Page 2: Price Outlook + Strategy ───────────────────────────
        draw_page2_header(c, ticker, company, "Price Outlook & Investment Strategy")
        draw_footer(c, f"{ticker} | Extended Analysis — Page 2")

        PAGE2_BODY_TOP = 730
        PAGE2_BODY_BOT = BODY_BOT
        PAGE2_BODY_H   = PAGE2_BODY_TOP - PAGE2_BODY_BOT
        FULL_W         = 540  # 576 - 36

        # Price Outlook TABLE (full width)
        outlook_story = [Spacer(1, 4)]
        outlook_story.append(_sec_hdr_table("Price Outlook — Scenario Targets by Timeframe", FULL_W, st))
        outlook_story.append(Spacer(1, 4))
        outlook_story.append(build_price_outlook_table(analysis, st, FULL_W))
        outlook_story.append(Spacer(1, 4))
        thesis_txt = analysis.get("price_outlook_thesis", "").strip()
        if thesis_txt:
            outlook_story.append(Paragraph(xe(thesis_txt), st["body_sm"]))
        outlook_story.append(Spacer(1, 10))

        # Estimate height consumed by outlook table (approx)
        OUTLOOK_H = 180  # ~12pt header + 6×18pt rows + spacers + thesis

        # Two columns below: sector intelligence (L) + investment strategy (R)
        L2_W = int(FULL_W * 0.55)
        R2_W = FULL_W - L2_W - 12
        L2_X = 36
        R2_X = 36 + L2_W + 12

        L2_H = PAGE2_BODY_H - OUTLOOK_H
        R2_H = L2_H

        # Render the outlook section first (in a full-width frame)
        top_frame = Frame(
            36, PAGE2_BODY_BOT + L2_H, FULL_W, OUTLOOK_H,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
            showBoundary=0,
        )
        top_frame.addFromList(outlook_story, c)

        # Left: Sector Intelligence + Outperformers
        left2_story = build_page2_left_story(analysis, st, L2_W)
        left2_frame = Frame(
            L2_X, PAGE2_BODY_BOT, L2_W, L2_H,
            leftPadding=0, rightPadding=4, topPadding=0, bottomPadding=0,
            showBoundary=0,
        )
        left2_frame.addFromList(left2_story, c)

        # Right: Investment Strategy
        right2_story = build_page2_right_story(analysis, st, R2_W)
        right2_frame = Frame(
            R2_X, PAGE2_BODY_BOT, R2_W, R2_H,
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
            showBoundary=0,
        )
        right2_frame.addFromList(right2_story, c)

        # Column divider for page 2
        c.setStrokeColor(GS_LINE)
        c.setLineWidth(0.4)
        c.line(R2_X - 6, PAGE2_BODY_BOT + L2_H - 2, R2_X - 6, PAGE2_BODY_BOT + 2)

        c.showPage()

    # ── Final page: Watchlist Suggestions ─────────────────────────────────
    if prospects:
        draw_prospects_page(c, prospects, date_tag)
        c.showPage()

    c.save()
    log.info("  Combined PDF: %s", out.name)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  PER-TICKER PDF BUILDER (2-PAGE)
# ════════════════════════════════════════════════════════════════════════════

def build_ticker_pdf(td: dict) -> Path:
    """Build a standalone 2-page PDF for one ticker."""
    ticker    = td["ticker"]
    company   = td["company"]
    exch      = td["exch"]
    price_data= td["price_data"]
    analysis  = td["analysis"]
    news      = td["news"]
    filings   = td["filings"]
    charts    = td["charts"]

    REPORTS_DIR.mkdir(exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out      = REPORTS_DIR / f"{ticker}_morning_report_{date_tag}.pdf"

    c = pdfcanvas.Canvas(str(out), pagesize=letter)
    c.setTitle(f"{ticker} — Company Update  {date_tag}")
    c.setAuthor(f"{FIRM_NAME} | Equity Research")
    c.setSubject(f"Company Update: {company}")

    st = _styles()

    # ── Page 1: Company Update ────────────────────────────────────────────────
    draw_header(c, ticker, company, exch, price_data, analysis)
    draw_footer(c, f"{ticker} | Company Update — Page 1")
    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.4)
    c.line(RCOL_X - 6, BODY_TOP - 2, RCOL_X - 6, BODY_BOT + 2)
    left_frame = Frame(LCOL_X, BODY_BOT, LCOL_W, BODY_H,
                       leftPadding=0, rightPadding=6, topPadding=0, bottomPadding=0, showBoundary=0)
    left_frame.addFromList(build_left_story(
        analysis, news, filings, st,
        options_data=td.get("options"),
        insider_data=td.get("insider"),
        institutional_data=td.get("institutional"),
    ), c)
    right_frame = Frame(RCOL_X, BODY_BOT, RCOL_W, BODY_H,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, showBoundary=0)
    right_frame.addFromList(build_right_story(analysis, price_data, charts, st), c)
    c.showPage()

    # ── Page 2: Price Outlook + Strategy ─────────────────────────────────────
    draw_page2_header(c, ticker, company, "Price Outlook & Investment Strategy")
    draw_footer(c, f"{ticker} | Extended Analysis — Page 2")

    FULL_W       = 540
    P2_BODY_TOP  = 730
    P2_BODY_BOT  = BODY_BOT
    P2_BODY_H    = P2_BODY_TOP - P2_BODY_BOT
    OUTLOOK_H    = 180
    L2_W = int(FULL_W * 0.55)
    R2_W = FULL_W - L2_W - 12
    L2_X = 36
    R2_X = 36 + L2_W + 12
    L2_H = P2_BODY_H - OUTLOOK_H

    # Top: price outlook
    outlook_story = [Spacer(1, 4),
                     _sec_hdr_table("Price Outlook — Scenario Targets by Timeframe", FULL_W, st),
                     Spacer(1, 4),
                     build_price_outlook_table(analysis, st, FULL_W),
                     Spacer(1, 4)]
    thesis_txt = analysis.get("price_outlook_thesis", "").strip()
    if thesis_txt:
        outlook_story.append(Paragraph(xe(thesis_txt), st["body_sm"]))
    outlook_story.append(Spacer(1, 10))

    top_frame = Frame(36, P2_BODY_BOT + L2_H, FULL_W, OUTLOOK_H,
                      leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, showBoundary=0)
    top_frame.addFromList(outlook_story, c)

    left2_frame = Frame(L2_X, P2_BODY_BOT, L2_W, L2_H,
                        leftPadding=0, rightPadding=4, topPadding=0, bottomPadding=0, showBoundary=0)
    left2_frame.addFromList(build_page2_left_story(analysis, st, L2_W), c)

    right2_frame = Frame(R2_X, P2_BODY_BOT, R2_W, L2_H,
                         leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, showBoundary=0)
    right2_frame.addFromList(build_page2_right_story(analysis, st, R2_W), c)

    c.setStrokeColor(GS_LINE)
    c.setLineWidth(0.4)
    c.line(R2_X - 6, P2_BODY_BOT + L2_H - 2, R2_X - 6, P2_BODY_BOT + 2)

    c.save()
    log.info("  PDF: %s", out.name)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  SENDGRID EMAIL DELIVERY
# ════════════════════════════════════════════════════════════════════════════

def _sendgrid_send(
    api_key: str,
    from_email: str,
    from_name: str,
    to_emails: "list[str] | str",
    subject: str,
    html_body: str,
    attachments: list[dict] | None = None,
) -> bool:
    """
    POST to SendGrid v3 /mail/send using stdlib urllib only — no extra package.
    to_emails may be a single address string or a list of addresses.
    Returns True on HTTP 202, False on any error.

    Logs exhaustive diagnostics so every failure is visible in GitHub Actions.
    """
    import urllib.request
    import urllib.error

    if isinstance(to_emails, str):
        to_emails = [to_emails]

    # One personalization per recipient so Gmail FROM==TO suppression cannot
    # drop marklangston3@gmail.com when it also appears as the FROM address.
    payload: dict = {
        "personalizations": [{"to": [{"email": e}]} for e in to_emails],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    if attachments:
        payload["attachments"] = attachments

    raw_payload = json.dumps(payload).encode()
    payload_kb  = len(raw_payload) / 1024

    # ── Pre-flight diagnostics ────────────────────────────────────────────────
    log.info("  [SendGrid] POST https://api.sendgrid.com/v3/mail/send")
    log.info("  [SendGrid]   key prefix : %s…", api_key[:8])
    log.info("  [SendGrid]   from       : %s (%s)", from_email, from_name)
    log.info("  [SendGrid]   to         : %s", ", ".join(to_emails))
    log.info("  [SendGrid]   subject    : %s", subject[:100])
    log.info("  [SendGrid]   attachments: %d file(s)", len(attachments or []))
    log.info("  [SendGrid]   payload    : %.1f KB", payload_kb)
    for att in (attachments or []):
        kb = len(att.get("content", "")) * 3 / 4 / 1024  # base64 → bytes approx
        log.info("  [SendGrid]     → %s  (%.0f KB)", att.get("filename", "?"), kb)

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=raw_payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("  [SendGrid] ✓ HTTP %d — message accepted and queued for delivery.", resp.status)
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log.error("  [SendGrid] ✗ HTTP %d %s", exc.code, exc.reason)
        # Print full JSON error list (no truncation)
        try:
            err_data = json.loads(body)
            errors   = err_data.get("errors", [err_data])
            for i, e in enumerate(errors, 1):
                msg  = e.get("message", str(e))
                fld  = e.get("field", "")
                help_ = e.get("help", "")
                log.error("  [SendGrid]   error %d: %s%s%s",
                          i, msg,
                          f"  (field: {fld})" if fld else "",
                          f"  → {help_}" if help_ else "")
        except Exception:
            log.error("  [SendGrid]   raw body: %s", body)
        # Human-readable guidance per status code
        if exc.code == 403:
            log.error("  [SendGrid] *** 403 FORBIDDEN — most likely cause: ***")
            log.error("  [SendGrid]   '%s' is not a Verified Sender in SendGrid.", from_email)
            log.error("  [SendGrid]   Fix: run 'python verify_sender.py --register'")
            log.error("  [SendGrid]        then click the verification link emailed to %s", from_email)
            log.error("  [SendGrid]        then run 'python verify_sender.py --check' to confirm.")
        elif exc.code == 401:
            log.error("  [SendGrid] *** 401 UNAUTHORIZED — most likely cause: ***")
            log.error("  [SendGrid]   SENDGRID_API_KEY is invalid, expired, or lacks 'Mail Send' scope.")
            log.error("  [SendGrid]   Fix: regenerate the key in SendGrid → Settings → API Keys")
            log.error("  [SendGrid]        ensure it has 'Mail Send' → Full Access.")
        elif exc.code == 400:
            log.error("  [SendGrid] *** 400 BAD REQUEST — payload or address format issue ***")
        elif exc.code == 413:
            log.error("  [SendGrid] *** 413 PAYLOAD TOO LARGE — total %.1f KB ***", payload_kb)
            log.error("  [SendGrid]   Try reducing attachment size.")
        return False
    except Exception as exc:
        log.error("  [SendGrid] ✗ Network / unexpected error: %s", exc)
        return False


def send_email_report(
    pdf_paths: list[Path],
    all_ticker_data: list[dict],
    macro_text: str,
) -> bool:
    """
    Email the morning report PDF(s) via SendGrid.

    Reads environment variables:
      SENDGRID_API_KEY  — required; if absent, logs and skips silently

    FROM and TO are both hardcoded to marklangston3@gmail.com.
    The FROM address must be verified in SendGrid before emails deliver:
      python verify_sender.py --register   # one-time setup
      python verify_sender.py --check      # confirm status

    Attaches the combined morning_report_{date}.pdf when present;
    falls back to attaching all generated PDFs.
    """
    import base64

    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    log.info("  [Email] SENDGRID_API_KEY : %s",
             f"SET (prefix: {api_key[:8]}…)" if api_key else "NOT SET — skipping delivery")
    if not api_key:
        log.warning("  [Email] Set the SENDGRID_API_KEY secret in GitHub → Settings → Secrets → Actions")
        return False

    # FROM is marklangston3@gmail.com (must be verified via SendGrid Sender
    # Authentication — run: python verify_sender.py --register).
    # TO goes to every address in RECIPIENTS (config.py).
    from_email = EMAIL
    to_emails  = RECIPIENTS
    date_str   = datetime.now(timezone.utc).strftime("%B %d, %Y")
    log.info("  [Email] from : %s", from_email)
    log.info("  [Email] to   : %s", ", ".join(to_emails))

    # ── Conviction table HTML rows ────────────────────────────────────────────
    sorted_data = sorted(
        all_ticker_data,
        key=lambda x: x["analysis"].get("conviction_score", 0),
        reverse=True,
    )
    ticker_rows = ""
    for rank, td in enumerate(sorted_data, 1):
        an   = td["analysis"]
        pd_  = td["price_data"]
        conv = an.get("conviction_score", 5)
        chg  = pd_["change_pct"]
        rtg  = an.get("rating", "Neutral")
        chg_color = "#1A5276" if chg >= 0 else "#7B241C"
        rtg_color = "#1A5276" if rtg == "Buy" else ("#7B241C" if rtg == "Sell" else "#4A5568")
        sign   = "+" if chg >= 0 else ""
        row_bg = "#EEF1F6" if rank % 2 == 0 else "#FFFFFF"
        thesis = xe(an.get("one_line_thesis", "—")[:65])
        ticker_rows += (
            f'<tr style="background:{row_bg}">'
            f'<td style="padding:7px 6px;font-weight:bold;color:#4A5568">{rank}</td>'
            f'<td style="padding:7px 6px;font-weight:bold;color:#002F5F">{xe(td["ticker"])}</td>'
            f'<td style="padding:7px 6px;color:#4A5568">{xe(td["company"])}</td>'
            f'<td style="padding:7px 6px;text-align:right">${pd_["price"]:.2f}</td>'
            f'<td style="padding:7px 6px;text-align:right;color:{chg_color};'
            f'font-weight:bold">{sign}{chg:.2f}%</td>'
            f'<td style="padding:7px 6px;text-align:center;color:{rtg_color};'
            f'font-weight:bold">{xe(rtg)}</td>'
            f'<td style="padding:7px 6px;text-align:center;font-weight:bold;'
            f'color:#002F5F">{conv}/10</td>'
            f'<td style="padding:7px 6px;color:#4A5568;font-size:11px">{thesis}</td>'
            f'</tr>'
        )

    # ── Macro bullets HTML ────────────────────────────────────────────────────
    macro_bullets = ""
    for line in macro_text.split("\n"):
        line = line.strip().lstrip("•‒–—-").strip()
        if line:
            macro_bullets += f'<li style="margin-bottom:6px">{xe(line)}</li>\n'
    macro_bullets = macro_bullets or '<li>No macro data available.</li>'

    # ── Full HTML body ────────────────────────────────────────────────────────
    html_body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,Helvetica,sans-serif;color:#1A202C;'
        'max-width:700px;margin:0 auto;padding:0">'

        # Header band
        f'<div style="background:#002F5F;padding:22px 28px;border-bottom:3px solid #C9A84C">'
        f'<h1 style="color:white;margin:0;font-size:18px;letter-spacing:1px">'
        f'{FIRM_NAME_U}</h1>'
        f'<p style="color:#A8C8F0;margin:5px 0 0;font-size:12px">'
        f'MORNING INTELLIGENCE BRIEF &nbsp;&middot;&nbsp; EQUITY RESEARCH'
        f' &nbsp;&middot;&nbsp; {date_str.upper()}</p></div>'

        # Sub-bar
        '<div style="padding:12px 28px;background:#EEF1F6;border-bottom:1px solid #CDD3DF">'
        '<p style="margin:0;font-size:12px;color:#4A5568">'
        'Your pre-market equity research brief is ready. '
        'Full report attached as PDF.</p></div>'

        # Conviction table
        '<div style="padding:20px 28px">'
        '<h2 style="font-size:13px;color:#002F5F;border-bottom:2px solid #002F5F;'
        'padding-bottom:6px;margin-bottom:12px;letter-spacing:0.5px">'
        'CONVICTION RANKING</h2>'
        '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        '<thead><tr style="background:#002F5F;color:white">'
        '<th style="padding:8px 6px;text-align:left">#</th>'
        '<th style="padding:8px 6px;text-align:left">TICKER</th>'
        '<th style="padding:8px 6px;text-align:left">COMPANY</th>'
        '<th style="padding:8px 6px;text-align:right">PRICE</th>'
        '<th style="padding:8px 6px;text-align:right">1D%</th>'
        '<th style="padding:8px 6px;text-align:center">RATING</th>'
        '<th style="padding:8px 6px;text-align:center">CONV.</th>'
        '<th style="padding:8px 6px;text-align:left">THESIS</th>'
        '</tr></thead>'
        f'<tbody>{ticker_rows}</tbody></table></div>'

        # Macro briefing
        '<div style="padding:0 28px 20px">'
        '<h2 style="font-size:13px;color:#002F5F;border-bottom:2px solid #002F5F;'
        'padding-bottom:6px;margin-bottom:12px;letter-spacing:0.5px">'
        'MACRO INTELLIGENCE</h2>'
        '<ul style="margin:0;padding-left:18px;font-size:12px;'
        f'line-height:1.7;color:#1A202C">{macro_bullets}</ul></div>'

        # Footer
        '<div style="padding:14px 28px;background:#002F5F;margin-top:8px">'
        f'<p style="color:#A8C8F0;margin:0;font-size:10px">'
        f'{xe(FIRM_NAME)} &nbsp;&middot;&nbsp; Equity Research'
        ' &nbsp;&middot;&nbsp; AI-generated'
        ' &nbsp;&middot;&nbsp; Not investment advice.</p></div>'
        '</body></html>'
    )

    # ── Choose PDFs to attach ─────────────────────────────────────────────────
    # Prefer the combined morning_report_{date}.pdf; fall back to all PDFs.
    attach_paths = [p for p in pdf_paths if p.name.startswith("morning_report_")]
    if not attach_paths:
        attach_paths = list(pdf_paths)

    attachments: list[dict] = []
    for path in attach_paths:
        try:
            with open(path, "rb") as fh:
                enc = base64.b64encode(fh.read()).decode()
            attachments.append({
                "content":     enc,
                "type":        "application/pdf",
                "filename":    path.name,
                "disposition": "attachment",
            })
            log.info("  Attaching %s (%.0f KB)", path.name, path.stat().st_size / 1024)
        except Exception as exc:
            log.warning("  Could not attach %s: %s", path.name, exc)

    subject = f"{FIRM_NAME} Morning Brief — {date_str}"
    log.info("  Sending to %s via SendGrid (from: %s) …", ", ".join(to_emails), from_email)
    return _sendgrid_send(
        api_key    = api_key,
        from_email = from_email,
        from_name  = f"{FIRM_NAME} Research",
        to_emails  = to_emails,
        subject    = subject,
        html_body  = html_body,
        attachments= attachments,
    )


# ════════════════════════════════════════════════════════════════════════════
#  GIT PUSH
# ════════════════════════════════════════════════════════════════════════════

def git_push_reports(pdf_paths: list[Path], extra_files: list[Path] | None = None) -> bool:
    rels     = [str(p.relative_to(REPO_DIR)) for p in pdf_paths]
    if extra_files:
        rels += [str(p.relative_to(REPO_DIR)) for p in extra_files]
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmds = [
        ["git", "add"] + rels,
        ["git", "commit", "-m",
         f"Morning reports {date_tag} — {', '.join(p.stem for p in pdf_paths)}\n\n"
         f"Langston's Financial Intelligence equity research PDFs.\n"
         f"Front page + per-ticker analysis (2pp each). Dashboard data updated.\n\n"
         "https://claude.ai/code/session_014hesikAtm8zzGNsXbYWmGV"],
        ["git", "push", "-u", GIT_REMOTE, BRANCH],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
        out = (r.stdout + r.stderr).strip()
        if r.returncode != 0 and "nothing to commit" not in r.stdout and "nothing to commit" not in r.stderr:
            log.error("git FAILED (rc=%d): %s\nstdout: %s\nstderr: %s",
                      r.returncode, " ".join(cmd), r.stdout.strip(), r.stderr.strip())
            return False
        log.info("$ %s → %s", " ".join(cmd), out or "(ok)")
    return True


def _build_market_pulse(market_intel: dict | None) -> dict:
    """Build a slim market_pulse dict for the dashboard JSON."""
    if not market_intel:
        return {}
    sf = market_intel.get("sector_flow", {})
    sectors = sf.get("sectors", [])
    return {
        "sector_flow": [
            {"sector": s["sector"], "etf": s["etf"], "pc_ratio": s["put_call_ratio"],
             "signal": s["signal"], "unusual": s.get("unusual_volume", False)}
            for s in sectors
        ],
        "squeeze_candidates": [
            {"ticker": s["ticker"], "short_pct": s["short_pct"], "change_pct": s["change_pct"]}
            for s in market_intel.get("short_interest", {}).get("squeeze_candidates", [])
        ],
        "congressional_trades": [
            {"member": t["member"], "ticker": t["ticker"], "type": t["type"],
             "amount": t["amount"], "date": t["date"], "chamber": t["chamber"]}
            for t in market_intel.get("congressional_trades", [])[:10]
        ],
        "earnings_calendar": [
            {"ticker": e["ticker"], "date": e["earnings_date"],
             "implied_move": e.get("implied_move_pct"), "rich_cheap": e.get("rich_cheap")}
            for e in market_intel.get("earnings_calendar", [])
        ],
        "credit_risk": market_intel.get("credit_signals", {}).get("risk_signal", "neutral"),
        "rotation_analysis": market_intel.get("rotation_analysis", ""),
    }


def write_dashboard_json(
    all_ticker_data: list[dict],
    macro_data: dict,
    prospects: list[dict] | None = None,
    market_intel: dict | None = None,
) -> Path:
    """Write docs/dashboard_data.json for the GitHub Pages dashboard."""
    import glob as _glob
    DOCS_DIR = REPO_DIR / "docs"
    DOCS_DIR.mkdir(exist_ok=True)

    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tickers_json: dict = {}

    for td in all_ticker_data:
        tk   = td["ticker"]
        pd_  = td["price_data"]
        an   = td["analysis"]
        opt  = td.get("options")  or {}
        ins  = td.get("insider")  or {}
        inst = td.get("institutional") or {}

        # Locate latest weekly PDF (if any)
        weekly_files = sorted(_glob.glob(str(REPORTS_DIR / "weekly_report_*.pdf")))
        weekly_rel   = ("reports/" + Path(weekly_files[-1]).name) if weekly_files else None

        tickers_json[tk] = {
            "name":       td["company"],
            "exch":       td["exch"],
            "price":      pd_.get("price"),
            "change_pct": pd_.get("change_pct"),
            "wk52_high":  pd_.get("wk52_high"),
            "wk52_low":   pd_.get("wk52_low"),
            "market_cap": pd_.get("market_cap"),
            "rating":           an.get("rating"),
            "conviction_score": an.get("conviction_score"),
            "price_target":     an.get("price_target"),
            "pt_1yr_base":      an.get("price_targets", {}).get("1yr", {}).get("base"),
            "headline":         an.get("headline"),
            "report_date":      date_tag,
            "combined_report_url": f"reports/morning_report_{date_tag}.pdf",
            "ticker_report_url":   f"reports/{tk}_morning_report_{date_tag}.pdf",
            "weekly_report_url":   weekly_rel,
            # Smart money signals for dashboard badges
            "options_signal":       opt.get("flow_signal"),
            "insider_signal":       ins.get("net_signal"),
            "institutional_signal": inst.get("smart_money_signal"),
        }

    # Macro rates
    macro_rates: dict = {}
    for rt, v in macro_data.get("rates", {}).items():
        macro_rates[rt] = {
            "name":  v["name"],
            "value": v["value"],
            "chg":   v.get("chg", 0),
        }

    # Prospects — slim representation for the dashboard card display
    prospects_json: list[dict] = []
    for p in (prospects or []):
        prospects_json.append({
            "rank":                p.get("rank"),
            "ticker":              p.get("ticker"),
            "company":             p.get("company"),
            "theme":               p.get("theme"),
            "rating":              p.get("rating"),
            "score":               p.get("score"),
            "thesis":              p.get("thesis"),
            "action":              p.get("action"),
            "price":               p.get("price"),
            "ret_1m":              p.get("ret_1m"),
            "market_cap":          p.get("market_cap"),
            "ps_ratio":            p.get("ps_ratio"),
            "options_signal":      p.get("options_signal"),
            "insider_signal":      p.get("insider_signal"),
            "institutional_signal": p.get("institutional_signal"),
        })

    payload = {
        "_comment":   "Auto-generated by morning_report.py — do not edit manually",
        "generated":  datetime.now(timezone.utc).isoformat(),
        "report_date": date_tag,
        "macro_rates": macro_rates,
        "tickers":    tickers_json,
        "prospects":  prospects_json,
        "market_pulse": _build_market_pulse(market_intel),
    }

    out_path = DOCS_DIR / "dashboard_data.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    log.info("  Dashboard JSON: %s", out_path.relative_to(REPO_DIR))
    return out_path


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Langston's morning report PDFs")
    parser.add_argument("--no-push",  action="store_true", help="Skip git push")
    parser.add_argument("--no-email", action="store_true",
                        help="Skip SendGrid email delivery (default: send if SENDGRID_API_KEY set)")
    parser.add_argument("--ticker",   metavar="T", help="Single ticker (default: all)")
    args = parser.parse_args()

    api_key = get_anthropic_key()
    client  = anthropic.Anthropic(api_key=api_key)

    now_utc    = datetime.now(timezone.utc)
    watch_list = [args.ticker.upper()] if args.ticker else list(TICKERS.keys())

    log.info("=" * 70)
    log.info("%s Morning Reports started: %s", FIRM_NAME, now_utc.isoformat())
    log.info("Tickers: %s", ", ".join(watch_list))

    state = load_watchdog_state()

    # ── 1. Fetch macro data (once, shared across all tickers) ─────────────────
    log.info("─── MACRO DATA ───")
    macro_data = get_macro_data()
    macro_text = generate_macro_analysis(macro_data, client)
    log.info("  Macro analysis complete.")

    # ── 1b. Market-wide intelligence (independent of individual tickers) ──
    log.info("─── MARKET-WIDE INTELLIGENCE ───")
    try:
        market_intel = collect_market_intelligence(watch_list, macro_data, client)
    except Exception as exc:
        log.warning("Market intelligence collection failed (non-fatal): %s", exc)
        market_intel = {}

    # ── 2. Per-ticker data collection + analysis ──────────────────────────────
    all_ticker_data: list[dict] = []

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

        # ── Smart money data (collected before Claude call so it enriches the prompt) ──
        cik = meta.get("cik", "")
        log.info("  Fetching options flow …")
        options_data = get_options_flow(ticker)
        log.info("  Options: PC=%.2f  signal=%s  unusual=%d",
                 options_data.get("put_call_ratio", 0),
                 options_data.get("flow_signal", "n/a"),
                 len(options_data.get("unusual", [])))

        log.info("  Fetching insider activity (EDGAR Form 4) …")
        insider_data = get_insider_activity(ticker, cik=cik, days_back=30)
        log.info("  Insider: signal=%s  txns=%d  sig_buys=%d  cluster_sell=%s",
                 insider_data.get("net_signal", "n/a"),
                 len(insider_data.get("transactions", [])),
                 insider_data.get("significant_buys", 0),
                 insider_data.get("cluster_selling", False))

        log.info("  Fetching institutional ownership …")
        institutional_data = get_institutional_ownership(ticker)
        log.info("  Institutional: %.1f%%  signal=%s  holders=%d",
                 institutional_data.get("pct_institutional", 0),
                 institutional_data.get("smart_money_signal", "n/a"),
                 institutional_data.get("holder_count", 0))

        analysis = generate_analysis(
            ticker, company, exch,
            price_data, news, filings,
            macro_data, client,
            options_data=options_data,
            insider_data=insider_data,
            institutional_data=institutional_data,
        )
        log.info("  Rating=%s  PT=%s  Conviction=%s/10  Headline: %s",
                 analysis.get("rating"),
                 analysis.get("price_target"),
                 analysis.get("conviction_score"),
                 analysis.get("headline", "")[:60])

        log.info("  Generating charts …")
        charts = {
            "price":   make_price_chart(ticker, RCOL_W, 200),
            "profile": make_profile_chart(analysis, RCOL_W, 88),
        }

        all_ticker_data.append({
            "ticker":        ticker,
            "company":       company,
            "exch":          exch,
            "price_data":    price_data,
            "analysis":      analysis,
            "news":          news,
            "filings":       filings,
            "charts":        charts,
            "options":       options_data,
            "insider":       insider_data,
            "institutional": institutional_data,
        })

    if not all_ticker_data:
        log.error("No ticker data collected — exiting.")
        sys.exit(1)

    # ── 2b. Watchlist prospects ───────────────────────────────────────────────
    prospects: list[dict] = []
    if not args.ticker:   # only when running all tickers
        log.info("─── Finding watchlist prospects ───")
        try:
            prospects = find_prospects(watch_list, macro_data, client)
            log.info("  Found %d prospects", len(prospects))
            for p in prospects:
                log.info("    #%d %s (%s) score=%.1f  %s",
                         p.get("rank", 0), p.get("ticker"), p.get("company", ""),
                         p.get("score", 0), p.get("theme", ""))
        except Exception as exc:
            log.warning("  Prospect finder failed (non-fatal): %s", exc)

    # ── 3. Build PDFs ─────────────────────────────────────────────────────────
    pdf_list: list[Path] = []

    # Combined PDF (with front page) — only when running all tickers
    if len(all_ticker_data) > 1:
        log.info("─── Building combined PDF ───")
        try:
            combined = build_combined_pdf(all_ticker_data, macro_data, macro_text,
                                          prospects=prospects,
                                          market_intel=market_intel)
            pdf_list.append(combined)
        except Exception as exc:
            log.error("Combined PDF build FAILED: %s", exc, exc_info=True)

    # Individual 2-page ticker PDFs
    for td in all_ticker_data:
        log.info("─── Building %s PDF ───", td["ticker"])
        try:
            p = build_ticker_pdf(td)
            pdf_list.append(p)
        except Exception as exc:
            log.error("Ticker PDF build FAILED for %s: %s", td["ticker"], exc, exc_info=True)

    if not pdf_list:
        log.error("All PDF builds failed — no PDFs generated.")
        sys.exit(1)

    # ── 4. Dashboard JSON ──────────────────────────────────────────────────────
    log.info("─── Updating GitHub Pages dashboard ───")
    try:
        dash_path = write_dashboard_json(all_ticker_data, macro_data, prospects=prospects,
                                         market_intel=market_intel)
    except Exception as exc:
        log.error("Dashboard JSON failed (non-fatal): %s", exc, exc_info=True)
        dash_path = None

    # ── 5. Push ───────────────────────────────────────────────────────────────
    if not args.no_push:
        extra = [dash_path] if dash_path else None
        if git_push_reports(pdf_list, extra_files=extra):
            log.info("Reports + dashboard pushed to %s:%s", GIT_REMOTE, BRANCH)
        else:
            log.warning("Push failed — PDFs saved locally in reports/")

    # ── 6. Email ──────────────────────────────────────────────────────────────
    if not args.no_email:
        log.info("─── Sending email report ───")
        ok = send_email_report(pdf_list, all_ticker_data, macro_text)
        if ok:
            from config import RECIPIENTS
            log.info("  ✓ Email accepted by SendGrid → %s", ", ".join(RECIPIENTS))
        else:
            log.error("  ✗ Email NOT delivered — check [SendGrid] log lines above for the exact error.")

    log.info("Done. Generated: %s", ", ".join(p.name for p in pdf_list))
    log.info("=" * 70)


if __name__ == "__main__":
    main()
