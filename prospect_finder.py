#!/usr/bin/env python3
"""
prospect_finder.py — Daily watchlist suggestion engine for Langston's Financial Intelligence.

Screens a curated universe of ~100 stocks, scores each against the current watchlist's
investment themes and macro environment, then uses Claude to generate theses for the
top 5 prospects.

Standalone usage:
  python prospect_finder.py                 # uses tickers.txt + today's macro
  python prospect_finder.py --top 5         # default

Returns are importable via find_prospects().
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yfinance as yf

from config import ANTHROPIC_MODEL, get_anthropic_key, load_tickers, REPO_DIR

log = logging.getLogger("prospects")

# ════════════════════════════════════════════════════════════════════════════
#  CANDIDATE UNIVERSE  (curated; ~100 liquid, theme-relevant stocks)
# ════════════════════════════════════════════════════════════════════════════

# Maps theme name → list of candidate tickers
UNIVERSE: dict[str, list[str]] = {
    "AI & Machine Learning": [
        "NVDA", "AMD", "GOOGL", "META", "MSFT", "PLTR", "AI", "SOUN",
        "PATH", "SNOW", "DDOG", "C3AI", "GTLB", "MDB", "CFLT",
    ],
    "Quantum Computing": [
        "IONQ", "QBTS", "QUBT", "IBM", "HON", "MSFT",
    ],
    "Defense Technology": [
        "KTOS", "RCAT", "AVAV", "PLTR", "CACI", "BAH", "SAIC",
        "LHX", "DRS", "LDOS", "HII", "NOC", "LMT", "RTX",
    ],
    "Clean Energy & Utilities": [
        "FSLR", "ENPH", "CEG", "NRG", "VST", "ARRY", "BE",
        "RUN", "AES", "EIX", "PCG", "DUK", "SO", "XEL",
    ],
    "Healthcare AI & Biotech": [
        "RXRX", "HIMS", "DOCS", "VEEV", "EXAS", "NVCR",
        "PACB", "TXG", "ILMN", "MDAI", "CERT",
    ],
    "Semiconductors": [
        "AVGO", "AMAT", "KLAC", "MRVL", "MU", "ARM", "ASML",
        "SMCI", "ONTO", "LRCX", "INTC", "QCOM",
    ],
    "Space & Next-Gen Aerospace": [
        "RKLB", "ASTS", "LUNR", "PL", "MNTS", "SPCE",
        "HWM", "TDG", "AXON",
    ],
    "FinTech & Digital Payments": [
        "SOFI", "AFRM", "UPST", "LC", "PYPL", "SQ", "HOOD",
        "NU", "OPEN", "FLUT",
    ],
}

# Known theme(s) for each watchlist ticker (used to identify active themes)
TICKER_THEMES: dict[str, list[str]] = {
    "TEM":  ["AI & Machine Learning", "Healthcare AI & Biotech"],
    "RGTI": ["Quantum Computing"],
    "BBAI": ["AI & Machine Learning", "Defense Technology"],
    "NEE":  ["Clean Energy & Utilities"],
    # Add more as the watchlist grows
    "IONQ": ["Quantum Computing"],
    "PLTR": ["AI & Machine Learning", "Defense Technology"],
    "KTOS": ["Defense Technology"],
    "FSLR": ["Clean Energy & Utilities"],
}

# Sector ETF → theme mapping (for macro alignment scoring)
SECTOR_TO_THEME: dict[str, str] = {
    "Technology":     "AI & Machine Learning",
    "Industrials":    "Defense Technology",
    "Utilities":      "Clean Energy & Utilities",
    "Healthcare":     "Healthcare AI & Biotech",
    "Cons. Disc.":    "FinTech & Digital Payments",
}


# ════════════════════════════════════════════════════════════════════════════
#  THEME DETECTION
# ════════════════════════════════════════════════════════════════════════════

def detect_active_themes(current_tickers: list[str]) -> list[str]:
    """Return de-duplicated list of themes represented by the current watchlist."""
    themes: list[str] = []
    for tk in current_tickers:
        tk_upper = tk.upper()
        if tk_upper in TICKER_THEMES:
            for t in TICKER_THEMES[tk_upper]:
                if t not in themes:
                    themes.append(t)
        else:
            # Try to infer from yfinance industry / sector
            try:
                info = yf.Ticker(tk_upper).info or {}
                sector   = info.get("sector", "")
                industry = info.get("industry", "").lower()
                if "quantum" in industry:
                    _add(themes, "Quantum Computing")
                elif "semiconductor" in industry or "chip" in industry:
                    _add(themes, "Semiconductors")
                elif "defense" in sector or "defense" in industry:
                    _add(themes, "Defense Technology")
                elif sector in ("Healthcare", "Biotechnology"):
                    _add(themes, "Healthcare AI & Biotech")
                elif sector == "Utilities":
                    _add(themes, "Clean Energy & Utilities")
                elif sector == "Technology":
                    _add(themes, "AI & Machine Learning")
            except Exception:
                pass
    return themes or list(UNIVERSE.keys())  # fallback: all themes


def _add(lst: list, item: str) -> None:
    if item not in lst:
        lst.append(item)


def build_candidate_pool(
    active_themes: list[str],
    exclude: set[str],
) -> list[dict]:
    """Return all candidates from active themes, excluding the current watchlist."""
    seen: set[str] = set()
    candidates: list[dict] = []
    for theme in active_themes:
        for tk in UNIVERSE.get(theme, []):
            if tk.upper() in exclude or tk.upper() in seen:
                continue
            seen.add(tk.upper())
            candidates.append({"ticker": tk.upper(), "theme": theme})
    return candidates


# ════════════════════════════════════════════════════════════════════════════
#  DATA FETCH
# ════════════════════════════════════════════════════════════════════════════

def fetch_basic_metrics(ticker: str) -> dict:
    """Pull price, 1M return, P/S, market cap from yfinance."""
    try:
        yt   = yf.Ticker(ticker)
        info = yt.info or {}
        hist = yt.history(period="1mo")

        price   = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        hist_1m = hist["Close"]
        ret_1m  = ((hist_1m.iloc[-1] / hist_1m.iloc[0]) - 1) * 100 if len(hist_1m) >= 2 else 0.0
        ret_5d  = ((hist_1m.iloc[-1] / hist_1m.iloc[-6]) - 1) * 100 if len(hist_1m) >= 6 else 0.0

        return {
            "price":     round(price, 2),
            "ret_1m":    round(float(ret_1m), 2),
            "ret_5d":    round(float(ret_5d), 2),
            "market_cap": int(info.get("marketCap") or 0),
            "ps_ratio":  float(info.get("priceToSalesTrailing12Months") or 0),
            "pe_fwd":    float(info.get("forwardPE") or 0),
            "short_name": info.get("shortName", ticker),
            "sector":    info.get("sector", ""),
            "industry":  info.get("industry", ""),
            "exch":      info.get("exchange", ""),
            "error":     None,
        }
    except Exception as exc:
        return {
            "price": 0, "ret_1m": 0, "ret_5d": 0, "market_cap": 0,
            "ps_ratio": 0, "pe_fwd": 0, "short_name": ticker,
            "sector": "", "industry": "", "exch": "",
            "error": str(exc),
        }


# ════════════════════════════════════════════════════════════════════════════
#  SCORING
# ════════════════════════════════════════════════════════════════════════════

def score_candidate(
    cand: dict,
    metrics: dict,
    active_themes: list[str],
    macro_data: dict,
    options_signal: str = "neutral",
    insider_signal: str = "neutral",
    institutional_signal: str = "neutral",
) -> float:
    """Compute a composite 0-10 score for a prospect candidate."""

    # 1. Theme alignment (0-10)
    theme_score = 10.0 if cand["theme"] in active_themes else 5.0

    # 2. Momentum (0-10)
    r1m = metrics.get("ret_1m", 0)
    if r1m >= 25:     mom = 10
    elif r1m >= 15:   mom = 8.5
    elif r1m >= 5:    mom = 7
    elif r1m >= -3:   mom = 5.5   # mild pullback = potential entry
    elif r1m >= -10:  mom = 4
    else:             mom = 2

    # 3. Valuation (0-10) — lower P/S is better for high-growth names
    ps = metrics.get("ps_ratio", 0)
    if ps <= 0:     val = 5       # unknown
    elif ps <= 3:   val = 9
    elif ps <= 8:   val = 7
    elif ps <= 15:  val = 6
    elif ps <= 30:  val = 5
    elif ps <= 60:  val = 4
    else:           val = 3

    # 4. Smart money (0-10)
    def _sig_val(s: str) -> float:
        return {"bullish": 3.0, "neutral": 1.0, "bearish": 0.0}.get(s, 1.0)

    sm = (_sig_val(options_signal) + _sig_val(insider_signal) +
          _sig_val(institutional_signal)) / 9.0 * 10.0

    # 5. Macro sector alignment (0-10)
    hot_sectors = {
        v["name"]: v.get("chg_5d", 0)
        for v in macro_data.get("sectors", {}).values()
    }
    ticker_sector = metrics.get("sector", "")
    matched_chg   = hot_sectors.get(SECTOR_TO_THEME.get(ticker_sector, ""), 0)
    macro_align   = min(10.0, max(2.0, 5.0 + matched_chg * 0.6))

    # Weighted composite
    score = (
        0.30 * theme_score +
        0.25 * mom +
        0.15 * val +
        0.20 * sm +
        0.10 * macro_align
    )
    return round(score, 2)


# ════════════════════════════════════════════════════════════════════════════
#  CLAUDE THESIS GENERATION
# ════════════════════════════════════════════════════════════════════════════

def generate_prospect_theses(
    top_candidates: list[dict],
    current_tickers: list[str],
    active_themes: list[str],
    macro_data: dict,
    client: anthropic.Anthropic,
    n: int = 5,
) -> list[dict]:
    """Call Claude once to rank + write theses for the top N prospects."""

    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Build macro context string
    rates   = macro_data.get("rates", {})
    sectors = macro_data.get("sectors", {})
    rate_lines = "\n".join(
        f"  {v['name']}: {v['value']:.2f} (WoW {v.get('chg', 0):+.4f})"
        for v in list(rates.values())[:5]
    ) or "  No rate data."
    hot_sectors = sorted(sectors.values(), key=lambda x: x.get("chg_5d", 0), reverse=True)
    sector_lines = "\n".join(
        f"  {v['name']:14s} | 5D: {v.get('chg_5d', 0):+.2f}%"
        for v in hot_sectors[:5]
    ) or "  No sector data."

    # Build candidate table
    cand_lines = []
    for i, c in enumerate(top_candidates[:12], 1):
        m = c.get("metrics", {})
        cand_lines.append(
            f"{i:2d}. {c['ticker']:6s} — {c.get('company', c['ticker'])[:30]:30s}"
            f"  Theme: {c['theme']:28s}"
            f"  Score: {c['score']:.1f}/10"
            f"  1M: {m.get('ret_1m', 0):+.1f}%"
            f"  P/S: {m.get('ps_ratio', 0):.1f}x"
            f"  Cap: ${m.get('market_cap', 0)/1e9:.1f}B"
            f"  Options: {c.get('options_signal', 'neutral')}"
            f"  Insider: {c.get('insider_signal', 'neutral')}"
            f"  Inst: {c.get('institutional_signal', 'neutral')}"
        )

    prompt = f"""You are a senior equity research analyst at Langston's Financial Intelligence.
