#!/usr/bin/env python3
"""
watchdog.py — Multi-ticker EDGAR filing watchdog

Runs daily (via cron / GitHub Actions).  For each watched ticker it:
  1. Checks the SEC EDGAR RSS feed for new 10-K / 10-Q filings
  2. When a new filing is detected, pulls fresh XBRL company facts
  3. Rebuilds the DCF workbook using build_dcf.py
  4. Commits & pushes the updated workbook(s) to the repo
  5. Appends structured log entries to watchdog.log

Watched tickers
---------------
  TEM   Tempus AI          CIK 0001717115
  RGTI  Rigetti Computing  CIK 0001838359
  BBAI  BigBear.ai         CIK 0001836981

Usage
-----
  python watchdog.py             # normal run (all tickers)
  python watchdog.py --force     # skip cache check, rebuild all unconditionally
  python watchdog.py --ticker TEM          # single ticker
  python watchdog.py --ticker TEM --force  # force single ticker
"""

import argparse
import json
import logging
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config (centralized) ─────────────────────────────────────────────────────
from config import (
    REPO_DIR, BRANCH, GIT_REMOTE,
    load_tickers,
)

# Build TICKERS dict in the format watchdog expects: {TICKER: {cik, name}}
_TICKER_CFG = load_tickers()  # {TICKER: {name, exch, cik}}
TICKERS = {
    tk: {"cik": v["cik"], "name": v["name"]}
    for tk, v in _TICKER_CFG.items()
}

WATCH_FORMS   = {"10-K", "10-Q"}
STATE_FILE    = REPO_DIR / ".watchdog_state.json"
LOG_FILE      = REPO_DIR / "watchdog.log"
BUILD_SCRIPT  = REPO_DIR / "build_dcf.py"
REBUILD_SUMMARY = REPO_DIR / ".rebuild_summary.json"

EDGAR_HEADERS = {"User-Agent": "ModelingAgent watchdog@example.com"}


# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("watchdog")


# ── State helpers ─────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def ticker_state(state: dict, ticker: str) -> dict:
    """Return (or create) the per-ticker sub-dict."""
    if ticker not in state:
        state[ticker] = {
            "seen_accessions": [],
            "last_run": None,
            "last_rebuild": None,
            "last_snapshot": {},
        }
    return state[ticker]


# ── EDGAR helpers ─────────────────────────────────────────────────────────────
def _get(url: str) -> requests.Response:
    for attempt in range(4):
        try:
            r = requests.get(url, headers=EDGAR_HEADERS, timeout=30)
            r.raise_for_status()
            time.sleep(0.15)   # SEC rate-limit courtesy
            return r
        except requests.RequestException as exc:
            wait = 2 ** attempt
            log.warning("Request failed (%s), retrying in %ss …", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after 4 attempts")


def rss_url(cik: str, form_type: str) -> str:
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type={form_type}&dateb=&owner=include&count=10&output=atom"
    )


def facts_url(cik: str) -> str:
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def fetch_rss_filings(cik: str, form_type: str) -> list[dict]:
    """Return a list of {accession, date, form, title, url} from the RSS feed."""
    url = rss_url(cik, form_type)
    r   = _get(url)
    ns  = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as exc:
        log.error("RSS parse error for %s %s: %s", cik, form_type, exc)
        return []

    filings = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        upd   = (entry.findtext("atom:updated", "", ns) or "").strip()
        link  = entry.find("atom:link", ns)
        href  = link.get("href", "") if link is not None else ""
        acc = ""
        if "/Archives/edgar/data/" in href:
            for p in href.rstrip("/").split("/"):
                if len(p) == 18 and p.replace("-", "").isdigit():
                    acc = p
                    break
        filings.append({
            "form":      form_type,
            "accession": acc,
            "date":      upd[:10],
            "title":     title,
            "url":       href,
        })
    return filings


def fetch_xbrl_facts(cik: str) -> dict:
    """Return the full companyfacts JSON for the given CIK."""
    log.info("  Fetching XBRL company facts …")
    return _get(facts_url(cik)).json()


def extract_annual_value(facts: dict, concept: str, taxonomy: str = "us-gaap") -> dict[str, int]:
    """Return {end_date: value} for 10-K / 10-K/A entries of a concept."""
    data = facts.get("facts", {}).get(taxonomy, {}).get(concept, {})
    out  = {}
    for entries in data.get("units", {}).values():
        for e in entries:
            if e.get("form") in ("10-K", "10-K/A") and e.get("end"):
                out[e["end"]] = e["val"]
    return dict(sorted(out.items()))


