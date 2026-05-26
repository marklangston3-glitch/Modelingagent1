#!/usr/bin/env python3
"""
verify_sender.py — Manage SendGrid Single Sender Verification for Langston's.

SendGrid requires every FROM address to be verified before mail will deliver.
This script uses the SendGrid v3 API to register, check, and test the sender.

Commands
--------
  python verify_sender.py              # register marklangston3@gmail.com as a sender
  python verify_sender.py --check      # list verified senders + status
  python verify_sender.py --test       # send a live test email and confirm delivery
  python verify_sender.py --resend     # resend the verification email if it expired

Environment
-----------
  SENDGRID_API_KEY  — required (GitHub secret or local export)
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

SENDGRID_FROM = "marklangston3@gmail.com"
SENDGRID_NAME = "Langston's Financial Intelligence"
SENDGRID_TO   = "marklangston3@gmail.com"
API_BASE      = "https://api.sendgrid.com/v3"


# ── Low-level API helper ──────────────────────────────────────────────────────

def _api(method: str, path: str, api_key: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw}


# ── Commands ──────────────────────────────────────────────────────────────────

def check_verified_senders(api_key: str) -> bool:
    """
    List all registered senders and their verification status.
    Returns True if marklangston3@gmail.com is verified.
    """
    status, data = _api("GET", "/verified_senders", api_key)
    if status != 200:
        print(f"  ✗ GET /verified_senders returned {status}: {data}")
        return False

    senders = data.get("results", [])
    if not senders:
        print("  No verified senders registered yet.")
        return False

    target_verified = False
    print(f"\n  Registered senders ({len(senders)}):")
    print(f"  {'EMAIL':42s}  {'NAME':30s}  STATUS")
    print("  " + "─" * 84)
    for s in senders:
        email    = s.get("from_email", "")
        name     = s.get("from_name",  "")
        verified = s.get("verified", False)
        badge    = "✓ VERIFIED" if verified else "○ PENDING  (check inbox)"
        print(f"  {email:42s}  {name:30s}  {badge}")
        if email == SENDGRID_FROM and verified:
            target_verified = True
    print()
    return target_verified


def register_sender(api_key: str):
    """POST /verified_senders to register the FROM address."""
    payload = {
        "nickname":      "Langston's Financial Intelligence",
        "from_email":    SENDGRID_FROM,
        "from_name":     SENDGRID_NAME,
        "reply_to":      SENDGRID_FROM,
        "reply_to_name": SENDGRID_NAME,
        # SendGrid requires a physical address for CAN-SPAM compliance
        "address":  "123 Main St",
        "city":     "New York",
        "state":    "NY",
        "zip":      "10001",
        "country":  "US",
    }

    print(f"  Registering {SENDGRID_FROM} as a verified sender …")
    status, data = _api("POST", "/verified_senders", api_key, payload)

    if status == 201:
        print(f"  ✓ Sender registered (HTTP 201).")
        print(f"  → SendGrid has emailed a verification link to {SENDGRID_FROM}.")
        print(f"    Open that email and click 'Verify Single Sender'.")
        print(f"    (Check spam/promotions if you don't see it within 5 minutes.)")
        return True

    if status == 400:
        errors = data.get("errors", [])
        for e in errors:
            msg = e.get("message", str(e))
            if "already exists" in msg.lower() or "duplicate" in msg.lower():
                print(f"  ℹ  {SENDGRID_FROM} is already registered.")
                print(f"     Run --check to see its verification status.")
                print(f"     Run --resend if you need a new verification email.")
                return True
            print(f"  ✗ {msg}")
        return False

    print(f"  ✗ HTTP {status}: {data}")
    return False


def resend_verification(api_key: str):
    """Resend the verification email for the registered sender."""
    # First get the sender's ID
    status, data = _api("GET", "/verified_senders", api_key)
    if status != 200:
        print(f"  ✗ Could not list senders: {status}")
        return

    sender_id = None
    for s in data.get("results", []):
        if s.get("from_email") == SENDGRID_FROM:
            sender_id = s.get("id")
            break

    if not sender_id:
        print(f"  ✗ {SENDGRID_FROM} not found — run without --resend to register first.")
        return

    status2, data2 = _api("POST", f"/verified_senders/resend/{sender_id}", api_key)
    if status2 in (200, 204):
        print(f"  ✓ Verification email re-sent to {SENDGRID_FROM}.")
    else:
        print(f"  ✗ HTTP {status2}: {data2}")


def send_test_email(api_key: str) -> bool:
    """Send a branded test email via the SendGrid API and report the result."""
    date_str = datetime.now(timezone.utc).strftime("%B %d, %Y  %H:%M UTC")

    html_body = (
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head>'
        '<body style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto">'

        # Header
        '<div style="background:#002F5F;padding:22px 26px;border-bottom:3px solid #C9A84C">'
        '<h2 style="color:white;margin:0;font-size:17px;letter-spacing:1px">'
        "LANGSTON'S FINANCIAL INTELLIGENCE</h2>"
        '<p style="color:#A8C8F0;margin:5px 0 0;font-size:12px">'
        'SENDGRID DELIVERY TEST &nbsp;&middot;&nbsp; CONFIGURATION VERIFICATION</p></div>'

        # Body
        '<div style="padding:22px 26px;background:white">'
        '<h3 style="color:#002F5F;margin-top:0">&#10003; SendGrid Delivery Confirmed</h3>'
        '<p style="color:#1A202C;font-size:13px;line-height:1.6">'
        'This test email was sent from <code>verify_sender.py</code> via the SendGrid API '
        'to confirm that email delivery is working correctly for <b>morning_report.py</b>.'
        '</p>'
        '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:14px">'

        f'<tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568;'
        f'width:38%">From</td><td style="padding:8px 10px">{SENDGRID_FROM}</td></tr>'

        f'<tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">'
        f'To</td><td style="padding:8px 10px">{SENDGRID_TO}</td></tr>'

        f'<tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">'
        f'Sent at</td><td style="padding:8px 10px">{date_str}</td></tr>'

        '<tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">'
        'Next delivery</td><td style="padding:8px 10px">'
        'Weekdays at 12:15 UTC via <em>morning-report</em> GitHub Actions workflow'
        '</td></tr>'

        '</table>'
        '<p style="margin-top:18px;color:#4A5568;font-size:12px">'
        'If you received this email, your SendGrid configuration is working correctly. '
        'The morning intelligence brief will be delivered to this address each weekday '
        'morning with the combined PDF attached.</p>'
        '</div>'

        # Footer
        '<div style="padding:12px 26px;background:#EEF1F6;font-size:10px;color:#B0BAC9">'
        "Langston's Financial Intelligence &nbsp;&middot;&nbsp; SendGrid Test"
        ' &nbsp;&middot;&nbsp; Not investment advice.</div>'

        '</body></html>'
    )

    payload = {
        "personalizations": [{"to": [{"email": SENDGRID_TO}]}],
        "from": {"email": SENDGRID_FROM, "name": SENDGRID_NAME},
        "subject": f"Langston's — SendGrid Test Email  ({date_str})",
        "content": [{"type": "text/html", "value": html_body}],
    }

    print(f"  Sending test email from {SENDGRID_FROM} to {SENDGRID_TO} …")
    req = urllib.request.Request(
        f"{API_BASE}/mail/send",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"  ✓ Test email accepted by SendGrid (HTTP {resp.status}).")
            print(f"    → Check {SENDGRID_TO} — arrives within 1–2 minutes.")
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"  ✗ SendGrid HTTP {exc.code}:")
        try:
            for e in json.loads(body).get("errors", []):
                print(f"      {e.get('message', e)}")
        except Exception:
            print(f"      {body[:400]}")
        return False
    except Exception as exc:
        print(f"  ✗ Request error: {exc}")
        return False


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Manage SendGrid sender verification for Langston's reports",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  SENDGRID_API_KEY=SG.xxx python verify_sender.py           # register sender
  SENDGRID_API_KEY=SG.xxx python verify_sender.py --check   # check status
  SENDGRID_API_KEY=SG.xxx python verify_sender.py --test    # send test email
  SENDGRID_API_KEY=SG.xxx python verify_sender.py --resend  # resend verification
        """,
    )
    parser.add_argument("--check",  action="store_true", help="List verified senders and status")
    parser.add_argument("--test",   action="store_true", help="Send a test email")
    parser.add_argument("--resend", action="store_true", help="Resend verification email")
    args = parser.parse_args()

    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()
    if not api_key:
        print("Error: SENDGRID_API_KEY is not set.")
        print("  export SENDGRID_API_KEY=SG.your_key_here")
        sys.exit(1)

    print(f"\nSendGrid Sender Management — {SENDGRID_FROM}")
    print("─" * 55)

    if args.check:
        check_verified_senders(api_key)

    elif args.test:
        ok = send_test_email(api_key)
        sys.exit(0 if ok else 1)

    elif args.resend:
        resend_verification(api_key)

    else:
        # Default: register sender then show current status
        register_sender(api_key)
        print()
        check_verified_senders(api_key)


if __name__ == "__main__":
    main()
