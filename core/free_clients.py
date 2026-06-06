"""
core/free_clients.py — Supplementary free data sources
  • yfinance  : pre-market quotes, options chain, historical OHLCV
  • SEC EDGAR : recent 8-K filings (material events)
  • FDA calendar scraper (free public page)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# yfinance wrapper  (free, no API key)
# ─────────────────────────────────────────────────────────────────────────────
try:
    import yfinance as yf
    YF_AVAILABLE = True
except ImportError:
    YF_AVAILABLE = False
    logger.warning("yfinance not installed — pip install yfinance")


# ── yfinance ERROR suppressor ────────────────────────────────────────────────
# yfinance logs HTTP 404s at ERROR level for non-equity symbols (options roots
# like FDXF, warrants, etc.) that pass the gap filter but have no fundamentals.
# We temporarily mute yfinance loggers for calls that commonly hit 404s.

import logging as _logging

_YF_LOGGERS = (
    "yfinance", "yfinance.base", "yfinance.scrapers.quote",
    "yfinance.scrapers.history", "yfinance.scrapers.fundamentals",
)

def _silence_yf_errors():
    for name in _YF_LOGGERS:
        _logging.getLogger(name).setLevel(_logging.CRITICAL)

def _restore_yf_errors():
    for name in _YF_LOGGERS:
        _logging.getLogger(name).setLevel(_logging.WARNING)


class YFinanceClient:
    """Lightweight yfinance wrapper for pre-market + options fallback."""

    def get_premarket_data(self, ticker: str) -> Optional[Dict]:
        if not YF_AVAILABLE:
            return None
        try:
            t = yf.Ticker(ticker)
            info = t.fast_info
            return {
                "symbol":              ticker,
                "preMarketPrice":      getattr(info, "pre_market_price", None),
                "preMarketChange":     getattr(info, "pre_market_change", None),
                "preMarketChangePct":  getattr(info, "pre_market_change_percent", None),
                "preMarketVolume":     getattr(info, "pre_market_volume", None),
                "regularMarketPrice":  getattr(info, "last_price", None),
            }
        except Exception as e:
            logger.debug(f"yf premarket {ticker}: {e}")
            return None

    def get_options_activity(self, ticker: str) -> Dict:
        """
        Options chain from yfinance — call/put vol ratio + ATM IV.
        Silences yfinance ERROR logger for expected 404s on non-equity
        symbols (options roots like FDXF, warrants, units) that pass
        the gap filter but have no options chain in Yahoo Finance.
        """
        if not YF_AVAILABLE:
            return {}
        _silence_yf_errors()
        try:
            t = yf.Ticker(ticker)
            expirations = t.options
            if not expirations:
                return {}
            chain = t.option_chain(expirations[0])
            calls = chain.calls
            puts  = chain.puts

            total_call_vol = int(calls["volume"].fillna(0).sum())
            total_put_vol  = int(puts["volume"].fillna(0).sum())
            avg_call_iv    = float(calls["impliedVolatility"].fillna(0).mean())

            last_price = getattr(t.fast_info, "last_price", None) or 0
            atm = calls.iloc[(calls["strike"] - last_price).abs().argsort()[:1]]
            atm_iv = float(atm["impliedVolatility"].values[0]) if len(atm) else avg_call_iv

            return {
                "call_volume": total_call_vol,
                "put_volume":  total_put_vol,
                "cp_ratio":    round(total_call_vol / max(total_put_vol, 1), 2),
                "avg_call_iv": round(avg_call_iv, 4),
                "atm_iv":      round(atm_iv, 4),
            }
        except Exception as e:
            logger.debug("yf options %s: %s", ticker, str(e)[:80])
            return {}
        finally:
            _restore_yf_errors()

    def get_iv_rank(self, ticker: str) -> Optional[float]:
        """
        52-week IV rank via rolling 30-day realised vol.
        Silences yfinance ERROR logger for expected 404s.
        """
        if not YF_AVAILABLE:
            return None
        _silence_yf_errors()
        try:
            t    = yf.Ticker(ticker)
            hist = t.history(period="1y")
            if hist.empty:
                return None
            rv = hist["Close"].pct_change().dropna().rolling(30).std() * (252 ** 0.5)
            rv = rv.dropna()
            if rv.empty:
                return None
            low, high, current = rv.min(), rv.max(), rv.iloc[-1]
            return round(float((current - low) / (high - low + 1e-9) * 100), 1)
        except Exception as e:
            logger.debug("yf iv_rank %s: %s", ticker, str(e)[:80])
            return None
        finally:
            _restore_yf_errors()


# ─────────────────────────────────────────────────────────────────────────────
# SEC EDGAR  8-K filings  (free, no API key)
# ─────────────────────────────────────────────────────────────────────────────
EDGAR_HEADERS = {"User-Agent": "premarket-screener contact@example.com"}

class EDGARClient:
    SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

    def get_recent_8k(self, hours_back: int = 20) -> List[Dict]:
        """
        Returns list of {ticker, company, filed_at} for 8-Ks filed
        in the past hours_back hours.

        Robust to EDGAR API response shape changes:
          - display_names as list-of-dicts: [{"ticker":"MSFT","name":"Microsoft"}]
          - display_names as list-of-strings: ["MSFT", "Microsoft Corp"]
          - hits as nested {"hits":{"hits":[...]}} OR flat list
        """
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).strftime("%Y-%m-%d")
        try:
            r = requests.get(
                self.SEARCH_URL,
                params={"q": "8-K", "dateRange": "custom",
                        "startdt": cutoff, "forms": "8-K"},
                headers=EDGAR_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()

            # Normalise hits - handle both nested and flat structures
            raw_hits = data.get("hits", [])
            if isinstance(raw_hits, dict):
                raw_hits = raw_hits.get("hits", [])
            if not isinstance(raw_hits, list):
                raw_hits = []

            results = []
            for h in raw_hits:
                if not isinstance(h, dict):
                    continue
                src = h.get("_source", {})
                if not isinstance(src, dict):
                    src = {}

                # Parse display_names - list of dicts OR list of strings
                display = src.get("display_names") or []
                ticker, company = "", ""

                if display:
                    first = display[0]
                    if isinstance(first, dict):
                        ticker  = first.get("ticker", "") or ""
                        company = first.get("name", "") or ""
                    elif isinstance(first, str):
                        # Flat format: ["MSFT", "Microsoft Corp"]
                        ticker  = first if len(first) <= 5 else ""
                        company = display[1] if len(display) > 1 else first

                results.append({
                    "ticker":   ticker.upper().strip(),
                    "company":  company.strip(),
                    "filed_at": src.get("file_date", "") or src.get("period_of_report", ""),
                    "form":     src.get("form_type", "8-K"),
                })
            logger.debug("EDGAR 8-K: %d filings since %s", len(results), cutoff)
            return results

        except requests.HTTPError as e:
            logger.warning("EDGAR HTTP error: %s", e)
            return []
        except Exception as e:
            logger.warning("EDGAR 8-K fetch failed: %s", e)
            return []

    def has_recent_8k(self, ticker: str, hours_back: int = 20) -> bool:
        filings = self.get_recent_8k(hours_back)
        return any(f["ticker"] == ticker.upper() for f in filings)


class BenzingaClient:
    BASE = "https://api.benzinga.com/api/v2"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def get_news(self, ticker: str, hours_back: int = 20) -> List[Dict]:
        if not self.api_key:
            return []
        cutoff = int((datetime.utcnow() - timedelta(hours=hours_back)).timestamp())
        try:
            r = requests.get(
                f"{self.BASE}/news",
                params={
                    "token":    self.api_key,
                    "tickers":  ticker,
                    "pageSize": 10,
                    "sort":     "created:desc",
                },
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("news", [])
        except Exception as e:
            logger.debug(f"Benzinga news {ticker}: {e}")
            return []


# ─────────────────────────────────────────────────────────────────────────────
# Unusual Whales  (paid — graceful no-op if key absent)
# ─────────────────────────────────────────────────────────────────────────────
class UnusualWhalesClient:
    BASE = "https://phx.unusualwhales.com/api"

    def __init__(self, api_key: str = ""):
        self.api_key = api_key

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}"}

    def get_flow(self, ticker: str) -> List[Dict]:
        if not self.api_key:
            return []
        try:
            r = requests.get(
                f"{self.BASE}/stock/{ticker}/options-flow",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            return r.json().get("data", [])
        except Exception as e:
            logger.debug(f"UW flow {ticker}: {e}")
            return []

    def get_iv_rank(self, ticker: str) -> Optional[float]:
        if not self.api_key:
            return None
        try:
            r = requests.get(
                f"{self.BASE}/stock/{ticker}/iv-rank",
                headers=self._headers(),
                timeout=10,
            )
            r.raise_for_status()
            d = r.json().get("data", {})
            return float(d.get("iv_rank", 0)) * 100
        except Exception:
            return None