def snapshot_key_metrics(facts: dict) -> dict:
    """Snapshot the XBRL concepts used by build_dcf.py."""
    concepts = {
        "revenue":     ("RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap"),
        "sga":         ("GeneralAndAdministrativeExpense",                     "us-gaap"),
        "rd":          ("ResearchAndDevelopmentExpense",                        "us-gaap"),
        "ebit":        ("OperatingIncomeLoss",                                  "us-gaap"),
        "da":          ("DepreciationDepletionAndAmortization",                 "us-gaap"),
        "sbc":         ("AllocatedShareBasedCompensationExpense",               "us-gaap"),
        "capex":       ("PaymentsToAcquirePropertyPlantAndEquipment",           "us-gaap"),
        "capsw":       ("CapitalizedComputerSoftwareAdditions",                 "us-gaap"),
        "int_exp":     ("InterestExpenseDebt",                                  "us-gaap"),
        "int_inc":     ("InvestmentIncomeInterest",                             "us-gaap"),
        "net_loss":    ("NetIncomeLoss",                                        "us-gaap"),
        "cash":        ("CashAndCashEquivalentsAtCarryingValue",                "us-gaap"),
        "conv_debt":   ("ConvertibleDebtNoncurrent",                            "us-gaap"),
        "shares":      ("WeightedAverageNumberOfDilutedSharesOutstanding",      "us-gaap"),
        "goodwill":    ("Goodwill",                                             "us-gaap"),
        "intangibles": ("IntangibleAssetsNetExcludingGoodwill",                 "us-gaap"),
        "ar":          ("AccountsReceivableNetCurrent",                         "us-gaap"),
        "dta":         ("DeferredTaxAssetsGross",                               "us-gaap"),
    }
    snap = {}
    for key, (concept, tax) in concepts.items():
        snap[key] = extract_annual_value(facts, concept, tax)
    return snap


def diff_snapshots(old: dict, new: dict) -> list[str]:
    """Return human-readable lines describing what changed."""
    changes = []
    all_keys = set(old) | set(new)
    for key in sorted(all_keys):
        old_vals = old.get(key, {})
        new_vals = new.get(key, {})
        all_dates = set(old_vals) | set(new_vals)
        for dt in sorted(all_dates):
            ov = old_vals.get(dt)
            nv = new_vals.get(dt)
            if ov != nv:
                if ov is None:
                    changes.append(f"  NEW  {key}[{dt}] = {nv:,}")
                elif nv is None:
                    changes.append(f"  DEL  {key}[{dt}] (was {ov:,})")
                else:
                    pct = (nv - ov) / abs(ov) * 100 if ov else float("inf")
                    changes.append(f"  CHG  {key}[{dt}]: {ov:,} → {nv:,}  ({pct:+.1f}%)")
    return changes


# ── Build & push ──────────────────────────────────────────────────────────────
def rebuild_xlsx(ticker: str) -> bool:
    """Re-run build_dcf.py for the given ticker.  Returns True on success."""
    log.info("  Running build_dcf.py --ticker %s …", ticker)
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--ticker", ticker],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("  build_dcf.py failed:\n%s", result.stderr)
        return False
    log.info("  build_dcf.py succeeded: %s", result.stdout.strip())
    return True


