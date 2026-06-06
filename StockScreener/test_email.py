"""
test_email.py — Diagnose and test email configuration without running the screener.

Usage:
    python test_email.py            # check config + send a test email
    python test_email.py --check    # check config only, do not send
"""

import argparse
import logging
import os
import smtplib
import sys
from datetime import datetime

# Load .env before config import
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_email")


def check_config() -> dict:
    """Load and validate email config. Returns dict of findings."""
    from config import OUTPUT

    findings = {}
    ok = True

    # send_email flag
    findings["send_email"]       = OUTPUT.send_email
    findings["email_recipients"] = OUTPUT.email_recipients
    findings["smtp_host"]        = OUTPUT.smtp_host
    findings["smtp_port"]        = OUTPUT.smtp_port
    findings["smtp_user"]        = OUTPUT.smtp_user
    findings["smtp_password"]    = "***set***" if OUTPUT.smtp_password else "(empty)"
    findings["email_top_n"]      = OUTPUT.email_top_n

    # Show .env status
    env_path = os.path.join(os.getcwd(), ".env")
    print()
    if os.path.exists(env_path):
        print(f"  ✓ .env file found: {env_path}")
    else:
        print("  ⚠  No .env file — using shell env vars")
        print("     Run: cp .env.example .env  then fill in your values")
    print()
    print("═" * 58)
    print("  EMAIL CONFIGURATION CHECK")
    print("═" * 58)

    # 1. send_email flag
    if OUTPUT.send_email:
        print("  ✓ send_email          = True")
    else:
        print("  ✗ send_email          = False  ← MUST SET TO True")
        ok = False

    # 2. recipients
    if OUTPUT.email_recipients:
        print(f"  ✓ email_recipients    = {OUTPUT.email_recipients}")
    else:
        print("  ✗ email_recipients    = []     ← ADD YOUR EMAIL ADDRESS")
        ok = False

    # 3. SMTP user
    if OUTPUT.smtp_user:
        print(f"  ✓ smtp_user           = {OUTPUT.smtp_user}")
    else:
        print("  ✗ smtp_user           = (empty)  ← SET SMTP_USER env var")
        ok = False

    # 4. Password
    if OUTPUT.smtp_password:
        print(f"  ✓ smtp_password       = (set, {len(OUTPUT.smtp_password)} chars)")
        if len(OUTPUT.smtp_password.replace(" ","")) != 16:
            print("    ⚠  Gmail App Passwords are exactly 16 chars (spaces optional)")
    else:
        print("  ✗ smtp_password       = (empty)  ← SET SMTP_PASSWORD env var")
        ok = False

    # 5. SMTP host/port
    print(f"  ✓ smtp_host           = {OUTPUT.smtp_host}:{OUTPUT.smtp_port}")
    print(f"  ✓ email_top_n         = {OUTPUT.email_top_n}")

    print("═" * 58)

    if not ok:
        print()
        print("  FIX REQUIRED — complete these steps:")
        print()
        if not OUTPUT.send_email:
            print("  1. In config.py, change:")
            print("       send_email: bool = False")
            print("     to:")
            print("       send_email: bool = True")
            print()
        if not OUTPUT.email_recipients:
            print("  2. In config.py, change:")
            print("       email_recipients: List[str] = field(default_factory=list)")
            print("     to:")
            print("       email_recipients: List[str] = field(")
            print('           default_factory=lambda: ["you@gmail.com"]')
            print("       )")
            print()
        if not OUTPUT.smtp_user or not OUTPUT.smtp_password:
            print("  3. In your terminal, run:")
            print('       export SMTP_USER="you@gmail.com"')
            print('       export SMTP_PASSWORD="xxxx xxxx xxxx xxxx"')
            print()
            print("     Gmail App Password (NOT your account password):")
            print("     → https://myaccount.google.com/apppasswords")
            print("     → Create → name it 'StockScreener' → copy the 16-char code")
            print()
        print("  After fixing, re-run:  python test_email.py")
        print()

    return {"ok": ok, **findings}


