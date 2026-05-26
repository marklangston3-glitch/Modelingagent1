#!/usr/bin/env python3
"""
debug_sendgrid.py — Full SendGrid diagnostic for Langston's email pipeline.

Checks:
  1. SENDGRID_API_KEY present and well-formed
  2. GET /verified_senders  → show status of every registered sender
  3. POST /mail/send        → force-send a test email, print raw API response

Usage:
  SENDGRID_API_KEY=SG.xxx python debug_sendgrid.py
  python debug_sendgrid.py          # will report that key is missing

Run in GitHub Actions by triggering the test-sendgrid.yml workflow with
action = "debug", or add a step in morning-report.yml that calls this script.
"""

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

API_BASE        = "https://api.sendgrid.com/v3"
FROM_EMAIL      = "marklangston3@gmail.com"
FROM_NAME       = "Langston's Financial Intelligence"
REPO_DIR        = Path(__file__).parent.resolve()
RECIPIENTS_FILE = REPO_DIR / "recipients.txt"


def _load_recipients() -> list[str]:
    """Read recipients.txt — one address per line, # lines ignored."""
    if not RECIPIENTS_FILE.exists():
        return [FROM_EMAIL]
    result = [
        ln.strip()
        for ln in RECIPIENTS_FILE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    return result or [FROM_EMAIL]


TO_EMAILS = _load_recipients()


def sep(title: str = "") -> None:
    bar = "─" * 60
    if title:
        print(f"\n{bar}")
        print(f"  {title}")
        print(bar)
    else:
        print(bar)


def _api(method: str, path: str, api_key: str, body: dict | None = None) -> tuple[int, dict | str]:
    """Make a SendGrid API call; return (status_code, parsed_body_or_raw_string)."""
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
            raw = resp.read().decode(errors="replace")
            try:
                return resp.status, json.loads(raw) if raw.strip() else {}
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw


# ── 1. API Key check ──────────────────────────────────────────────────────────

def check_api_key(api_key: str) -> bool:
    sep("1. SENDGRID_API_KEY")
    if not api_key:
        print("  ✗ NOT SET — export SENDGRID_API_KEY=SG.xxx before running")
        print("    In GitHub Actions: add it under Settings → Secrets → Actions")
        return False
    if not api_key.startswith("SG."):
        print(f"  ✗ Unexpected key format (expected 'SG.' prefix, got '{api_key[:6]}…')")
        return False
    print(f"  ✓ Key present  (prefix: {api_key[:8]}…  length: {len(api_key)} chars)")
    return True


# ── 2. Verified Senders ───────────────────────────────────────────────────────

def check_verified_senders(api_key: str) -> bool:
    sep("2. GET /verified_senders — Sender Identity Status")
    status, data = _api("GET", "/verified_senders", api_key)
    print(f"  HTTP {status}")

    if status == 401:
        print("  ✗ 401 Unauthorized — API key is invalid or missing 'Sender Verification' scope.")
        print("    SendGrid → Settings → API Keys → edit key → enable Sender Authentication.")
        return False
    if status == 403:
        print("  ✗ 403 Forbidden — API key lacks permission to read verified senders.")
        return False
    if status != 200:
        print(f"  ✗ Unexpected status. Raw response:\n    {data}")
        return False

    senders = data.get("results", []) if isinstance(data, dict) else []
    if not senders:
        print("  No senders registered yet.")
        print(f"  → Run: python verify_sender.py --register")
        return False

    target_ok = False
    print(f"\n  {'EMAIL':42s}  {'NICKNAME':28s}  STATUS")
    print("  " + "─" * 88)
    for s in senders:
        email    = s.get("from_email", "—")
        nick     = s.get("nickname",   "—")
        verified = s.get("verified", False)
        badge    = "✓ VERIFIED" if verified else "✗ PENDING  ← click verification link in your inbox"
        print(f"  {email:42s}  {nick:28s}  {badge}")
        if email == FROM_EMAIL and verified:
            target_ok = True

    print()
    if not target_ok:
        print(f"  ✗ '{FROM_EMAIL}' is not VERIFIED — SendGrid will reject all sends from this address.")
        print(f"    Fix:")
        print(f"    1. Run: python verify_sender.py --register")
        print(f"       (or trigger test-sendgrid.yml → action=register)")
        print(f"    2. Open the SendGrid verification email in your inbox and click 'Verify'.")
        print(f"    3. Re-run this script or trigger test-sendgrid.yml → action=check.")
    else:
        print(f"  ✓ '{FROM_EMAIL}' is verified — sends should be accepted.")
    return target_ok


# ── 3. Force test email ───────────────────────────────────────────────────────

def send_test_email(api_key: str) -> bool:
    sep("3. POST /mail/send — Force Test Email")
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    to_str   = ", ".join(TO_EMAILS)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto">
  <div style="background:#002F5F;padding:22px 26px;border-bottom:3px solid #C9A84C">
    <h2 style="color:white;margin:0;font-size:17px;letter-spacing:1px">
      LANGSTON'S FINANCIAL INTELLIGENCE</h2>
    <p style="color:#A8C8F0;margin:5px 0 0;font-size:12px">
      SENDGRID DIAGNOSTIC &nbsp;&middot;&nbsp; debug_sendgrid.py</p>
  </div>
  <div style="padding:22px 26px;background:white">
    <h3 style="color:#002F5F;margin-top:0">&#10003; SendGrid Delivery Confirmed</h3>
    <table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:14px">
      <tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568;width:30%">
        From</td><td style="padding:8px 10px">{FROM_EMAIL}</td></tr>
      <tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">
        To</td><td style="padding:8px 10px">{to_str}</td></tr>
      <tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">
        Sent</td><td style="padding:8px 10px">{now_str}</td></tr>
      <tr><td style="padding:8px 10px;background:#EEF1F6;font-weight:bold;color:#4A5568">
        Source</td><td style="padding:8px 10px">debug_sendgrid.py — forced diagnostic send</td></tr>
    </table>
    <p style="margin-top:18px;color:#4A5568;font-size:12px">
      If you received this, SendGrid delivery is working. Morning reports will
      arrive on weekday mornings at 12:15 UTC.</p>
  </div>
  <div style="padding:12px 26px;background:#EEF1F6;font-size:10px;color:#B0BAC9">
    Langston's Financial Intelligence &nbsp;&middot;&nbsp; Not investment advice.
  </div>
</body></html>"""

    payload = {
        "personalizations": [{"to": [{"email": e} for e in TO_EMAILS]}],
        "from": {"email": FROM_EMAIL, "name": FROM_NAME},
        "subject": f"Langston's — SendGrid Diagnostic Email  ({now_str})",
        "content": [{"type": "text/html", "value": html}],
    }
    raw_bytes = json.dumps(payload).encode()

    print(f"  Sending to : {to_str}")
    print(f"  From       : {FROM_EMAIL}")
    print(f"  Payload    : {len(raw_bytes) / 1024:.1f} KB")
    print()

    req = urllib.request.Request(
        f"{API_BASE}/mail/send",
        data=raw_bytes,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode(errors="replace")
            print(f"  HTTP {resp.status} ✓  Message accepted — will be delivered within 1–2 min.")
            print(f"  Response headers:")
            for k, v in resp.headers.items():
                print(f"    {k}: {v}")
            if resp_body.strip():
                print(f"  Response body: {resp_body}")
            return True
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        print(f"  HTTP {exc.code} ✗  {exc.reason}")
        print(f"\n  Full error response:")
        try:
            err = json.loads(body)
            print(json.dumps(err, indent=4))
            errors = err.get("errors", [])
            if errors:
                print(f"\n  Error summary:")
                for e in errors:
                    print(f"    [{e.get('field', '?')}] {e.get('message', str(e))}")
        except Exception:
            print(f"  {body}")
        print()
        if exc.code == 403:
            print("  *** 403 FORBIDDEN ***")
            print(f"  '{FROM_EMAIL}' is not a Verified Sender in SendGrid.")
            print("  Steps to fix:")
            print("    1. python verify_sender.py --register")
            print(f"       → SendGrid will email a verification link to {FROM_EMAIL}")
            print("    2. Open that email and click 'Verify Single Sender'")
            print("    3. python verify_sender.py --check  (confirm VERIFIED status)")
            print("    4. Re-run this script")
        elif exc.code == 401:
            print("  *** 401 UNAUTHORIZED ***")
            print("  SENDGRID_API_KEY is invalid, expired, or missing 'Mail Send' scope.")
            print("  Fix: SendGrid → Settings → API Keys → regenerate with 'Mail Send' full access.")
        return False
    except Exception as exc:
        print(f"  Network error: {exc}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    sep("Langston's SendGrid Diagnostic")
    print(f"  Timestamp : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  FROM      : {FROM_EMAIL}")
    print(f"  TO        : {', '.join(TO_EMAILS)}")
    print(f"  Endpoint  : {API_BASE}")

    api_key = os.environ.get("SENDGRID_API_KEY", "").strip()

    ok_key     = check_api_key(api_key)
    if not ok_key:
        sep("RESULT: Cannot proceed — fix API key first")
        sys.exit(1)

    ok_sender  = check_verified_senders(api_key)
    ok_send    = send_test_email(api_key)

    sep("SUMMARY")
    print(f"  API key present  : {'✓' if ok_key    else '✗'}")
    print(f"  Sender verified  : {'✓' if ok_sender  else '✗  ← most common cause of 403'}")
    print(f"  Test email sent  : {'✓' if ok_send    else '✗'}")
    sep()

    if not ok_send:
        sys.exit(1)


if __name__ == "__main__":
    main()
