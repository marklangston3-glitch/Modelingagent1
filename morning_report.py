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
    FIRM_NAME, FIRM_NAME_U, FIRM_NAME_FULL, EMAIL,
    REPO_DIR, BRANCH, GIT_REMOTE,
    ANTHROPIC_MODEL, get_anthropic_key,
    SECTOR_ETFS, MACRO_RATE_TICKERS,
    load_tickers,
)

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
[2–3 sentences. What does this mean for the investment thesis? Be specific about {ticker}'s business and model drivers.]
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
    msg = client.messages.create(
        model=MODEL, max_tokens=2500,
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


# ════════════════════════════════════════════════════════════════════════════
#  PAGE 1 — LEFT COLUMN
# ════════════════════════════════════════════════════════════════════════════

def build_left_story(analysis: dict, news: list, filings: list, st: dict) -> list:
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
    story.append(Paragraph(xe(impl) if impl else "No implications to report.", st["body"]))
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
        Paragraph("#",       hdr_p),
        Paragraph("TICKER",  hdr_p),
        Paragraph("COMPANY", hdr_l),
        Paragraph("PRICE",   hdr_p),
        Paragraph("1D%",     hdr_p),
        Paragraph("RATING",  hdr_p),
        Paragraph("CONV.",   hdr_p),
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

        conv_rows.append([
            Paragraph(str(rank), cell_b),
            Paragraph(f'<font color="#0E4DA4"><b>{xe(tk)}</b></font>', cell_p),
            Paragraph(xe(td["company"])[:28], cell_l),
            Paragraph(f"${pd_['price']:.2f}", cell_p),
            Paragraph(f'<font color="{chg_col_hex}">{chg:+.2f}%</font>', cell_p),
            Paragraph(f'<font color="{rtg_col_hex}"><b>{xe(rtg)}</b></font>', cell_p),
            Paragraph(f'<font color="#002F5F"><b>{conv}/10</b></font>', cell_p),
            Paragraph(xe(thesis), cell_l),
        ])

    CW = [18, 36, 100, 44, 36, 36, 32, 238]
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
#  MULTI-PAGE PDF BUILDER — COMBINED
# ════════════════════════════════════════════════════════════════════════════

def build_combined_pdf(
    all_ticker_data: list[dict],
    macro_data: dict,
    macro_text: str,
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
        left_story  = build_left_story(analysis, news, filings, st)
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
    left_frame.addFromList(build_left_story(analysis, news, filings, st), c)
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
    to_email: str,
    subject: str,
    html_body: str,
    attachments: list[dict] | None = None,
) -> bool:
    """
    POST to SendGrid v3 /mail/send using stdlib urllib only — no extra package.
    Returns True on HTTP 202, False on any error.
    """
    import urllib.request
    import urllib.error

    payload: dict = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }
    if attachments:
        payload["attachments"] = attachments

    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log.info("  SendGrid: email sent (HTTP %d)", resp.status)
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log.warning("  SendGrid HTTP %d: %s", exc.code, body[:400])
        return False
    except Exception as exc:
        log.warning("  SendGrid error: %s", exc)
        return False


def send_email_report(
    pdf_paths: list[Path],
    all_ticker_data: list[dict],
    macro_text: str,
) -> bool:
    """
    Email the morning report PDF(s) via SendGrid.

    Reads environment variables:
      SENDGRID_API_KEY     — required; if absent, logs and skips silently
      SENDGRID_FROM_EMAIL  — verified sender address in your SendGrid account
                             (default: config.EMAIL = marklangston3@gmail.com)
                             NOTE: the FROM address must be verified via
                             SendGrid → Settings → Sender Authentication before
                             emails will deliver successfully.

    Attaches the combined morning_report_{date}.pdf when present;
    falls back to attaching all generated PDFs.
    Sends to config.EMAIL (marklangston3@gmail.com).
    """
    import base64

    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not api_key:
        log.info("  SENDGRID_API_KEY not set — skipping email delivery.")
        return False

    from_email = os.environ.get("SENDGRID_FROM_EMAIL", EMAIL).strip()
    to_email   = EMAIL
    date_str   = datetime.now(timezone.utc).strftime("%B %d, %Y")

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
    log.info("  Sending to %s via SendGrid (from: %s) …", to_email, from_email)
    return _sendgrid_send(
        api_key    = api_key,
        from_email = from_email,
        from_name  = f"{FIRM_NAME} Research",
        to_email   = to_email,
        subject    = subject,
        html_body  = html_body,
        attachments= attachments,
    )


# ════════════════════════════════════════════════════════════════════════════
#  GIT PUSH
# ════════════════════════════════════════════════════════════════════════════

def git_push_reports(pdf_paths: list[Path]) -> bool:
    rels     = [str(p.relative_to(REPO_DIR)) for p in pdf_paths]
    date_tag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cmds = [
        ["git", "add"] + rels,
        ["git", "commit", "-m",
         f"Morning reports {date_tag} — {', '.join(p.stem for p in pdf_paths)}\n\n"
         f"Langston's Financial Intelligence equity research PDFs.\n"
         f"Front page + per-ticker analysis (2pp each).\n\n"
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

        analysis = generate_analysis(
            ticker, company, exch,
            price_data, news, filings,
            macro_data, client,
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
            "ticker":     ticker,
            "company":    company,
            "exch":       exch,
            "price_data": price_data,
            "analysis":   analysis,
            "news":       news,
            "filings":    filings,
            "charts":     charts,
        })

    if not all_ticker_data:
        log.error("No ticker data collected — exiting.")
        sys.exit(1)

    # ── 3. Build PDFs ─────────────────────────────────────────────────────────
    pdf_list: list[Path] = []

    # Combined PDF (with front page) — only when running all tickers
    if len(all_ticker_data) > 1:
        log.info("─── Building combined PDF ───")
        combined = build_combined_pdf(all_ticker_data, macro_data, macro_text)
        pdf_list.append(combined)

    # Individual 2-page ticker PDFs
    for td in all_ticker_data:
        log.info("─── Building %s PDF ───", td["ticker"])
        p = build_ticker_pdf(td)
        pdf_list.append(p)

    # ── 4. Push ───────────────────────────────────────────────────────────────
    if not args.no_push:
        if git_push_reports(pdf_list):
            log.info("Reports pushed to %s:%s", GIT_REMOTE, BRANCH)
        else:
            log.warning("Push failed — PDFs saved locally in reports/")

    # ── 5. Email ──────────────────────────────────────────────────────────────
    if not args.no_email:
        log.info("─── Sending email report ───")
        ok = send_email_report(pdf_list, all_ticker_data, macro_text)
        if ok:
            log.info("  Email delivered to %s", EMAIL)
        else:
            log.info("  Email skipped or failed (see above).")

    log.info("Done. Generated: %s", ", ".join(p.name for p in pdf_list))
    log.info("=" * 70)


if __name__ == "__main__":
    main()
