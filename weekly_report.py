#!/usr/bin/env python3
"""
weekly_report.py — Langston's Financial Intelligence weekly rollup PDF.

Runs every Sunday at 8 am EST.  Covers all tickers in tickers.txt.

Produces:
  reports/weekly_report_{YYYY-MM-DD}.pdf  — week-in-review for all tickers

Structure:
  Page 1:     Front page — weekly performance table + sector heat map + macro narrative
  Pages 2+:   Per-ticker weekly review — what moved, filings, news, forward look

Usage:
  python weekly_report.py              # build + push + email
  python weekly_report.py --no-push   # build only (no git push)
  python weekly_report.py --no-email  # skip SendGrid delivery
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request
import urllib.error
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

from reportlab.lib.colors import HexColor, black, white
from reportlab.lib.enums import TA_LEFT, TA_RIGHT, TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import Frame, Paragraph, Spacer, Table, TableStyle, Image

from config import (
    FIRM_NAME, FIRM_NAME_U, FIRM_NAME_FULL, EMAIL, RECIPIENTS,
    REPO_DIR, BRANCH, GIT_REMOTE,
    ANTHROPIC_MODEL, get_anthropic_key,
    SECTOR_ETFS, MACRO_RATE_TICKERS,
    load_tickers,
)

# ════════════════════════════════════════════════════════════════════════════
#  CONSTANTS
# ════════════════════════════════════════════════════════════════════════════

TICKERS     = load_tickers()
REPORTS_DIR = REPO_DIR / "reports"
LOG_FILE    = REPO_DIR / "weekly_report.log"
MODEL       = ANTHROPIC_MODEL

# ── Colour palette (matches morning_report.py) ───────────────────────────────
GS_NAVY  = HexColor("#002F5F")
GS_BLUE  = HexColor("#0E4DA4")
GS_LGRAY = HexColor("#EEF1F6")
GS_MGRAY = HexColor("#B0BAC9")
GS_DGRAY = HexColor("#4A5568")
GS_LINE  = HexColor("#CDD3DF")
GS_TEXT  = HexColor("#1A202C")
GOLD_COL = HexColor("#C9A84C")
BULL_COL = HexColor("#1A5276")
BEAR_COL = HexColor("#7B241C")

# ── Page constants ────────────────────────────────────────────────────────────
PW, PH = letter   # 612 × 792 pts
MARGIN = 28
COL_GAP = 12
LCOL_W = 242
RCOL_W = 270
FULL_W = PW - 2 * MARGIN   # 556

# ── Logging ───────────────────────────────────────────────────────────────────
REPORTS_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ],
)
log = logging.getLogger("weekly")


# ════════════════════════════════════════════════════════════════════════════
#  DATA FETCHING
# ════════════════════════════════════════════════════════════════════════════

def get_week_dates() -> tuple[str, str, str]:
    """Return (week_label, start_iso, end_iso) for the past trading week."""
    now   = datetime.now(timezone.utc)
    # Sunday run: the week ended Friday.  Go back to last Monday–Friday.
    end   = now - timedelta(days=1)        # Saturday → Friday
    start = end  - timedelta(days=4)       # Monday
    return (
        f"Week of {start.strftime('%B %d')}–{end.strftime('%d, %Y')}",
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
    )


def get_weekly_price_data(ticker: str) -> dict:
    """Pull 5-day price history + week open/close/change."""
    try:
        yt   = yf.Ticker(ticker)
        info = yt.info or {}
        hist = yt.history(period="1mo")   # 1 month gives us full week + prior context
        if hist.empty:
            raise ValueError("no history")

        closes = hist["Close"]
        price  = float(closes.iloc[-1])

        # Week open = Monday open; use 5d window tail
        week_hist = hist.tail(5)
        wk_open   = float(week_hist["Open"].iloc[0])
        wk_close  = float(week_hist["Close"].iloc[-1])
        wk_chg    = (wk_close - wk_open) / wk_open * 100 if wk_open else 0.0

        # Prior close (end of previous week = row before the 5-day window)
        prior_idx = max(0, len(hist) - 6)
        prior_close = float(hist["Close"].iloc[prior_idx]) if len(hist) > 5 else wk_open

        daily = []
        for idx, row in hist.tail(7).iterrows():
            daily.append({
                "date":   idx.strftime("%Y-%m-%d"),
                "open":   round(float(row["Open"]),  2),
                "close":  round(float(row["Close"]), 2),
                "high":   round(float(row["High"]),  2),
                "low":    round(float(row["Low"]),   2),
                "volume": int(row["Volume"]),
            })

        return {
            "price":       round(price, 2),
            "wk_open":     round(wk_open, 2),
            "wk_close":    round(wk_close, 2),
            "wk_chg_pct":  round(wk_chg, 2),
            "prior_close": round(prior_close, 2),
            "wk52_high":   round(float(info.get("fiftyTwoWeekHigh") or 0), 2),
            "wk52_low":    round(float(info.get("fiftyTwoWeekLow")  or 0), 2),
            "market_cap":  int(info.get("marketCap") or 0),
            "avg_volume":  int(info.get("averageVolume") or 0),
            "short_name":  info.get("shortName", ticker),
            "daily":       daily,
        }
    except Exception as exc:
        log.warning("yfinance error for %s: %s", ticker, exc)
        return {
            "price": 0, "wk_open": 0, "wk_close": 0, "wk_chg_pct": 0,
            "prior_close": 0, "wk52_high": 0, "wk52_low": 0,
            "market_cap": 0, "avg_volume": 0, "short_name": ticker, "daily": [],
        }


def get_weekly_filings(ticker: str) -> list[dict]:
    """Pull EDGAR filings from the past 7 days via EDGAR full-text search."""
    try:
        from config import _KNOWN_CIKS  # type: ignore[attr-defined]
        cik = _KNOWN_CIKS.get(ticker, "")
        if not cik:
            return []
        url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&dateRange=custom&startdt={(datetime.now(timezone.utc)-timedelta(days=8)).strftime('%Y-%m-%d')}"
            f"&enddt={datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
            f"&hits.hits._source=period_of_report,file_date,form_type,entity_name,file_num"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Langstons/1.0 marklangston3@gmail.com"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        hits = data.get("hits", {}).get("hits", [])
        filings = []
        for h in hits[:6]:
            src = h.get("_source", {})
            form = src.get("form_type", "")
            if form in ("4", "144"):    # skip insider tiny forms
                continue
            filings.append({
                "form": form,
                "date": src.get("file_date", ""),
                "title": src.get("entity_name", ""),
            })
        return filings
    except Exception as exc:
        log.debug("EDGAR weekly filings error %s: %s", ticker, exc)
        return []


def get_weekly_news(ticker: str, n: int = 10) -> list[dict]:
    """Pull latest news items for the ticker via yfinance."""
    def _parse(item: dict) -> dict:
        ts = item.get("providerPublishTime", 0)
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a %b %d")
        except Exception:
            dt = "—"
        return {
            "title":     item.get("title", ""),
            "publisher": item.get("publisher", ""),
            "time":      dt,
            "link":      item.get("link", ""),
        }
    try:
        return [_parse(i) for i in (yf.Ticker(ticker).news or [])[:n]]
    except Exception as exc:
        log.warning("News error %s: %s", ticker, exc)
        return []


def get_macro_weekly() -> dict:
    """Pull sector ETF and rate data with weekly (5D) perspective."""
    log.info("  Fetching weekly macro & sector data …")
    sectors = {}
    for etf, name in SECTOR_ETFS.items():
        try:
            hist = yf.Ticker(etf).history(period="1mo")
            if hist.empty:
                continue
            closes = hist["Close"]
            price  = float(closes.iloc[-1])
            c_5d   = float(closes.iloc[-6]) if len(closes) >= 6 else price
            c_1m   = float(closes.iloc[0])
            sectors[etf] = {
                "name":   name,
                "price":  round(price, 2),
                "chg_5d": round((price - c_5d) / c_5d * 100, 2) if c_5d else 0,
                "chg_1m": round((price - c_1m) / c_1m * 100, 2) if c_1m else 0,
            }
        except Exception as exc:
            log.debug("Sector ETF error %s: %s", etf, exc)

    rates = {}
    for rt, name in MACRO_RATE_TICKERS.items():
        try:
            hist = yf.Ticker(rt).history(period="5d")
            if hist.empty:
                continue
            closes = hist["Close"]
            val  = float(closes.iloc[-1])
            prev = float(closes.iloc[0]) if len(closes) >= 2 else val
            rates[rt] = {
                "name":  name,
                "value": round(val, 2),
                "chg_w": round(val - prev, 4),
            }
        except Exception as exc:
            log.debug("Rate data error %s: %s", rt, exc)

    return {"sectors": sectors, "rates": rates}


def fmt_cap(v: int) -> str:
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    return f"${v:,}" if v else "N/A"


# ════════════════════════════════════════════════════════════════════════════
#  CLAUDE ANALYSIS
# ════════════════════════════════════════════════════════════════════════════

def generate_weekly_macro(macro: dict, week_label: str, client: anthropic.Anthropic) -> str:
    """Generate a weekly macro/sector narrative from Claude."""
    sectors = macro.get("sectors", {})
    rates   = macro.get("rates",   {})

    sector_lines = "\n".join(
        f"  {v['name']:14s} | 5D: {v['chg_5d']:+.2f}% | 1M: {v['chg_1m']:+.2f}%"
        for v in sorted(sectors.values(), key=lambda x: x["chg_5d"], reverse=True)
    ) or "  No sector data."

    rate_lines = "\n".join(
        f"  {v['name']:12s} | {v['value']:8.2f} | WoW: {v['chg_w']:+.4f}"
        for v in rates.values()
    ) or "  No rate data."

    prompt = f"""You are the Chief Macro Strategist at {FIRM_NAME}.
{week_label} — write a concise institutional weekly macro narrative for AI/tech equity investors.

