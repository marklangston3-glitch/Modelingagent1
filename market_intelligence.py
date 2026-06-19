"""
market_intelligence.py — Market-Wide Options & Flow Intelligence

Collects broad-market data for the Morning Intelligence Report:
  1. Sector ETF options flow (all 11 SPDR sectors)
  2. Unusual market-wide options activity
  3. Cyclical rotation analysis (Claude)
  4. Dark pool / off-exchange volume signals
  5. Short interest changes & squeeze candidates
  6. Congressional trading disclosures
  7. Earnings calendar with options-implied moves
  8. Fed speak & macro event calendar
  9. Credit market signals (HYG / LQD)

Usage:
    from market_intelligence import collect_market_intelligence
    data = collect_market_intelligence(watchlist, macro_data, client)
"""

from __future__ import annotations

import json
import logging
import math
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

import anthropic
import yfinance as yf

from config import SECTOR_ETFS, ANTHROPIC_MODEL

log = logging.getLogger("market_intel")

# ════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _safe(val: Any, default: float = 0.0) -> float:
    try:
        f = float(val) if val is not None else default
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    return int(_safe(val, float(default)))


def _fetch_json(url: str, timeout: int = 15) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "LangstonFI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.debug("Fetch failed %s: %s", url, exc)
        return None


# Universe of liquid stocks for broad screening (outside typical watchlist)
SCREEN_UNIVERSE: list[str] = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "NFLX",
    "CRM", "ORCL", "ADBE", "INTC", "QCOM", "AVGO", "MU", "AMAT", "LRCX",
    "JPM", "BAC", "GS", "MS", "WFC", "C", "BLK", "SCHW",
    "UNH", "JNJ", "PFE", "ABBV", "MRK", "LLY", "BMY", "AMGN",
    "XOM", "CVX", "COP", "SLB", "EOG", "OXY",
    "CAT", "DE", "GE", "HON", "UPS", "BA", "LMT", "RTX",
    "HD", "LOW", "NKE", "SBUX", "MCD", "DIS", "ABNB",
    "PG", "KO", "PEP", "WMT", "COST",
    "NEE", "DUK", "SO", "D", "AEP",
    "AMT", "PLD", "CCI", "EQIX",
    "LIN", "APD", "SHW", "ECL",
    "PLTR", "SNOW", "DDOG", "CRWD", "ZS", "NET", "PANW",
    "SOFI", "COIN", "HOOD", "AFRM", "SQ", "PYPL",
    "SMCI", "ARM", "MRVL", "ON", "KLAC",
    "RIVN", "LCID", "NIO", "XPEV", "LI",
    "IONQ", "RGTI", "QBTS",
    "AI", "PATH", "SOUN", "RKLB", "ASTS",
]

# Sector mapping for labeling
TICKER_SECTORS: dict[str, str] = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology",
    "AMZN": "Cons. Disc.", "META": "Technology", "NVDA": "Technology",
    "TSLA": "Cons. Disc.", "AMD": "Technology", "NFLX": "Comm. Svcs",
    "JPM": "Financials", "BAC": "Financials", "GS": "Financials",
    "UNH": "Healthcare", "JNJ": "Healthcare", "PFE": "Healthcare",
    "LLY": "Healthcare", "XOM": "Energy", "CVX": "Energy",
    "CAT": "Industrials", "BA": "Industrials", "LMT": "Industrials",
    "HD": "Cons. Disc.", "NKE": "Cons. Disc.", "DIS": "Comm. Svcs",
    "PG": "Cons. Staples", "KO": "Cons. Staples", "WMT": "Cons. Staples",
    "NEE": "Utilities", "DUK": "Utilities", "AMT": "Real Estate",
    "LIN": "Materials", "PLTR": "Technology", "CRWD": "Technology",
    "SMCI": "Technology", "ARM": "Technology", "COIN": "Financials",
    "IONQ": "Technology", "RGTI": "Technology", "AI": "Technology",
    "SOFI": "Financials", "RIVN": "Cons. Disc.", "RKLB": "Industrials",
}


# ════════════════════════════════════════════════════════════════════════════
#  1. SECTOR OPTIONS FLOW
# ════════════════════════════════════════════════════════════════════════════

