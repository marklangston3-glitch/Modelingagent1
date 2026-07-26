#!/usr/bin/env python3
"""
send_discord_report.py — Post a PDF report to a Discord channel via webhook.

Called by GitHub Actions after PDFs are generated.
Can also be run locally:
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python send_discord_report.py morning 2026-07-26
  DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python send_discord_report.py weekly

Usage:
  python send_discord_report.py <report_type> [--date YYYY-MM-DD]
  report_type: 'morning' or 'weekly'

Reads:
  DISCORD_WEBHOOK_URL  env var (required; exits 0 with message if absent)

Exits:
  0   posted to Discord, or DISCORD_WEBHOOK_URL not set (non-fatal)
  1   API error
"""

import argparse
import glob
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Error: 'requests' not installed. Run: pip install requests")


def main() -> None:
    parser = argparse.ArgumentParser(description="Post report PDF to Discord via webhook")
    parser.add_argument("report_type", choices=["morning", "weekly"],
                        help="Type of report to post")
    parser.add_argument("--date", help="Report date YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set — skipping Discord delivery.")
        print("  To enable: add DISCORD_WEBHOOK_URL under")
        print("  GitHub → Settings → Secrets → Actions")
        print("  Create the webhook in Discord: Channel → Edit → Integrations → Webhooks")
        sys.exit(0)

    date_utc = datetime.now(timezone.utc)
    date_tag = args.date or date_utc.strftime("%Y-%m-%d")
    date_pretty = datetime.strptime(date_tag, "%Y-%m-%d").strftime("%B %d, %Y")

    if args.report_type == "morning":
        pdf_path = f"reports/morning_report_{date_tag}.pdf"
        if not os.path.exists(pdf_path):
            today_pdfs = sorted(glob.glob(f"reports/*_{date_tag}.pdf"))
            if today_pdfs:
                pdf_path = today_pdfs[-1]
                print(f"Note: combined PDF not found; using {os.path.basename(pdf_path)}")
            else:
                stale = sorted(glob.glob("reports/morning_report_*.pdf"))
                if stale:
                    print(f"ERROR: No report found for {date_tag}.")
                    print(f"  Latest available (NOT posting): {os.path.basename(stale[-1])}")
                else:
                    print(f"ERROR: No morning PDF found for {date_tag}.")
                sys.exit(1)

        message = (
            f"📊 **Morning Intelligence Brief — {date_pretty}**\n"
            "Pre-market equity research report is attached.\n"
            "Coverage: TEM · RGTI · BBAI · NEE (+ tickers.txt)\n"
            "*AI-generated · Not investment advice*"
        )

    else:  # weekly
        pdf_path = f"reports/weekly_report_{date_tag}.pdf"
        if not os.path.exists(pdf_path):
            matches = sorted(glob.glob("reports/weekly_report_*.pdf"))
            if not matches:
                print(f"ERROR: No weekly PDF found for {date_tag}.")
                sys.exit(1)
            pdf_path = matches[-1]
            print(f"Note: using {os.path.basename(pdf_path)}")

        end = date_utc
        from datetime import timedelta
        start = end - timedelta(days=4)
        week_label = f"Week of {start.strftime('%B %d')}–{end.strftime('%d, %Y')}"

        message = (
            f"📈 **Weekly Intelligence Rollup — {week_label}**\n"
            "Weekly equity research rollup is attached.\n"
            "Coverage: all portfolio tickers — what moved, why, SEC filings, sector themes, forward look.\n"
            "*AI-generated · Not investment advice*"
        )

    filename = os.path.basename(pdf_path)
    pdf_kb = os.path.getsize(pdf_path) / 1024

    print(f"Discord webhook POST")
    print(f"  url     : {webhook_url[:40]}…")
    print(f"  pdf     : {filename}  ({pdf_kb:.0f} KB)")
    print(f"  message : {message[:80]}…")
    print()

    with open(pdf_path, "rb") as fh:
        resp = requests.post(
            webhook_url,
            data={"content": message},
            files={"file": (filename, fh, "application/pdf")},
            timeout=60,
        )

    if resp.status_code in (200, 204):
        print(f"✓ HTTP {resp.status_code} — posted to Discord successfully.")
    else:
        print(f"✗ HTTP {resp.status_code} — Discord webhook error.")
        try:
            print(f"  {resp.json()}")
        except Exception:
            print(f"  {resp.text[:300]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
