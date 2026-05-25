#!/usr/bin/env python3
"""
watchdog.py — TEM EDGAR filing watchdog

Runs daily (via cron).  Checks the SEC EDGAR RSS feed for new 10-K / 10-Q
filings from Tempus AI (CIK 0001717115 / ticker TEM).  When a new filing is
detected it:
  1. Pulls fresh XBRL financials from data.sec.gov
  2. Rebuilds TEM_dcf.xlsx using build_tem_dcf.py
  3. Commits & pushes the updated workbook to the repo
  4. Appends a structured log entry to watchdog.log

Usage
-----
  python watchdog.py            # normal run
  python watchdog.py --force    # skip cache check, rebuild unconditionally
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
TEM_CIK        = "0001717115"
WATCH_FORMS    = {"10-K", "10-Q"}
REPO_DIR       = Path(__file__).parent.resolve()
STATE_FILE     = REPO_DIR / ".watchdog_state.json"
LOG_FILE       = REPO_DIR / "watchdog.log"
BUILD_SCRIPT   = REPO_DIR / "build_tem_dcf.py"
OUTPUT_XLSX    = REPO_DIR / "TEM_dcf.xlsx"
BRANCH         = "claude/agent-tools-edgar-setup-PimAK"
GIT_REMOTE     = "origin"

EDGAR_HEADERS  = {"User-Agent": "ModelingAgent watchdog@example.com"}
RSS_URL        = (
    f"https://www.sec.gov/cgi-bin/browse-edgar"
    f"?action=getcompany&CIK={TEM_CIK}&type=10-K&dateb=&owner=include"
    f"&count=10&search_text=&output=atom"
)
RSS_URLS = {
    "10-K": (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={TEM_CIK}&type=10-K&dateb=&owner=include&count=10&output=atom"
    ),
    "10-Q": (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={TEM_CIK}&type=10-Q&dateb=&owner=include&count=10&output=atom"
    ),
}
FACTS_URL = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{TEM_CIK}.json"

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
    return {"seen_accessions": [], "last_run": None, "last_rebuild": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


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


def fetch_rss_filings(form_type: str) -> list[dict]:
    """Return a list of {accession, date, form, title} from the EDGAR RSS feed."""
    url = RSS_URLS[form_type]
    r   = _get(url)
    ns  = {"atom": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(r.text)
    except ET.ParseError as exc:
        log.error("RSS parse error for %s: %s", form_type, exc)
        return []

    filings = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip()
        upd   = (entry.findtext("atom:updated", "", ns) or "").strip()
        link  = entry.find("atom:link", ns)
        href  = link.get("href", "") if link is not None else ""
        # accession number embedded in the filing-index URL
        acc = ""
        if "/Archives/edgar/data/" in href:
            parts = href.rstrip("/").split("/")
            # URL pattern: .../data/<cik>/<accession-no-dashes>/...
            for p in parts:
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


def fetch_xbrl_facts() -> dict:
    """Return the full companyfacts JSON for TEM."""
    log.info("Fetching XBRL company facts …")
    r = _get(FACTS_URL)
    return r.json()


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
    """Pull the metrics that build_tem_dcf.py hard-codes from EDGAR."""
    concepts = {
        "revenue":       ("RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap"),
        "sga":           ("GeneralAndAdministrativeExpense",                     "us-gaap"),
        "rd":            ("ResearchAndDevelopmentExpense",                        "us-gaap"),
        "ebit":          ("OperatingIncomeLoss",                                  "us-gaap"),
        "da":            ("DepreciationDepletionAndAmortization",                 "us-gaap"),
        "sbc":           ("AllocatedShareBasedCompensationExpense",               "us-gaap"),
        "capex":         ("PaymentsToAcquirePropertyPlantAndEquipment",           "us-gaap"),
        "capsw":         ("CapitalizedComputerSoftwareAdditions",                 "us-gaap"),
        "int_exp":       ("InterestExpenseDebt",                                  "us-gaap"),
        "int_inc":       ("InvestmentIncomeInterest",                             "us-gaap"),
        "net_loss":      ("NetIncomeLoss",                                        "us-gaap"),
        "cash":          ("CashAndCashEquivalentsAtCarryingValue",                "us-gaap"),
        "conv_debt":     ("ConvertibleDebtNoncurrent",                            "us-gaap"),
        "shares":        ("WeightedAverageNumberOfDilutedSharesOutstanding",      "us-gaap"),
        "goodwill":      ("Goodwill",                                             "us-gaap"),
        "intangibles":   ("IntangibleAssetsNetExcludingGoodwill",                 "us-gaap"),
        "ar":            ("AccountsReceivableNetCurrent",                         "us-gaap"),
        "dta":           ("DeferredTaxAssetsGross",                               "us-gaap"),
    }
    snap = {}
    for key, (concept, tax) in concepts.items():
        snap[key] = extract_annual_value(facts, concept, tax)
    return snap


def diff_snapshots(old: dict, new: dict) -> list[str]:
    """Return human-readable lines describing what changed between two snapshots."""
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
def rebuild_xlsx(dry_run: bool = False) -> bool:
    """Re-run build_tem_dcf.py.  Returns True on success."""
    if dry_run:
        log.info("[dry-run] would run build_tem_dcf.py")
        return True
    log.info("Running build_tem_dcf.py …")
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT)],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("build_tem_dcf.py failed:\n%s", result.stderr)
        return False
    log.info("build_tem_dcf.py succeeded:\n%s", result.stdout.strip())
    return True


def git_commit_push(message: str, dry_run: bool = False) -> bool:
    """Stage TEM_dcf.xlsx + watchdog.log, commit, and push."""
    if dry_run:
        log.info("[dry-run] would git commit & push")
        return True
    cmds = [
        ["git", "add", "TEM_dcf.xlsx", "watchdog.log", ".watchdog_state.json"],
        ["git", "commit", "-m", message],
        ["git", "push", "-u", GIT_REMOTE, BRANCH],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            log.error("git command failed: %s\n%s", " ".join(cmd), result.stderr)
            return False
        log.info("$ %s  →  %s", " ".join(cmd), result.stdout.strip() or result.stderr.strip())
    return True


# ── Main watchdog logic ───────────────────────────────────────────────────────
def run(force: bool = False, dry_run: bool = False):
    now_utc = datetime.now(timezone.utc).isoformat()
    log.info("=" * 70)
    log.info("Watchdog run started at %s  (force=%s, dry_run=%s)", now_utc, force, dry_run)

    state = load_state()
    seen  = set(state.get("seen_accessions", []))

    # ── 1. Poll RSS feeds ────────────────────────────────────────────────────
    new_filings = []
    for form_type in WATCH_FORMS:
        filings = fetch_rss_filings(form_type)
        log.info("RSS %s: found %d entries", form_type, len(filings))
        for f in filings:
            if f["accession"] and f["accession"] not in seen:
                new_filings.append(f)
                log.info(
                    "  NEW FILING: %s  acc=%s  date=%s  title=%s",
                    f["form"], f["accession"], f["date"], f["title"],
                )

    trigger = bool(new_filings) or force

    if not trigger:
        log.info("No new 10-K / 10-Q filings detected. Nothing to do.")
        state["last_run"] = now_utc
        save_state(state)
        return

    # ── 2. Fetch fresh XBRL facts ────────────────────────────────────────────
    facts   = fetch_xbrl_facts()
    new_snap = snapshot_key_metrics(facts)

    # Diff against previous snapshot
    old_snap = state.get("last_snapshot", {})
    changes  = diff_snapshots(old_snap, new_snap)
    if changes:
        log.info("Financial data changes detected (%d):", len(changes))
        for line in changes:
            log.info(line)
    else:
        log.info("XBRL snapshot unchanged (filing may be an amendment or metadata update).")

    # ── 3. Rebuild xlsx ──────────────────────────────────────────────────────
    ok = rebuild_xlsx(dry_run=dry_run)
    if not ok:
        log.error("Rebuild failed — aborting push.")
        return

    # ── 4. Commit & push ─────────────────────────────────────────────────────
    form_strs = ", ".join(f"{f['form']} ({f['date']})" for f in new_filings) or "forced rebuild"
    commit_msg = (
        f"Auto-update TEM_dcf.xlsx — new EDGAR filing(s): {form_strs}\n\n"
        f"Run: {now_utc}\n"
        + ("\n".join(changes[:30]) if changes else "No metric changes detected.")
        + "\n\nhttps://claude.ai/code/session_014hesikAtm8zzGNsXbYWmGV"
    )
    git_commit_push(commit_msg, dry_run=dry_run)

    # ── 5. Update state ──────────────────────────────────────────────────────
    for f in new_filings:
        if f["accession"]:
            seen.add(f["accession"])
    state["seen_accessions"] = sorted(seen)
    state["last_run"]        = now_utc
    state["last_rebuild"]    = now_utc
    state["last_snapshot"]   = new_snap
    state["last_filings"]    = new_filings
    save_state(state)

    log.info("Watchdog run complete.  Rebuilt and pushed TEM_dcf.xlsx.")
    log.info("=" * 70)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TEM EDGAR filing watchdog")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild and push even if no new filings are detected.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Poll and diff, but skip the actual rebuild and git push.",
    )
    args = parser.parse_args()
    run(force=args.force, dry_run=args.dry_run)