WEEKLY RATE MOVES:
{rate_lines}

SECTOR ETF — WEEKLY PERFORMANCE:
{sector_lines}

OUTPUT EXACTLY this format — no extra text outside markers:

===WEEKLY_MACRO===
• [Key rate/Fed development this week — specific data points and investment implication]
• [Strongest sector driver this week and whether the trend has legs]
• [Weakest sector this week and the likely cause]
• [Theme that benefited or hurt AI/technology/quantum names specifically]
• [Key risk or opportunity heading into next week based on macro positioning]
===END_WEEKLY_MACRO==="""

    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=700,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        s = raw.find("===WEEKLY_MACRO===")
        e = raw.find("===END_WEEKLY_MACRO===")
        if s != -1 and e != -1:
            return raw[s + len("===WEEKLY_MACRO==="):e].strip()
        return raw.strip()
    except Exception as exc:
        log.warning("Weekly macro call failed: %s", exc)
        return "• Weekly macro analysis unavailable."


def generate_ticker_weekly(
    ticker: str, company: str, exch: str,
    price_data: dict, news: list, filings: list,
    macro: dict, week_label: str,
    client: anthropic.Anthropic,
) -> dict:
    """Generate a week-in-review analysis for one ticker."""
    wk_chg    = price_data["wk_chg_pct"]
    wk_open   = price_data["wk_open"]
    wk_close  = price_data["wk_close"]
    hi52      = price_data["wk52_high"]
    lo52      = price_data["wk52_low"]
    pct_rng   = ((wk_close - lo52) / (hi52 - lo52) * 100) if (hi52 > lo52) else 0

    daily_lines = "\n".join(
        f"  {d['date']}: open ${d['open']} → close ${d['close']}  (vol {d['volume']:,})"
        for d in price_data.get("daily", [])
    ) or "  (no daily data)"

    news_lines = "\n".join(
        f"  [{n['time']}] {n['publisher']}: {n['title']}" for n in news[:8]
    ) or "  No news this week."

    filing_lines = "\n".join(
        f"  🚨 {f['form']} filed {f['date']}: {f.get('title','')}" for f in filings
    ) or "  No new SEC filings this week."

    sectors = macro.get("sectors", {})
    sector_lines = "\n".join(
        f"  {v['name']:14s} | 5D: {v['chg_5d']:+.2f}%"
        for v in sorted(sectors.values(), key=lambda x: x["chg_5d"], reverse=True)[:6]
    ) or "  No sector data."

    prompt = f"""You are a senior equity research analyst at {FIRM_NAME} writing a weekly review note.
