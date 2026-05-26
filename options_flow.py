"""
options_flow.py — Langston's Financial Intelligence
====================================================
Pulls unusual options activity for a given ticker using yfinance.

Usage
-----
    from options_flow import get_options_flow
    result = get_options_flow("AAPL")

    # Standalone CLI
    python options_flow.py AAPL

The function never raises; it always returns a fully-populated dict.
If yfinance fails or returns no usable data the dict is zeroed out and
the 'error' key contains the exception message.

Keys returned
-------------
    put_call_ratio  float          total_puts / total_calls (0.0 if no calls)
    total_calls     int
    total_puts      int
    unusual         list[dict]     top-5 contracts flagged by volume/OI >= 2.0
    largest_blocks  list[dict]     top-3 contracts by dollar notional
    flow_signal     str            "bullish" | "bearish" | "neutral"
    summary         str            1-2 sentence plain-English summary
    error           str | None     None on success

Each contract dict
------------------
    type            "call" | "put"
    strike          float
    expiry          str            YYYY-MM-DD
    volume          int
    open_interest   int
    last_price      float          premium per share
    notional        int            volume * last_price * 100
    vol_oi_ratio    float          volume / open_interest
    flag            str            human-readable label, e.g. "3.1x OI — $875K block"
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import yfinance as yf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_int(value, default: int = 0) -> int:
    """Convert a potentially NaN/None value to int."""
    try:
        if value is None:
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return int(f)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    """Convert a potentially NaN/None value to float."""
    try:
        if value is None:
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _format_notional(notional: int) -> str:
    """Return a human-readable dollar string: $1.2M, $875K, $42K."""
    if notional >= 1_000_000:
        return f"${notional / 1_000_000:.1f}M"
    if notional >= 1_000:
        return f"${notional / 1_000:.0f}K"
    return f"${notional}"


def _build_contract_dict(row, contract_type: str, expiry: str) -> dict:
    """
    Build a single contract dict from a pandas Series (one options row).
    Returns None if the row is missing essential data.
    """
    volume = _safe_int(row.get("volume"))
    open_interest = _safe_int(row.get("openInterest"))
    last_price = _safe_float(row.get("lastPrice"))
    strike = _safe_float(row.get("strike"))

    notional = volume * last_price * 100
    notional_int = int(notional)

    vol_oi_ratio = (volume / open_interest) if open_interest > 0 else 0.0

    flag = f"{vol_oi_ratio:.1f}x OI — {_format_notional(notional_int)} block"

    return {
        "type": contract_type,
        "strike": strike,
        "expiry": expiry,
        "volume": volume,
        "open_interest": open_interest,
        "last_price": last_price,
        "notional": notional_int,
        "vol_oi_ratio": round(vol_oi_ratio, 4),
        "flag": flag,
    }


def _empty_result(error_msg: str | None = None) -> dict:
    """Return a fully-zeroed result dict."""
    return {
        "put_call_ratio": 0.0,
        "total_calls": 0,
        "total_puts": 0,
        "unusual": [],
        "largest_blocks": [],
        "flow_signal": "neutral",
        "summary": "No options data available.",
        "error": error_msg,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_options_flow(ticker: str) -> dict:
    """
    Fetch and analyse unusual options activity for *ticker*.

    Parameters
    ----------
    ticker : str
        Stock ticker symbol (e.g. "AAPL", "TEM", "NVDA").

    Returns
    -------
    dict
        See module docstring for full key/value specification.
        Never raises — returns zeroed dict with 'error' key on failure.
    """
    ticker = ticker.strip().upper()

    try:
        tk = yf.Ticker(ticker)

        # ------------------------------------------------------------------ #
        # 1. Collect expirations within the next 60 days (max 4)             #
        # ------------------------------------------------------------------ #
        try:
            all_expirations = tk.options  # tuple of "YYYY-MM-DD" strings
        except Exception as exc:
            return _empty_result(f"Could not retrieve options expirations: {exc}")

        if not all_expirations:
            return _empty_result("No options expirations found for this ticker.")

        now = datetime.now(tz=timezone.utc).date()
        cutoff = now + timedelta(days=60)

        near_expirations: list[str] = []
        for exp_str in all_expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if now <= exp_date <= cutoff:
                near_expirations.append(exp_str)
            if len(near_expirations) >= 4:
                break

        if not near_expirations:
            # Fall back to the first available expiration even if > 60 days out
            near_expirations = list(all_expirations[:1])

        # ------------------------------------------------------------------ #
        # 2. Fetch option chains and accumulate contracts                     #
        # ------------------------------------------------------------------ #
        all_calls: list[dict] = []
        all_puts: list[dict] = []

        for exp in near_expirations:
            try:
                chain = tk.option_chain(exp)
            except Exception:
                # Skip this expiration silently; try next
                continue

            calls_df = chain.calls
            puts_df = chain.puts

            for _, row in calls_df.iterrows():
                contract = _build_contract_dict(row, "call", exp)
                if contract is not None:
                    all_calls.append(contract)

            for _, row in puts_df.iterrows():
                contract = _build_contract_dict(row, "put", exp)
                if contract is not None:
                    all_puts.append(contract)

        if not all_calls and not all_puts:
            return _empty_result("Option chains returned no usable rows.")

        # ------------------------------------------------------------------ #
        # 3. Aggregate totals                                                 #
        # ------------------------------------------------------------------ #
        total_calls = sum(c["volume"] for c in all_calls)
        total_puts = sum(p["volume"] for p in all_puts)

        put_call_ratio = (total_puts / total_calls) if total_calls > 0 else 0.0
        put_call_ratio = round(put_call_ratio, 4)

        # ------------------------------------------------------------------ #
        # 4. Unusual activity: vol/OI >= 2.0, volume >= 50, OI > 0           #
        # ------------------------------------------------------------------ #
        unusual_candidates: list[dict] = []
        for contract in all_calls + all_puts:
            vol = contract["volume"]
            oi = contract["open_interest"]
            if vol >= 50 and oi > 0 and (vol / oi) >= 2.0:
                unusual_candidates.append(contract)

        unusual_candidates.sort(key=lambda c: c["notional"], reverse=True)
        unusual = unusual_candidates[:5]

        # ------------------------------------------------------------------ #
        # 5. Largest blocks: volume >= 10 and last_price > 0                 #
        # ------------------------------------------------------------------ #
        block_candidates: list[dict] = []
        for contract in all_calls + all_puts:
            if contract["volume"] >= 10 and contract["last_price"] > 0:
                block_candidates.append(contract)

        block_candidates.sort(key=lambda c: c["notional"], reverse=True)
        largest_blocks = block_candidates[:3]

        # ------------------------------------------------------------------ #
        # 6. Flow signal                                                      #
        # ------------------------------------------------------------------ #
        if put_call_ratio < 0.6:
            flow_signal = "bullish"
        elif put_call_ratio > 1.4:
            flow_signal = "bearish"
        else:
            flow_signal = "neutral"

        # Override if the largest unusual block is substantial (> $500K)
        if unusual:
            top_unusual = unusual[0]
            if top_unusual["notional"] > 500_000:
                flow_signal = "bullish" if top_unusual["type"] == "call" else "bearish"

        # ------------------------------------------------------------------ #
        # 7. Plain-English summary                                            #
        # ------------------------------------------------------------------ #
        pc_str = f"{put_call_ratio:.2f}"
        signal_word = flow_signal.capitalize()

        if unusual:
            top = unusual[0]
            notional_str = _format_notional(top["notional"])
            summary = (
                f"{signal_word} flow: P/C ratio {pc_str} with unusual "
                f"{top['type']} block at ${top['strike']:.0f} strike "
                f"({top['vol_oi_ratio']:.1f}x OI, {notional_str} notional). "
            )
        else:
            summary = (
                f"{signal_word} flow: P/C ratio {pc_str} with no strongly "
                "unusual contracts detected. "
            )

        if flow_signal == "bullish":
            summary += "Institutional positioning appears long-oriented."
        elif flow_signal == "bearish":
            summary += "Institutional positioning appears defensive or short-oriented."
        else:
            summary += "Options positioning is mixed with no clear directional bias."

        return {
            "put_call_ratio": put_call_ratio,
            "total_calls": total_calls,
            "total_puts": total_puts,
            "unusual": unusual,
            "largest_blocks": largest_blocks,
            "flow_signal": flow_signal,
            "summary": summary,
            "error": None,
        }

    except Exception as exc:
        return _empty_result(str(exc))


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import pprint
    import sys

    tk = sys.argv[1] if len(sys.argv) > 1 else "TEM"
    result = get_options_flow(tk)
    pprint.pprint(result)
