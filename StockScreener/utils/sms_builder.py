"""
utils/sms_builder.py — SMS alerts for pre-market momentum screener.

Supported providers (all free-tier friendly):
  • Twilio     — most reliable, ~$0.0079/SMS, free trial $15 credit
  • AWS SNS    — $0.00645/SMS, free if within AWS free tier
  • Vonage     — free trial, €0.0062/SMS after trial

SMS format (160-char limit per segment):
  Segment 1 — summary  : session, counts, run time
  Segment 2-N — top-3  : one line per top TRADE idea
                          #1 NVDA +6.2% | Score 5/5 | Conv 70 | EPS +18%
                          #2 MSFT +5.5% | Score 5/5 | Conv 69 | EPS +8%
                          #3 TSLA +4.1% | Score 4/5 | Conv 56 | 8-K

Kept to 3 ideas max for SMS readability — full top-10 goes in the email.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing   import Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ── SMS text builder ──────────────────────────────────────────────────────────

def build_sms_lines(df: pd.DataFrame, run_time: Optional[datetime] = None,
                    top_n: int = 3) -> List[str]:
    """
    Build a list of SMS-ready strings (each ≤ 160 chars).
    Returns 1 + top_n messages: header + one card per top TRADE idea.
    """
    from utils.email_builder import rank_trade_tier, _infer_session

    run_time  = run_time or datetime.now()
    session   = _infer_session(run_time)
    n_trade   = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
    n_watch   = len(df[df["Tier"] == "WATCH"]) if "Tier" in df.columns else 0
    ts        = run_time.strftime("%I:%M%p")

    # Message 1: summary
    summary = (
        f"[PreMkt {session}] {ts} ET\n"
        f"TRADE:{n_trade} WATCH:{n_watch}\n"
        f"Top ideas below ↓"
    )
    messages = [summary]

    # Messages 2–N: top TRADE cards
    ranked = rank_trade_tier(df)
    if ranked.empty:
        messages.append("No TRADE-tier stocks this session.")
        return messages

    for _, row in ranked.head(top_n).iterrows():
        rank       = int(row.get("Rank",       0))
        ticker     = str(row.get("Ticker",     ""))
        chg        = float(row.get("PM Change %", 0))
        score      = int(row.get("Score",       0))
        conviction = float(row.get("Conviction", 0))
        catalyst   = str(row.get("L1 Catalyst",  ""))

        # Shorten catalyst to the first meaningful phrase (≤ 35 chars)
        cat_short = _shorten_catalyst(catalyst)

        line = (
            f"#{rank} {ticker} {chg:+.1f}% | "
            f"{score}/5 | Conv {conviction:.0f} | "
            f"{cat_short}"
        )
        # Trim to 160 chars if needed
        if len(line) > 160:
            line = line[:157] + "…"
        messages.append(line)

    return messages


def _shorten_catalyst(catalyst: str) -> str:
    """Extract the first key phrase from a catalyst detail string."""
    if not catalyst or "No catalyst" in catalyst:
        return "No catalyst"
    # Take text up to first semicolon or 35 chars
    first = catalyst.split(";")[0].strip()
    # Further shorten common verbose patterns
    replacements = [
        ("EPS surprise ",       "EPS "),
        ("Strong analyst consensus: ", ""),
        ("Revenue estimate ",   "Rev "),
        ("analyst(s)",          ""),
        ("article(s) — ",       ""),
        ('" (Reuters)',          ""),
        ('" (Bloomberg)',        ""),
        ('" (CNBC)',             ""),
        ("Recent 8-K filing (SEC EDGAR)", "8-K"),
        ("Analyst upgrade today",         "Upgrade"),
    ]
    for old, new in replacements:
        first = first.replace(old, new)
    first = first.strip().strip('"')
    return first[:35]


# ── Provider clients ──────────────────────────────────────────────────────────

class TwilioSMS:
    """
    Twilio SMS sender.
    Credentials: account_sid + auth_token + from_number
    Free trial: https://www.twilio.com/try-twilio  ($15 credit)
    Pricing: ~$0.0079 / SMS segment in the US
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token  = auth_token
        self.from_number = from_number

    def send(self, to_numbers: List[str], messages: List[str]) -> bool:
        """Send each message in messages[] to all to_numbers."""
        if not self.account_sid or not self.auth_token:
            logger.warning("Twilio: account_sid or auth_token not set")
            return False
        try:
            from twilio.rest import Client
        except ImportError:
            logger.error(
                "Twilio SDK not installed. Run:  pip install twilio\n"
                "Then add to requirements.txt: twilio>=8.0"
            )
            return False

        client   = Client(self.account_sid, self.auth_token)
        success  = True
        combined = "\n\n".join(messages)   # send as one concatenated message

        for number in to_numbers:
            try:
                msg = client.messages.create(
                    body  = combined,
                    from_ = self.from_number,
                    to    = number,
                )
                logger.info("✓ Twilio SMS sent → %s  (sid=%s)", number, msg.sid)
            except Exception as e:
                logger.error("✗ Twilio SMS failed → %s: %s", number, e)
                success = False
        return success


