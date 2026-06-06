"""
core/fmp_client.py — FMP stable API wrapper (post-Aug 2025)

Key findings from diagnose.py output:
  - Batch (comma-separated symbols) returns empty on Starter/Premium plans
    → use single-symbol calls; screener parallelises with ThreadPoolExecutor
  - Technical indicators: /stable/technical-indicators/{type}
    params: symbol, periodLength, timeframe=1day  (NOT /daily sub-path)
  - Earnings: /stable/earnings  (not earnings-surprises)
  - Grades/upgrades: /stable/grades  (not upgrades-downgrades)
  - Extended-hours: /stable/aftermarket-quote + aftermarket-trade (replaces pre-post-market 404)
  - Auth: ?apikey= query param only. No Authorization: Bearer header.
"""

import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)

STABLE_BASE = "https://financialmodelingprep.com/stable"


class FMPClient:
    def __init__(self, api_key: str, rate_limit_pause: float = 0.12):
        self.api_key = api_key
        self.pause   = rate_limit_pause
        self.session = requests.Session()
        # Auth: apikey as query param only. FMP explicitly rejects Authorization: Bearer.
        self.session.headers.update({"User-Agent": "premarket-screener/2.0"})

    # ── helpers ────────────────────────────────────────────────────────────
    def _get(self, path: str, params: Optional[Dict] = None) -> Any:
        params = params or {}
        params["apikey"] = self.api_key
        url = f"{STABLE_BASE}/{path}"
        try:
            r = self.session.get(url, params=params, timeout=15)

            if r.status_code == 401:
                logger.error(
                    "FMP 401 Unauthorized [%s].\n"
                    "  Plan upgrades generate a NEW key — old key is now invalid.\n"
                    "  Fix: https://site.financialmodelingprep.com → Dashboard → API Keys\n"
                    "  Copy the active key → update FMP_API_KEY in config.py",
                    path,
                )
                return []
            if r.status_code == 403:
                body = r.text[:100]
                if "allowlist" in body.lower():
                    logger.debug("FMP 403 IP allowlist [%s] — yfinance fallback active", path)
                else:
                    logger.debug("FMP 403 plan restriction [%s]: %s", path, body)
                return []
            if r.status_code == 404:
                logger.debug("FMP 404 endpoint not found [%s] — check path", path)
                return []

            r.raise_for_status()
            time.sleep(self.pause)
            data = r.json()
            if isinstance(data, dict) and ("Error Message" in data or "error" in data):
                msg = data.get("Error Message") or data.get("error", "")
                logger.debug("FMP API error [%s]: %s", path, msg[:100])
                return []
            return data

        except requests.HTTPError as e:
            logger.debug("FMP HTTP error [%s]: %s", path, str(e)[:80])
            return []
        except Exception as e:
            logger.debug("FMP request failed [%s]: %s", path, str(e)[:80])
            return []

    # ── validation ─────────────────────────────────────────────────────────
    def validate_key(self) -> bool:
        """Validate key and diagnose failure mode with actionable instructions."""
        try:
            r = requests.get(
                f"{STABLE_BASE}/profile",
                params={"symbol": "AAPL", "apikey": self.api_key},
                headers={"User-Agent": "premarket-screener/2.0"},
                timeout=10,
            )
        except Exception as e:
            logger.error("FMP network error: %s", e)
            return False

        if r.status_code == 200:
            try:
                data = r.json()
                if isinstance(data, list) and data and data[0].get("symbol"):
                    logger.info("✓ FMP key valid — data flowing")
                    return True
                # Empty list = batch not supported on this plan (that's OK — single works)
                if isinstance(data, list) and len(data) == 0:
                    logger.info("✓ FMP key valid (plan uses single-symbol calls)")
                    return True
            except Exception:
                pass
            logger.error("FMP returned 200 but unexpected payload: %s", r.text[:200])
            return False

        if r.status_code == 401:
            logger.error(
                "✗ FMP 401 — key rejected.\n\n"
                "  Most likely: plan upgrade issued a NEW API key.\n"
                "  1. Log in → https://site.financialmodelingprep.com\n"
                "  2. Dashboard → API Keys → copy the ACTIVE key\n"
                "  3. Set in config.py:  FMP_API_KEY = 'new_key'\n"
                "     or: export FMP_API_KEY='new_key'\n"
                "  4. Re-run: python main.py --dry-run\n"
            )
            return False

        if r.status_code == 403:
            body = r.text[:120]
            if "allowlist" in body.lower():
                logger.warning(
                    "⚠  FMP 403 — IP not in allowlist.\n"
                    "  Key is valid. Plan restricts calls to whitelisted IPs.\n"
                    "  Screener will use yfinance as fallback automatically.\n"
                    "  To remove: Dashboard → API Settings → clear IP whitelist."
                )
                return True
            logger.warning("⚠  FMP 403 plan restriction: %s", body)
            return True

        logger.error("FMP HTTP %s: %s", r.status_code, r.text[:200])
        return False

    # ── universe ────────────────────────────────────────────────────────────
    def get_sp500_constituents(self) -> List[str]:
        data = self._get("sp500-constituent")
        tickers = [d["symbol"] for d in data if isinstance(d, dict) and "symbol" in d]
        if tickers:
            logger.info("SP500 from FMP: %d tickers", len(tickers))
            return tickers
        return _sp500_fallback()

    def get_nasdaq100_constituents(self) -> List[str]:
        data = self._get("nasdaq-constituent")
        tickers = [d["symbol"] for d in data if isinstance(d, dict) and "symbol" in d]
        if tickers:
            return tickers
        return _nasdaq100_fallback()

    def screen_universe(self, min_price: float, max_price: float,
                        min_volume: int) -> List[str]:
        data = self._get("company-screener", {
            "priceMoreThan": min_price, "priceLessThan": max_price,
            "volumeMoreThan": min_volume, "isEtf": "false",
            "isActivelyTrading": "true", "country": "US", "limit": 500,
        })
        tickers = [d["symbol"] for d in data if isinstance(d, dict) and "symbol" in d]
        return tickers if tickers else _sp500_fallback()

    def get_exchange_tickers(self, exchange: str,
                             min_price: float = 1.0,
                             min_volume: int = 100_000) -> List[str]:
        """
        Fetch tickers listed on a specific exchange via FMP company-screener.
        exchange: "NYSE" | "AMEX" (NYSE American) | "NASDAQ"

        Endpoint: /stable/company-screener?exchange=NYSE&isEtf=false&limit=3000
        Returns empty list if exchange filtering not on plan — caller handles fallback.
        """
        data = self._get("company-screener", {
            "exchange":          exchange,
            "priceMoreThan":     min_price,
            "volumeMoreThan":    min_volume,
            "isEtf":             "false",
            "isActivelyTrading": "true",
            "limit":             3000,
        })
        if not isinstance(data, list):
            return []
        tickers = [d["symbol"] for d in data if isinstance(d, dict) and "symbol" in d]
        logger.info("FMP exchange-screener [%s]: %d tickers", exchange, len(tickers))
        return tickers

    # ── quotes ─────────────────────────────────────────────────────────────
    def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Dict]:
        """
        Fetch quotes for multiple tickers.
        Tries comma-batch first; falls back to parallel single calls if batch
        returns empty (Starter/Premium plans don't support batch).
        """
        if not tickers:
            return {}

        # Try batch first (works on Ultimate plan)
        chunk = ",".join(tickers[:50])
        data  = self._get("quote", {"symbol": chunk})
        if isinstance(data, list) and data:
            result = {q["symbol"]: q for q in data if isinstance(q, dict) and "symbol" in q}
            if len(result) >= max(1, len(tickers) // 2):
                return result  # batch worked

        # Batch returned empty — fall back to parallel single calls
        logger.debug("FMP batch empty — using parallel single-symbol quote calls")
        return self._parallel_single(tickers, self._get_single_quote)

    def _get_single_quote(self, ticker: str) -> Optional[Dict]:
        data = self._get("quote", {"symbol": ticker})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and "symbol" in data:
            return data
        return None

    def _parallel_single(self, tickers: List[str],
                         fn, max_workers: int = 10) -> Dict[str, Dict]:
        """Run fn(ticker) in parallel and collect results."""
        results: Dict[str, Dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = {ex.submit(fn, t): t for t in tickers}
            for fut in as_completed(futures):
                ticker = futures[fut]
                try:
                    res = fut.result()
                    if res and isinstance(res, dict):
                        sym = res.get("symbol", ticker)
                        results[sym] = res
                except Exception as e:
                    logger.debug("Parallel call failed %s: %s", ticker, e)
        return results

    # ── extended-hours quotes ──────────────────────────────────────────────
    def get_aftermarket_quote(self, ticker: str) -> Optional[Dict]:
        """
        Pre-market and after-hours bid/ask quote.
        Endpoint: /stable/aftermarket-quote?symbol=AAPL
        Correct path for extended-hours quotes (replaces the non-existent pre-post-market).

        Response fields (from FMP docs):
          symbol, ask, bid, asize, bsize, timestamp

        Used by the screener to:
          • Confirm pre-market activity (bid > 0 means active pre-market)
          • Measure bid-ask spread (wide spread = low liquidity, reduces conviction)
          • Cross-check preMarketPrice from /quote for accuracy
        """
        data = self._get("aftermarket-quote", {"symbol": ticker})
        if isinstance(data, list) and data:
            row = data[0]
            return {
                "symbol":    row.get("symbol", ticker),
                "ask":       float(row.get("ask") or 0),
                "bid":       float(row.get("bid") or 0),
                "asize":     int(row.get("asize") or 0),   # ask size (shares)
                "bsize":     int(row.get("bsize") or 0),   # bid size (shares)
                "timestamp": row.get("timestamp", ""),
                "spread":    round(
                    float(row.get("ask") or 0) - float(row.get("bid") or 0), 4
                ),
                "spread_pct": round(
                    (float(row.get("ask") or 0) - float(row.get("bid") or 0))
                    / max(float(row.get("bid") or 1), 0.01) * 100, 3
                ),
            }
        if isinstance(data, dict) and "symbol" in data:
            return data
        return None

    def get_aftermarket_trade(self, ticker: str) -> Optional[Dict]:
        """
        Last aftermarket trade price, size, and timestamp.
        Endpoint: /stable/aftermarket-trade?symbol=AAPL

        Response fields (from FMP docs):
          symbol, price, size, timestamp

        Used as a secondary signal when aftermarket-quote bid/ask is absent
        (some brokers only report trades, not quotes, during extended hours).
        """
        data = self._get("aftermarket-trade", {"symbol": ticker})
        if isinstance(data, list) and data:
            row = data[0]
            return {
                "symbol":    row.get("symbol", ticker),
                "price":     float(row.get("price") or 0),
                "size":      int(row.get("size") or 0),
                "timestamp": row.get("timestamp", ""),
            }
        return None

    # ── historical OHLCV ────────────────────────────────────────────────────
    def get_daily_ohlcv(self, ticker: str, limit: int = 60) -> List[Dict]:
        data = self._get("historical-price-eod/full",
                         {"symbol": ticker, "limit": limit})
        if isinstance(data, dict) and "historical" in data:
            return data["historical"]
        if isinstance(data, list):
            return data
        return []

    # ── technical indicators ────────────────────────────────────────────────
    def get_sma(self, ticker: str, period: int = 50, limit: int = 1) -> Optional[float]:
        """
        Correct stable endpoint:
          /stable/technical-indicators/sma?symbol=AAPL&periodLength=50&timeframe=1day
        Note: param is 'periodLength' (not 'period'), path uses indicator type directly.
        """
        data = self._get(f"technical-indicators/sma", {
            "symbol":       ticker,
            "periodLength": period,
            "timeframe":    "1day",
        })
        if isinstance(data, list) and data:
            row = data[0]
            val = row.get("sma") or row.get("value") or row.get("SMA")
            if val is not None:
                return round(float(val), 4)
        return None

    # ── earnings ────────────────────────────────────────────────────────────
    def get_earnings_surprise(self, ticker: str, limit: int = 1) -> List[Dict]:
        """
        Correct stable path: /stable/earnings?symbol=AAPL
        Returns fields: date, symbol, eps, epsEstimated, revenue, revenueEstimated
        Normalises to the same keys the signal layer expects.
        """
        data = self._get("earnings", {"symbol": ticker, "limit": limit})
        if not isinstance(data, list):
            return []
        normalised = []
        for row in data:
            if not isinstance(row, dict):
                continue
            normalised.append({
                "actualEarningResult": row.get("eps") or row.get("actualEarningResult"),
                "estimatedEarning":    row.get("epsEstimated") or row.get("estimatedEarning"),
                "date":                row.get("date", ""),
                "symbol":              row.get("symbol", ticker),
            })
        return normalised

    # ── press releases ──────────────────────────────────────────────────────
    def get_press_releases(self, ticker: str, limit: int = 5) -> List[Dict]:
        """
        Correct stable path: /stable/news/press-releases?symbol=AAPL
        """
        data = self._get("news/press-releases", {"symbol": ticker, "limit": limit})
        return data if isinstance(data, list) else []

    # ── stock news feed ─────────────────────────────────────────────────────
    def get_stock_news_latest(self, limit: int = 50, page: int = 0) -> List[Dict]:
        """
        Global latest stock news feed.
        Endpoint: /stable/news/stock-latest?page=0&limit=50
        Response fields per article:
          symbol, publishedDate, publisher, title, text, url, site, image

        Used for:
          • Pre-market catalyst scan — bulk pull once, filter by ticker in-process
          • Avoids N individual ticker calls; one call covers entire watchlist
        """
        data = self._get("news/stock-latest", {"page": page, "limit": limit})
        if not isinstance(data, list):
            return []
        # Normalise: drop articles with null symbol, ensure all fields present
        cleaned = []
        for item in data:
            if not isinstance(item, dict):
                continue
            cleaned.append({
                "symbol":        (item.get("symbol") or "").upper().strip(),
                "publishedDate": item.get("publishedDate", ""),
                "publisher":     item.get("publisher", ""),
                "title":         item.get("title", ""),
                "text":          item.get("text", ""),
                "url":           item.get("url", ""),
                "site":          item.get("site", ""),
            })
        return cleaned

    def get_stock_news_for_ticker(self, ticker: str, limit: int = 10) -> List[Dict]:
        """
        Per-ticker news: /stable/news/stock-latest?symbols=AAPL&limit=10
        Used as fallback when the bulk feed doesn't contain articles for a ticker
        (e.g. very recent news published after the bulk pull).
        """
        data = self._get("news/stock-latest", {"symbols": ticker, "limit": limit})
        if not isinstance(data, list):
            return []
        return [
            {
                "symbol":        (item.get("symbol") or ticker).upper(),
                "publishedDate": item.get("publishedDate", ""),
                "publisher":     item.get("publisher", ""),
                "title":         item.get("title", ""),
                "text":          item.get("text", ""),
                "url":           item.get("url", ""),
                "site":          item.get("site", ""),
            }
            for item in data if isinstance(item, dict)
        ]

    # ── analyst grades / upgrades ────────────────────────────────────────────
    def get_analyst_upgrades(self, from_date: str, to_date: str) -> List[Dict]:
        """
        Correct stable path: /stable/grades?symbol=... (per-symbol)
        Bulk date-range not available on most plans — return empty and rely on
        per-ticker calls in score_ticker instead.
        """
        return []

    def get_grades_for_ticker(self, ticker: str, limit: int = 5) -> List[Dict]:
        """Per-ticker analyst grades: /stable/grades?symbol=AAPL"""
        data = self._get("grades", {"symbol": ticker, "limit": limit})
        return data if isinstance(data, list) else []

    def get_analyst_estimates(self, ticker: str,
                              period: str = "quarter",
                              limit: int = 4) -> List[Dict]:
        """
        Analyst consensus estimates for a ticker.
        Endpoint: /stable/analyst-estimates?symbol=AAPL&period=quarter&page=0&limit=4

        period: "annual" | "quarter"
        Returns list sorted newest-first with fields (all confirmed from live response):
          symbol, date,
          revenueLow, revenueHigh, revenueAvg,   numAnalystsRevenue
          ebitdaLow,  ebitdaHigh,  ebitdaAvg,
          ebitLow,    ebitHigh,    ebitAvg,
          netIncomeLow, netIncomeHigh, netIncomeAvg,
          sgaExpenseLow, sgaExpenseHigh, sgaExpenseAvg,
          epsLow, epsHigh, epsAvg,                numAnalystsEps

        Screener uses the nearest upcoming period to build L1 forward signals:
          • epsAvg  → consensus EPS target
          • epsHigh - epsLow → analyst dispersion (wide range = uncertainty)
          • numAnalystsEps   → conviction (more analysts = stronger signal)
          • revenueAvg       → revenue growth expectation
        """
        data = self._get("analyst-estimates", {
            "symbol": ticker,
            "period": period,
            "page":   0,
            "limit":  limit,
        })
        if not isinstance(data, list):
            return []

        # Normalise — ensure every numeric field is float/int, never None
        normalised = []
        for row in data:
            if not isinstance(row, dict):
                continue
            def _f(key):   return float(row.get(key) or 0)
            def _i(key):   return int(row.get(key) or 0)
            normalised.append({
                "symbol":              row.get("symbol", ticker),
                "date":                row.get("date", ""),
                # EPS
                "epsAvg":              _f("epsAvg"),
                "epsHigh":             _f("epsHigh"),
                "epsLow":              _f("epsLow"),
                "numAnalystsEps":      _i("numAnalystsEps"),
                # Revenue
                "revenueAvg":          _f("revenueAvg"),
                "revenueHigh":         _f("revenueHigh"),
                "revenueLow":          _f("revenueLow"),
                "numAnalystsRevenue":  _i("numAnalystsRevenue"),
                # EBITDA
                "ebitdaAvg":           _f("ebitdaAvg"),
                "ebitdaHigh":          _f("ebitdaHigh"),
                "ebitdaLow":           _f("ebitdaLow"),
                # Net income
                "netIncomeAvg":        _f("netIncomeAvg"),
                "netIncomeHigh":       _f("netIncomeHigh"),
                "netIncomeLow":        _f("netIncomeLow"),
                # SG&A
                "sgaExpenseAvg":       _f("sgaExpenseAvg"),
                "sgaExpenseHigh":      _f("sgaExpenseHigh"),
                "sgaExpenseLow":       _f("sgaExpenseLow"),
            })
        return normalised

    # ── earnings calendar ────────────────────────────────────────────────────
    def get_earnings_calendar(self, from_date: str, to_date: str) -> List[Dict]:
        return self._get("earnings-calendar", {"from": from_date, "to": to_date})

    # ── company profile ─────────────────────────────────────────────────────
    def get_profiles_batch(self, tickers: List[str]) -> Dict[str, Dict]:
        """
        Batch profile — works on Ultimate plan.
        Falls back to parallel single calls for Starter/Premium.
        """
        if not tickers:
            return {}
        chunk = ",".join(tickers[:50])
        data  = self._get("profile", {"symbol": chunk})
        if isinstance(data, list) and data:
            result = {p["symbol"]: p for p in data if isinstance(p, dict) and "symbol" in p}
            if len(result) >= max(1, len(tickers) // 2):
                return result
        logger.debug("FMP profile batch empty — using parallel single calls")
        return self._parallel_single(tickers, self._get_single_profile)

    def _get_single_profile(self, ticker: str) -> Optional[Dict]:
        data = self._get("profile", {"symbol": ticker})
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and "symbol" in data:
            return data
        return None

    # ── options IV (premium) ─────────────────────────────────────────────────
    def get_options_iv(self, ticker: str) -> Optional[Dict]:
        data = self._get("stock/implied-volatility", {"symbol": ticker})
        if isinstance(data, list) and data:
            return data[0]
        return None


# ── Free universe fallbacks ────────────────────────────────────────────────

def _sp500_fallback() -> List[str]:
    tickers = _sp500_via_github()
    if tickers:
        return tickers
    tickers = _sp500_from_wikipedia()
    if tickers:
        return tickers
    logger.warning("All SP500 sources failed — using hardcoded top-50")
    return list(_TOP50_FALLBACK)

def _nasdaq100_fallback() -> List[str]:
    try:
        import pandas as pd
        tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100",
                               attrs={"id": "constituents"})
        return [str(t) for t in tables[0]["Ticker"].tolist()]
    except Exception:
        return _sp500_fallback()

def _sp500_via_github() -> List[str]:
    try:
        import requests as req
        r = req.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
            "/main/data/constituents.csv", timeout=10)
        if r.ok:
            lines   = r.text.strip().split("\n")[1:]
            tickers = [l.split(",")[0].strip().replace(".", "-") for l in lines if l]
            if len(tickers) > 400:
                logger.info("SP500 from GitHub CSV: %d", len(tickers))
                return tickers
    except Exception:
        pass
    return []

def _sp500_from_wikipedia() -> List[str]:
    try:
        import pandas as pd
        tables = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"})
        tickers = [str(t).replace(".", "-") for t in tables[0]["Symbol"].tolist()]
        logger.info("SP500 from Wikipedia: %d", len(tickers))
        return tickers
    except Exception:
        return []

_TOP50_FALLBACK = [
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","GOOG","TSLA","BRK-B","AVGO",
    "JPM","LLY","V","UNH","XOM","MA","JNJ","PG","COST","HD",
    "WMT","ABBV","MRK","CVX","ORCL","CRM","NFLX","BAC","KO","PEP",
    "TMO","CSCO","ACN","AMD","MCD","ABT","ADBE","WFC","LIN","DHR",
    "TXN","NEE","PM","INTU","AMGN","RTX","QCOM","SPGI","LOW","CAT",
]
