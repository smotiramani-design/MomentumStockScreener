"""
aws/lambda_handler.py — AWS Lambda entry point for the Pre-Market Momentum Screener.

Replaces main.py when running on AWS Lambda + EventBridge.

Flow:
  1. Load secrets from AWS Secrets Manager into os.environ
  2. Run the screener (same logic as main.py --email --sms)
  3. Upload CSV + HTML reports to S3
  4. Return summary as Lambda response

EventBridge rule fires this at 9:15am ET Mon-Fri.
"""

import json
import logging
import os
import sys
import tempfile
from datetime import datetime

# Lambda gives us a writable /tmp directory only
TMP_DIR = "/tmp/screener_output"

# Configure logging for CloudWatch
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("lambda_handler")


# ── Step 1: Load secrets from AWS Secrets Manager ────────────────────────────

def load_secrets():
    """
    Pull all screener secrets from AWS Secrets Manager and inject
    into os.environ so config.py picks them up via _env() calls.

    Secret name in Secrets Manager: "momentum-screener/credentials"
    Stored as a single JSON object:
    {
        "FMP_API_KEY":          "...",
        "SMTP_USER":            "...",
        "SMTP_PASSWORD":        "...",
        "EMAIL_RECIPIENTS":     "you@gmail.com,other@gmail.com",
        "SMS_TO_NUMBERS":       "+14155551234",
        "TWILIO_ACCOUNT_SID":   "ACxx...",
        "TWILIO_AUTH_TOKEN":    "...",
        "TWILIO_FROM_NUMBER":   "+1...",
        "BENZINGA_API_KEY":     "",
        "UNUSUAL_WHALES_KEY":   ""
    }
    """
    import boto3
    from botocore.exceptions import ClientError

    secret_name = os.environ.get("SECRET_NAME", "momentum-screener/credentials")
    region      = os.environ.get("AWS_REGION_NAME", "us-east-1")

    logger.info("Loading secrets from Secrets Manager: %s", secret_name)

    try:
        client  = boto3.client("secretsmanager", region_name=region)
        resp    = client.get_secret_value(SecretId=secret_name)
        secrets = json.loads(resp["SecretString"])

        # Inject into environment so config.py _env() picks them up
        for key, value in secrets.items():
            if value:  # skip empty strings
                os.environ[key] = str(value)

        logger.info("✓ Secrets loaded (%d keys)", len(secrets))
        return True

    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error("✗ Secrets Manager error (%s): %s", code, e)
        return False
    except Exception as e:
        logger.error("✗ Failed to load secrets: %s", e)
        return False


# ── Step 2: Upload output files to S3 ────────────────────────────────────────

def upload_to_s3(local_dir: str, bucket: str, prefix: str) -> list:
    """
    Upload all files from local_dir to s3://bucket/prefix/
    Returns list of S3 keys that were uploaded.
    """
    import boto3

    if not bucket:
        logger.info("S3_BUCKET not set — skipping S3 upload")
        return []

    s3      = boto3.client("s3")
    uploads = []

    if not os.path.exists(local_dir):
        logger.warning("Output dir %s does not exist — nothing to upload", local_dir)
        return []

    for fname in os.listdir(local_dir):
        local_path = os.path.join(local_dir, fname)
        if not os.path.isfile(local_path):
            continue

        s3_key = f"{prefix}/{fname}"
        try:
            # Set content type so files open correctly in browser
            content_type = (
                "text/csv"  if fname.endswith(".csv")  else
                "text/html" if fname.endswith(".html") else
                "application/octet-stream"
            )
            s3.upload_file(
                local_path, bucket, s3_key,
                ExtraArgs={"ContentType": content_type},
            )
            s3_url = f"s3://{bucket}/{s3_key}"
            logger.info("✓ Uploaded: %s", s3_url)
            uploads.append(s3_key)
        except Exception as e:
            logger.error("✗ S3 upload failed for %s: %s", fname, e)

    return uploads


