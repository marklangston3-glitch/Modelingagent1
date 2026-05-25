"""
edgar.py – Utilities for fetching data from the SEC EDGAR API.

Endpoints used:
  - https://data.sec.gov/submissions/{cik}.json      → company submissions / filings index
  - https://data.sec.gov/api/xbrl/companyfacts/{cik}.json → XBRL financial facts
  - https://efts.sec.gov/LATEST/search-index?q=...   → full-text filing search
"""

import time
import requests

# EDGAR requires a descriptive User-Agent per SEC policy
HEADERS = {
    "User-Agent": "ModelingAgent contact@example.com",
    "Accept-Encoding": "gzip, deflate",
}

BASE_SUBMISSIONS = "https://data.sec.gov/submissions"
BASE_FACTS       = "https://data.sec.gov/api/xbrl/companyfacts"
BASE_SEARCH      = "https://efts.sec.gov/LATEST/search-index"


def _get(url: str, params: dict | None = None) -> dict:
    """GET wrapper with basic rate-limit courtesy (max 10 req/s per SEC guidance)."""
    response = requests.get(url, headers=HEADERS, params=params, timeout=30)
    response.raise_for_status()
    time.sleep(0.11)          # ~9 req/s to stay within limits
    return response.json()


def normalize_cik(cik: str | int) -> str:
    """Return a zero-padded 10-digit CIK string."""
    return str(int(cik)).zfill(10)


# ---------------------------------------------------------------------------
# Company look-up
# ---------------------------------------------------------------------------

def get_company_submissions(cik: str | int) -> dict:
    """
    Fetch the submissions JSON for a company.

    Returns a dict containing company metadata and a 'filings' key with
    recent filing history.
    """
    cik_str = normalize_cik(cik)
    url = f"{BASE_SUBMISSIONS}/CIK{cik_str}.json"
    return _get(url)


def search_company_by_name(name: str, hits: int = 10) -> list[dict]:
    """
    Search EDGAR full-text for companies whose name contains *name*.

    Returns a list of hit dicts with keys: cik, entityName, ticker, sic.
    """
    params = {
        "q": f'"{name}"',
        "dateRange": "custom",
        "category": "form-type",
        "forms": "10-K",
        "_source": "file-index",
        "hits.hits._source": "period_of_report,entity_name,file_num",
        "hits.hits.total": hits,
    }
    results = _get(BASE_SEARCH, params=params)
    hits_list = results.get("hits", {}).get("hits", [])
    return [h.get("_source", {}) for h in hits_list]


# ---------------------------------------------------------------------------
# Financial facts (XBRL)
# ---------------------------------------------------------------------------

def get_company_facts(cik: str | int) -> dict:
    """
    Fetch all XBRL company facts (financial data) for a given CIK.

    The returned dict has the structure:
      { "cik": ..., "entityName": ..., "facts": { "us-gaap": { concept: {...} } } }
    """
    cik_str = normalize_cik(cik)
    url = f"{BASE_FACTS}/CIK{cik_str}.json"
    return _get(url)


def get_concept_values(cik: str | int, concept: str, taxonomy: str = "us-gaap") -> list[dict]:
    """
    Return time-series values for a single XBRL concept (e.g. 'Revenues').

    Each item in the returned list looks like:
      { "end": "2023-12-31", "val": 123456789, "form": "10-K", "unit": "USD", ... }
    """
    facts = get_company_facts(cik)
    concept_data = (
        facts.get("facts", {})
             .get(taxonomy, {})
             .get(concept, {})
    )
    units = concept_data.get("units", {})
    # Most financial concepts are reported in USD
    rows = []
    for unit_label, entries in units.items():
        for entry in entries:
            entry["unit"] = unit_label
            rows.append(entry)
    return rows


# ---------------------------------------------------------------------------
# Filing documents
# ---------------------------------------------------------------------------

def get_recent_filings(cik: str | int, form_type: str = "10-K", limit: int = 5) -> list[dict]:
    """
    Return the most recent *limit* filings of *form_type* for a company.

    Each item contains: accessionNumber, filingDate, reportDate, primaryDocument.
    """
    submissions = get_company_submissions(cik)
    filings = submissions.get("filings", {}).get("recent", {})

    forms   = filings.get("form", [])
    dates   = filings.get("filingDate", [])
    accnums = filings.get("accessionNumber", [])
    docs    = filings.get("primaryDocument", [])

    results = []
    for form, date, acc, doc in zip(forms, dates, accnums, docs):
        if form == form_type:
            results.append({
                "form":             form,
                "filingDate":       date,
                "accessionNumber":  acc,
                "primaryDocument":  doc,
            })
        if len(results) >= limit:
            break
    return results


def build_filing_url(cik: str | int, accession_number: str, document: str) -> str:
    """Construct the HTTPS URL for a specific filing document."""
    cik_str    = normalize_cik(cik)
    acc_clean  = accession_number.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/"
        f"{acc_clean}/{document}"
    )
