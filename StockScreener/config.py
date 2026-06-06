"""
config.py — Central configuration for Pre-Market Momentum Screener

All secrets (API keys, passwords, phone numbers) live in .env — never in this file.

SETUP:
  1. cp .env.example .env
  2. Fill in your values in .env
  3. pip install python-dotenv   (already in requirements.txt)
  4. python main.py --dry-run    (validates all keys)

To change screener behaviour (thresholds, universe, email on/off) edit this file.
Secrets always go in .env.
"""

import os
from dataclasses import dataclass, field
from typing      import List

# ── Load .env before anything else ───────────────────────────────────────────
# python-dotenv reads .env from the project root and injects values into
# os.environ so all os.getenv() calls below pick them up automatically.
# If .env doesn't exist (e.g. on a CI server using real env vars) this is
# a silent no-op — os.environ is used as-is.
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)   # override=False: real env vars take priority over .env
except ImportError:
    pass   # dotenv not installed — fall back to shell env vars only


def _env(key: str, default: str = "") -> str:
    """Read a key from environment (already loaded from .env if present)."""
    return os.getenv(key, default)


def _env_list(key: str) -> List[str]:
    """
    Read a comma-separated env var into a Python list.
    EMAIL_RECIPIENTS=a@b.com,c@d.com  →  ["a@b.com", "c@d.com"]
    SMS_TO_NUMBERS=+14155551234,+14085559876  →  ["+14155551234", "+14085559876"]
    Empty string or missing key → [].
    """
    raw = _env(key, "").strip()
    if not raw:
        return []
    return [v.strip() for v in raw.split(",") if v.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# API KEYS  — all values come from .env (or shell environment)
# ─────────────────────────────────────────────────────────────────────────────
FMP_API_KEY        = _env("FMP_API_KEY")         # required
BENZINGA_API_KEY   = _env("BENZINGA_API_KEY")    # optional — paid
UNUSUAL_WHALES_KEY = _env("UNUSUAL_WHALES_KEY")  # optional — paid
ALPACA_API_KEY     = _env("ALPACA_API_KEY")      # optional
ALPACA_SECRET_KEY  = _env("ALPACA_SECRET_KEY")   # optional


# ─────────────────────────────────────────────────────────────────────────────
# UNIVERSE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class UniverseConfig:
    # Choose which index / exchange to scan:
    #   "sp500"       S&P 500              ~503 tickers   large-cap US
    #   "nasdaq100"   Nasdaq-100           ~101 tickers   tech-heavy
    #   "russell3000" Russell 3000        ~3,000 tickers  large+mid+small
    #   "nyse"        NYSE + NYSE American ~3,500 tickers all NYSE-listed
    #   "custom"      use custom_tickers below
    source: str = "sp500"
    custom_tickers: List[str] = field(default_factory=list)

    min_price:      float = 5.0     # skip penny stocks
    max_price:      float = 5000.0
    min_avg_volume: int   = 0       # 0 = auto per universe (see UNIVERSE_LIQUIDITY_DEFAULTS)


# ── Per-universe liquidity defaults ──────────────────────────────────────────
UNIVERSE_LIQUIDITY_DEFAULTS = {
    "sp500":       {"min_avg_volume": 500_000,  "min_price": 5.0},
    "nasdaq100":   {"min_avg_volume": 500_000,  "min_price": 5.0},
    "russell3000": {"min_avg_volume": 200_000,  "min_price": 3.0},
    "nyse":        {"min_avg_volume": 100_000,  "min_price": 1.0},
    "custom":      {"min_avg_volume": 0,        "min_price": 1.0},
}


# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL THRESHOLDS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SignalConfig:
    # Layer 1 – Catalyst
    earnings_surprise_pct: float = 5.0    # EPS beat % to flag as catalyst
    price_reaction_pct:    float = 2.0    # pre-market move % to flag

    # Layer 2 – Volume
    premarket_vol_pct_of_adv: float = 20.0  # pre-mkt vol ≥ this % of 20-day ADV
    relative_vol_ratio:       float = 2.0   # RVOL floor

    # Layer 3 – Price / Technical
    gap_pct_min:    float = 1.5   # minimum gap % to include as a mover
    ma_buffer_pct:  float = 0.5   # within 0.5% of MA counts as a reclaim

    # Layer 4 – Relative Strength
    rs_min_outperformance: float = 1.0    # must outperform sector ETF by ≥ 1%

    # Layer 5 – Options
    iv_percentile_floor:     float = 50.0  # IV rank ≥ 50 to score
    unusual_call_multiplier: float = 3.0   # call vol / avg call vol ≥ 3×


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ScoringConfig:
    watch_threshold: int  = 3   # score ≥ 3 → WATCH
    trade_threshold: int  = 4   # score ≥ 4 → TRADE
    weights: dict = field(default_factory=lambda: {
        "catalyst":          1,
        "volume":            1,
        "price_action":      1,
        "relative_strength": 1,
        "options":           1,
    })


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT & EMAIL
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class OutputConfig:
    save_csv:   bool = True
    save_html:  bool = True
    output_dir: str  = "./output"

    # ── Email ────────────────────────────────────────────────────────────────
    # Flip send_email to True to enable alerts after every run.
    # All credentials come from .env — never edit them here.
    send_email: bool = True

    # Recipients loaded from .env:  EMAIL_RECIPIENTS=you@gmail.com,other@gmail.com
    email_recipients: List[str] = field(
        default_factory=lambda: _env_list("EMAIL_RECIPIENTS")
    )

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

    # Credentials from .env — SMTP_USER and SMTP_PASSWORD
    smtp_user:     str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))

    # Email content
    email_top_n:      int  = 10    # TRADE idea cards shown in email body
    email_attach_csv: bool = True  # attach full CSV to every email