# ── Step 3: Main Lambda handler ───────────────────────────────────────────────

def handler(event, context):
    """
    AWS Lambda entry point.
    Called by EventBridge at 9:15am ET Mon-Fri.
    event: {} (EventBridge passes an empty-ish event)
    """
    run_time  = datetime.now()
    stamp     = run_time.strftime("%Y%m%d_%H%M")
    s3_bucket = os.environ.get("S3_BUCKET", "")
    s3_prefix = f"reports/{run_time.strftime('%Y/%m/%d')}"

    logger.info("═══ Pre-Market Momentum Screener — Lambda ═══")
    logger.info("Run time: %s", run_time.strftime("%Y-%m-%d %H:%M ET"))

    # 1. Load secrets
    if not load_secrets():
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to load secrets from Secrets Manager"}),
        }

    # 2. Reload config after secrets are injected into os.environ
    #    (config.py reads env vars at import time — force a reload)
    import importlib
    for mod_name in list(sys.modules.keys()):
        if mod_name.startswith("config"):
            del sys.modules[mod_name]

    # 3. Run the screener
    try:
        import config
        from config import OUTPUT, SMS
        from core.screener import PreMarketScreener
        from utils.report  import save_outputs

        # Override output dir to Lambda's writable /tmp
        os.makedirs(TMP_DIR, exist_ok=True)
        OUTPUT.output_dir  = TMP_DIR
        OUTPUT.send_email  = True
        SMS.send_sms       = True

        screener = PreMarketScreener()
        result   = screener.run()

        if isinstance(result, tuple):
            df, results_map = result
        else:
            df, results_map = result, {}

        if df.empty:
            logger.warning("No movers found — markets may be closed or low activity")
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "status":  "no_movers",
                    "message": "Screener ran but found no qualifying movers",
                    "run_time": stamp,
                }),
            }

        n_trade = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
        n_watch = len(df[df["Tier"] == "WATCH"]) if "Tier" in df.columns else 0
        logger.info("Results: TRADE=%d  WATCH=%d  TOTAL=%d", n_trade, n_watch, len(df))

        # 4. Save outputs (email + SMS fire inside save_outputs)
        paths = save_outputs(
            df,
            output_dir  = TMP_DIR,
            results_map = results_map,
            send_email  = True,
            email_top_n = OUTPUT.email_top_n,
            email_cfg   = {
                "smtp_host":     OUTPUT.smtp_host,
                "smtp_port":     OUTPUT.smtp_port,
                "smtp_user":     OUTPUT.smtp_user,
                "smtp_password": OUTPUT.smtp_password,
                "recipients":    OUTPUT.email_recipients,
            },
            sms_cfg = {
                "send_sms":              True,
                "provider":              SMS.provider,
                "to_numbers":            SMS.to_numbers,
                "sms_top_n":             SMS.sms_top_n,
                "twilio_account_sid":    SMS.twilio_account_sid,
                "twilio_auth_token":     SMS.twilio_auth_token,
                "twilio_from_number":    SMS.twilio_from_number,
                "aws_access_key_id":     SMS.aws_access_key_id,
                "aws_secret_access_key": SMS.aws_secret_access_key,
                "aws_region":            SMS.aws_region,
            },
        )
        logger.info("Outputs: %s", paths)

        # 5. Upload to S3
        s3_keys = upload_to_s3(TMP_DIR, s3_bucket, s3_prefix)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "status":     "success",
                "run_time":   stamp,
                "trade":      n_trade,
                "watch":      n_watch,
                "total":      len(df),
                "email":      paths.get("email", "not_sent"),
                "sms":        paths.get("sms",   "not_sent"),
                "s3_uploads": s3_keys,
                "s3_bucket":  s3_bucket,
            }),
        }

    except Exception as e:
        logger.exception("Screener failed: %s", e)
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error":    str(e),
                "run_time": stamp,
            }),
        }