def git_commit_push(message: str, xlsx_files: list[str]) -> bool:
    """Stage updated workbooks + state + log, commit, and push."""
    stage = xlsx_files + ["watchdog.log", ".watchdog_state.json"]
    cmds = [
        ["git", "add"] + stage,
        ["git", "commit", "-m", message],
        ["git", "push", "-u", GIT_REMOTE, BRANCH],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            log.error("  git command failed: %s\n%s", " ".join(cmd), result.stderr)
            return False
        log.info("  $ %s  →  %s", " ".join(cmd), result.stdout.strip() or result.stderr.strip())
    return True


# ── Per-ticker watchdog logic ─────────────────────────────────────────────────
def check_ticker(ticker: str, cik: str, ts: dict, force: bool, now_utc: str) -> dict:
    """
    Poll EDGAR for one ticker, optionally rebuild, return updated ticker state.
    Returns a dict with keys: rebuilt, new_filings, changes, xlsx.
    """
    result = {"rebuilt": False, "new_filings": [], "changes": [], "xlsx": None}
    seen   = set(ts.get("seen_accessions", []))

    # ── 1. Poll RSS ──────────────────────────────────────────────────────────
    new_filings = []
    for form_type in WATCH_FORMS:
        filings = fetch_rss_filings(cik, form_type)
        log.info("  RSS %s %s: %d entries", ticker, form_type, len(filings))
        for f in filings:
            if f["accession"] and f["accession"] not in seen:
                new_filings.append(f)
                log.info(
                    "    NEW FILING: %s acc=%s date=%s  %s",
                    f["form"], f["accession"], f["date"], f["title"],
                )

    trigger = bool(new_filings) or force

    if not trigger:
        log.info("  %s — no new filings detected.", ticker)
        ts["last_run"] = now_utc
        return result

    # ── 2. Fetch XBRL facts + diff ───────────────────────────────────────────
    facts   = fetch_xbrl_facts(cik)
    new_snap = snapshot_key_metrics(facts)
    old_snap = ts.get("last_snapshot", {})
    changes  = diff_snapshots(old_snap, new_snap)
    if changes:
        log.info("  %s — %d financial data changes:", ticker, len(changes))
        for line in changes[:30]:
            log.info(line)
    else:
        log.info("  %s — XBRL snapshot unchanged.", ticker)

    # ── 3. Rebuild xlsx ──────────────────────────────────────────────────────
    xlsx_name = f"{ticker}_dcf.xlsx"
    ok = rebuild_xlsx(ticker)
    if not ok:
        log.error("  %s — rebuild failed; skipping push.", ticker)
        return result

    # ── 4. Update state ──────────────────────────────────────────────────────
    for f in new_filings:
        if f["accession"]:
            seen.add(f["accession"])
    ts["seen_accessions"] = sorted(seen)
    ts["last_run"]        = now_utc
    ts["last_rebuild"]    = now_utc
    ts["last_snapshot"]   = new_snap
    ts["last_filings"]    = new_filings

    result.update(rebuilt=True, new_filings=new_filings, changes=changes, xlsx=xlsx_name)
    return result


# ── Main orchestrator ─────────────────────────────────────────────────────────
def run(tickers_to_watch: list[str] | None = None, force: bool = False):
    now_utc = datetime.now(timezone.utc).isoformat()
    log.info("=" * 70)
    log.info("Watchdog run started at %s  (force=%s)", now_utc, force)

    watch_list = tickers_to_watch or list(TICKERS.keys())
    log.info("Watching: %s", ", ".join(watch_list))

    state = load_state()

    rebuilt_xlsx  = []
    all_filings   = []
    all_changes   = []

    for ticker in watch_list:
        if ticker not in TICKERS:
            log.warning("Unknown ticker %s — skipping.", ticker)
            continue
        cik = TICKERS[ticker]["cik"]
        log.info("─── %s (CIK %s) ───", ticker, cik)
        ts     = ticker_state(state, ticker)
        result = check_ticker(ticker, cik, ts, force, now_utc)

        if result["rebuilt"]:
            rebuilt_xlsx.append(result["xlsx"])
            all_filings.extend(result["new_filings"])
            all_changes.extend(result["changes"])

    # ── Commit & push all rebuilt files in one shot ──────────────────────────
    if rebuilt_xlsx:
        form_strs  = ", ".join(
            f"{f['form']} {f.get('ticker','?')} ({f['date']})"
            for f in all_filings
        ) or "forced rebuild"
        commit_msg = (
            f"Auto-update DCF models — new EDGAR filing(s): {form_strs}\n\n"
            f"Rebuilt: {', '.join(rebuilt_xlsx)}\n"
            f"Run: {now_utc}\n"
            + ("\n".join(all_changes[:40]) if all_changes else "No metric changes detected.")
            + "\n\nhttps://claude.ai/code/session_014hesikAtm8zzGNsXbYWmGV"
        )
        git_commit_push(commit_msg, rebuilt_xlsx)
    else:
        log.info("No rebuilds required this run.")
        # Still save state (updates last_run timestamps)
        save_state(state)
        # Write "not triggered" summary so email step is clearly skipped
        REBUILD_SUMMARY.write_text(json.dumps(
            {"triggered": False, "rebuilds": [], "run_at": now_utc, "filings": []},
            indent=2,
        ))
        return

    save_state(state)

    # Write rebuild summary so GitHub Actions can detect what triggered
    summary = {
        "triggered": True,
        "rebuilds":  [x.replace("_dcf.xlsx", "") for x in rebuilt_xlsx],
        "run_at":    now_utc,
        "filings":   [{"ticker": f.get("ticker","?"), "form": f["form"],
                       "date": f["date"]} for f in all_filings],
    }
    REBUILD_SUMMARY.write_text(json.dumps(summary, indent=2))

    log.info("Watchdog run complete.  Rebuilt: %s", ", ".join(rebuilt_xlsx))
    log.info("=" * 70)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-ticker EDGAR filing watchdog")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild and push even if no new filings detected.",
    )
    parser.add_argument(
        "--ticker",
        metavar="TICKER",
        help="Watch only this ticker (default: all).",
    )
    args = parser.parse_args()

    watch = [args.ticker.upper()] if args.ticker else None
    run(tickers_to_watch=watch, force=args.force)
