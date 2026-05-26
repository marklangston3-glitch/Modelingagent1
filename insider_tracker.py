"""
insider_tracker.py — SEC EDGAR Form 4 insider transaction tracker.

Part of Langston's Financial Intelligence system.

Public API:
    get_insider_activity(ticker, cik="", days_back=30) -> dict

Uses only stdlib: urllib.request, xml.etree.ElementTree, json, datetime.
"""

import json
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ── Constants ─────────────────────────────────────────────────────────────────
_USER_AGENT = "Langstons/1.0 marklangston3@gmail.com"
_TIMEOUT    = 15  # seconds per HTTP request

_BASE_SUBMISSIONS = "https://data.sec.gov/submissions"
_BASE_ARCHIVES    = "https://www.sec.gov/Archives/edgar/data"
_BASE_SEARCH      = "https://efts.sec.gov/LATEST/search-index"

_CODE_MAP = {
    "P": "buy",
    "S": "sell",
    "M": "exercise",
}
_SKIP_CODES = {"A"}  # awards — we keep "D" (disposition code in transactionCode)
# Note: "D" in transactionAcquiredDisposedCode means "disposed of", which is
# normal for sells; skip only the transactionCode "A" (grants/awards).

# XSLT stylesheet subdirectories that wrap the raw XML in some filings
_XSLT_DIRS = {"xslF345X06", "xslF345X05", "xslF345X04", "xslF345X03"}