def get_sector_options_flow() -> dict:
    """Pull options activity across all 11 sector ETFs.

    Returns dict with 'sectors' list (sorted most bullish → most bearish)
    and 'summary' string.
    """
    results: list[dict] = []

    for etf, sector_name in SECTOR_ETFS.items():
        try:
            tk = yf.Ticker(etf)
            expirations = tk.options
            if not expirations:
                continue

            # Use nearest 2 expirations for near-term sentiment
            near_exp = expirations[:2]
            total_call_vol, total_put_vol = 0, 0
            total_call_oi, total_put_oi = 0, 0

            for exp in near_exp:
                chain = tk.option_chain(exp)
                total_call_vol += _safe_int(chain.calls["volume"].sum())
                total_put_vol += _safe_int(chain.puts["volume"].sum())
                total_call_oi += _safe_int(chain.calls["openInterest"].sum())
                total_put_oi += _safe_int(chain.puts["openInterest"].sum())

            pc_ratio = total_put_vol / total_call_vol if total_call_vol > 0 else 0.0

            # Estimate unusual volume: compare today's total to rough average
            # (OI serves as a proxy for normal activity level)
            avg_daily_vol = (total_call_oi + total_put_oi) * 0.05  # ~5% of OI is typical daily vol
            today_vol = total_call_vol + total_put_vol
            vol_ratio = today_vol / avg_daily_vol if avg_daily_vol > 0 else 1.0
            unusual = vol_ratio > 1.5

            if pc_ratio < 0.7:
                signal = "bullish"
            elif pc_ratio > 1.3:
                signal = "bearish"
            else:
                signal = "neutral"

            results.append({
                "etf": etf,
                "sector": sector_name,
                "put_call_ratio": round(pc_ratio, 2),
                "call_volume": total_call_vol,
                "put_volume": total_put_vol,
                "call_oi": total_call_oi,
                "put_oi": total_put_oi,
                "vol_ratio": round(vol_ratio, 1),
                "unusual_volume": unusual,
                "signal": signal,
            })
        except Exception as exc:
            log.debug("Sector options error %s: %s", etf, exc)

    # Sort: bullish first (lowest P/C), bearish last
    results.sort(key=lambda x: x["put_call_ratio"])

    bullish = [r for r in results if r["signal"] == "bullish"]
    bearish = [r for r in results if r["signal"] == "bearish"]
    summary = (
        f"{len(bullish)} sectors bullish, {len(bearish)} bearish. "
        f"Most bullish: {bullish[0]['sector'] if bullish else 'None'}. "
        f"Most bearish: {bearish[-1]['sector'] if bearish else 'None'}."
    )

    return {"sectors": results, "summary": summary, "error": None}


# ════════════════════════════════════════════════════════════════════════════
#  2. UNUSUAL MARKET-WIDE ACTIVITY
# ════════════════════════════════════════════════════════════════════════════

def get_unusual_market_activity(watchlist: list[str], n: int = 10) -> list[dict]:
    """Screen broad market for unusual options activity outside the watchlist.

    Returns top N stocks by volume/OI ratio with sector labels.
    """
    candidates: list[dict] = []
    skip = {tk.upper() for tk in watchlist}

    for ticker in SCREEN_UNIVERSE:
        if ticker in skip:
            continue
        try:
            tk = yf.Ticker(ticker)
            exps = tk.options
            if not exps:
                continue

            # Check nearest expiration only for speed
            chain = tk.option_chain(exps[0])
            call_vol = _safe_int(chain.calls["volume"].sum())
            put_vol = _safe_int(chain.puts["volume"].sum())
            call_oi = _safe_int(chain.calls["openInterest"].sum())
            put_oi = _safe_int(chain.puts["openInterest"].sum())

            total_vol = call_vol + put_vol
            total_oi = call_oi + put_oi
            if total_oi < 100 or total_vol < 50:
                continue

            vol_oi_ratio = total_vol / (total_oi * 0.05) if total_oi > 0 else 0
            pc_ratio = put_vol / call_vol if call_vol > 0 else 0

            # Find the single largest contract by volume
            all_contracts = []
            for _, row in chain.calls.iterrows():
                v = _safe_int(row.get("volume"))
                oi = _safe_int(row.get("openInterest"))
                if v > 0 and oi > 0:
                    all_contracts.append(("call", _safe(row.get("strike")), v, oi, v / oi))
            for _, row in chain.puts.iterrows():
                v = _safe_int(row.get("volume"))
                oi = _safe_int(row.get("openInterest"))
                if v > 0 and oi > 0:
                    all_contracts.append(("put", _safe(row.get("strike")), v, oi, v / oi))

            biggest = max(all_contracts, key=lambda x: x[4]) if all_contracts else None

            if vol_oi_ratio >= 3.0:
                signal = "bearish" if pc_ratio > 1.3 else ("bullish" if pc_ratio < 0.7 else "neutral")
                activity_desc = (
                    f"{vol_oi_ratio:.1f}x normal volume, P/C {pc_ratio:.2f}"
                )
                if biggest:
                    activity_desc += f", largest: {biggest[0].upper()} ${biggest[1]:.0f} ({biggest[4]:.1f}x OI)"

                candidates.append({
                    "ticker": ticker,
                    "sector": TICKER_SECTORS.get(ticker, "Other"),
                    "vol_oi_ratio": round(vol_oi_ratio, 1),
                    "put_call_ratio": round(pc_ratio, 2),
                    "total_volume": total_vol,
                    "activity": activity_desc,
                    "signal": signal,
                    "implication": (
                        f"{'Heavy call buying' if pc_ratio < 0.7 else 'Heavy put buying' if pc_ratio > 1.3 else 'Mixed flow'}"
                        f" at {vol_oi_ratio:.1f}x normal levels suggests "
                        f"{'institutional accumulation' if pc_ratio < 0.7 else 'hedging/positioning for downside' if pc_ratio > 1.3 else 'event-driven positioning'}."
                    ),
                })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["vol_oi_ratio"], reverse=True)
    return candidates[:n]


