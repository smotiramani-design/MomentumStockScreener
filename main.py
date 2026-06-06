"""
main.py — Entry point for the Pre-Market Momentum Screener.

Usage:
    python main.py                              # run once immediately
    python main.py --schedule                   # run daily at 08:45 ET
    python main.py --dry-run                    # validate config & API key
    python main.py --tickers AAPL MSFT NVDA    # override universe
    python main.py --top 20                     # show only top N results
    python main.py --tier TRADE                 # filter by tier
"""

import argparse
import logging
import sys
import time
from datetime import datetime

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


def parse_args():
    p = argparse.ArgumentParser(description="Pre-Market Momentum Screener")
    p.add_argument("--schedule",  action="store_true", help="Run on schedule at 08:45 ET daily")
    p.add_argument(
        "--universe",
        choices=["sp500","nasdaq100","russell3000","nyse","custom"],
        default=None,
        help=(
            "Universe to scan. Overrides config.py setting.\n"
            "  sp500       S&P 500          ~503 tickers  (default)\n"
            "  nasdaq100   Nasdaq-100        ~101 tickers\n"
            "  russell3000 Russell 3000     ~3,000 tickers  large/mid/small\n"
            "  nyse        NYSE + NYSE American ~3,500 tickers\n"
            "  custom      Use --tickers flag"
        ),
    )
    p.add_argument("--dry-run",   action="store_true", help="Validate config and API keys only")
    p.add_argument("--tickers",   nargs="+",           help="Override universe with specific tickers")
    p.add_argument("--top",       type=int, default=0, help="Show only top N results")
    p.add_argument("--min-score", type=int, default=0, help="Filter to min score")
    p.add_argument("--tier",      choices=["TRADE","WATCH","SKIP","ALL"], default="ALL")
    p.add_argument("--no-email",  action="store_true")
    p.add_argument("--sms",       action="store_true",
                   help="Force send SMS even if SMS.send_sms=False in config")
    p.add_argument("--no-sms",    action="store_true",
                   help="Skip SMS even if SMS.send_sms=True in config")
    p.add_argument("--email",     action="store_true",
                   help="Force send email even if OUTPUT.send_email=False")
    p.add_argument("--top-n",     type=int, default=0,
                   help="Override number of top TRADE ideas in email (default: config.email_top_n)")
    return p.parse_args()


def validate_config() -> bool:
    from config import FMP_API_KEY, BENZINGA_API_KEY, UNUSUAL_WHALES_KEY
    from core.fmp_client import FMPClient

    if not FMP_API_KEY or FMP_API_KEY == "YOUR_FMP_KEY_HERE":
        logger.error("FMP_API_KEY not set. Export it or edit config.py")
        return False

    fmp = FMPClient(FMP_API_KEY)
    ok  = fmp.validate_key()

    # Optional keys — just report status
    if BENZINGA_API_KEY:
        logger.info("✓ Benzinga key configured (news feed enabled)")
    else:
        logger.info("  Benzinga key not set  (news feed disabled — L1 partial)")

    if UNUSUAL_WHALES_KEY:
        logger.info("✓ Unusual Whales key configured (options sweep enabled)")
    else:
        logger.info("  Unusual Whales key not set  (yfinance C/P ratio used instead)")

    return ok