def test_smtp_connection(host: str, port: int, user: str, password: str) -> bool:
    """Try to connect and authenticate — does NOT send any email."""
    print()
    print(f"  Testing SMTP connection to {host}:{port}…")
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(user, password)
        print("  ✓ SMTP connection and authentication successful")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  ✗ Authentication failed")
        print()
        print("    Gmail fix:")
        print("    1. Make sure 2-Step Verification is ON for your account")
        print("       → https://myaccount.google.com/security")
        print("    2. Create an App Password (NOT your regular password)")
        print("       → https://myaccount.google.com/apppasswords")
        print("    3. Select app: Mail, device: Other → enter 'StockScreener'")
        print("    4. Copy the 16-char code and set:")
        print('       export SMTP_PASSWORD="abcd efgh ijkl mnop"')
        return False
    except smtplib.SMTPConnectError as e:
        print(f"  ✗ Cannot connect to {host}:{port}: {e}")
        print("    Check your internet connection and firewall settings.")
        return False
    except Exception as e:
        print(f"  ✗ SMTP error: {e}")
        return False


def send_test_email(cfg: dict) -> bool:
    """Send a simple plain-text test email to verify the full pipeline."""
    from email.mime.multipart import MIMEMultipart
    from email.mime.text      import MIMEText

    now     = datetime.now()
    subject = f"[StockScreener] Test email — {now.strftime('%b %d %Y %I:%M %p')}"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:-apple-system,sans-serif;padding:32px;background:#f1f5f9">
<div style="max-width:480px;margin:0 auto;background:#fff;border-radius:12px;
            padding:28px;border:1px solid #e5e7eb">
  <div style="font-size:24px;font-weight:700;color:#0f172a;margin-bottom:8px">
    ✅ Test email delivered
  </div>
  <div style="font-size:14px;color:#374151;line-height:1.7">
    Your Pre-Market Momentum Screener email alert is configured correctly.<br><br>
    <strong>Sent:</strong> {now.strftime('%A, %B %d %Y at %I:%M %p')}<br>
    <strong>From:</strong> {cfg['smtp_user']}<br>
    <strong>To:</strong> {', '.join(cfg['recipients'])}<br>
    <strong>Top-N:</strong> {cfg.get('top_n', 10)} TRADE ideas per email
  </div>
  <div style="margin-top:20px;padding:14px;background:#f0fdf4;border-radius:8px;
              border-left:4px solid #16a34a;font-size:13px;color:#166534">
    Email alerts will fire automatically after each screener run when
    <code>send_email = True</code> in config.py — or use
    <code>python main.py --email</code> to force-send on any run.
  </div>
</div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_user"]
    msg["To"]      = ", ".join(cfg["recipients"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.sendmail(cfg["smtp_user"], cfg["recipients"], msg.as_string())
        print(f"  ✓ Test email sent → {cfg['recipients']}")
        print(f"    Subject: {subject}")
        print()
        print("  Check your inbox (and spam folder).")
        print("  If it arrived — email is working. Run the screener normally:")
        print("    python main.py --tickers NVDA TSLA MSFT AAPL --email")
        return True
    except smtplib.SMTPAuthenticationError:
        print("  ✗ Authentication failed — see fix instructions above")
        return False
    except Exception as e:
        print(f"  ✗ Send failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test email configuration")
    parser.add_argument("--check", action="store_true",
                        help="Check config only — do not send a test email")
    args = parser.parse_args()

    sys.path.insert(0, ".")

    # 1. Check config
    result = check_config()
    if not result["ok"]:
        sys.exit(1)

    if args.check:
        print("  Config looks good. Run without --check to send a test email.")
        print()
        sys.exit(0)

    # 2. Test SMTP connection
    from config import OUTPUT
    conn_ok = test_smtp_connection(
        OUTPUT.smtp_host, OUTPUT.smtp_port,
        OUTPUT.smtp_user, OUTPUT.smtp_password,
    )
    if not conn_ok:
        sys.exit(1)

    # 3. Send test email
    print()
    print("  Sending test email…")
    ok = send_test_email({
        "smtp_host":  OUTPUT.smtp_host,
        "smtp_port":  OUTPUT.smtp_port,
        "smtp_user":  OUTPUT.smtp_user,
        "smtp_password": OUTPUT.smtp_password,
        "recipients": OUTPUT.email_recipients,
        "top_n":      OUTPUT.email_top_n,
    })

    print()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
