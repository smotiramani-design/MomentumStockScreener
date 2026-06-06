## Pre-Market Momentum Screener — Setup & Usage

### Prerequisites
- Python 3.9+
- FMP paid membership (Starter plan or above for batch quotes + options)

### Install dependencies
```bash
pip install -r requirements.txt
```

### Set environment variables
```bash
export FMP_API_KEY="your_fmp_key"
export BENZINGA_API_KEY=""          # optional
export UNUSUAL_WHALES_KEY=""        # optional
export ALPACA_API_KEY=""            # optional
export SMTP_USER="you@gmail.com"    # optional, for email alerts
export SMTP_PASSWORD="app_password" # optional
```

### Run immediately
```bash
python main.py
```

### Run with custom tickers
```bash
python main.py --tickers NVDA AMD TSLA AAPL META MSFT
```

### Run on daily 08:45 ET schedule
```bash
python main.py --schedule
```

### Show only high-conviction TRADE ideas
```bash
python main.py --tier TRADE --min-score 4
```

### Validate API key only
```bash
python main.py --dry-run
```

### Output
- `output/momentum_YYYYMMDD_HHMM.csv`   — full ranked table
- `output/momentum_YYYYMMDD_HHMM.html`  — color-coded HTML report

---

### Scoring reference

| Layer | Signal | Data Source | Required |
|-------|--------|-------------|----------|
| L1 Catalyst | Earnings surprise, 8-K, analyst action, news | FMP + SEC EDGAR | FMP |
| L2 Volume | Pre-mkt vol vs ADV, RVOL | FMP historical + batch quotes | FMP |
| L3 Price Action | Gap %, prior high break, SMA reclaim | FMP OHLCV + technicals | FMP |
| L4 Relative Strength | vs sector ETF, vs SPY | FMP batch quotes | FMP |
| L5 Options | IV rank, C/P ratio, sweeps | yfinance (free) + UW (paid) | yfinance |

**Score 4–5 → TRADE · Score 3 → WATCH · Score < 3 → SKIP**

---

### Optional paid upgrades (ranked by ROI)

| Service | Cost | What it adds |
|---------|------|-------------|
| **Unusual Whales** | ~$50/mo | Real-time options flow, sweep detection, dark pool |
| **Benzinga Pro** | ~$99/mo | Pre-market catalyst news feed, analyst ratings wire |
| **FMP Advanced** | ~$79/mo | Live options chain IV, real-time tick data |
| **Alpaca Data+** | ~$9/mo | Consolidated pre-market NBBO, accurate volume |
| **Trade Ideas** | ~$228/mo | Real-time AI scanner (Holly), pre-built momentum alerts |
| **pytz** | free | Accurate ET timezone scheduling |