# ════════════════════════════════════════════════════════════════════════════
#  3. CYCLICAL ROTATION ANALYSIS (Claude)
# ════════════════════════════════════════════════════════════════════════════

def generate_rotation_analysis(
    sector_flow: dict,
    credit_signals: dict,
    short_data: dict,
    client: anthropic.Anthropic,
) -> str:
    """Claude-generated sector rotation analysis based on options flow data."""
    sectors = sector_flow.get("sectors", [])
    if not sectors:
        return "Sector options data unavailable for rotation analysis."

    flow_lines = "\n".join(
        f"  {s['sector']:14s} ({s['etf']}) | P/C: {s['put_call_ratio']:.2f} | "
        f"Vol Ratio: {s['vol_ratio']:.1f}x | Signal: {s['signal'].upper()}"
        + (" | UNUSUAL VOLUME" if s.get("unusual_volume") else "")
        for s in sectors
    )

    credit_line = ""
    if credit_signals:
        hyg = credit_signals.get("hyg", {})
        lqd = credit_signals.get("lqd", {})
        credit_line = (
            f"\nCREDIT SIGNALS:\n"
            f"  HYG: P/C {hyg.get('put_call_ratio', 0):.2f}, signal={hyg.get('signal', 'n/a')}\n"
            f"  LQD: P/C {lqd.get('put_call_ratio', 0):.2f}, signal={lqd.get('signal', 'n/a')}\n"
            f"  Credit spread trend: {credit_signals.get('spread_trend', 'n/a')}"
        )

    squeeze_line = ""
    squeeze_candidates = short_data.get("squeeze_candidates", [])
    if squeeze_candidates:
        squeeze_line = "\nSHORT SQUEEZE CANDIDATES:\n" + "\n".join(
            f"  {s['ticker']}: SI={s.get('short_pct', 0):.1f}%, options={s.get('options_signal', 'n/a')}"
            for s in squeeze_candidates[:5]
        )

    prompt = f"""You are a senior macro strategist. Analyze the sector options flow data below and write a concise paragraph (4-6 sentences) on what smart money options positioning implies about sector rotation over the next 30-60 days.

SECTOR OPTIONS FLOW (ranked most bullish to most bearish):
{flow_lines}
{credit_line}
{squeeze_line}

Cover:
1. Which sectors are being positioned for (heavy call buying, low P/C)
2. Which sectors are being hedged or exited (heavy put buying, high P/C)
3. What this implies about the market outlook
4. Any notable divergences between equity and options positioning

Output ONLY the paragraph — no headers, bullets, or extra formatting."""

    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as exc:
        log.warning("Rotation analysis failed: %s", exc)
        return "Sector rotation analysis unavailable."


# ════════════════════════════════════════════════════════════════════════════
#  4. DARK POOL / OFF-EXCHANGE VOLUME SIGNALS
# ════════════════════════════════════════════════════════════════════════════

