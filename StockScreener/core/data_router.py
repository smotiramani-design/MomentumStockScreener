"""
core/data_router.py — Unified data access layer.

Priority:
  1. FMP stable endpoints  (fastest, most complete)
  2. yfinance              (free, no IP restrictions, slower)

All public methods return the same dict structure regardless of source,
so the screener/signal layers are completely decoupled from the data source.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class DataRouter:
    """
    Wraps FMPClient + YFinanceClient and routes each call to the
    first available source that returns real data.
    """

    def __init__(self, fmp_client, yf_client):
        self.fmp = fmp_client
        self.yf  = yf_client
        self._fmp_ok: Optional[bool] = None   # cached after first real call

    def _fmp_alive(self) -> bool:
        """Lazy-check whether FMP is returning real data (not blocked)."""
        if self._fmp_ok is None:
            test = self.fmp.get_profiles_batch(["AAPL"])
            self._fmp_ok = bool(test)
            src = "FMP" if self._fmp_ok else "yfinance (FMP unavailable)"
            logger.info("Data source: %s", src)
        return self._fmp_ok

    # ── Universe ─────────────────────────────────────────────────────────────
    def get_sp500(self) -> List[str]:
        if self._fmp_alive():
            t = self.fmp.get_sp500_constituents()
            if t:
                return t
        return _yf_sp500()

    def get_nasdaq100(self) -> List[str]:
        if self._fmp_alive():
            t = self.fmp.get_nasdaq100_constituents()
            if t:
                return t
        return _yf_nasdaq100()

    def get_russell3000(self, min_price: float = 3.0,
                        min_volume: int = 200_000) -> List[str]:
        """
        Russell 3000 universe — ~3,000 US large/mid/small cap stocks.

        Source priority:
          1. FMP company-screener (marketCapMoreThan=$300M covers R3000 well)
          2. iShares IWV ETF holdings CSV (official Russell 3000 ETF)
          3. NASDAQ trader otherlisted.txt + SP500 + Nasdaq100 combined
          4. SEC EDGAR company_tickers_exchange.json filtered by NYSE/Nasdaq
        """
        logger.info("Building Russell 3000 universe…")

        # 1. FMP screener — marketCap > $300M catches large + mid + small cap
        if self._fmp_alive():
            tickers = self.fmp.screen_universe(min_price, 10_000, min_volume)
            if len(tickers) >= 500:
                logger.info("Russell 3000 from FMP screener: %d tickers", len(tickers))
                return _dedupe(tickers)

        # 2. iShares IWV (official Russell 3000 ETF) holdings
        tickers = _ishares_etf_holdings("IWV")
        if len(tickers) >= 500:
            logger.info("Russell 3000 from iShares IWV: %d tickers", len(tickers))
            return _dedupe(tickers)

        # 3. NASDAQ trader files — combined NYSE + AMEX + NASDAQ = ~7,000
        #    then filter to liquid subset approximating Russell 3000
        tickers = _nasdaq_trader_all()
        if len(tickers) >= 1000:
            logger.info("Russell 3000 via NASDAQ trader files: %d tickers (pre-filter)", len(tickers))
            return _dedupe(tickers)

        # 4. SEC EDGAR — all listed US companies with exchange field
        tickers = _sec_edgar_tickers(exchanges={"NYSE", "Nasdaq"})
        if tickers:
            logger.info("Russell 3000 via SEC EDGAR: %d tickers", len(tickers))
            return _dedupe(tickers)

        # 5. Fallback: SP500 + Nasdaq100 (503 + 101 = ~560, deduplicated)
        logger.warning("Russell 3000 sources failed — using SP500+Nasdaq100 (~560 tickers)")
        return _dedupe(_yf_sp500() + _yf_nasdaq100())

    def get_nyse(self, min_price: float = 1.0,
                 min_volume: int = 100_000) -> List[str]:
        """
        NYSE + NYSE American (AMEX) universe — ~3,500 stocks.

        NYSE:          Large/mid cap traditional exchange (exchange code N)
        NYSE American: Small-cap / growth companies (exchange code A, formerly AMEX)

        Source priority:
          1. FMP company-screener with exchange=NYSE then exchange=AMEX
          2. NASDAQ trader otherlisted.txt (official daily file, free)
          3. SEC EDGAR company_tickers_exchange.json filtered by NYSE + AMEX
        """
        logger.info("Building NYSE + NYSE American universe…")

        # 1. FMP screener filtered by exchange
        all_tickers: List[str] = []
        if self._fmp_alive():
            for exchange in ["NYSE", "AMEX"]:
                t = self.fmp.get_exchange_tickers(exchange, min_price, min_volume)
                all_tickers.extend(t)
                logger.info("  FMP %s: %d tickers", exchange, len(t))
            if len(all_tickers) >= 200:
                return _dedupe(all_tickers)

        # 2. NASDAQ trader otherlisted.txt — Exchange col N=NYSE, A=NYSE American
        tickers = _nasdaq_trader_nyse_amex()
        if len(tickers) >= 200:
            logger.info("NYSE universe from NASDAQ trader files: %d tickers", len(tickers))
            return _dedupe(tickers)

        # 3. SEC EDGAR
        tickers = _sec_edgar_tickers(exchanges={"NYSE", "AMEX"})
        if tickers:
            logger.info("NYSE universe via SEC EDGAR: %d tickers", len(tickers))
            return _dedupe(tickers)

        # 4. Fallback: SP500 (majority are NYSE-listed)
        logger.warning("NYSE universe sources failed — using SP500 as proxy")
        return _yf_sp500()

    # ── Batch quotes ──────────────────────────────────────────────────────────
    def get_batch_quotes(self, tickers: List[str]) -> Dict[str, Dict]:
        """
        Returns dict of symbol → normalised quote dict with keys:
          symbol, price, previousClose, changesPercentage,
          preMarketPrice, preMarketChangePercent, preMarketVolume,
          volume, avgVolume
        """
        if self._fmp_alive():
            raw = self.fmp.get_batch_quotes(tickers)
            if raw:
                return raw
        logger.info("FMP quotes unavailable — using yfinance for %d tickers", len(tickers))
        return _yf_batch_quotes(tickers)

    # ── Extended-hours quote (aftermarket-quote / aftermarket-trade) ─────────
    def get_aftermarket_quote(self, ticker: str) -> Optional[Dict]:
        """
        Bid/ask quote during pre-market and after-hours.
        FMP: /stable/aftermarket-quote?symbol=AAPL
        Fields: symbol, ask, bid, asize, bsize, timestamp, spread, spread_pct

        Returns None if outside extended-hours or endpoint not on plan.
        No yfinance fallback — bid/ask spread not available free.
        """
        if self._fmp_alive():
            return self.fmp.get_aftermarket_quote(ticker)
        return None

    # ── Profiles / sector ─────────────────────────────────────────────────────
    def get_profiles_batch(self, tickers: List[str]) -> Dict[str, Dict]:
        if self._fmp_alive():
            raw = self.fmp.get_profiles_batch(tickers)
            if raw:
                return raw
        logger.info("FMP profiles unavailable — using yfinance")
        return _yf_profiles(tickers)

    # ── Historical OHLCV ──────────────────────────────────────────────────────
    def get_daily_ohlcv(self, ticker: str, limit: int = 60) -> List[Dict]:
        if self._fmp_alive():
            data = self.fmp.get_daily_ohlcv(ticker, limit)
            if data:
                return data
        return _yf_ohlcv(ticker, limit)

    # ── SMA ────────────────────────────────────────────────────────────────────
    def get_sma(self, ticker: str, period: int, limit: int = 1) -> Optional[float]:
        if self._fmp_alive():
            val = self.fmp.get_sma(ticker, period, limit)
            if val is not None:
                return val
        return _yf_sma(ticker, period)

    # ── Earnings surprise ─────────────────────────────────────────────────────
    def get_earnings_surprise(self, ticker: str) -> List[Dict]:
        if self._fmp_alive():
            data = self.fmp.get_earnings_surprise(ticker)
            if data:
                return data
        return _yf_earnings_surprise(ticker)

    # ── Press releases ────────────────────────────────────────────────────────
    def get_press_releases(self, ticker: str) -> List[Dict]:
        if self._fmp_alive():
            return self.fmp.get_press_releases(ticker)
        return []   # no yfinance equivalent

    # ── Analyst upgrades ──────────────────────────────────────────────────────
    def get_analyst_upgrades(self, from_date: str, to_date: str) -> List[Dict]:
        if self._fmp_alive():
            return self.fmp.get_analyst_upgrades(from_date, to_date)
        return _yf_upgrades()

    # ── Analyst estimates ─────────────────────────────────────────────────────
    def get_analyst_estimates(self, ticker: str,
                              period: str = "quarter") -> List[Dict]:
        """
        Forward analyst consensus estimates.
        FMP: /stable/analyst-estimates?symbol=AAPL&period=quarter&limit=4
        yfinance fallback: Ticker.analyst_price_targets + earnings_estimate
        """
        if self._fmp_alive():
            data = self.fmp.get_analyst_estimates(ticker, period=period, limit=4)
            if data:
                return data
        return _yf_analyst_estimates(ticker)


# ── yfinance implementations ───────────────────────────────────────────────


# ── New universe helper functions ─────────────────────────────────────────────

def _dedupe(tickers: List[str]) -> List[str]:
    """Deduplicate preserving order, strip whitespace, skip empty/invalid."""
    seen = set()
    out  = []
    for t in tickers:
        t = t.strip().upper()
        if t and t not in seen and not t.startswith("$") and len(t) <= 6:
            seen.add(t)
            out.append(t)
    return out


def _ishares_etf_holdings(etf_symbol: str) -> List[str]:
    """
    Download iShares ETF holdings CSV and extract ticker symbols.
    IWV = Russell 3000, IWB = Russell 1000, IWM = Russell 2000.
    iShares CSV has ~9 header lines then pipe/comma data rows.
    """
    etf_ids = {
        "IWV": "239714/ishares-russell-3000-etf",
        "IWB": "239832/ishares-russell-1000-etf",
        "IWM": "239710/ishares-russell-2000-etf",
    }
    etf_path = etf_ids.get(etf_symbol.upper(), "239714/ishares-russell-3000-etf")
    url = (f"https://www.ishares.com/us/products/{etf_path}"
           f"/1467271812596.ajax?tab=portfolio&fileType=csv")
    try:
        import requests as req
        r = req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        if not r.ok or len(r.text) < 1000:
            return []
        tickers = []
        for line in r.text.strip().split("\n"):
            parts = line.split(",")
            sym = parts[0].strip().strip('"').upper()
            # Valid ticker: 1-6 alpha chars, not a header word
            if (sym and 1 <= len(sym) <= 6 and sym.replace("-","").isalpha()
                    and sym not in {"TICKER","FUND","NAME","ASSET","ISIN","CUSIP"}):
                tickers.append(sym)
        return tickers
    except Exception as e:
        logger.debug("iShares %s: %s", etf_symbol, e)
        return []


def _nasdaq_trader_all() -> List[str]:
    """
    NASDAQ trader symbol files — all US listed stocks, free/no auth, daily updated.
      nasdaqlisted.txt  → all NASDAQ stocks
      otherlisted.txt   → NYSE, AMEX, ARCA, BATS, IEX stocks
    Both are pipe-separated.
    """
    import requests as req
    tickers: List[str] = []
    files = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]
    for url in files:
        try:
            r = req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if not r.ok:
                continue
            for line in r.text.split("\n")[1:]:
                if "|" not in line:
                    continue
                parts      = line.split("|")
                sym        = parts[0].strip().upper()
                test_issue = parts[3].strip() if len(parts) > 3 else "Y"
                if (sym and 1 <= len(sym) <= 6
                        and sym.replace("-","").replace(".","").isalpha()
                        and test_issue != "Y"):
                    tickers.append(sym)
        except Exception as e:
            logger.debug("NASDAQ trader %s: %s", url, e)
    return tickers


def _nasdaq_trader_nyse_amex() -> List[str]:
    """
    Parse otherlisted.txt for NYSE (exchange=N) and NYSE American (exchange=A) only.
    Exchange codes: N=NYSE, A=NYSE American, P=NYSE Arca, Z=BATS, V=IEX
    """
    import requests as req
    url  = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
    out: List[str] = []
    try:
        r = req.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if not r.ok:
            return []
        # Format: ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot|Test Issue|NASDAQ Symbol
        for line in r.text.split("\n")[1:]:
            if "|" not in line:
                continue
            parts      = line.split("|")
            if len(parts) < 7:
                continue
            sym        = parts[0].strip().upper()
            exchange   = parts[2].strip().upper()   # N or A
            test_issue = parts[6].strip()
            if (sym and 1 <= len(sym) <= 6
                    and exchange in {"N", "A"}
                    and test_issue != "Y"):
                out.append(sym)
        logger.debug("NASDAQ trader otherlisted → %d NYSE/AMEX tickers", len(out))
    except Exception as e:
        logger.debug("NASDAQ trader otherlisted: %s", e)
    return out


def _sec_edgar_tickers(exchanges: set = None) -> List[str]:
    """
    SEC EDGAR company_tickers_exchange.json — all SEC-registered listed companies.
    Free, no auth, covers NYSE + Nasdaq + AMEX + OTC.
    exchange values: "NYSE", "Nasdaq", "AMEX", "CBOE", "OTC"
    """
    import requests as req
    if exchanges is None:
        exchanges = {"NYSE", "Nasdaq"}
    url = "https://www.sec.gov/files/company_tickers_exchange.json"
    try:
        r = req.get(url,
                    headers={"User-Agent": "premarket-screener contact@example.com"},
                    timeout=20)
        if not r.ok:
            return []
        data    = r.json()
        tickers = []
        for rec in data.values():
            if not isinstance(rec, dict):
                continue
            ex  = rec.get("exchange", "")
            sym = rec.get("ticker",   "").upper().strip()
            if ex in exchanges and sym and 1 <= len(sym) <= 6:
                tickers.append(sym)
        logger.debug("SEC EDGAR → %d tickers (%s)", len(tickers), exchanges)
        return tickers
    except Exception as e:
        logger.debug("SEC EDGAR tickers: %s", e)
        return []


def _yf_sp500() -> List[str]:
    """GitHub CSV → Wikipedia → hardcoded."""
    try:
        import requests as req
        r = req.get(
            "https://raw.githubusercontent.com/datasets/s-and-p-500-companies"
            "/main/data/constituents.csv",
            timeout=10,
        )
        if r.ok:
            lines   = r.text.strip().split("\n")[1:]
            tickers = [l.split(",")[0].strip().replace(".", "-") for l in lines if l]
            if len(tickers) > 400:
                logger.info("SP500 via GitHub CSV: %d", len(tickers))
                return tickers
    except Exception:
        pass
    try:
        import pandas as pd
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            attrs={"id": "constituents"},
        )[0]
        tickers = [str(t).replace(".", "-") for t in df["Symbol"].tolist()]
        logger.info("SP500 via Wikipedia: %d", len(tickers))
        return tickers
    except Exception:
        pass
    from core.fmp_client import _TOP50_FALLBACK
    logger.warning("Using hardcoded top-50 fallback")
    return list(_TOP50_FALLBACK)


def _yf_nasdaq100() -> List[str]:
    try:
        import pandas as pd
        df = pd.read_html(
            "https://en.wikipedia.org/wiki/Nasdaq-100",
            attrs={"id": "constituents"},
        )[0]
        return [str(t) for t in df["Ticker"].tolist()]
    except Exception:
        return _yf_sp500()


def _yf_batch_quotes(tickers: List[str]) -> Dict[str, Dict]:
    """
    Use yfinance.download for price/volume, fast_info for pre-market.
    Returns normalised quote dicts compatible with the screener.
    """
    try:
        import yfinance as yf
        import pandas as pd

        result: Dict[str, Dict] = {}

        # Bulk download — fastest yfinance method for OHLCV
        chunk_size = 50
        for i in range(0, len(tickers), chunk_size):
            chunk = tickers[i : i + chunk_size]
            try:
                data = yf.download(
                    chunk, period="2d", interval="1d",
                    auto_adjust=True, progress=False, threads=True,
                )
                # yf.download multi-ticker returns MultiIndex columns
                if isinstance(data.columns, pd.MultiIndex):
                    close_df  = data["Close"]
                    volume_df = data["Volume"]
                    for sym in chunk:
                        if sym not in close_df.columns:
                            continue
                        closes  = close_df[sym].dropna()
                        volumes = volume_df[sym].dropna()
                        if len(closes) < 1:
                            continue
                        price      = float(closes.iloc[-1])
                        prev_close = float(closes.iloc[-2]) if len(closes) > 1 else price
                        chg_pct    = (price - prev_close) / prev_close * 100 if prev_close else 0
                        avg_vol    = float(volumes.mean()) if len(volumes) > 0 else 0
                        result[sym] = {
                            "symbol":               sym,
                            "price":                round(price, 2),
                            "previousClose":        round(prev_close, 2),
                            "changesPercentage":    round(chg_pct, 4),
                            "preMarketPrice":       None,
                            "preMarketChangePercent": None,
                            "preMarketVolume":      None,
                            "volume":               int(volumes.iloc[-1]) if len(volumes) > 0 else 0,
                            "avgVolume":            int(avg_vol),
                            "companyName":          sym,
                        }
                else:
                    # Single ticker — flat columns
                    sym = chunk[0]
                    closes  = data["Close"].dropna()
                    volumes = data["Volume"].dropna()
                    if len(closes) >= 1:
                        price      = float(closes.iloc[-1])
                        prev_close = float(closes.iloc[-2]) if len(closes) > 1 else price
                        chg_pct    = (price - prev_close) / prev_close * 100 if prev_close else 0
                        result[sym] = {
                            "symbol": sym, "price": round(price, 2),
                            "previousClose": round(prev_close, 2),
                            "changesPercentage": round(chg_pct, 4),
                            "preMarketPrice": None, "preMarketChangePercent": None,
                            "preMarketVolume": None,
                            "volume": int(volumes.iloc[-1]) if len(volumes) > 0 else 0,
                            "avgVolume": int(volumes.mean()) if len(volumes) > 0 else 0,
                        }
            except Exception as e:
                logger.debug("yf download chunk failed: %s", e)

        # Enrich pre-market data via fast_info (individual calls, best-effort)
        _enrich_premarket(result)

        logger.info("yfinance batch quotes: %d/%d tickers", len(result), len(tickers))
        return result

    except Exception as e:
        logger.error("yf_batch_quotes failed: %s", e)
        return {}


def _enrich_premarket(result: Dict[str, Dict]):
    """
    Try to add pre-market price/change for each ticker via fast_info.
    Silently skips on failure — not all tickers have pre-market data.
    """
    import yfinance as yf
    for sym, q in result.items():
        try:
            fi = yf.Ticker(sym).fast_info
            pm_price = getattr(fi, "pre_market_price", None)
            pm_chg   = getattr(fi, "pre_market_change_percent", None)
            pm_vol   = getattr(fi, "pre_market_volume", None)
            if pm_price:
                q["preMarketPrice"]          = round(float(pm_price), 2)
                q["preMarketChangePercent"]  = round(float(pm_chg or 0), 4)
                q["preMarketVolume"]         = int(pm_vol or 0)
        except Exception:
            pass


def _yf_profiles(tickers: List[str]) -> Dict[str, Dict]:
    """Fetch sector/name via yfinance info (slower, but works)."""
    import yfinance as yf
    result: Dict[str, Dict] = {}
    for sym in tickers:
        try:
            info = yf.Ticker(sym).info
            result[sym] = {
                "symbol":      sym,
                "companyName": info.get("longName") or info.get("shortName") or sym,
                "sector":      info.get("sector") or "Unknown",
                "industry":    info.get("industry") or "",
                "mktCap":      info.get("marketCap") or 0,
            }
        except Exception:
            result[sym] = {"symbol": sym, "companyName": sym, "sector": "Unknown"}
    return result


def _yf_ohlcv(ticker: str, limit: int = 60) -> List[Dict]:
    """Return [{date, open, high, low, close, volume}, ...] newest first."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="3mo", auto_adjust=True)
        rows = []
        for dt, row in hist.iloc[::-1].iterrows():
            rows.append({
                "date":   str(dt.date()),
                "open":   round(float(row["Open"]), 4),
                "high":   round(float(row["High"]), 4),
                "low":    round(float(row["Low"]), 4),
                "close":  round(float(row["Close"]), 4),
                "volume": int(row["Volume"]),
            })
            if len(rows) >= limit:
                break
        return rows
    except Exception as e:
        logger.debug("yf ohlcv %s: %s", ticker, e)
        return []


