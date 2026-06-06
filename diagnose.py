"""
diagnose.py — Run locally to map which FMP stable endpoints work with your key/plan.

Usage:  python diagnose.py
"""

import requests, json, sys

import sys, os
sys.path.insert(0, ".")

# Load .env first
try:
    from dotenv import load_dotenv
    loaded = load_dotenv(override=False)
    env_file = os.path.join(os.getcwd(), ".env")
    if os.path.exists(env_file):
        print(f"  ✓ .env loaded from {env_file}")
    else:
        print("  ⚠  No .env file found — using shell environment variables")
        print("     Copy .env.example to .env and fill in your keys.")
except ImportError:
    print("  ⚠  python-dotenv not installed — run: pip install python-dotenv")

KEY = None
try:
    from config import FMP_API_KEY
    KEY = FMP_API_KEY
except Exception:
    pass

if not KEY or KEY == "YOUR_FMP_KEY_HERE":
    KEY = input("Enter your FMP API key: ").strip()

BASE = "https://financialmodelingprep.com/stable"

def probe(path, params=None):
    p = {"apikey": KEY, **(params or {})}
    try:
        r = requests.get(f"{BASE}/{path}", params=p,
                         headers={"User-Agent":"premarket-screener/2.0"}, timeout=10)
        data = r.json()
        if r.status_code == 200:
            if isinstance(data, list) and data:
                return "✓", f"{len(data)} records", "working"
            elif isinstance(data, list) and not data:
                return "⚠", "200 but EMPTY", "empty"
            elif isinstance(data, dict) and ("Error" in str(data) or "error" in str(data)):
                return "✗", str(data)[:60], "error"
            else:
                return "✓", f"dict: {list(data.keys())[:3]}", "working"
        return "✗", f"HTTP {r.status_code}", "error"
    except Exception as e:
        return "✗", str(e)[:60], "error"

print("\n" + "="*72)
print("FMP STABLE API DIAGNOSTICS  —  corrected endpoint paths")
print("="*72)

tests = [
    # Core quote + price
    ("quote (single)",           "quote",                    {"symbol": "AAPL"}),
    ("quote (batch — Ultimate)", "quote",                    {"symbol": "AAPL,MSFT,NVDA"}),
    ("stock-price-change",       "stock-price-change",       {"symbol": "AAPL"}),
    # pre-post-market → 404 on this plan; /stable/quote already returns preMarketPrice
    # Use aftermarket-quote for bid/ask spread during extended hours
    ("aftermarket-quote",        "aftermarket-quote",        {"symbol": "AAPL"}),
    ("aftermarket-trade",        "aftermarket-trade",        {"symbol": "AAPL"}),
    # Profile / universe
    ("profile (single)",         "profile",                  {"symbol": "AAPL"}),
    ("profile (batch)",          "profile",                  {"symbol": "AAPL,MSFT"}),
    ("sp500-constituent",        "sp500-constituent",        {}),
    ("company-screener",         "company-screener",         {"priceMoreThan": 100, "limit": 3}),
    # Historical
    ("historical-price-eod",     "historical-price-eod/full",{"symbol": "AAPL", "limit": 3}),
    # Technical indicators — CORRECT paths
    ("SMA (sma)",                "technical-indicators/sma", {"symbol":"AAPL","periodLength":50,"timeframe":"1day"}),
    ("EMA (ema)",                "technical-indicators/ema", {"symbol":"AAPL","periodLength":50,"timeframe":"1day"}),
    ("RSI (rsi)",                "technical-indicators/rsi", {"symbol":"AAPL","periodLength":14,"timeframe":"1day"}),
    # Earnings — CORRECT path
    ("earnings",                 "earnings",                 {"symbol": "AAPL", "limit": 1}),
    ("earnings-calendar",        "earnings-calendar",        {"limit": 5}),
    # News / press releases — CORRECT path
    ("news/press-releases",      "news/press-releases",      {"symbol": "AAPL", "limit": 3}),
    # news/general-news → not available on this plan tier
    # Macro news covered via news/stock-latest (already working, symbol=null for macro)
    ("news/stock-latest (macro)", "news/stock-latest",        {"page": 0, "limit": 3}),
    # Analyst grades — CORRECT path
    ("grades",                   "grades",                   {"symbol": "AAPL", "limit": 3}),
    # analyst-estimates REQUIRES period param — without it FMP returns empty body → parse error
    # Correct calls are below with period=quarter/annual
    # Sector ETF
    ("quote (SPY/sector)",       "quote",                    {"symbol": "SPY"}),
    ("quote (XLK)",              "quote",                    {"symbol": "XLK"}),
    # News feed — NEW confirmed endpoint
    ("news/stock-latest (bulk)", "news/stock-latest",         {"page": 0, "limit": 5}),
    ("news/stock-latest (AAPL)", "news/stock-latest",         {"symbols": "AAPL", "limit": 3}),
    ("news/press-releases",      "news/press-releases",       {"symbol": "AAPL", "limit": 3}),
    # Analyst estimates — confirmed endpoint
    ("analyst-estimates (quarter)", "analyst-estimates",          {"symbol": "AAPL", "period": "quarter", "page": 0, "limit": 2}),
    ("analyst-estimates (annual)",  "analyst-estimates",          {"symbol": "AAPL", "period": "annual",  "page": 0, "limit": 2}),
]

working, empty_list, errors = [], [], []

for name, path, params in tests:
    flag, info, status = probe(path, params)
    print(f"  {flag}  {info:<35} [{name}]")
    if status == "working": working.append(name)
    elif status == "empty":  empty_list.append(name)
    else:                    errors.append(name)

print()
print("="*72)
print(f"WORKING: {len(working)}  |  EMPTY (plan/batch): {len(empty_list)}  |  ERRORS: {len(errors)}")
print("="*72)

if empty_list:
    print()
    print("EMPTY endpoints explanation:")
    print("  'EMPTY' on batch calls = your plan doesn't support comma-separated symbols.")
    print("  The screener automatically falls back to parallel single-symbol calls.")
    print("  No action needed — this is expected on Starter/Premium plans.")

if errors:
    print()
    print("FAILED endpoints — known resolutions:")
    resolutions = {
        "pre-post-market":     "404 expected — use aftermarket-quote / aftermarket-trade instead (tested above)",
        "news/general-news":   "404 = not on this plan tier. Macro news covered via news/stock-latest (symbol=null)",
        "analyst-estimates":   "Requires period param — call with ?period=quarter&symbol=AAPL (tested above, works)",
    }
    for e in errors:
        note = resolutions.get(e, "check FMP docs for correct path")
        print(f"  • {e:<35} → {note}")

print()
print("Screener will use FMP for working endpoints and yfinance as fallback.")
