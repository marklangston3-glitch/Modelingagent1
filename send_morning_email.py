#!/usr/bin/env python3
"""
send_morning_email.py — Send the morning intelligence brief via SendGrid.

Called by the GitHub Actions morning-report workflow after PDFs are generated.
Can also be run locally:
  SENDGRID_API_KEY=SG.xxx python send_morning_email.py
  SENDGRID_API_KEY=SG.xxx python send_morning_email.py --date 2026-05-26

Reads:
  SENDGRID_API_KEY  env var (required; exits 0 with message if absent)

Finds:
  reports/morning_report_{date}.pdf  — combined PDF to attach
  Falls back to the newest morning_report_*.pdf if today's isn't present.

Exits:
  0   email sent, or SENDGRID_API_KEY not set (non-fatal)
  1   API error (step shows red in Actions)
"""

import argparse
import base64
import glob
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

FROM_EMAIL       = "marklangston3@gmail.com"
FROM_NAME        = "Langston's Research"
API_URL          = "https://api.sendgrid.com/v3/mail/send"
REPO_DIR         = Path(__file__).parent.resolve()
RECIPIENTS_FILE  = REPO_DIR / "recipients.txt"


def _load_recipients() -> list[str]:
    """Read recipients.txt — one address per line, # lines ignored.
    Falls back to [FROM_EMAIL] so delivery never silently drops to zero."""
    if not RECIPIENTS_FILE.exists():
        return [FROM_EMAIL]
    result = [
        ln.strip()
        for ln in RECIPIENTS_FILE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return result or [FROM_EMAIL]


TO_EMAILS = _load_recipients()


def main() -> None:
    parser = argparse.ArgumentParser(description="Send morning report email via SendGrid")
    parser.add_argument("--date", help="Report date YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args()

    # ── API key ───────────────────────────────────────────────────────────────
    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not api_key:
        print("SENDGRID_API_KEY not set — skipping email delivery.")
        print("  To enable: add SENDGRID_API_KEY under")
        print("  GitHub → Settings → Secrets → Actions")
        sys.exit(0)  # non-fatal; PDF is already pushed to the repo

    # ── Date strings ──────────────────────────────────────────────────────────
    date_utc    = datetime.now(timezone.utc)
    date_tag    = args.date or date_utc.strftime("%Y-%m-%d")   # e.g. 2026-05-26
    date_pretty = datetime.strptime(date_tag, "%Y-%m-%d").strftime("%B %d, %Y")

    # ── Find PDF ──────────────────────────────────────────────────────────────
    pdf_path = f"reports/morning_report_{date_tag}.pdf"
    if not os.path.exists(pdf_path):
        matches = sorted(glob.glob("reports/morning_report_*.pdf"))
        if not matches:
            print(f"No combined PDF found under reports/ for {date_tag} — skipping email.")
            sys.exit(0)
        pdf_path = matches[-1]
        print(f"Note: today's PDF not found; using {os.path.basename(pdf_path)}")

    with open(pdf_path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode()
    pdf_kb   = os.path.getsize(pdf_path) / 1024
    filename = os.path.basename(pdf_path)

    subject = f"Langston's Morning Intelligence Brief — {date_pretty}"

    # ── Email bodies ──────────────────────────────────────────────────────────
    text_body = (
        f"Langston's Financial Intelligence — Morning Brief  {date_pretty}\n\n"
        "Your pre-market equity research report is attached as a PDF.\n\n"
        "The combined report covers: TEM · RGTI · BBAI · NEE "
        "(and any other tickers in tickers.txt).  Each ticker gets a two-page write-up:\n"
        "  Page 1: Company update, rating, price target, and key catalysts.\n"
        "  Page 2: Price outlook, scenario analysis, and investment strategy.\n\n"
        "A front-page conviction-ranking table summarises every position.\n\n"
        "—\n"
        "Langston's Financial Intelligence | AI-generated | Not investment advice.\n"
    )

    html_body = f"""\
<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;color:#1A202C;max-width:640px;margin:0 auto">

  <div style="background:#002F5F;padding:22px 28px;border-bottom:3px solid #C9A84C">
    <h1 style="color:white;margin:0;font-size:18px;letter-spacing:1px">
      LANGSTON&rsquo;S FINANCIAL INTELLIGENCE</h1>
    <p style="color:#A8C8F0;margin:5px 0 0;font-size:12px">
      MORNING INTELLIGENCE BRIEF &nbsp;&middot;&nbsp; EQUITY RESEARCH
      &nbsp;&middot;&nbsp; {date_pretty.upper()}</p>
  </div>

  <div style="padding:20px 28px;background:#EEF1F6;border-bottom:1px solid #CDD3DF">
    <p style="margin:0;font-size:13px;color:#4A5568">
      Your pre-market equity research brief is attached as a PDF.
      The combined report includes a front-page conviction ranking table
      plus a two-page write-up per ticker (TEM &middot; RGTI &middot; BBAI &middot; NEE).</p>
  </div>

  <div style="padding:20px 28px;background:white">
    <table style="width:100%;border-collapse:collapse;font-size:12px;color:#4A5568">
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6;
                   font-weight:bold;width:30%">Report date</td>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6">{date_pretty}</td>
      </tr>
      <tr>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6;font-weight:bold">Attachment</td>
        <td style="padding:8px 0;border-bottom:1px solid #EEF1F6">
          {filename} &nbsp;({pdf_kb:.0f}&thinsp;KB)</td>
      </tr>
      <tr>
        <td style="padding:8px 0;font-weight:bold">Coverage</td>
        <td style="padding:8px 0">TEM &middot; RGTI &middot; BBAI &middot; NEE
          &nbsp;(per tickers.txt)</td>
      </tr>
    </table>
  </div>

  <div style="padding:14px 28px;background:#002F5F">
    <p style="color:#A8C8F0;margin:0;font-size:10px">
      Langston&rsquo;s Financial Intelligence &nbsp;&middot;&nbsp; AI-generated
      &nbsp;&middot;&nbsp; Not investment advice.</p>
  </div>

</body></html>
"""

    # ── SendGrid payload ──────────────────────────────────────────────────────
    payload = {
        "personalizations": [{"to": [{"email": e} for e in TO_EMAILS]}],
        "from":    {"email": FROM_EMAIL, "name": FROM_NAME},
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
    raw = json.dumps(payload).encode()

    # ── Send ──────────────────────────────────────────────────────────────────
    print(f"SendGrid POST /v3/mail/send")
    print(f"  key     : {api_key[:8]}…")
    print(f"  subject : {subject}")
    print(f"  pdf     : {filename}  ({pdf_kb:.0f} KB)")
    print(f"  payload : {len(raw) / 1024:.1f} KB total")
    print(f"  to      : {', '.join(TO_EMAILS)}")
    print()

    req = urllib.request.Request(
        API_URL,
        data=raw,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"✓ HTTP {resp.status} — message accepted.  Should arrive within 2 minutes.")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"✗ HTTP {exc.code} {exc.reason}")
        try:
            for e in json.loads(body).get("errors", []):
                print(f"  [{e.get('field', '?')}] {e.get('message', e)}")
        except Exception:
            print(f"  {body}")
        if exc.code == 403:
            print()
            print("  FIX: marklangston3@gmail.com is not a Verified Sender.")
            print("  Run: Actions → Test SendGrid Email → action=register")
            print("  Then click the verification link in your inbox.")
        elif exc.code == 401:
            print()
            print("  FIX: SENDGRID_API_KEY is invalid or missing 'Mail Send' scope.")
        sys.exit(1)
    except Exception as exc:
        print(f"✗ Network error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