def _yf_sma(ticker: str, period: int) -> Optional[float]:
    """Compute SMA from yfinance history."""
    try:
        import yfinance as yf
        hist = yf.Ticker(ticker).history(period="1y", auto_adjust=True)
        closes = hist["Close"].dropna()
        if len(closes) >= period:
            return round(float(closes.iloc[-period:].mean()), 4)
    except Exception:
        pass
    return None


def _yf_earnings_surprise(ticker: str) -> List[Dict]:
    """Map yfinance earnings to FMP-compatible format."""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).earnings_history
        if cal is None or cal.empty:
            return []
        row = cal.iloc[0]
        return [{
            "actualEarningResult": row.get("Reported EPS") or row.get("epsActual"),
            "estimatedEarning":    row.get("EPS Estimate") or row.get("epsEstimate"),
        }]
    except Exception:
        return []


def _yf_upgrades() -> List[Dict]:
    """yfinance doesn't provide bulk upgrades — return empty."""
    return []


def _yf_analyst_estimates(ticker: str) -> List[Dict]:
    """
    Build an FMP-compatible analyst estimates dict from yfinance.
    yfinance provides:
      Ticker.analyst_price_targets  → dict with 'mean', 'high', 'low', etc.
      Ticker.earnings_estimate       → DataFrame with rows 'avg', 'high', 'low',
                                       'numberOfAnalysts' indexed by period (0q, +1q…)
      Ticker.revenue_estimate        → same structure for revenue
    Maps to the same keys the signal layer expects from FMP.
    """
    try:
        import yfinance as yf
        t    = yf.Ticker(ticker)
        eps_est = getattr(t, "earnings_estimate", None)
        rev_est = getattr(t, "revenue_estimate", None)

        if eps_est is None or eps_est.empty:
            return []

        rows = []
        for col in eps_est.columns:                 # columns are periods: 0q, +1q …
            try:
                eps_avg  = float(eps_est[col].get("avg", 0) or 0)
                eps_high = float(eps_est[col].get("high", 0) or 0)
                eps_low  = float(eps_est[col].get("low", 0) or 0)
                n_eps    = int(eps_est[col].get("numberOfAnalysts", 0) or 0)

                rev_avg  = float(rev_est[col].get("avg", 0) or 0) if (
                    rev_est is not None and not rev_est.empty and col in rev_est.columns
                ) else 0.0
                rev_high = float(rev_est[col].get("high", 0) or 0) if (
                    rev_est is not None and not rev_est.empty and col in rev_est.columns
                ) else 0.0
                rev_low  = float(rev_est[col].get("low", 0) or 0) if (
                    rev_est is not None and not rev_est.empty and col in rev_est.columns
                ) else 0.0
                n_rev    = int(rev_est[col].get("numberOfAnalysts", 0) or 0) if (
                    rev_est is not None and not rev_est.empty and col in rev_est.columns
                ) else 0

                rows.append({
                    "symbol":             ticker,
                    "date":               str(col),   # e.g. "0q" or "+1q"
                    "epsAvg":             eps_avg,
                    "epsHigh":            eps_high,
                    "epsLow":             eps_low,
                    "numAnalystsEps":     n_eps,
                    "revenueAvg":         rev_avg,
                    "revenueHigh":        rev_high,
                    "revenueLow":         rev_low,
                    "numAnalystsRevenue": n_rev,
                    # Fields not available from yfinance — zero-fill
                    "ebitdaAvg": 0.0, "ebitdaHigh": 0.0, "ebitdaLow": 0.0,
                    "netIncomeAvg": 0.0, "netIncomeHigh": 0.0, "netIncomeLow": 0.0,
                    "sgaExpenseAvg": 0.0, "sgaExpenseHigh": 0.0, "sgaExpenseLow": 0.0,
                })
            except Exception:
                continue

        return rows
    except Exception as e:
        logger.debug("yf analyst estimates %s: %s", ticker, e)
        return []
