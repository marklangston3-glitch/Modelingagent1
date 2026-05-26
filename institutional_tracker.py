"""
institutional_tracker.py
Langston's Financial Intelligence — Institutional Ownership Module

Pulls institutional ownership data for a given ticker using yfinance + stdlib only.
"""

from __future__ import annotations

import yfinance as yf

# Well-known institutional names that signal high-conviction backing
_HIGH_CONVICTION_NAMES = ("Vanguard", "BlackRock", "Fidelity", "State Street", "ARK", "Renaissance")


def _normalise_pct(value: float) -> float:
    """Ensure a percentage is expressed in the 0-100 range."""
    if value is None:
        return 0.0
    # yfinance sometimes returns 0-1 float, sometimes 0-100
    if abs(value) <= 1.0:
        return float(value) * 100.0
    return float(value)


def _find_col(df, *candidates: str):
    """Return the first column name from *candidates* found in *df.columns*, else None."""
    lower_cols = {str(c).lower(): c for c in df.columns}
    for cand in candidates:
        if cand in df.columns:
            return cand
        if cand.lower() in lower_cols:
            return lower_cols[cand.lower()]
    return None


def _parse_major_holders(major) -> tuple[float, float, int]:
    """
    Extract pct_insiders, pct_institutional, holder_count from the
    major_holders DataFrame.  Layout varies across yfinance versions, so
    we try several strategies before giving up.

    Returns (pct_insiders, pct_institutional, holder_count).
    """
    pct_insiders = 0.0
    pct_institutional = 0.0
    holder_count = 0

    if major is None:
        return pct_insiders, pct_institutional, holder_count

    def _to_float(v) -> float:
        try:
            return float(str(v).replace("%", "").replace(",", "").strip())
        except (ValueError, TypeError):
            return 0.0

    try:
        # ----------------------------------------------------------------
        # Strategy A: index-labelled single-value-column DataFrame
        # (current yfinance: index = ["insidersPercentHeld", "institutionsPercentHeld",
        #  "institutionsFloatPercentHeld", "institutionsCount"])
        # ----------------------------------------------------------------
        idx_lower = {str(i).lower(): i for i in major.index}
        val_col = major.columns[0]  # first (and usually only) column

        for key, idx_key in idx_lower.items():
            raw = _to_float(major.loc[idx_key, val_col])
            if "insiderspercent" in key or ("insider" in key and "percent" in key):
                pct_insiders = _normalise_pct(raw)
            elif "institutionspercent" in key and "float" not in key:
                pct_institutional = _normalise_pct(raw)
            elif "institutionscount" in key or ("institution" in key and "count" in key):
                holder_count = int(raw)

        if pct_institutional > 0 or holder_count > 0:
            return pct_insiders, pct_institutional, holder_count

        # ----------------------------------------------------------------
        # Strategy B: two-column DataFrame [Value, Breakdown] (older yfinance)
        # ----------------------------------------------------------------
        if len(major.columns) >= 2:
            val_col = major.columns[0]
            lbl_col = major.columns[1]
            for _, row in major.iterrows():
                label = str(row[lbl_col]).lower()
                num = _to_float(row[val_col])
                if "insider" in label and "number" not in label:
                    pct_insiders = _normalise_pct(num)
                elif "institution" in label and "number" not in label:
                    pct_institutional = _normalise_pct(num)
                elif "number" in label and "institution" in label:
                    holder_count = int(num)
            return pct_insiders, pct_institutional, holder_count

        # ----------------------------------------------------------------
        # Strategy C: iterate rows using index as label
        # ----------------------------------------------------------------
        for idx, row in major.iterrows():
            label = str(idx).lower()
            num = _to_float(row.iloc[0])
            if "insider" in label and "number" not in label and "percent" in label:
                pct_insiders = _normalise_pct(num)
            elif "institution" in label and "number" not in label and "percent" in label and "float" not in label:
                pct_institutional = _normalise_pct(num)
            elif ("number" in label or "count" in label) and "institution" in label:
                holder_count = int(num)

    except Exception:
        pass

    return pct_insiders, pct_institutional, holder_count


def _build_smart_money_signal(
    pct_institutional: float,
    holder_count: int,
    top_holders: list[dict],
) -> tuple[str, str]:
    """
    Return (signal, extra_tag) where extra_tag may be an empty string or
    "high-conviction institutional backing".
    """
    # Primary signal
    if pct_institutional >= 50 and holder_count >= 100:
        signal = "bullish"
    elif pct_institutional < 5:
        signal = "bearish"
    else:
        signal = "neutral"

    # Additional nuance
    extra_tag = ""
    if top_holders:
        top = top_holders[0]
        top_name = top.get("name", "")
        top_pct_out = top.get("pct_out", 0.0)
        if top_pct_out >= 10 and any(inst in top_name for inst in _HIGH_CONVICTION_NAMES):
            extra_tag = "high-conviction institutional backing"

    return signal, extra_tag