def get_dark_pool_signals(tickers: list[str] | None = None) -> dict:
    """Estimate dark pool activity using exchange vs total volume ratios.

    True dark pool data requires FINRA ATS feeds (paid/delayed).
    This approximation flags stocks where volume significantly exceeds
    exchange-reported averages, suggesting off-exchange institutional flow.
    """
    scan_list = tickers or list(SCREEN_UNIVERSE[:40])
    flagged: list[dict] = []

    for ticker in scan_list:
        try:
            tk = yf.Ticker(ticker)
            info = tk.info or {}
            hist = tk.history(period="1mo")
            if hist.empty or len(hist) < 5:
                continue

            today_vol = _safe_int(info.get("volume") or info.get("regularMarketVolume"))
            avg_vol = _safe_int(info.get("averageVolume"))
            avg_vol_10d = _safe_int(info.get("averageVolume10days", avg_vol))

            if avg_vol < 100_000 or today_vol < 100_000:
                continue

            vol_spike = today_vol / avg_vol if avg_vol > 0 else 1.0

            # Large vol spike with low price movement suggests institutional dark pool activity
            price = _safe(info.get("currentPrice") or info.get("regularMarketPrice"))
            prev = _safe(info.get("previousClose") or info.get("regularMarketPreviousClose"))
            pct_move = abs((price - prev) / prev * 100) if prev > 0 else 0

            if vol_spike >= 2.0 and pct_move < 3.0:
                bias = "accumulation" if price >= prev else "distribution"
                flagged.append({
                    "ticker": ticker,
                    "sector": TICKER_SECTORS.get(ticker, "Other"),
                    "volume": today_vol,
                    "avg_volume": avg_vol,
                    "vol_spike": round(vol_spike, 1),
                    "price_move_pct": round(pct_move, 2),
                    "bias": bias,
                    "implication": (
                        f"{vol_spike:.1f}x avg volume with only {pct_move:.1f}% price move "
                        f"suggests institutional {bias}."
                    ),
                })
        except Exception:
            continue

    flagged.sort(key=lambda x: x["vol_spike"], reverse=True)
    return {
        "flagged": flagged[:10],
        "summary": f"{len(flagged)} stocks show elevated off-exchange volume signals.",
        "error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
#  5. SHORT INTEREST CHANGES
# ════════════════════════════════════════════════════════════════════════════

def get_short_interest_data(
    watchlist: list[str],
    sector_etfs: dict[str, str] | None = None,
) -> dict:
    """Track short interest across sectors and watchlist tickers.

    Returns sector-level aggregates and individual ticker data,
    plus squeeze candidates (high SI + bullish options flow).
    """
    sector_etfs = sector_etfs or SECTOR_ETFS
    all_tickers = list(watchlist) + list(sector_etfs.keys())
    ticker_data: list[dict] = []

    for ticker in all_tickers:
        try:
            info = yf.Ticker(ticker).info or {}
            short_pct = _safe(info.get("shortPercentOfFloat", 0)) * 100
            short_ratio = _safe(info.get("shortRatio", 0))
            shares_short = _safe_int(info.get("sharesShort", 0))
            shares_short_prior = _safe_int(info.get("sharesShortPriorMonth", 0))

            if shares_short == 0 and short_pct == 0:
                continue

            change_pct = 0.0
            if shares_short_prior > 0:
                change_pct = (shares_short - shares_short_prior) / shares_short_prior * 100

            significant = abs(change_pct) > 10 or short_pct > 15

            ticker_data.append({
                "ticker": ticker,
                "is_sector_etf": ticker in sector_etfs,
                "sector": sector_etfs.get(ticker) or TICKER_SECTORS.get(ticker, "Other"),
                "short_pct": round(short_pct, 1),
                "short_ratio": round(short_ratio, 1),
                "shares_short": shares_short,
                "shares_short_prior": shares_short_prior,
                "change_pct": round(change_pct, 1),
                "significant": significant,
                "direction": "increasing" if change_pct > 5 else ("decreasing" if change_pct < -5 else "stable"),
            })
        except Exception:
            continue

    # Identify squeeze candidates: high SI + would need bullish options signal
    # (caller can cross-reference with sector flow)
    squeeze_candidates = [
        t for t in ticker_data
        if t["short_pct"] > 10 and t["change_pct"] > 0 and not t["is_sector_etf"]
    ]
    squeeze_candidates.sort(key=lambda x: x["short_pct"], reverse=True)

    significant_changes = [t for t in ticker_data if t["significant"]]

    return {
        "tickers": ticker_data,
        "squeeze_candidates": squeeze_candidates[:5],
        "significant_changes": significant_changes,
        "summary": (
            f"{len(significant_changes)} tickers with significant SI changes. "
            f"{len(squeeze_candidates)} potential squeeze setups."
        ),
        "error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
#  6. CONGRESSIONAL TRADING DISCLOSURES
# ════════════════════════════════════════════════════════════════════════════

# Committee oversight mapping for policy-relevance flagging
COMMITTEE_MAP: dict[str, list[str]] = {
    "Armed Services": ["Defense Technology", "Industrials"],
    "Energy": ["Energy", "Utilities", "Clean Energy"],
    "Finance": ["Financials", "FinTech"],
    "Banking": ["Financials", "FinTech"],
    "Commerce": ["Technology", "Comm. Svcs"],
    "Health": ["Healthcare", "Biotech"],
    "Intelligence": ["Defense Technology", "Technology"],
    "Appropriations": ["All sectors"],
}


def get_congressional_trades(days_back: int = 7) -> list[dict]:
    """Pull recent House and Senate trading disclosures.

    Sources:
      House: house-stock-watcher-data S3 bucket (public, no auth)
      Senate: senate-stock-watcher-data S3 bucket (public, no auth)
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    trades: list[dict] = []

    # House disclosures
    house_url = "https://house-stock-watcher-data.s3-us-west-2.amazonaws.com/data/all_transactions.json"
    house_data = _fetch_json(house_url, timeout=20)
    if house_data and isinstance(house_data, list):
        for t in house_data:
            try:
                date_str = t.get("transaction_date", "")
                if not date_str:
                    continue
                tx_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if tx_date < cutoff:
                    continue
                ticker = (t.get("ticker") or "").strip().upper()
                if not ticker or ticker in ("--", "N/A", ""):
                    continue
                trades.append({
                    "chamber": "House",
                    "member": t.get("representative", "Unknown"),
                    "ticker": ticker,
                    "asset": t.get("asset_description", ""),
                    "type": t.get("type", ""),
                    "amount": t.get("amount", ""),
                    "date": date_str,
                    "disclosure_date": t.get("disclosure_date", ""),
                    "sector": TICKER_SECTORS.get(ticker, "Other"),
                    "district": t.get("district", ""),
                })
            except Exception:
                continue

    # Senate disclosures
    senate_url = "https://senate-stock-watcher-data.s3-us-west-2.amazonaws.com/aggregate/all_transactions.json"
    senate_data = _fetch_json(senate_url, timeout=20)
    if senate_data and isinstance(senate_data, list):
        for t in senate_data:
            try:
                date_str = t.get("transaction_date", "")
                if not date_str:
                    continue
                tx_date = datetime.strptime(date_str, "%m/%d/%Y").replace(tzinfo=timezone.utc)
                if tx_date < cutoff:
                    continue
                ticker = (t.get("ticker") or "").strip().upper()
                if not ticker or ticker in ("--", "N/A", ""):
                    continue
                trades.append({
                    "chamber": "Senate",
                    "member": f"Sen. {t.get('senator', 'Unknown')}",
                    "ticker": ticker,
                    "asset": t.get("asset_description", ""),
                    "type": t.get("type", ""),
                    "amount": t.get("amount", ""),
                    "date": tx_date.strftime("%Y-%m-%d"),
                    "disclosure_date": t.get("disclosure_date", ""),
                    "sector": TICKER_SECTORS.get(ticker, "Other"),
                    "district": "",
                })
            except Exception:
                continue

    trades.sort(key=lambda x: x["date"], reverse=True)
    return trades[:30]


# ════════════════════════════════════════════════════════════════════════════
#  7. EARNINGS CALENDAR WITH OPTIONS-IMPLIED MOVE
# ════════════════════════════════════════════════════════════════════════════

def get_earnings_calendar(days_ahead: int = 5) -> list[dict]:
    """Show companies reporting earnings in the next N trading days
    with options-implied move vs historical average.
    """
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead + 2)  # buffer for weekends
    results: list[dict] = []

    # Screen a universe of major stocks for upcoming earnings
    check_list = SCREEN_UNIVERSE[:60]

    for ticker in check_list:
        try:
            tk = yf.Ticker(ticker)
            cal = tk.calendar
            if cal is None or (hasattr(cal, 'empty') and cal.empty):
                continue

            # yfinance calendar format varies; try to extract earnings date
            earnings_date = None
            if isinstance(cal, dict):
                ed = cal.get("Earnings Date")
                if ed:
                    if isinstance(ed, list) and len(ed) > 0:
                        earnings_date = ed[0]
                    else:
                        earnings_date = ed
            elif hasattr(cal, "iloc"):
                try:
                    ed_val = cal.iloc[0, 0] if cal.shape[1] > 0 else None
                    if ed_val:
                        earnings_date = ed_val
                except Exception:
                    pass

            if earnings_date is None:
                continue

            # Normalize to date
            if hasattr(earnings_date, "date"):
                ed = earnings_date.date()
            elif isinstance(earnings_date, str):
                ed = datetime.strptime(earnings_date[:10], "%Y-%m-%d").date()
            else:
                continue

            if ed < today or ed > end:
                continue

            # Calculate implied move from ATM straddle
            implied_move = _get_implied_move(tk)

            # Get historical earnings moves
            hist_avg_move = _get_historical_earnings_move(tk)

            rich_cheap = "—"
            if implied_move and hist_avg_move and hist_avg_move > 0:
                ratio = implied_move / hist_avg_move
                if ratio > 1.2:
                    rich_cheap = "RICH"
                elif ratio < 0.8:
                    rich_cheap = "CHEAP"
                else:
                    rich_cheap = "FAIR"

            results.append({
                "ticker": ticker,
                "earnings_date": ed.isoformat(),
                "implied_move_pct": round(implied_move, 1) if implied_move else None,
                "hist_avg_move_pct": round(hist_avg_move, 1) if hist_avg_move else None,
                "rich_cheap": rich_cheap,
                "sector": TICKER_SECTORS.get(ticker, "Other"),
            })
        except Exception:
            continue

    results.sort(key=lambda x: x["earnings_date"])
    return results


def _get_implied_move(tk: yf.Ticker) -> float | None:
    """Estimate options-implied move for nearest expiration ATM straddle."""
    try:
        exps = tk.options
        if not exps:
            return None
        chain = tk.option_chain(exps[0])
        info = tk.info or {}
        price = _safe(info.get("currentPrice") or info.get("regularMarketPrice"))
        if price <= 0:
            return None

        # Find ATM strike
        strikes = chain.calls["strike"].values
        if len(strikes) == 0:
            return None
        atm_idx = abs(strikes - price).argmin()
        atm_strike = strikes[atm_idx]

        call_price = _safe(chain.calls.iloc[atm_idx].get("lastPrice"))
        # Find matching put
        put_match = chain.puts[chain.puts["strike"] == atm_strike]
        put_price = _safe(put_match.iloc[0].get("lastPrice")) if len(put_match) > 0 else 0

        straddle = call_price + put_price
        implied_pct = (straddle / price) * 100
        return round(implied_pct, 1)
    except Exception:
        return None


def _get_historical_earnings_move(tk: yf.Ticker) -> float | None:
    """Average absolute move on last 4 earnings dates."""
    try:
        hist = tk.history(period="1y")
        if len(hist) < 60:
            return None
        earnings_dates = tk.earnings_dates
        if earnings_dates is None or len(earnings_dates) == 0:
            return None

        moves: list[float] = []
        for ed in earnings_dates.index[:4]:
            ed_date = ed.date() if hasattr(ed, "date") else ed
            # Find closest trading day
            for i, idx in enumerate(hist.index):
                if idx.date() >= ed_date:
                    if i > 0:
                        prev_close = float(hist.iloc[i - 1]["Close"])
                        close = float(hist.iloc[i]["Close"])
                        move = abs((close - prev_close) / prev_close * 100)
                        moves.append(move)
                    break

        return round(sum(moves) / len(moves), 1) if moves else None
    except Exception:
        return None


# ════════════════════════════════════════════════════════════════════════════
#  8. FED SPEAK & MACRO EVENT CALENDAR
# ════════════════════════════════════════════════════════════════════════════

# Static FOMC schedule for 2026 (update annually)
FOMC_DATES_2026 = [
    "2026-01-28", "2026-01-29",
    "2026-03-17", "2026-03-18",
    "2026-05-05", "2026-05-06",
    "2026-06-16", "2026-06-17",
    "2026-07-28", "2026-07-29",
    "2026-09-15", "2026-09-16",
    "2026-11-03", "2026-11-04",
    "2026-12-15", "2026-12-16",
]

# Major recurring economic releases (approximate monthly dates — day-of-month patterns)
ECON_CALENDAR_PATTERNS: list[dict] = [
    {"name": "CPI", "typical_day": 13, "importance": "HIGH"},
    {"name": "PPI", "typical_day": 14, "importance": "HIGH"},
    {"name": "Nonfarm Payrolls", "typical_day": 5, "importance": "HIGH"},
    {"name": "Retail Sales", "typical_day": 15, "importance": "MEDIUM"},
    {"name": "PMI (Mfg)", "typical_day": 1, "importance": "MEDIUM"},
    {"name": "PMI (Services)", "typical_day": 3, "importance": "MEDIUM"},
    {"name": "GDP (Quarterly)", "typical_day": 28, "importance": "HIGH"},
    {"name": "PCE Price Index", "typical_day": 28, "importance": "HIGH"},
    {"name": "JOLTS", "typical_day": 7, "importance": "MEDIUM"},
    {"name": "Consumer Confidence", "typical_day": 25, "importance": "MEDIUM"},
]


def get_macro_events(days_ahead: int = 5) -> dict:
    """Flag upcoming Fed speak, economic releases, and FOMC dates."""
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)
    events: list[dict] = []

    # Check FOMC dates
    for fomc in FOMC_DATES_2026:
        fomc_date = datetime.strptime(fomc, "%Y-%m-%d").date()
        if today <= fomc_date <= end:
            day_label = "Day 1" if fomc == FOMC_DATES_2026[FOMC_DATES_2026.index(fomc)] else "Day 2"
            events.append({
                "date": fomc,
                "event": f"FOMC Meeting {day_label}",
                "importance": "CRITICAL",
                "category": "fed",
            })

    # Estimate upcoming economic releases based on day-of-month patterns
    for day_offset in range(days_ahead + 1):
        check_date = today + timedelta(days=day_offset)
        dom = check_date.day
        for pat in ECON_CALENDAR_PATTERNS:
            if abs(dom - pat["typical_day"]) <= 1 and check_date.weekday() < 5:
                events.append({
                    "date": check_date.isoformat(),
                    "event": pat["name"],
                    "importance": pat["importance"],
                    "category": "economic",
                })

    # Treasury auction schedule (10Y/30Y typically mid-month, 2Y/5Y weekly)
    for day_offset in range(days_ahead + 1):
        check_date = today + timedelta(days=day_offset)
        if check_date.weekday() < 5:
            dom = check_date.day
            if 8 <= dom <= 12 and check_date.weekday() in (1, 2):
                events.append({
                    "date": check_date.isoformat(),
                    "event": "Treasury Auction (10Y/30Y)",
                    "importance": "MEDIUM",
                    "category": "treasury",
                })

    # Rate path sensitivity from options (VIX level as proxy)
    rate_sensitivity = ""
    try:
        vix_info = yf.Ticker("^VIX").info or {}
        vix = _safe(vix_info.get("regularMarketPrice") or vix_info.get("previousClose"))
        if vix > 25:
            rate_sensitivity = f"VIX at {vix:.1f} — elevated rate sensitivity. Options market pricing significant move risk around upcoming data."
        elif vix > 18:
            rate_sensitivity = f"VIX at {vix:.1f} — moderate. Market pricing normal uncertainty around scheduled releases."
        else:
            rate_sensitivity = f"VIX at {vix:.1f} — low. Options market complacent ahead of scheduled events."
    except Exception:
        rate_sensitivity = "VIX data unavailable."

    events.sort(key=lambda x: (x["date"], {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2}.get(x["importance"], 3)))

    return {
        "events": events,
        "rate_sensitivity": rate_sensitivity,
        "fomc_upcoming": any(e["category"] == "fed" for e in events),
        "error": None,
    }


# ════════════════════════════════════════════════════════════════════════════
#  9. CREDIT MARKET SIGNALS
# ════════════════════════════════════════════════════════════════════════════

def get_credit_market_signals() -> dict:
    """HYG and LQD options flow + spread analysis as risk-off indicators."""
    result: dict = {"hyg": {}, "lqd": {}, "spread_trend": "stable", "risk_signal": "neutral", "error": None}

    for etf_ticker, label in [("HYG", "hyg"), ("LQD", "lqd")]:
        try:
            tk = yf.Ticker(etf_ticker)
            exps = tk.options
            if not exps:
                continue

            chain = tk.option_chain(exps[0])
            call_vol = _safe_int(chain.calls["volume"].sum())
            put_vol = _safe_int(chain.puts["volume"].sum())
            call_oi = _safe_int(chain.calls["openInterest"].sum())
            put_oi = _safe_int(chain.puts["openInterest"].sum())

            pc_ratio = put_vol / call_vol if call_vol > 0 else 0
            signal = "bearish" if pc_ratio > 1.3 else ("bullish" if pc_ratio < 0.7 else "neutral")

            # Price trend (proxy for spread direction)
            hist = tk.history(period="1mo")
            if len(hist) >= 5:
                recent_close = float(hist["Close"].iloc[-1])
                week_ago_close = float(hist["Close"].iloc[-5])
                month_ago_close = float(hist["Close"].iloc[0])
                pct_1w = (recent_close - week_ago_close) / week_ago_close * 100
                pct_1m = (recent_close - month_ago_close) / month_ago_close * 100
            else:
                recent_close = pct_1w = pct_1m = 0

            result[label] = {
                "put_call_ratio": round(pc_ratio, 2),
                "call_volume": call_vol,
                "put_volume": put_vol,
                "signal": signal,
                "price": round(recent_close, 2),
                "chg_1w_pct": round(pct_1w, 2),
                "chg_1m_pct": round(pct_1m, 2),
            }
        except Exception as exc:
            log.debug("Credit signal error %s: %s", etf_ticker, exc)

    # Determine overall credit risk signal
    hyg_sig = result["hyg"].get("signal", "neutral")
    lqd_sig = result["lqd"].get("signal", "neutral")
    hyg_1w = result["hyg"].get("chg_1w_pct", 0)

    if hyg_sig == "bearish" or lqd_sig == "bearish":
        result["risk_signal"] = "risk-off"
        result["spread_trend"] = "widening"
    elif hyg_1w < -1.0:
        result["risk_signal"] = "caution"
        result["spread_trend"] = "widening"
    elif hyg_sig == "bullish" and hyg_1w > 0:
        result["risk_signal"] = "risk-on"
        result["spread_trend"] = "tightening"
    else:
        result["risk_signal"] = "neutral"
        result["spread_trend"] = "stable"

    result["summary"] = (
        f"Credit: {result['risk_signal'].upper()}. "
        f"HYG P/C={result['hyg'].get('put_call_ratio', 0):.2f} ({hyg_sig}), "
        f"LQD P/C={result['lqd'].get('put_call_ratio', 0):.2f} ({lqd_sig}). "
        f"Spread trend: {result['spread_trend']}."
    )
    return result


# ════════════════════════════════════════════════════════════════════════════
#  MASTER COLLECTION FUNCTION
# ════════════════════════════════════════════════════════════════════════════

def collect_market_intelligence(
    watchlist: list[str],
    macro_data: dict,
    client: anthropic.Anthropic,
) -> dict:
    """Collect all market-wide intelligence data.

    Returns a dict with all sections populated. Never raises — each section
    is independently try/excepted.
    """
    result: dict = {
        "sector_flow": {},
        "unusual_activity": [],
        "rotation_analysis": "",
        "dark_pool": {},
        "short_interest": {},
        "congressional_trades": [],
        "earnings_calendar": [],
        "macro_events": {},
        "credit_signals": {},
    }

    log.info("  [Market Intel] Collecting sector options flow …")
    try:
        result["sector_flow"] = get_sector_options_flow()
        log.info("    Sector flow: %d sectors analyzed", len(result["sector_flow"].get("sectors", [])))
    except Exception as exc:
        log.warning("    Sector flow failed: %s", exc)

    log.info("  [Market Intel] Screening unusual market-wide activity …")
    try:
        result["unusual_activity"] = get_unusual_market_activity(watchlist)
        log.info("    Unusual activity: %d stocks flagged", len(result["unusual_activity"]))
    except Exception as exc:
        log.warning("    Unusual activity screen failed: %s", exc)

    log.info("  [Market Intel] Collecting credit market signals …")
    try:
        result["credit_signals"] = get_credit_market_signals()
        log.info("    Credit: %s", result["credit_signals"].get("risk_signal", "n/a"))
    except Exception as exc:
        log.warning("    Credit signals failed: %s", exc)

    log.info("  [Market Intel] Collecting short interest data …")
    try:
        result["short_interest"] = get_short_interest_data(watchlist)
        log.info("    Short interest: %d tickers, %d squeeze candidates",
                 len(result["short_interest"].get("tickers", [])),
                 len(result["short_interest"].get("squeeze_candidates", [])))
    except Exception as exc:
        log.warning("    Short interest failed: %s", exc)

    log.info("  [Market Intel] Generating rotation analysis …")
    try:
        result["rotation_analysis"] = generate_rotation_analysis(
            result["sector_flow"], result["credit_signals"],
            result["short_interest"], client,
        )
    except Exception as exc:
        log.warning("    Rotation analysis failed: %s", exc)

    log.info("  [Market Intel] Checking dark pool signals …")
    try:
        result["dark_pool"] = get_dark_pool_signals()
        log.info("    Dark pool: %d stocks flagged", len(result["dark_pool"].get("flagged", [])))
    except Exception as exc:
        log.warning("    Dark pool failed: %s", exc)

    log.info("  [Market Intel] Fetching congressional trades …")
    try:
        result["congressional_trades"] = get_congressional_trades(days_back=7)
        log.info("    Congressional: %d trades in last 7 days", len(result["congressional_trades"]))
    except Exception as exc:
        log.warning("    Congressional trades failed: %s", exc)

    log.info("  [Market Intel] Building earnings calendar …")
    try:
        result["earnings_calendar"] = get_earnings_calendar(days_ahead=5)
        log.info("    Earnings: %d companies reporting in next 5 days", len(result["earnings_calendar"]))
    except Exception as exc:
        log.warning("    Earnings calendar failed: %s", exc)

    log.info("  [Market Intel] Checking macro event calendar …")
    try:
        result["macro_events"] = get_macro_events(days_ahead=5)
        log.info("    Macro events: %d upcoming", len(result["macro_events"].get("events", [])))
    except Exception as exc:
        log.warning("    Macro events failed: %s", exc)

    log.info("  [Market Intel] Collection complete.")
    return result