Date: {date_str}

CURRENT WATCHLIST THEMES:
{', '.join(active_themes)}

CURRENT WATCHLIST TICKERS (DO NOT suggest these):
{', '.join(current_tickers)}

MACRO ENVIRONMENT:
{rate_lines}

HOT SECTORS (5D):
{sector_lines}

TOP SCREENED PROSPECTS (ranked by composite score):
{chr(10).join(cand_lines)}

SELECT the TOP {n} prospects to recommend. For each, output EXACTLY this format:

===PROSPECT_1===
TICKER: [ticker]
COMPANY: [full company name]
THEME: [theme category]
RATING: [Buy | Watch | Avoid]
SCORE: [composite score from table]
THESIS: [2-3 sentences: why this stock fits the current strategy, what the key catalyst is, and why now is a good entry point. Be specific about the company's business and near-term drivers.]
ACTION: [One of: "Add to watchlist via tickers.txt" | "Monitor for pullback to $XX entry" | "Avoid — [specific reason]"]
===END_PROSPECT_1===

===PROSPECT_2===
...
===END_PROSPECT_2===

Continue through PROSPECT_{n}. Choose stocks with the best combination of theme fit, momentum, and smart money signals. Prioritize diversity (no two picks from identical sub-niches). Only pick from the screened list above."""

    log.info("  Calling Claude for %d prospect theses …", n)
    try:
        msg = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
    except Exception as exc:
        log.error("  Claude call failed: %s", exc)
        return []

    # Parse
    results: list[dict] = []
    for i in range(1, n + 1):
        open_m  = f"===PROSPECT_{i}==="
        close_m = f"===END_PROSPECT_{i}==="
        s = raw.find(open_m)
        e = raw.find(close_m)
        if s == -1 or e == -1:
            continue
        block = raw[s + len(open_m):e].strip()

        def _field(key: str) -> str:
            for line in block.splitlines():
                if line.startswith(f"{key}:"):
                    return line[len(f"{key}:"):].strip()
            return ""

        ticker = _field("TICKER").upper()
        if not ticker:
            continue

        # Find original candidate data
        orig = next((c for c in top_candidates if c["ticker"] == ticker), {})
        metrics = orig.get("metrics", {})

        results.append({
            "rank":                 i,
            "ticker":               ticker,
            "company":              _field("COMPANY") or orig.get("company", ticker),
            "theme":                _field("THEME") or orig.get("theme", ""),
            "rating":               _field("RATING") or "Watch",
            "score":                float(orig.get("score", 0)),
            "thesis":               _field("THESIS"),
            "action":               _field("ACTION"),
            "price":                metrics.get("price", 0),
            "ret_1m":               metrics.get("ret_1m", 0),
            "market_cap":           metrics.get("market_cap", 0),
            "ps_ratio":             metrics.get("ps_ratio", 0),
            "exch":                 metrics.get("exch", ""),
            "options_signal":       orig.get("options_signal", "neutral"),
            "insider_signal":       orig.get("insider_signal", "neutral"),
            "institutional_signal": orig.get("institutional_signal", "neutral"),
        })

    return results


# ════════════════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

def find_prospects(
    current_tickers: list[str],
    macro_data: dict,
    client: anthropic.Anthropic,
    n: int = 5,
) -> list[dict]:
    """
    Screen the universe, score candidates, generate Claude theses.

    Args:
        current_tickers: tickers already in tickers.txt (never suggest these)
        macro_data:       dict from get_macro_data() — used for sector alignment
        client:           Anthropic client
        n:                number of prospects to return (default 5)

    Returns:
        List of up to n prospect dicts, ranked by alignment score.
    """
    exclude = {t.upper() for t in current_tickers}

    # ── 1. Identify active themes ──────────────────────────────────────────
    active_themes = detect_active_themes(list(exclude))
    log.info("  Active themes: %s", ", ".join(active_themes))

    # ── 2. Build candidate pool ────────────────────────────────────────────
    candidates = build_candidate_pool(active_themes, exclude)
    log.info("  Candidate pool: %d stocks", len(candidates))

    # ── 3. Phase 1: Basic metrics for all candidates ───────────────────────
    log.info("  Fetching basic metrics …")
    for i, cand in enumerate(candidates):
        m = fetch_basic_metrics(cand["ticker"])
        cand["metrics"]  = m
        cand["company"]  = m.get("short_name", cand["ticker"])
        # Skip if no price data (likely invalid ticker)
        if m.get("error") or m.get("market_cap", 0) < 50_000_000:
            cand["score"] = -1.0
        else:
            cand["score"] = score_candidate(
                cand, m, active_themes, macro_data,
            )
        # Brief pause every 10 to be polite to yfinance
        if i % 10 == 9:
            time.sleep(0.5)

    # Filter out invalid tickers and sort
    candidates = [c for c in candidates if c["score"] >= 0]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    log.info("  After phase-1 scoring: top 15 candidates")

    # ── 4. Phase 2: Smart money deep-dive for top 15 ──────────────────────
    try:
        from options_flow          import get_options_flow
        from insider_tracker       import get_insider_activity
        from institutional_tracker import get_institutional_ownership
        from config                import _KNOWN_CIKS

        for cand in candidates[:15]:
            tk = cand["ticker"]
            log.info("    Smart money: %s", tk)
            try:
                opt  = get_options_flow(tk)
                cand["options_signal"] = opt.get("flow_signal", "neutral")
            except Exception:
                cand["options_signal"] = "neutral"

            try:
                cik = _KNOWN_CIKS.get(tk, "")
                ins = get_insider_activity(tk, cik=cik, days_back=30)
                cand["insider_signal"] = ins.get("net_signal", "neutral")
            except Exception:
                cand["insider_signal"] = "neutral"

            try:
                inst = get_institutional_ownership(tk)
                cand["institutional_signal"] = inst.get("smart_money_signal", "neutral")
            except Exception:
                cand["institutional_signal"] = "neutral"

            # Re-score with smart money signals
            cand["score"] = score_candidate(
                cand, cand["metrics"], active_themes, macro_data,
                options_signal=cand["options_signal"],
                insider_signal=cand["insider_signal"],
                institutional_signal=cand["institutional_signal"],
            )
            time.sleep(0.3)

    except ImportError as exc:
        log.warning("  Smart money modules unavailable: %s", exc)

    # Re-sort after smart money scoring
    candidates.sort(key=lambda x: x["score"], reverse=True)

    # ── 5. Phase 3: Claude thesis for top prospects ────────────────────────
    prospects = generate_prospect_theses(
        candidates, list(exclude), active_themes, macro_data, client, n=n,
    )

    log.info("  Generated %d prospect theses", len(prospects))
    return prospects


# ════════════════════════════════════════════════════════════════════════════
#  STANDALONE
# ════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Langston's prospect finder")
    parser.add_argument("--top", type=int, default=5, help="Number of prospects (default 5)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    client = anthropic.Anthropic(api_key=get_anthropic_key())
    tickers = list(load_tickers().keys())

    # Minimal macro data for standalone run
    try:
        from morning_report import get_macro_data
        macro_data = get_macro_data()
    except ImportError:
        log.warning("morning_report not available — using empty macro data")
        macro_data = {}

    log.info("Current watchlist: %s", ", ".join(tickers))
    prospects = find_prospects(tickers, macro_data, client, n=args.top)

    print(f"\n{'='*70}")
    print(f"TOP {len(prospects)} WATCHLIST PROSPECTS — {datetime.now(timezone.utc).strftime('%B %d, %Y')}")
    print(f"{'='*70}")
    for p in prospects:
        print(f"\n#{p['rank']}  {p['ticker']:6s}  {p['company']}")
        print(f"  Theme:   {p['theme']}  |  Rating: {p['rating']}  |  Score: {p['score']:.1f}/10")
        print(f"  Price:   ${p['price']:.2f}  |  1M: {p['ret_1m']:+.1f}%  |  P/S: {p['ps_ratio']:.1f}x")
        print(f"  Signals: Options={p['options_signal']}  Insider={p['insider_signal']}  Inst={p['institutional_signal']}")
        print(f"  Thesis:  {p['thesis']}")
        print(f"  Action:  ▶  {p['action']}")
    print(f"\n{'='*70}")


if __name__ == "__main__":
    main()