def _build_summary(
    pct_institutional: float,
    holder_count: int,
    top_holders: list[dict],
    signal: str,
    extra_tag: str,
) -> str:
    """Generate a concise 1-2 sentence summary."""
    pct_str = f"{pct_institutional:.1f}%"

    if top_holders:
        top_name = top_holders[0]["name"]
        top_pct = top_holders[0]["pct_out"]
        top_pct_str = f"{top_pct:.1f}%"

        # Check if top holder is a well-known name
        is_known = any(inst in top_name for inst in _HIGH_CONVICTION_NAMES)

        if signal == "bullish":
            if is_known and extra_tag:
                return (
                    f"{top_name} holds {top_pct_str} with {pct_str} total institutional "
                    f"ownership — strong smart-money backing. "
                    f"{holder_count} institutions currently hold positions."
                )
            return (
                f"{pct_str} institutional ownership across {holder_count} holders "
                f"signals strong smart-money interest. "
                f"{top_name} is the largest holder at {top_pct_str}."
            )

        if signal == "bearish":
            return (
                f"Low institutional ownership ({pct_str}) — retail-dominated float "
                f"with high speculative risk. "
                f"Only {holder_count} institutional holders on record."
            )

        # neutral
        if is_known:
            return (
                f"{pct_str} institutional ownership; {top_name} has been a significant "
                f"accumulator at {top_pct_str} of shares outstanding."
            )
        return (
            f"Moderate institutional ownership of {pct_str} across {holder_count} holders. "
            f"{top_name} leads with {top_pct_str} of shares outstanding."
        )

    # No top-holder data
    if signal == "bullish":
        return (
            f"{pct_str} institutional ownership across {holder_count} holders "
            f"signals strong smart-money interest."
        )
    if signal == "bearish":
        return (
            f"Low institutional ownership ({pct_str}) — retail-dominated float "
            f"with high speculative risk."
        )
    return f"Moderate institutional ownership of {pct_str} across {holder_count} holders."


def get_institutional_ownership(ticker: str) -> dict:
    """
    Pull institutional ownership data for *ticker* using yfinance.

    Returns a dict with keys:
        top_holders         list[dict]
        pct_institutional   float   (0-100)
        pct_insiders        float   (0-100)
        holder_count        int
        smart_money_signal  str     "bullish" | "bearish" | "neutral"
        summary             str
        error               str | None
    """
    result: dict = {
        "top_holders": [],
        "pct_institutional": 0.0,
        "pct_insiders": 0.0,
        "holder_count": 0,
        "smart_money_signal": "neutral",
        "summary": "",
        "error": None,
    }

    try:
        yt = yf.Ticker(ticker)

        # ------------------------------------------------------------------
        # Step 1 — institutional_holders
        # ------------------------------------------------------------------
        inst_df = yt.institutional_holders
        top_holders: list[dict] = []

        if inst_df is not None and not inst_df.empty:
            # Locate relevant columns robustly across yfinance versions
            # Current version:  Date Reported | Holder | pctHeld | Shares | Value | pctChange
            # Older versions:   Holder | Shares | Date Reported | % Out | Value
            holder_col   = _find_col(inst_df, "Holder", "holder")
            shares_col   = _find_col(inst_df, "Shares", "shares")
            value_col    = _find_col(inst_df, "Value", "value")
            pct_col      = _find_col(inst_df, "pctHeld", "% Out", "% out", "pct_out", "pctout")
            date_col     = _find_col(inst_df, "Date Reported", "date reported", "dateReported", "Date")

            # Sort by value descending (in case not already sorted) and take top 5
            if value_col and value_col in inst_df.columns:
                inst_df = inst_df.sort_values(value_col, ascending=False)
            top5 = inst_df.head(5)

            for _, row in top5.iterrows():
                # Holder name
                name = str(row[holder_col]) if holder_col else "Unknown"

                # Shares
                try:
                    shares = int(row[shares_col]) if shares_col else 0
                except (ValueError, TypeError):
                    shares = 0

                # Value in dollars
                try:
                    value = float(row[value_col]) if value_col else 0.0
                except (ValueError, TypeError):
                    value = 0.0

                # % Out — normalise to 0-100
                try:
                    raw_pct = float(row[pct_col]) if pct_col else 0.0
                    pct_out = _normalise_pct(raw_pct)
                except (ValueError, TypeError):
                    pct_out = 0.0

                # Date Reported — convert Timestamp → ISO string YYYY-MM-DD
                if date_col:
                    raw_date = row[date_col]
                    try:
                        reported_date = str(raw_date)[:10]
                    except Exception:
                        reported_date = ""
                else:
                    reported_date = ""

                top_holders.append(
                    {
                        "name": name,
                        "shares": shares,
                        "value": value,
                        "pct_out": round(pct_out, 4),
                        "reported_date": reported_date,
                    }
                )

        result["top_holders"] = top_holders

        # ------------------------------------------------------------------
        # Step 2 — major_holders
        # ------------------------------------------------------------------
        major = yt.major_holders
        pct_insiders, pct_institutional, holder_count = _parse_major_holders(major)

        result["pct_insiders"] = round(pct_insiders, 4)
        result["pct_institutional"] = round(pct_institutional, 4)
        result["holder_count"] = holder_count

        # ------------------------------------------------------------------
        # Step 3 — Smart money signal
        # ------------------------------------------------------------------
        signal, extra_tag = _build_smart_money_signal(pct_institutional, holder_count, top_holders)
        result["smart_money_signal"] = signal

        # ------------------------------------------------------------------
        # Step 4 — Summary
        # ------------------------------------------------------------------
        result["summary"] = _build_summary(
            pct_institutional, holder_count, top_holders, signal, extra_tag
        )

    except Exception as exc:
        result["error"] = str(exc)
        result["summary"] = f"Unable to retrieve institutional data: {exc}"

    return result


if __name__ == "__main__":
    import sys
    import pprint

    tk = sys.argv[1] if len(sys.argv) > 1 else "TEM"
    pprint.pprint(get_institutional_ownership(tk))