def run_screener(args) -> pd.DataFrame:
    from config import UNIVERSE, OUTPUT, SCORING, SMS
    from core.screener import PreMarketScreener
    from utils.report  import save_outputs

    if args.tickers:
        UNIVERSE.source          = "custom"
        UNIVERSE.custom_tickers  = [t.upper() for t in args.tickers]
    elif getattr(args, "universe", None):
        UNIVERSE.source = args.universe
        logger.info("Universe overridden via CLI: %s", args.universe)

    screener = PreMarketScreener()
    result = screener.run()
    if isinstance(result, tuple):
        df, results_map = result
    else:
        df, results_map = result, {}

    if df.empty:
        logger.warning("No results — see guidance above.")
        return df

    # Apply CLI filters
    if args.min_score > 0 and "Score" in df.columns:
        df = df[df["Score"] >= args.min_score]
    if args.tier != "ALL" and "Tier" in df.columns:
        df = df[df["Tier"] == args.tier]
    if args.top > 0:
        df = df.head(args.top)

    _print_console_table(df)

    send_email = (OUTPUT.send_email or getattr(args, 'email', False)) and not args.no_email
    send_sms   = (SMS.send_sms or getattr(args, 'sms', False)) and not getattr(args, 'no_sms', False)
    top_n = args.top_n if args.top_n > 0 else OUTPUT.email_top_n

    # Always show email status so it's never silently skipped
    if not send_email:
        logger.info(
            "Email alerts are OFF.  To enable:\n"
            "  Option A — one-time:  python main.py --email\n"
            "  Option B — permanent: set send_email=True in config.py\n"
            "  Run 'python test_email.py' to check + test your config."
        )
    paths = save_outputs(
        df,
        output_dir   = OUTPUT.output_dir,
        results_map  = results_map,
        send_email   = send_email,
        email_top_n  = top_n,
        email_cfg    = {
            "smtp_host":     OUTPUT.smtp_host,
            "smtp_port":     OUTPUT.smtp_port,
            "smtp_user":     OUTPUT.smtp_user,
            "smtp_password": OUTPUT.smtp_password,
            "recipients":    OUTPUT.email_recipients,
        } if send_email else None,
        sms_cfg = {
            "send_sms":               send_sms,
            "provider":               SMS.provider,
            "to_numbers":             SMS.to_numbers,
            "sms_top_n":              SMS.sms_top_n,
            "twilio_account_sid":     SMS.twilio_account_sid,
            "twilio_auth_token":      SMS.twilio_auth_token,
            "twilio_from_number":     SMS.twilio_from_number,
            "aws_access_key_id":      SMS.aws_access_key_id,
            "aws_secret_access_key":  SMS.aws_secret_access_key,
            "aws_region":             SMS.aws_region,
            "vonage_api_key":         SMS.vonage_api_key,
            "vonage_api_secret":      SMS.vonage_api_secret,
            "vonage_from_name":       SMS.vonage_from_name,
        } if send_sms else None,
    )
    for k, v in paths.items():
        logger.info("Output [%s]: %s", k, v)

    return df


def _print_console_table(df: pd.DataFrame):
    cols = ["Ticker","Tier","Score","PM Change %","Gap %","PM Volume",
            "Session","L1 Catalyst"]
    cols = [c for c in cols if c in df.columns]
    sub  = df[cols].copy()
    if "PM Volume" in sub.columns:
        sub["PM Volume"] = sub["PM Volume"].apply(
            lambda x: f"{int(x):,}" if pd.notna(x) and x else "—"
        )
    pd.set_option("display.max_colwidth", 55)
    pd.set_option("display.width", 220)

    trade_n = len(df[df.get("Tier","") == "TRADE"]) if "Tier" in df.columns else 0
    watch_n = len(df[df.get("Tier","") == "WATCH"]) if "Tier" in df.columns else 0

    print("\n" + "═"*100)
    print(f"  PRE-MARKET MOMENTUM SCREENER  ·  {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"  ● TRADE={trade_n}  ● WATCH={watch_n}  ● TOTAL={len(df)}")
    print("═"*100)
    print(sub.to_string(index=False))
    print("═"*100 + "\n")


def schedule_loop(args):
    TARGET_HOUR, TARGET_MINUTE = 8, 45
    logger.info("Scheduled mode — running daily at %02d:%02d ET", TARGET_HOUR, TARGET_MINUTE)
    try:
        import zoneinfo
        from datetime import timezone
        ET = zoneinfo.ZoneInfo("America/New_York")
    except Exception:
        ET = None

    while True:
        now    = datetime.now(ET) if ET else datetime.now()
        target = now.replace(hour=TARGET_HOUR, minute=TARGET_MINUTE,
                             second=0, microsecond=0)
        if now >= target:
            from datetime import timedelta
            target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        logger.info("Next run in %.0f min  (at %s ET)", wait_s/60,
                    target.strftime("%H:%M"))
        time.sleep(wait_s)
        try:
            run_screener(args)
        except Exception as e:
            logger.error("Screener run failed: %s", e, exc_info=True)


def main():
    args = parse_args()
    if args.dry_run:
        ok = validate_config()
        sys.exit(0 if ok else 1)
    if args.schedule:
        schedule_loop(args)
    else:
        run_screener(args)


if __name__ == "__main__":
    main()