class AWSSNSSms:
    """
    AWS SNS SMS sender.
    Credentials: aws_access_key_id + aws_secret_access_key + region
    Free tier: first 100 SMS/month free in sandbox; $0.00645/SMS after.
    Setup: https://docs.aws.amazon.com/sns/latest/dg/sns-mobile-phone-number-as-subscriber.html
    """

    def __init__(self, access_key: str, secret_key: str,
                 region: str = "us-east-1"):
        self.access_key = access_key
        self.secret_key = secret_key
        self.region     = region

    def send(self, to_numbers: List[str], messages: List[str]) -> bool:
        if not self.access_key or not self.secret_key:
            logger.warning("AWS SNS: credentials not set")
            return False
        try:
            import boto3
        except ImportError:
            logger.error(
                "boto3 not installed. Run:  pip install boto3\n"
                "Then add to requirements.txt: boto3>=1.34"
            )
            return False

        sns     = boto3.client(
            "sns",
            aws_access_key_id     = self.access_key,
            aws_secret_access_key = self.secret_key,
            region_name           = self.region,
        )
        combined = "\n\n".join(messages)
        success  = True

        for number in to_numbers:
            try:
                resp = sns.publish(
                    PhoneNumber = number,
                    Message     = combined,
                    MessageAttributes={
                        "AWS.SNS.SMS.SenderID": {
                            "DataType":    "String",
                            "StringValue": "PreMktAlert",
                        },
                        "AWS.SNS.SMS.SMSType": {
                            "DataType":    "String",
                            "StringValue": "Transactional",
                        },
                    },
                )
                logger.info("✓ AWS SNS SMS sent → %s  (MessageId=%s)",
                            number, resp.get("MessageId","?"))
            except Exception as e:
                logger.error("✗ AWS SNS SMS failed → %s: %s", number, e)
                success = False
        return success


class VonageSMS:
    """
    Vonage (Nexmo) SMS sender.
    Credentials: api_key + api_secret
    Free trial: https://www.vonage.com/communications-apis/  (€2 credit)
    """

    def __init__(self, api_key: str, api_secret: str,
                 from_name: str = "PreMktAlert"):
        self.api_key    = api_key
        self.api_secret = api_secret
        self.from_name  = from_name

    def send(self, to_numbers: List[str], messages: List[str]) -> bool:
        if not self.api_key or not self.api_secret:
            logger.warning("Vonage: api_key or api_secret not set")
            return False
        try:
            import vonage
        except ImportError:
            logger.error(
                "Vonage SDK not installed. Run:  pip install vonage\n"
                "Then add to requirements.txt: vonage>=3.0"
            )
            return False

        client   = vonage.Client(key=self.api_key, secret=self.api_secret)
        sms      = vonage.Sms(client)
        combined = "\n\n".join(messages)
        success  = True

        for number in to_numbers:
            try:
                resp = sms.send_message({
                    "from": self.from_name,
                    "to":   number.lstrip("+"),
                    "text": combined,
                })
                if resp["messages"][0]["status"] == "0":
                    logger.info("✓ Vonage SMS sent → %s", number)
                else:
                    raise Exception(resp["messages"][0].get("error-text","unknown"))
            except Exception as e:
                logger.error("✗ Vonage SMS failed → %s: %s", number, e)
                success = False
        return success


# ── Unified sender (routes to configured provider) ────────────────────────────

def send_momentum_sms(df: pd.DataFrame, run_time: datetime,
                      sms_cfg: dict, top_n: int = 3) -> bool:
    """
    Send SMS alert via configured provider.

    sms_cfg keys:
      provider       : "twilio" | "aws_sns" | "vonage"
      to_numbers     : ["+14155551234", "+14085559876"]  — E.164 format
      top_n          : number of TRADE ideas to include (default 3)

      # Twilio:
      twilio_account_sid  : str
      twilio_auth_token   : str
      twilio_from_number  : str  (your Twilio number, e.g. "+14155550123")

      # AWS SNS:
      aws_access_key_id   : str
      aws_secret_access_key: str
      aws_region          : str  (default "us-east-1")

      # Vonage:
      vonage_api_key      : str
      vonage_api_secret   : str
      vonage_from_name    : str  (default "PreMktAlert")

    Returns True if all sends succeeded.
    """
    to_numbers = sms_cfg.get("to_numbers", [])
    if not to_numbers:
        logger.warning("SMS: no to_numbers configured")
        return False

    provider = sms_cfg.get("provider", "twilio").lower()
    messages = build_sms_lines(df, run_time=run_time,
                               top_n=sms_cfg.get("top_n", top_n))

    logger.info("SMS: sending %d message(s) via %s → %s",
                len(messages), provider, to_numbers)

    if provider == "twilio":
        client = TwilioSMS(
            account_sid = sms_cfg.get("twilio_account_sid", ""),
            auth_token  = sms_cfg.get("twilio_auth_token", ""),
            from_number = sms_cfg.get("twilio_from_number", ""),
        )
    elif provider == "aws_sns":
        client = AWSSNSSms(
            access_key = sms_cfg.get("aws_access_key_id", ""),
            secret_key = sms_cfg.get("aws_secret_access_key", ""),
            region     = sms_cfg.get("aws_region", "us-east-1"),
        )
    elif provider == "vonage":
        client = VonageSMS(
            api_key    = sms_cfg.get("vonage_api_key", ""),
            api_secret = sms_cfg.get("vonage_api_secret", ""),
            from_name  = sms_cfg.get("vonage_from_name", "PreMktAlert"),
        )
    else:
        logger.error("SMS: unknown provider '%s'. Use twilio / aws_sns / vonage", provider)
        return False

    return client.send(to_numbers, messages)