# ─────────────────────────────────────────────────────────────────────────────
# SMS ALERTS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SMSConfig:
    # Flip send_sms to True to enable SMS after every run.
    send_sms:  bool = True
    provider:  str  = "twilio"   # "twilio" | "aws_sns" | "vonage"
    sms_top_n: int  = 3          # max TRADE ideas per SMS (keep short)

    # Recipients loaded from .env:  SMS_TO_NUMBERS=+14155551234,+14085559876
    to_numbers: List[str] = field(
        default_factory=lambda: _env_list("SMS_TO_NUMBERS")
    )

    # ── Twilio ────────────────────────────────────────────────────────────────
    # Free trial: https://twilio.com/try-twilio
    # .env keys: TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
    twilio_account_sid:  str = field(default_factory=lambda: _env("TWILIO_ACCOUNT_SID"))
    twilio_auth_token:   str = field(default_factory=lambda: _env("TWILIO_AUTH_TOKEN"))
    twilio_from_number:  str = field(default_factory=lambda: _env("TWILIO_FROM_NUMBER"))

    # ── AWS SNS ───────────────────────────────────────────────────────────────
    # .env keys: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
    aws_access_key_id:     str = field(default_factory=lambda: _env("AWS_ACCESS_KEY_ID"))
    aws_secret_access_key: str = field(default_factory=lambda: _env("AWS_SECRET_ACCESS_KEY"))
    aws_region:            str = field(default_factory=lambda: _env("AWS_REGION", "us-east-1"))

    # ── Vonage ────────────────────────────────────────────────────────────────
    # .env keys: VONAGE_API_KEY, VONAGE_API_SECRET
    vonage_api_key:    str = field(default_factory=lambda: _env("VONAGE_API_KEY"))
    vonage_api_secret: str = field(default_factory=lambda: _env("VONAGE_API_SECRET"))
    vonage_from_name:  str = "PreMktAlert"


# ─────────────────────────────────────────────────────────────────────────────
# SECTOR ETF MAP  (Layer 4 relative strength)
# ─────────────────────────────────────────────────────────────────────────────
SECTOR_ETF_MAP = {
    "Technology":             "XLK",
    "Health Care":            "XLV",
    "Financials":             "XLF",
    "Consumer Discretionary": "XLY",
    "Consumer Staples":       "XLP",
    "Industrials":            "XLI",
    "Energy":                 "XLE",
    "Materials":              "XLB",
    "Utilities":              "XLU",
    "Real Estate":            "XLRE",
    "Communication Services": "XLC",
}


# ─────────────────────────────────────────────────────────────────────────────
# INSTANTIATE DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
UNIVERSE = UniverseConfig()
SIGNALS  = SignalConfig()
SCORING  = ScoringConfig()
OUTPUT   = OutputConfig()
SMS      = SMSConfig()
