# AWS Deployment Guide — Pre-Market Momentum Screener

Complete step-by-step instructions to deploy on AWS.
Estimated setup time: 30–45 minutes.
Estimated monthly cost: ~$3/month (Secrets Manager only — everything else is free tier).

---

## STEP 1 — Create AWS Account

1. Go to https://aws.amazon.com → **Create an AWS Account**
2. Enter email, password, account name
3. Add credit card (required, but free tier covers all costs)
4. Choose **Basic support** (free)
5. Sign in to the **AWS Management Console**
6. Top-right corner: set your region to **US East (N. Virginia) — us-east-1**
   → This is important — use the same region for all services

---

## STEP 2 — Create S3 Bucket (report storage)

1. Search **S3** in the console → **Create bucket**
2. **Bucket name**: `momentum-screener-reports-YOUR_NAME`
   (must be globally unique — add your name or initials)
3. **Region**: us-east-1
4. **Block all public access**: ON (keep checked)
5. Everything else: leave defaults
6. Click **Create bucket**
7. **Save the bucket name** — you'll need it later

---

## STEP 3 — Store Secrets in AWS Secrets Manager

This replaces your .env file on AWS.

1. Search **Secrets Manager** in the console → **Store a new secret**
2. **Secret type**: Other type of secret
3. **Key/value pairs** — add each one:

   | Key                  | Value                          |
   |----------------------|--------------------------------|
   | FMP_API_KEY          | your FMP key                   |
   | SMTP_USER            | you@gmail.com                  |
   | SMTP_PASSWORD        | your 16-char app password      |
   | EMAIL_RECIPIENTS     | you@gmail.com,other@gmail.com  |
   | SMS_TO_NUMBERS       | +14155551234                   |
   | TWILIO_ACCOUNT_SID   | ACxxxxxxxxxx                   |
   | TWILIO_AUTH_TOKEN    | your auth token                |
   | TWILIO_FROM_NUMBER   | +17816082988                   |
   | BENZINGA_API_KEY     | (leave blank if not using)     |
   | UNUSUAL_WHALES_KEY   | (leave blank if not using)     |

4. Click **Next**
5. **Secret name**: `momentum-screener/credentials`
   (must match exactly — the Lambda code looks for this name)
6. Description: `Pre-Market Momentum Screener API keys and credentials`
7. Click **Next** → **Next** → **Store**

---

## STEP 4 — Create IAM Role for Lambda

Lambda needs permissions to read Secrets Manager and write to S3.

1. Search **IAM** → **Roles** → **Create role**
2. **Trusted entity**: AWS service
3. **Use case**: Lambda → **Next**
4. **Add permissions** — search and check each:
   - `AWSLambdaBasicExecutionRole`  (CloudWatch logs — required)
   - `AmazonS3FullAccess`           (upload reports to S3)
   - `SecretsManagerReadWrite`      (read your credentials)
5. Click **Next**
6. **Role name**: `momentum-screener-lambda-role`
7. Click **Create role**

---

## STEP 5 — Package the Screener

On your Mac terminal, in the MomentumStockScreener project folder:

```bash
# Make the script executable
chmod +x aws/package_lambda.sh

# Run it (takes 2-3 minutes to install dependencies)
bash aws/package_lambda.sh
```

This creates `aws/lambda_package.zip` (~20-30 MB).

---

## STEP 6 — Create Lambda Function

1. Search **Lambda** → **Create function**
2. **Author from scratch**
3. Settings:
   - **Function name**: `momentum-stock-screener`
   - **Runtime**: Python 3.12
   - **Architecture**: x86_64
4. **Permissions** → **Use an existing role** → select `momentum-screener-lambda-role`
5. Click **Create function**

### Upload the ZIP:
1. On the function page → **Code** tab
2. Click **Upload from** → **.zip file**
3. Upload `aws/lambda_package.zip`
4. Click **Save**

### Set the handler:
1. **Code** tab → **Runtime settings** → **Edit**
2. **Handler**: `lambda_handler.handler`
3. Click **Save**

### Configure environment variables:
1. **Configuration** tab → **Environment variables** → **Edit**
2. Add these two:

   | Key             | Value                              |
   |-----------------|------------------------------------|
   | SECRET_NAME     | momentum-screener/credentials      |
   | S3_BUCKET       | momentum-screener-reports-YOUR_NAME|
   | AWS_REGION_NAME | us-east-1                          |

3. Click **Save**

### Increase timeout and memory:
1. **Configuration** tab → **General configuration** → **Edit**
2. **Memory**: 512 MB  (screener needs more than the 128 MB default)
3. **Timeout**: 5 min 0 sec  (SP500 scan takes ~2-3 minutes)
4. Click **Save**

---

## STEP 7 — Create EventBridge Schedule

1. Search **EventBridge** → **Schedules** → **Create schedule**
2. **Schedule name**: `momentum-screener-daily`
3. **Schedule pattern**: Recurring schedule → Cron-based
4. **Cron expression**: `15 13 ? * MON-FRI *`
   - This is 9:15 AM UTC / 9:15 AM ET (EDT, UTC-4)
   - Change to `15 14 ? * MON-FRI *` in Nov-Mar (EST, UTC-5)
5. **Flexible time window**: Off
6. **Timezone**: Select `America/New_York`
   (EventBridge Scheduler supports timezones — no UTC math needed!)
7. Click **Next**
8. **Target** → **AWS Lambda** → **Invoke**
9. Select your function: `momentum-stock-screener`
10. **Input**: leave as `{}`
11. Click **Next** → set permissions → **Create schedule**

---

## STEP 8 — Test End to End

### Manual test from Lambda console:
1. Lambda → your function → **Test** tab
2. **Create new test event**
3. **Event name**: `test`
4. **Event JSON**: `{}`
5. Click **Test**
6. Watch the logs in the **Execution result** panel
7. Check your email and SMS — should arrive within 3 minutes

### What success looks like in logs:
```
✓ Secrets loaded (10 keys)
═══ Pre-Market Momentum Screener — Lambda ═══
Building universe [source=sp500]
Universe: SP500 → 503 tickers
Movers (|Δ| ≥ 1.5%): 12 of 503
Scoring 12 movers…
Done. 12 results  TRADE=2  WATCH=8  SKIP=2
Email: ✓ sent → ['you@gmail.com']
SMS: ✓ sent → ['+14155551234']
✓ Uploaded: s3://momentum-screener-reports-xxx/reports/2026/06/09/momentum_20260609_0915.csv
```

---

## Viewing Reports in S3

1. S3 → your bucket → `reports/YYYY/MM/DD/`
2. Click any `.html` file → **Open** to view in browser
3. Click any `.csv` file → **Download**

---

## Monitoring & Alerts

### View logs:
CloudWatch → Log groups → `/aws/lambda/momentum-stock-screener`

### Get notified if Lambda fails:
1. CloudWatch → **Alarms** → **Create alarm**
2. Metric: Lambda → `Errors` → your function
3. Threshold: ≥ 1 error
4. Notification: your email via SNS

---

## Cost Summary

| Service            | Usage/month  | Cost      |
|--------------------|--------------|-----------|
| Lambda             | ~22 runs     | $0        |
| EventBridge        | 22 triggers  | $0        |
| S3                 | ~50 MB data  | $0        |
| Secrets Manager    | 1 secret     | $0.40     |
| CloudWatch logs    | minimal      | $0        |
| **Total**          |              | **~$0.40/month** |