_SIGNIFICANT_VALUE = 100_000
_CLUSTER_SELL_MIN  = 3
_LARGE_NET_SELL    = 50_000


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(url: str) -> bytes:
    """Fetch *url* with required headers; return raw bytes."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":       _USER_AGENT,
            "Accept-Encoding":  "identity",
            "Accept":           "application/json, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read()


def _get_json(url: str) -> dict:
    return json.loads(_http_get(url).decode("utf-8"))


def _get_xml(url: str) -> ET.Element:
    data = _http_get(url)
    return ET.fromstring(data)


# ── CIK helpers ───────────────────────────────────────────────────────────────

def _pad_cik(cik: str) -> str:
    """Return zero-padded 10-digit CIK string."""
    return str(int(cik)).zfill(10)


def _numeric_cik(cik: str) -> str:
    """Return CIK without leading zeros (for URL paths)."""
    return str(int(cik))


# ── Step 1: Collect Form 4 filing references ──────────────────────────────────

def _raw_doc_name(primary_document: str) -> str:
    """
    Strip any XSLT subdirectory prefix from a primaryDocument path.

    EDGAR sometimes stores the styled Form 4 viewer under a subdirectory like
    ``xslF345X06/form4-....xml``.  The raw, parseable XML lives at the same
    filename one level up (i.e. without the subdirectory).
    """
    parts = primary_document.split("/")
    if len(parts) >= 2 and parts[0] in _XSLT_DIRS:
        return parts[-1]
    return primary_document


def _filings_via_submissions(cik: str, start_date: str) -> list[dict]:
    """
    Return Form 4 filings on or after *start_date* using the EDGAR submissions API.

    Each item: {"accessionNumber": str, "filingDate": str, "primaryDocument": str, "cik": str}
    """
    cik_padded = _pad_cik(cik)
    url = f"{_BASE_SUBMISSIONS}/CIK{cik_padded}.json"
    try:
        data = _get_json(url)
    except Exception as exc:
        print(f"[insider_tracker] submissions fetch failed ({url}): {exc}")
        return []

    recent = data.get("filings", {}).get("recent", {})
    forms       = recent.get("form", [])
    acc_numbers = recent.get("accessionNumber", [])
    dates       = recent.get("filingDate", [])
    docs        = recent.get("primaryDocument", [])

    results = []
    for form, acc, date, doc in zip(forms, acc_numbers, dates, docs):
        if form == "4" and date >= start_date:
            results.append({
                "accessionNumber": acc,
                "filingDate":      date,
                "primaryDocument": _raw_doc_name(doc),
                "cik":             _numeric_cik(cik),
            })
    return results


def _filings_via_search(ticker: str, start_date: str) -> list[dict]:
    """
    Fallback: locate Form 4 filings via EDGAR full-text search when CIK is unknown.

    Each item: {"accessionNumber": str, "filingDate": str, "primaryDocument": str, "cik": str}
    """
    url = (
        f"{_BASE_SEARCH}?forms=4"
        f"&dateRange=custom&startdt={start_date}"
        f'&q=%22{ticker}%22'
    )
    try:
        data = _get_json(url)
    except Exception as exc:
        print(f"[insider_tracker] EFTS search failed ({url}): {exc}")
        return []

    hits = data.get("hits", {}).get("hits", [])
    results = []
    for hit in hits:
        src = hit.get("_source", {})
        acc = src.get("file_num") or src.get("accession_no", "")
        # EFTS returns accession numbers with dashes already
        filing_date = src.get("period_of_report") or src.get("file_date", "")
        entity_id   = src.get("entity_id") or src.get("ciks", [""])[0] if src.get("ciks") else ""
        doc         = src.get("file_name", "")
        if acc and filing_date >= start_date:
            results.append({
                "accessionNumber": acc,
                "filingDate":      filing_date,
                "primaryDocument": doc,
                "cik":             str(entity_id),
            })
    return results


# ── Step 2: Parse a single Form 4 XML ─────────────────────────────────────────

def _text(root: ET.Element, xpath: str) -> str:
    el = root.find(xpath)
    return (el.text or "").strip() if el is not None else ""


def _parse_form4_xml(xml_root: ET.Element, filing_date: str) -> list[dict]:
    """
    Extract individual transactions from a parsed Form 4 XML tree.

    Returns a list of transaction dicts (may be empty).
    """
    # Owner identity
    owner_name = _text(xml_root, ".//reportingOwner/reportingOwnerId/rptOwnerName")
    if not owner_name:
        owner_name = "Unknown"

    # Title / role
    officer_title = _text(xml_root, ".//reportingOwner/reportingOwnerRelationship/officerTitle")
    is_officer    = _text(xml_root, ".//reportingOwner/reportingOwnerRelationship/isOfficer")
    is_director   = _text(xml_root, ".//reportingOwner/reportingOwnerRelationship/isDirector")
    is_10pct      = _text(xml_root, ".//reportingOwner/reportingOwnerRelationship/isTenPercentOwner")

    if officer_title:
        title = officer_title
    elif is_director == "1":
        title = "Director"
    elif is_10pct == "1":
        title = "10% Owner"
    elif is_officer == "1":
        title = "Officer"
    else:
        title = "Insider"

    transactions = []

    # Iterate over every nonDerivativeTransaction element
    for txn in xml_root.findall(".//nonDerivativeTable/nonDerivativeTransaction"):
        # transactionCode lives under transactionCoding in all known schema versions
        code_el = txn.find(".//transactionCoding/transactionCode")
        if code_el is None:
            code_el = txn.find(".//transactionCode")
        code = (code_el.text or "").strip() if code_el is not None else ""

        if code in _SKIP_CODES or not code:
            continue

        txn_type = _CODE_MAP.get(code, "other")

        shares_el = txn.find(".//transactionAmounts/transactionShares/value")
        price_el  = txn.find(".//transactionAmounts/transactionPricePerShare/value")
        date_el   = txn.find(".//transactionDate/value")

        try:
            shares = int(float((shares_el.text or "0").strip())) if shares_el is not None else 0
        except ValueError:
            shares = 0

        try:
            price = float((price_el.text or "0").strip()) if price_el is not None else 0.0
        except ValueError:
            price = 0.0

        txn_date = (date_el.text or filing_date).strip() if date_el is not None else filing_date

        value = round(shares * price, 2)

        transactions.append({
            "date":        txn_date,
            "name":        owner_name,
            "title":       title,
            "type":        txn_type,
            "shares":      shares,
            "price":       price,
            "value":       value,
            "significant": value > _SIGNIFICANT_VALUE,
        })

    return transactions


# ── Step 3: Compute signals ────────────────────────────────────────────────────

def _compute_signals(transactions: list[dict]) -> tuple[int, int, bool, str]:
    """
    Return (net_shares, significant_buys, cluster_selling, net_signal).
    """
    if not transactions:
        return 0, 0, False, "neutral"

    buy_shares  = sum(t["shares"] for t in transactions if t["type"] == "buy")
    sell_shares = sum(t["shares"] for t in transactions if t["type"] == "sell")
    net_shares  = buy_shares - sell_shares

    significant_buys = sum(
        1 for t in transactions if t["type"] == "buy" and t["value"] > _SIGNIFICANT_VALUE
    )

    sellers = {t["name"] for t in transactions if t["type"] == "sell"}
    cluster_selling = len(sellers) >= _CLUSTER_SELL_MIN

    if net_shares > 0 and significant_buys >= 1:
        net_signal = "bullish"
    elif cluster_selling or (net_shares < 0 and abs(net_shares) > _LARGE_NET_SELL):
        net_signal = "bearish"
    else:
        net_signal = "neutral"

    return net_shares, significant_buys, cluster_selling, net_signal


# ── Step 4: Generate summary ───────────────────────────────────────────────────

def _make_summary(
    transactions: list[dict],
    net_shares:   int,
    significant_buys: int,
    cluster_selling:  bool,
    net_signal:   str,
    days_back:    int,
) -> str:
    if not transactions:
        return f"No insider transactions in the past {days_back} days."

    # Find largest buy by value for the lead sentence
    buys = [t for t in transactions if t["type"] == "buy"]
    sells = [t for t in transactions if t["type"] == "sell"]

    parts = []

    if buys:
        top_buy = max(buys, key=lambda t: t["value"])
        val_str = f"${top_buy['value']:,.0f}" if top_buy["value"] > 0 else "N/A"
        price_str = f"${top_buy['price']:.2f}" if top_buy["price"] > 0 else "N/A"
        parts.append(
            f"{top_buy['title']} {top_buy['name']} purchased {top_buy['shares']:,} shares"
            f" at {price_str} ({val_str})."
        )

    if cluster_selling and sells:
        total_sell_value = sum(t["value"] for t in sells)
        val_m = total_sell_value / 1_000_000
        parts.append(
            f"{len({t['name'] for t in sells})} insiders sold shares totaling"
            f" ${val_m:.1f}M — cluster selling is a risk flag."
        )
    elif sells and not buys:
        total_sell_value = sum(t["value"] for t in sells)
        val_str = f"${total_sell_value:,.0f}"
        parts.append(
            f"Net insider selling of {abs(net_shares):,} shares totaling {val_str}."
        )

    if net_signal == "bullish" and buys:
        parts.append("Net insider buying is a bullish signal.")
    elif net_signal == "bearish" and not cluster_selling:
        parts.append("Net insider selling exceeds the bearish threshold.")
    elif net_signal == "neutral":
        parts.append("Insider activity is mixed or inconclusive.")

    return " ".join(parts) if parts else f"Insider activity recorded for the past {days_back} days."


# ── Public API ─────────────────────────────────────────────────────────────────

def get_insider_activity(ticker: str, cik: str = "", days_back: int = 30) -> dict:
    """
    Fetch and analyse SEC Form 4 insider transactions for *ticker*.

    Parameters
    ----------
    ticker   : stock ticker symbol (e.g. "TEM")
    cik      : SEC CIK number (numeric string, with or without leading zeros).
               If empty the function falls back to an EDGAR full-text search.
    days_back: how many calendar days back to look for filings (default 30)

    Returns
    -------
    dict with keys: transactions, net_shares, significant_buys,
                    cluster_selling, net_signal, summary, error
    """
    _empty = {
        "transactions":    [],
        "net_shares":      0,
        "significant_buys": 0,
        "cluster_selling": False,
        "net_signal":      "neutral",
        "summary":         f"No insider transactions in the past {days_back} days.",
        "error":           None,
    }

    try:
        today      = datetime.now(tz=timezone.utc).date()
        start_date = (today - timedelta(days=days_back)).isoformat()

        # ── Step 1: Collect filing references ────────────────────────────────
        if cik:
            filing_refs = _filings_via_submissions(cik, start_date)
        else:
            filing_refs = _filings_via_search(ticker, start_date)

        if not filing_refs:
            return {**_empty, "summary": f"No Form 4 filings found for {ticker} in the past {days_back} days."}

        # Cap at 10 filings to avoid excessive requests
        filing_refs = filing_refs[:10]

        # ── Step 2: Parse each Form 4 XML ────────────────────────────────────
        all_transactions: list[dict] = []

        for ref in filing_refs:
            acc    = ref["accessionNumber"]
            doc    = ref["primaryDocument"]
            f_cik  = ref["cik"] or (cik and _numeric_cik(cik)) or ""
            f_date = ref["filingDate"]

            if not (f_cik and acc and doc):
                continue

            acc_no_dashes = acc.replace("-", "")
            xml_url = f"{_BASE_ARCHIVES}/{f_cik}/{acc_no_dashes}/{doc}"

            try:
                root = _get_xml(xml_url)
                txns = _parse_form4_xml(root, f_date)
                all_transactions.extend(txns)
            except Exception as exc:
                print(f"[insider_tracker] XML parse error for {xml_url}: {exc}")
                continue

        # Sort chronologically (most recent first)
        all_transactions.sort(key=lambda t: t["date"], reverse=True)

        # ── Step 3: Compute signals ───────────────────────────────────────────
        net_shares, significant_buys, cluster_selling, net_signal = _compute_signals(
            all_transactions
        )

        # ── Step 4: Summary ───────────────────────────────────────────────────
        summary = _make_summary(
            all_transactions,
            net_shares,
            significant_buys,
            cluster_selling,
            net_signal,
            days_back,
        )

        return {
            "transactions":     all_transactions,
            "net_shares":       net_shares,
            "significant_buys": significant_buys,
            "cluster_selling":  cluster_selling,
            "net_signal":       net_signal,
            "summary":          summary,
            "error":            None,
        }

    except Exception as exc:
        msg = f"get_insider_activity failed for {ticker}: {exc}"
        print(f"[insider_tracker] ERROR: {msg}")
        return {**_empty, "error": msg}


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    import pprint

    tk = sys.argv[1] if len(sys.argv) > 1 else "TEM"
    from config import _KNOWN_CIKS
    cik = _KNOWN_CIKS.get(tk, "")
    pprint.pprint(get_insider_activity(tk, cik))