Institutional quality: precise, data-referenced, concise. {week_label}.

TICKER: {ticker}  |  COMPANY: {company}  |  EXCHANGE: {exch}
Weekly: ${wk_open:.2f} → ${wk_close:.2f}  ({wk_chg:+.2f}%)  |  52-Wk: ${lo52}–${hi52}  ({pct_rng:.0f}% of range)

DAILY PRICE ACTION (this week):
{daily_lines}

SEC FILINGS (past 7 days):
{filing_lines}

WEEK'S NEWS:
{news_lines}

TOP SECTOR MOVERS (5D):
{sector_lines}

OUTPUT EXACTLY this format — no extra text outside markers:

===WEEK_SUMMARY===
[2–3 sentences covering the key price driver(s) this week for {ticker}. Reference specific price levels, catalysts, or sector tailwinds/headwinds. Be data-specific.]
===END_WEEK_SUMMARY===

===WHAT_MOVED===
• [Primary reason for the week's price action — specific, data-referenced]
• [Secondary driver or notable event]
• [Third bullet if relevant; omit if only two items]
===END_WHAT_MOVED===

===FILINGS_NOTE===
[If filings exist: 1–2 sentences on what was filed and why it matters for the thesis.
If no filings: "No material SEC filings this week. The thesis is unchanged."]
===END_FILINGS_NOTE===

===FORWARD_LOOK===
• [Key catalyst or event to watch next week — specific]
• [Price level or technical/fundamental signal to monitor]
• [One forward-looking risk or opportunity specific to {ticker}'s business drivers]
===END_FORWARD_LOOK===

===WEEKLY_RATING===
[One of: Strong Buy / Buy / Neutral / Sell / Strong Sell] | Conviction: [1-10]/10
[One sentence rationale based on the week's developments]
===END_WEEKLY_RATING==="""

    log.info("  Calling Claude for %s weekly analysis …", ticker)
    try:
        msg = client.messages.create(
            model=MODEL, max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
    except Exception as exc:
        log.warning("Claude call failed for %s: %s", ticker, exc)
        raw = ""

    def _sec(open_m: str, close_m: str) -> str:
        s = raw.find(open_m)
        e = raw.find(close_m)
        if s == -1 or e == -1:
            return ""
        return raw[s + len(open_m):e].strip()

    return {
        "week_summary":  _sec("===WEEK_SUMMARY===",  "===END_WEEK_SUMMARY==="),
        "what_moved":    _sec("===WHAT_MOVED===",     "===END_WHAT_MOVED==="),
        "filings_note":  _sec("===FILINGS_NOTE===",   "===END_FILINGS_NOTE==="),
        "forward_look":  _sec("===FORWARD_LOOK===",   "===END_FORWARD_LOOK==="),
        "weekly_rating": _sec("===WEEKLY_RATING===",  "===END_WEEKLY_RATING==="),
    }


# ════════════════════════════════════════════════════════════════════════════
#  CHART
# ════════════════════════════════════════════════════════════════════════════

def make_weekly_chart(ticker: str, w_pt: float, h_pt: float) -> BytesIO | None:
    """5-day candlestick-style bar chart (OHLC bars) vs SPY normalised to 100."""
    try:
        dpi = 150
        w_in = w_pt / 72
        h_in = h_pt / 72
        fig, ax = plt.subplots(figsize=(w_in, h_in), dpi=dpi)
        fig.patch.set_facecolor("#EEF1F6")
        ax.set_facecolor("#EEF1F6")

        yt   = yf.Ticker(ticker)
        spy  = yf.Ticker("SPY")
        hist = yt.history(period="1mo").tail(22)
        spy_h = spy.history(period="1mo").tail(22)

        if hist.empty:
            plt.close(fig)
            return None

        closes = hist["Close"].values
        norm   = closes / closes[0] * 100

        spy_cls = spy_h["Close"].values
        spy_len = min(len(spy_cls), len(norm))
        spy_norm = spy_cls[-spy_len:] / spy_cls[-spy_len] * 100

        dates = [d.to_pydatetime() for d in hist.index[-len(norm):]]

        ax.plot(dates[-spy_len:], spy_norm[-spy_len:],
                color="#B0BAC9", linewidth=1, linestyle="--", label="SPY", zorder=1)
        color = "#1A5276" if norm[-1] >= norm[0] else "#7B241C"
        ax.plot(dates, norm, color=color, linewidth=1.8, label=ticker, zorder=2)
        ax.axhline(100, color="#B0BAC9", linewidth=0.6, linestyle=":")
        ax.fill_between(dates, 100, norm, alpha=0.10,
                        color="#1A5276" if norm[-1] >= 100 else "#7B241C")

        # Shade the current week (last 5 bars)
        if len(dates) >= 5:
            ax.axvspan(dates[-5], dates[-1], alpha=0.08, color="#002F5F", zorder=0)

        ax.legend(fontsize=5.5, loc="upper left", framealpha=0.8)
        ax.set_ylabel("Indexed (100)", fontsize=5.5, color="#4A5568")
        ax.tick_params(axis="both", labelsize=5, colors="#4A5568")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        plt.xticks(rotation=30)
        for spine in ax.spines.values():
            spine.set_color("#CDD3DF")
        ax.grid(axis="y", color="#CDD3DF", linewidth=0.5, alpha=0.7)
        plt.tight_layout(pad=0.3)

        buf = BytesIO()
        fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight",
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as exc:
        log.warning("Chart error %s: %s", ticker, exc)
        return None


# ════════════════════════════════════════════════════════════════════════════
#  PARAGRAPH STYLES
# ════════════════════════════════════════════════════════════════════════════

def _styles() -> dict:
    return {
        "sec_hdr": ParagraphStyle(
            "SecHdr", fontName="Helvetica-Bold", fontSize=6.5, leading=8,
            textColor=GS_NAVY,
        ),
        "body": ParagraphStyle(
            "Body", fontName="Helvetica", fontSize=8.5, leading=12.5,
            textColor=GS_TEXT, alignment=TA_JUSTIFY,
        ),
        "body_sm": ParagraphStyle(
            "BodySm", fontName="Helvetica", fontSize=7.5, leading=11,
            textColor=GS_TEXT, alignment=TA_JUSTIFY,
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
            alignment=TA_CENTER,
        ),
        "tbl_hdr_l": ParagraphStyle(
            "TblHdrL", fontName="Helvetica-Bold", fontSize=7.5, textColor=white,
            alignment=TA_LEFT,
        ),
        "tbl_lbl": ParagraphStyle(
            "TblLbl", fontName="Helvetica", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_LEFT,
        ),
        "tbl_val": ParagraphStyle(
            "TblVal", fontName="Helvetica", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_RIGHT,
        ),
        "tbl_val_b": ParagraphStyle(
            "TblValB", fontName="Helvetica-Bold", fontSize=7.5, textColor=GS_TEXT,
            alignment=TA_RIGHT,
        ),
        "chart_cap": ParagraphStyle(
            "ChartCap", fontName="Helvetica", fontSize=6, textColor=GS_DGRAY,
            alignment=TA_CENTER,
        ),
    }


def _bullets(text: str, style) -> list:
    """Convert bullet text (• or – prefixed lines) to Paragraph list."""
    out = []
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        # normalise various bullet prefixes to •
        if ln.startswith(("•", "-", "–", "*")):
            ln = "• " + ln.lstrip("•-–* ").strip()
        out.append(Paragraph(xe(ln), style))
    return out or [Paragraph("—", style)]


def _section_header(title: str, col_w: float, st: dict) -> Table:
    """Navy section label bar."""
    t = Table([[Paragraph(title, st["sec_hdr"])]], colWidths=[col_w])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), GS_NAVY),
        ("TOPPADDING",  (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
    ]))
    return t


# ════════════════════════════════════════════════════════════════════════════
#  FRONT PAGE
# ════════════════════════════════════════════════════════════════════════════

def draw_front_page(
    c: pdfcanvas.Canvas,
    all_td: list[dict],
    macro: dict,
    macro_text: str,
    week_label: str,
    date_tag: str,
):
    """Draw the weekly rollup front page onto canvas page 1."""
    st = _styles()

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, PH - 56, PW, 56, fill=1, stroke=0)
    c.setFillColor(GOLD_COL)
    c.rect(0, PH - 59, PW, 3, fill=1, stroke=0)

    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(MARGIN, PH - 24, FIRM_NAME_U + "  FINANCIAL INTELLIGENCE")

    c.setFont("Helvetica", 8)
    c.drawString(MARGIN, PH - 40, "WEEKLY INTELLIGENCE ROLLUP  ·  " + week_label.upper())

    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_MGRAY)
    c.drawRightString(PW - MARGIN, PH - 40,
                      "Generated " + datetime.now(timezone.utc).strftime("%A, %B %d, %Y  %H:%M UTC"))

    # ── Portfolio weekly performance table ───────────────────────────────────
    y_cursor = PH - 70

    c.setFillColor(GS_NAVY)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN, y_cursor, "PORTFOLIO — WEEKLY PERFORMANCE")
    y_cursor -= 4

    hdr_p  = st["tbl_hdr"]
    hdr_l  = st["tbl_hdr_l"]
    cell_p = st["tbl_val"]
    cell_b = st["tbl_val_b"]
    cell_l = st["tbl_lbl"]

    rows = [[
        Paragraph("TICKER",   hdr_p),
        Paragraph("COMPANY",  hdr_l),
        Paragraph("WK OPEN",  hdr_p),
        Paragraph("WK CLOSE", hdr_p),
        Paragraph("WK CHG",   hdr_p),
        Paragraph("52W HI",   hdr_p),
        Paragraph("52W LO",   hdr_p),
        Paragraph("RATING",   hdr_p),
    ]]

    for td in all_td:
        pd    = td["price_data"]
        an    = td["analysis"]
        chg   = pd["wk_chg_pct"]
        chg_c = "#1A5276" if chg >= 0 else "#7B241C"
        rating = an.get("weekly_rating", "").split("|")[0].strip() or "—"
        rows.append([
            Paragraph(f'<b>{xe(td["ticker"])}</b>', cell_p),
            Paragraph(xe(td["company"]), cell_l),
            Paragraph(f'${pd["wk_open"]:.2f}',  cell_p),
            Paragraph(f'${pd["wk_close"]:.2f}', cell_p),
            Paragraph(f'<font color="{chg_c}"><b>{chg:+.2f}%</b></font>', cell_p),
            Paragraph(f'${pd["wk52_high"]:.2f}', cell_p),
            Paragraph(f'${pd["wk52_low"]:.2f}',  cell_p),
            Paragraph(xe(rating), cell_p),
        ])

    CW = [40, 148, 50, 54, 50, 48, 48, 62]   # total = 500
    tbl = Table(rows, colWidths=CW)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  GS_NAVY),
        ("BACKGROUND",    (0, 1), (-1, -1), GS_LGRAY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, GS_LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.25, GS_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
    ]))

    tbl_h = 18 + 18 * len(all_td)   # approx row height
    frame = Frame(MARGIN, y_cursor - tbl_h, FULL_W, tbl_h + 10,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    frame.addFromList([tbl], c)
    y_cursor -= tbl_h + 18

    # ── Sector heatmap ────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN, y_cursor, "SECTOR ETF — WEEKLY HEAT MAP (5D)")
    y_cursor -= 4

    sectors = macro.get("sectors", {})
    sorted_s = sorted(sectors.values(), key=lambda x: x["chg_5d"], reverse=True)

    sector_rows = [[
        Paragraph("SECTOR",  hdr_l),
        Paragraph("5D %",    hdr_p),
        Paragraph("1M %",    hdr_p),
    ]]
    for s in sorted_s:
        chg5 = s["chg_5d"]
        chg1 = s["chg_1m"]
        c5c = "#1A5276" if chg5 >= 0 else "#7B241C"
        c1c = "#1A5276" if chg1 >= 0 else "#7B241C"
        sector_rows.append([
            Paragraph(xe(s["name"]), cell_l),
            Paragraph(f'<font color="{c5c}"><b>{chg5:+.2f}%</b></font>', cell_p),
            Paragraph(f'<font color="{c1c}">{chg1:+.2f}%</font>', cell_p),
        ])

    # Two side-by-side sector tables (halve the list)
    mid   = (len(sector_rows) + 1) // 2
    left  = sector_rows[:mid]
    right = sector_rows[mid:]
    # pad right to same length
    empty_row = [Paragraph("", cell_p)] * 3
    while len(right) < len(left):
        right.append(empty_row)

    SCW = [110, 40, 40]  # 190 wide each; 2 cols + gap = 392
    gap_col = 16

    s_left  = Table(left,  colWidths=SCW)
    s_right = Table(right, colWidths=SCW)
    for st_tbl in (s_left, s_right):
        st_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  GS_NAVY),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, GS_LGRAY]),
            ("GRID",          (0, 0), (-1, -1), 0.25, GS_LINE),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
        ]))

    st_h = 16 * len(left) + 4
    f_left = Frame(MARGIN, y_cursor - st_h, 190, st_h + 8,
                   leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    f_right = Frame(MARGIN + 190 + gap_col, y_cursor - st_h, 190, st_h + 8,
                    leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    f_left.addFromList([s_left], c)
    f_right.addFromList([s_right], c)

    # Macro rates on the right
    rates = macro.get("rates", {})
    rate_rows = [[Paragraph("RATE / INDEX", hdr_l), Paragraph("LEVEL", hdr_p), Paragraph("WoW Δ", hdr_p)]]
    for v in rates.values():
        chg_c = "#1A5276" if v["chg_w"] >= 0 else "#7B241C"
        rate_rows.append([
            Paragraph(xe(v["name"]), cell_l),
            Paragraph(f'{v["value"]:.2f}', cell_p),
            Paragraph(f'<font color="{chg_c}">{v["chg_w"]:+.4f}</font>', cell_p),
        ])

    RCW = [100, 50, 50]  # 200 wide
    r_tbl = Table(rate_rows, colWidths=RCW)
    r_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0),  GS_NAVY),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [white, GS_LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.25, GS_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
    ]))

    r_h = 16 * len(rate_rows) + 4
    f_rates = Frame(PW - MARGIN - 200, y_cursor - max(st_h, r_h), 200, max(st_h, r_h) + 8,
                    leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    f_rates.addFromList([r_tbl], c)

    y_cursor -= max(st_h, r_h) + 22

    # ── Macro narrative ────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN, y_cursor, "WEEKLY MACRO & SECTOR INTELLIGENCE")
    y_cursor -= 4

    macro_bullets = _bullets(macro_text, _styles()["bullet_sm"])
    macro_frame   = Frame(MARGIN, MARGIN + 16, FULL_W, y_cursor - MARGIN - 20,
                          leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    macro_frame.addFromList(macro_bullets, c)

    # ── Footer ────────────────────────────────────────────────────────────────
    c.setFillColor(GS_LGRAY)
    c.rect(0, 0, PW, MARGIN + 4, fill=1, stroke=0)
    c.setFillColor(GS_DGRAY)
    c.setFont("Helvetica", 6.5)
    c.drawString(MARGIN, 8,
                 f"{FIRM_NAME_FULL}  ·  Weekly Intelligence Rollup  ·  {week_label}  "
                 f"·  AI-generated — not investment advice.")
    c.drawRightString(PW - MARGIN, 8, f"Page 1")


# ════════════════════════════════════════════════════════════════════════════
#  TICKER PAGE
# ════════════════════════════════════════════════════════════════════════════

def draw_ticker_page(
    c: pdfcanvas.Canvas,
    td: dict,
    page_num: int,
    week_label: str,
):
    """Draw one full-page ticker weekly review."""
    ticker  = td["ticker"]
    company = td["company"]
    exch    = td["exch"]
    pd_     = td["price_data"]
    an      = td["analysis"]
    chart   = td["chart"]
    st      = _styles()

    wk_chg  = pd_["wk_chg_pct"]
    chg_c   = HexColor("#1A5276") if wk_chg >= 0 else HexColor("#7B241C")
    arrow   = "▲" if wk_chg >= 0 else "▼"

    # ── Header band ───────────────────────────────────────────────────────────
    c.setFillColor(GS_NAVY)
    c.rect(0, PH - 56, PW, 56, fill=1, stroke=0)
    c.setFillColor(GOLD_COL)
    c.rect(0, PH - 59, PW, 3, fill=1, stroke=0)

    # Ticker & company
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 15)
    c.drawString(MARGIN, PH - 25, ticker)
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, PH - 40, company)
    c.setFont("Helvetica", 7.5)
    c.setFillColor(GS_MGRAY)
    c.drawString(MARGIN, PH - 52, exch + "  ·  " + week_label)

    # Weekly change badge
    c.setFillColor(chg_c)
    c.rect(PW - MARGIN - 100, PH - 54, 100, 48, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 18)
    c.drawCentredString(PW - MARGIN - 50, PH - 32, f"{wk_chg:+.2f}%")
    c.setFont("Helvetica", 7)
    c.drawCentredString(PW - MARGIN - 50, PH - 48, f"WK  {arrow}  ${pd_['wk_open']:.2f}→${pd_['wk_close']:.2f}")

    # Firm name top right
    c.setFillColor(GS_MGRAY)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(PW - MARGIN - 106, PH - 20, FIRM_NAME_U + " WEEKLY ROLLUP")

    # ── Layout: two columns ───────────────────────────────────────────────────
    y_top = PH - 68
    col_gap = 14
    left_w  = 280
    right_w = FULL_W - left_w - col_gap
    left_x  = MARGIN
    right_x = MARGIN + left_w + col_gap

    # ─── LEFT COLUMN ──────────────────────────────────────────────────────────
    left_story = []

    # Week Summary
    left_story.append(_section_header("WEEK IN REVIEW", left_w, st))
    left_story.append(Spacer(1, 3))
    summary = an.get("week_summary", "")
    left_story.append(Paragraph(xe(summary) if summary else "—", st["body"]))
    left_story.append(Spacer(1, 6))

    # What Moved
    left_story.append(_section_header("WHAT MOVED THE STOCK", left_w, st))
    left_story.append(Spacer(1, 3))
    left_story.extend(_bullets(an.get("what_moved", ""), st["bullet"]))
    left_story.append(Spacer(1, 6))

    # Filings
    left_story.append(_section_header("SEC FILINGS THIS WEEK", left_w, st))
    left_story.append(Spacer(1, 3))
    fn = an.get("filings_note", "")
    left_story.append(Paragraph(xe(fn) if fn else "No material filings this week.", st["body_sm"]))
    left_story.append(Spacer(1, 6))

    # News
    left_story.append(_section_header("NEWS HIGHLIGHTS", left_w, st))
    left_story.append(Spacer(1, 3))
    news = td.get("news", [])
    if news:
        for n in news[:5]:
            left_story.append(Paragraph(
                f'<b>[{xe(n["time"])}]</b> {xe(n["publisher"])}: {xe(n["title"])}',
                st["bullet_sm"],
            ))
    else:
        left_story.append(Paragraph("No news items available.", st["body_sm"]))

    left_frame = Frame(left_x, MARGIN + 16, left_w, y_top - MARGIN - 20,
                       leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    left_frame.addFromList(left_story, c)

    # ─── RIGHT COLUMN ─────────────────────────────────────────────────────────
    right_story = []

    # Price chart
    if chart:
        chart_h = 130
        right_story.append(Image(chart, width=right_w, height=chart_h))
        right_story.append(Paragraph(
            "1-month price performance vs SPY  (shaded = current week)",
            st["chart_cap"],
        ))
        right_story.append(Spacer(1, 8))

    # Weekly metrics mini-table
    right_story.append(_section_header("WEEKLY METRICS", right_w, st))
    right_story.append(Spacer(1, 3))
    hi52  = pd_["wk52_high"]
    lo52  = pd_["wk52_low"]
    pct_r = ((pd_["wk_close"] - lo52) / (hi52 - lo52) * 100) if (hi52 > lo52) else 0
    metrics = [
        ("Week Open",    f'${pd_["wk_open"]:.2f}'),
        ("Week Close",   f'${pd_["wk_close"]:.2f}'),
        ("Weekly Chg",   f'{wk_chg:+.2f}%'),
        ("52-Wk High",   f'${hi52:.2f}'),
        ("52-Wk Low",    f'${lo52:.2f}'),
        ("52-Wk Pos.",   f'{pct_r:.0f}% of range'),
        ("Market Cap",   fmt_cap(pd_["market_cap"])),
        ("Avg Volume",   f'{pd_["avg_volume"]:,}' if pd_["avg_volume"] else "—"),
    ]
    m_rows = [[
        Paragraph(k, st["tbl_lbl"]),
        Paragraph(xe(str(v)), st["tbl_val_b"]),
    ] for k, v in metrics]
    m_tbl = Table(m_rows, colWidths=[right_w * 0.55, right_w * 0.45])
    m_tbl.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [white, GS_LGRAY]),
        ("GRID",          (0, 0), (-1, -1), 0.25, GS_LINE),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
    ]))
    right_story.append(m_tbl)
    right_story.append(Spacer(1, 8))

    # Forward look
    right_story.append(_section_header("FORWARD LOOK — NEXT WEEK", right_w, st))
    right_story.append(Spacer(1, 3))
    right_story.extend(_bullets(an.get("forward_look", ""), st["bullet_sm"]))
    right_story.append(Spacer(1, 8))

    # Weekly rating
    right_story.append(_section_header("WEEKLY RATING & CONVICTION", right_w, st))
    right_story.append(Spacer(1, 3))
    wr = an.get("weekly_rating", "")
    right_story.append(Paragraph(xe(wr) if wr else "—", st["body_sm"]))

    right_frame = Frame(right_x, MARGIN + 16, right_w, y_top - MARGIN - 20,
                        leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    right_frame.addFromList(right_story, c)

    # ── Footer ────────────────────────────────────────────────────────────────
    c.setFillColor(GS_LGRAY)
    c.rect(0, 0, PW, MARGIN + 4, fill=1, stroke=0)
    c.setFillColor(GS_DGRAY)
    c.setFont("Helvetica", 6.5)
    c.drawString(MARGIN, 8,
                 f"{FIRM_NAME_FULL}  ·  {ticker}  ·  {week_label}  ·  AI-generated — not investment advice.")
    c.drawRightString(PW - MARGIN, 8, f"Page {page_num}")


# ════════════════════════════════════════════════════════════════════════════
#  PDF ASSEMBLY
# ════════════════════════════════════════════════════════════════════════════

def build_weekly_pdf(all_td: list[dict], macro: dict, macro_text: str,
                     week_label: str, date_tag: str) -> Path:
    """Build the complete weekly PDF and return its path."""
    fname = REPORTS_DIR / f"weekly_report_{date_tag}.pdf"
    c = pdfcanvas.Canvas(str(fname), pagesize=letter)
    c.setTitle(f"{FIRM_NAME_FULL} — {week_label}")
    c.setAuthor(FIRM_NAME_FULL)

    # Page 1: Front page
    draw_front_page(c, all_td, macro, macro_text, week_label, date_tag)
    c.showPage()

    # Pages 2+: per-ticker
    for i, td in enumerate(all_td, start=2):
        draw_ticker_page(c, td, page_num=i, week_label=week_label)
        c.showPage()

    c.save()
    log.info("  PDF saved: %s  (%d pages)", fname.name, 1 + len(all_td))
    return fname


# ════════════════════════════════════════════════════════════════════════════
#  GIT PUSH
# ════════════════════════════════════════════════════════════════════════════

def git_push_report(pdf_path: Path) -> bool:
    """Add, commit, and push the weekly report PDF."""
    try:
        subprocess.run(["git", "add", str(pdf_path)], check=True, capture_output=True, text=True)
        commit_msg = (
            f"Weekly report {datetime.now(timezone.utc).strftime('%Y-%m-%d')} — {pdf_path.name}\n\n"
            "https://claude.ai/code/session_014hesikAtm8zzGNsXbYWmGV"
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            check=True, capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "push", "-u", GIT_REMOTE, BRANCH],
            check=True, capture_output=True, text=True,
        )
        log.info("  ✓ Pushed %s to %s:%s", pdf_path.name, GIT_REMOTE, BRANCH)
        return True
    except subprocess.CalledProcessError as exc:
        log.error("  ✗ git push failed: %s", exc.stderr or exc)
        return False


# ════════════════════════════════════════════════════════════════════════════
#  EMAIL
# ════════════════════════════════════════════════════════════════════════════

def _load_recipients() -> list[str]:
    rf = REPO_DIR / "recipients.txt"
    if not rf.exists():
        return [EMAIL]
    result = [
        ln.strip()
        for ln in rf.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return result or [EMAIL]


def send_weekly_email(pdf_path: Path, week_label: str) -> bool:
    """Email the weekly PDF to all recipients via SendGrid."""
    import base64

    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not api_key:
        log.info("  SENDGRID_API_KEY not set — skipping email delivery.")
        return True   # non-fatal

    to_emails = _load_recipients()
    from_email = "marklangston3@gmail.com"
    from_name  = "Langston's Financial Intelligence"
    subject    = f"Langston's Weekly Intelligence Rollup — {week_label}"

    if not pdf_path.exists():
        log.warning("  PDF not found for email: %s", pdf_path)
        return False

    encoded  = base64.b64encode(pdf_path.read_bytes()).decode()
    pdf_kb   = pdf_path.stat().st_size / 1024
    filename = pdf_path.name
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    html_body = f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;color:#1A202C;max-width:640px;margin:0 auto">

  <div style="background:#002F5F;padding:22px 28px;border-bottom:3px solid #C9A84C">
    <h1 style="color:white;margin:0;font-size:18px;letter-spacing:1px">
      LANGSTON&rsquo;S FINANCIAL INTELLIGENCE</h1>
    <p style="color:#A8C8F0;margin:5px 0 0;font-size:12px">
      WEEKLY INTELLIGENCE ROLLUP &nbsp;&middot;&nbsp; {week_label.upper()}</p>
  </div>

  <div style="padding:20px 28px;background:#EEF1F6;border-bottom:1px solid #CDD3DF">
    <p style="margin:0;font-size:13px;color:#4A5568">
      Your weekly equity research rollup is attached. The report covers
      all tickers in the portfolio — what moved, why, any SEC filings,
      sector themes, and a forward look for the coming week.</p>
  </div>

  <div style="padding:20px 28px;background:white">
    <table style="width:100%;border-collapse:collapse;font-size:12px;color:#4A5568">
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6;font-weight:bold;width:30%">Coverage</td>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6">{week_label}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6;font-weight:bold">Tickers</td>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6">
          {' &middot; '.join(TICKERS.keys())}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6;font-weight:bold">Attachment</td>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6">
          {filename} &nbsp;({pdf_kb:.0f}&thinsp;KB)</td>
      </tr>
      <tr>
        <td style="padding:8px 0;font-weight:bold">Generated</td>
        <td style="padding:8px 0">{date_str} UTC</td>
      </tr>
    </table>
  </div>

  <div style="padding:14px 28px;background:#002F5F">
    <p style="color:#A8C8F0;margin:0;font-size:10px">
      Langston&rsquo;s Financial Intelligence &nbsp;&middot;&nbsp; Weekly Rollup
      &nbsp;&middot;&nbsp; AI-generated &nbsp;&middot;&nbsp; Not investment advice.</p>
  </div>

</body></html>
"""

    text_body = (
        f"Langston's Financial Intelligence — Weekly Intelligence Rollup\n"
        f"{week_label}\n\n"
        f"Your weekly equity research rollup is attached as a PDF.\n"
        f"Coverage: {', '.join(TICKERS.keys())}\n\n"
        f"—\nLangston's Financial Intelligence | AI-generated | Not investment advice.\n"
    )

    payload = {
        "personalizations": [{"to": [{"email": e} for e in to_emails]}],
        "from":    {"email": from_email, "name": from_name},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": text_body},
            {"type": "text/html",  "value": html_body},
        ],
        "attachments": [{
            "content":     encoded,
            "type":        "application/pdf",
            "filename":    filename,
            "disposition": "attachment",
        }],
    }

    log.info("  Sending weekly email to: %s", ", ".join(to_emails))
    log.info("  Subject: %s", subject)
    log.info("  PDF: %s  (%.0f KB)", filename, pdf_kb)

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
            log.info("  ✓ SendGrid HTTP %d — email accepted.", resp.status)
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        log.error("  ✗ SendGrid HTTP %d: %s", exc.code, body[:600])
        if exc.code == 403:
            log.error("  FIX: marklangston3@gmail.com not verified — run verify_sender.py --register")
        elif exc.code == 401:
            log.error("  FIX: SENDGRID_API_KEY invalid or missing 'Mail Send' scope")
        return False
    except Exception as exc:
        log.error("  ✗ Network error: %s", exc)
        return False


# ════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Langston's weekly rollup PDF")
    parser.add_argument("--no-push",  action="store_true", help="Skip git push")
    parser.add_argument("--no-email", action="store_true", help="Skip SendGrid delivery")
    args = parser.parse_args()

    api_key = get_anthropic_key()
    client  = anthropic.Anthropic(api_key=api_key)

    now_utc   = datetime.now(timezone.utc)
    date_tag  = now_utc.strftime("%Y-%m-%d")
    week_label, week_start, week_end = get_week_dates()
    watch_list = list(TICKERS.keys())

    log.info("=" * 70)
    log.info("%s Weekly Rollup started: %s", FIRM_NAME, now_utc.isoformat())
    log.info("Coverage: %s  |  %s", ", ".join(watch_list), week_label)

    # ── 1. Macro data ──────────────────────────────────────────────────────────
    log.info("─── WEEKLY MACRO ───")
    macro      = get_macro_weekly()
    macro_text = generate_weekly_macro(macro, week_label, client)
    log.info("  Macro narrative complete.")

    # ── 2. Per-ticker ──────────────────────────────────────────────────────────
    all_td: list[dict] = []
    for ticker in watch_list:
        if ticker not in TICKERS:
            log.warning("Unknown ticker %s — skipping.", ticker)
            continue

        meta    = TICKERS[ticker]
        company = meta["name"]
        exch    = meta["exch"]
        log.info("─── %s (%s) ───", ticker, company)

        price_data = get_weekly_price_data(ticker)
        log.info("  WK: $%.2f → $%.2f  (%+.2f%%)",
                 price_data["wk_open"], price_data["wk_close"], price_data["wk_chg_pct"])

        news    = get_weekly_news(ticker, n=10)
        filings = get_weekly_filings(ticker)
        log.info("  news=%d  filings=%d", len(news), len(filings))

        analysis = generate_ticker_weekly(
            ticker, company, exch,
            price_data, news, filings,
            macro, week_label, client,
        )

        log.info("  Generating chart …")
        chart = make_weekly_chart(ticker, RCOL_W, 130)

        all_td.append({
            "ticker":     ticker,
            "company":    company,
            "exch":       exch,
            "price_data": price_data,
            "analysis":   analysis,
            "news":       news,
            "filings":    filings,
            "chart":      chart,
        })

    if not all_td:
        log.error("No ticker data collected — exiting.")
        sys.exit(1)

    # ── 3. Build PDF ───────────────────────────────────────────────────────────
    log.info("─── Building weekly PDF ───")
    pdf_path = build_weekly_pdf(all_td, macro, macro_text, week_label, date_tag)

    # ── 4. Push ────────────────────────────────────────────────────────────────
    if not args.no_push:
        if git_push_report(pdf_path):
            log.info("Weekly report pushed to %s:%s", GIT_REMOTE, BRANCH)
        else:
            log.warning("Push failed — PDF saved locally: %s", pdf_path)

    # ── 5. Email ───────────────────────────────────────────────────────────────
    if not args.no_email:
        log.info("─── Sending weekly email ───")
        ok = send_weekly_email(pdf_path, week_label)
        if ok:
            log.info("  ✓ Email sent to: %s", ", ".join(_load_recipients()))
        else:
            log.error("  ✗ Email delivery failed — check log above.")

    log.info("Done. Generated: %s", pdf_path.name)
    log.info("=" * 70)


if __name__ == "__main__":
    main()
