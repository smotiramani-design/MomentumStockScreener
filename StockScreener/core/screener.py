"""
core/screener.py — Pre-market momentum pipeline.

Data source priority (automatic, no config needed):
  1. FMP /stable/ endpoints  — if key works and plan allows
  2. yfinance                — free fallback, no IP restrictions
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd

from config import (FMP_API_KEY, BENZINGA_API_KEY, UNUSUAL_WHALES_KEY,
                    SECTOR_ETF_MAP, SIGNALS, SCORING, UNIVERSE)
from core.fmp_client   import FMPClient
from core.free_clients import (YFinanceClient, EDGARClient,
                                BenzingaClient, UnusualWhalesClient)
from core.data_router  import DataRouter
from signals.layers    import (score_catalyst, score_volume, score_price_action,
                                score_relative_strength, score_options)

logger = logging.getLogger(__name__)


# ── Session helpers ─────────────────────────────────────────────────────────

def _is_scoreable_symbol(sym: str) -> bool:
    """
    Return True for symbols that are likely regular equities or ETFs.
    Filters out options roots (e.g. FDXF), futures, warrants (/W suffix),
    units (/U suffix), and rights (/R suffix) that pass gap filters but
    cause yfinance 404s and waste scoring time.

    Rules (conservative — only filter obvious non-equities):
      - Must be 1–6 chars, letters only (with optional hyphen for share classes)
      - Symbols ending in digits are likely options/futures series identifiers
      - Very short suffixes W/U/R on 4+ char tickers often indicate derivatives
    """
    import re
    sym = sym.strip().upper()
    if not sym or len(sym) > 6:
        return False
    # Must be alpha or alpha-hyphen-alpha (BRK-B, BF-B)
    if not re.match(r'^[A-Z]{1,5}(-[A-Z]{1})?$', sym):
        return False
    return True


def _et_now() -> datetime:
    try:
        import zoneinfo
        return datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone.utc) - timedelta(hours=4)


def _session_label() -> str:
    h = _et_now().hour
    if 4 <= h < 9:   return "pre-market"
    if 9 <= h < 16:  return "regular"
    return "after-hours"


def _best_change_pct(q: Dict) -> float:
    for f in ("preMarketChangePercent", "changesPercentage",
              "change", "changePercent"):
        v = q.get(f)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return 0.0


def _best_price(q: Dict) -> float:
    for f in ("preMarketPrice", "price", "regularMarketPrice"):
        v = q.get(f)
        if v:
            try:
                return float(v)
            except (ValueError, TypeError):
                continue
    return 0.0


# ── Result dataclass ────────────────────────────────────────────────────────

@dataclass
class MomentumResult:
    ticker: str; name: str; sector: str
    score: int;  tier: str; session: str
    pm_change_pct: float; pm_volume: int
    premarket_price: float; prev_close: float; gap_pct: float
    catalyst: str; volume_detail: str; price_detail: str
    rs_detail: str; options_detail: str
    raw_signals: Dict = field(default_factory=dict)

    def to_dict(self) -> Dict:
        return {
            "Ticker": self.ticker, "Name": self.name, "Sector": self.sector,
            "Score": self.score,   "Tier": self.tier, "Session": self.session,
            "PM Change %": self.pm_change_pct, "PM Volume": self.pm_volume,
            "PM Price": self.premarket_price,  "Prev Close": self.prev_close,
            "Gap %": self.gap_pct,
            "L1 Catalyst":  self.catalyst,    "L2 Volume":  self.volume_detail,
            "L3 Price":     self.price_detail, "L4 RS":     self.rs_detail,
            "L5 Options":   self.options_detail,
            "Data Sources": self._sources_summary(),
        }

    def _sources_summary(self) -> str:
        """Compact pipe-separated summary for CSV column."""
        lines = []
        layer_names = {"l1":"L1","l2":"L2","l3":"L3","l4":"L4","l5":"L5"}
        for key, label in layer_names.items():
            slist = self.raw_signals.get(key+"_sources", [])
            if slist:
                providers = sorted(set(s["provider"] for s in slist))
                lines.append(f"{label}:[{"|".join(providers)}]")
        return "  ".join(lines) if lines else ""


# ── Main screener ────────────────────────────────────────────────────────────

class PreMarketScreener:
    def __init__(self):
        fmp        = FMPClient(FMP_API_KEY)
        yf_client  = YFinanceClient()
        self.fmp   = fmp
        self.data  = DataRouter(fmp, yf_client)
        self.edgar = EDGARClient()
        self._news_cache: List[Dict] = []   # bulk FMP news — fetched once per run, filtered per ticker
        self.benz  = BenzingaClient(BENZINGA_API_KEY)
        self.uw    = UnusualWhalesClient(UNUSUAL_WHALES_KEY)
        self._profiles: Dict[str, Dict] = {}

    # ── 1. Universe ──────────────────────────────────────────────────────────
    def build_universe(self) -> List[str]:
        from config import UNIVERSE_LIQUIDITY_DEFAULTS
        src = UNIVERSE.source
        logger.info("Building universe [source=%s]", src)

        # Apply per-universe liquidity defaults when min_avg_volume not set
        defaults = UNIVERSE_LIQUIDITY_DEFAULTS.get(src, {})
        min_price  = UNIVERSE.min_price  if UNIVERSE.min_price  > 1.0 else defaults.get("min_price", 5.0)
        min_volume = UNIVERSE.min_avg_volume if UNIVERSE.min_avg_volume > 0 else defaults.get("min_avg_volume", 200_000)

        if src == "custom":
            tickers = [t.upper() for t in UNIVERSE.custom_tickers]
        elif src == "nasdaq100":
            tickers = self.data.get_nasdaq100()
        elif src == "russell3000":
            tickers = self.data.get_russell3000(min_price=min_price, min_volume=min_volume)
        elif src == "nyse":
            tickers = self.data.get_nyse(min_price=min_price, min_volume=min_volume)
        else:
            tickers = self.data.get_sp500()  # default: sp500

        if not tickers:
            logger.error("Universe returned 0 tickers")
        else:
            logger.info("Universe: %s → %d tickers (min_price=$%.2f  min_vol=%s)",
                        src.upper(), len(tickers), min_price, f"{min_volume:,}")
        return tickers

    # ── 2–3. Fetch quotes → filter movers ────────────────────────────────────
    def get_movers(self, tickers: List[str], session: str) -> Dict[str, Dict]:
        logger.info("Fetching quotes for %d tickers… [session=%s]",
                    len(tickers), session)
        quotes = self.data.get_batch_quotes(tickers)
        logger.info("Quotes received: %d", len(quotes))

        if not quotes:
            logger.error(
                "No quotes returned.\n"
                "  Run:  python diagnose.py  to identify the data source issue."
            )
            return {}

        gap_floor = SIGNALS.gap_pct_min
        movers: Dict[str, Dict] = {}

        for sym, q in quotes.items():
            chg   = abs(_best_change_pct(q))
            price = _best_price(q)

            # Skip non-equity instruments — options roots, futures, warrants,
            # units that pass gap filter but cause yfinance 404s downstream.
            # FMP quote includes quoteType field; also use a symbol heuristic.
            quote_type = str(q.get("quoteType") or q.get("typeDisp") or "").upper()
            if quote_type and quote_type not in ("", "EQUITY", "ETF", "COMMON STOCK"):
                logger.debug("Skipping %s (quoteType=%s)", sym, quote_type)
                continue
            if not _is_scoreable_symbol(sym):
                logger.debug("Skipping %s (non-equity symbol pattern)", sym)
                continue

            if chg >= gap_floor and UNIVERSE.min_price <= price <= UNIVERSE.max_price:
                movers[sym] = q

        logger.info("Movers (|Δ| ≥ %.1f%%): %d of %d",
                    gap_floor, len(movers), len(quotes))

        if not movers:
            # Show sample so user can debug thresholds
            sample = sorted(quotes.items(),
                            key=lambda kv: abs(_best_change_pct(kv[1])),
                            reverse=True)[:5]
            logger.info("Top movers by |change|:")
            for sym, q in sample:
                logger.info("  %-6s  price=%-8.2f  Δ=%.2f%%",
                            sym, _best_price(q), _best_change_pct(q))
            logger.info(
                "  → Gap threshold is %.1f%%. Lower it in config.py "
                "(SIGNALS.gap_pct_min) or test during market hours.", gap_floor
            )
        return movers

    # ── 4. Score one ticker ──────────────────────────────────────────────────
    def score_ticker(self, ticker: str, quote: Dict,
                     analyst_changes: List[Dict],
                     recent_8ks: List[Dict],
                     sector_quotes: Dict[str, Dict],
                     index_quote: Optional[Dict],
                     session: str) -> Optional[MomentumResult]:
        try:
            # Resolve which provider actually delivered each data type
            qp  = "FMP" if self.data._fmp_ok else "yfinance"   # quote provider
            hp  = "FMP" if self.data._fmp_ok else "yfinance"   # history provider
            sp  = "FMP" if self.data._fmp_ok else "yfinance"   # SMA provider
            ep  = "FMP" if self.data._fmp_ok else "yfinance"   # earnings provider
            pp  = "FMP" if self.data._fmp_ok else "N/A"        # press releases
            ap  = "FMP" if self.data._fmp_ok else "yfinance"   # analyst estimates

            hist   = self.data.get_daily_ohlcv(ticker, limit=60)
            sma50  = self.data.get_sma(ticker, period=50)
            sma200 = self.data.get_sma(ticker, period=200)

            earnings          = self.data.get_earnings_surprise(ticker)
            press             = self.data.get_press_releases(ticker)
            # Aftermarket bid/ask — replaces broken pre-post-market path
            # Returns None outside extended-hours or if plan doesn't include it
            aftermarket_q     = self.data.get_aftermarket_quote(ticker)
            analyst_estimates = self.data.get_analyst_estimates(ticker, period="quarter")
            ticker_grades = self.fmp.get_grades_for_ticker(ticker, limit=3) if self.data._fmp_ok else []
            all_analyst   = list(analyst_changes) + ticker_grades

            # News source priority:
            #   1. Filter bulk FMP cache (_news_cache) for this ticker — zero extra API calls
            #   2. Per-ticker FMP call (news/stock-latest?symbols=TICKER) if cache miss
            #   3. Benzinga fallback (if configured)
            news = [a for a in self._news_cache
                    if a.get("symbol", "").upper() == ticker.upper()]
            if not news and self.data._fmp_ok:
                news = self.fmp.get_stock_news_for_ticker(ticker, limit=5)
                if news:
                    logger.debug("FMP per-ticker news hit for %s: %d articles", ticker, len(news))
            if not news:
                news = self.benz.get_news(ticker)  # Benzinga fallback
            yf_client = YFinanceClient()
            yf_opts  = yf_client.get_options_activity(ticker)
            iv_rank  = yf_client.get_iv_rank(ticker)
            uw_flow  = self.uw.get_flow(ticker)
            fmp_iv   = None
            has_8k   = any(f.get("ticker","").upper() == ticker for f in recent_8ks)

            profile = self._profiles.get(ticker, {})
            sector  = profile.get("sector", "Unknown")
            etf     = SECTOR_ETF_MAP.get(sector)
            sec_q   = sector_quotes.get(etf) if etf else None

            l1 = score_catalyst(ticker, quote, earnings, press, all_analyst,
                                has_8k, news,
                                analyst_estimates=analyst_estimates,
                                quote_provider=qp, earnings_provider=ep,
                                press_provider=pp, analyst_provider=ap,
                                earnings_surprise_pct=SIGNALS.earnings_surprise_pct,
                                price_reaction_pct=SIGNALS.price_reaction_pct)
            # Enrich quote with aftermarket bid/ask spread if available
            if aftermarket_q and aftermarket_q.get("bid", 0) > 0:
                quote = {**quote,
                         "aftermarket_bid":        aftermarket_q["bid"],
                         "aftermarket_ask":        aftermarket_q["ask"],
                         "aftermarket_spread":     aftermarket_q["spread"],
                         "aftermarket_spread_pct": aftermarket_q["spread_pct"],
                         "aftermarket_bsize":      aftermarket_q["bsize"],
                         "aftermarket_asize":      aftermarket_q["asize"]}
                logger.debug("%s aftermarket bid=%.2f ask=%.2f spread=%.3f%%",
                             ticker, aftermarket_q["bid"], aftermarket_q["ask"],
                             aftermarket_q["spread_pct"])

            l2 = score_volume(quote, hist,
                              SIGNALS.premarket_vol_pct_of_adv,
                              SIGNALS.relative_vol_ratio,
                              quote_provider=qp, history_provider=hp)
            l3 = score_price_action(quote, hist, sma50, sma200,
                                    SIGNALS.gap_pct_min, SIGNALS.ma_buffer_pct,
                                    quote_provider=qp, history_provider=hp,
                                    sma_provider=sp)
            l4 = score_relative_strength(ticker, quote, sec_q, index_quote,
                                         SIGNALS.rs_min_outperformance,
                                         quote_provider=qp, etf_provider=qp)
            l5 = score_options(yf_opts, iv_rank, uw_flow, fmp_iv,
                               SIGNALS.iv_percentile_floor,
                               SIGNALS.unusual_call_multiplier,
                               iv_provider="yfinance", options_provider="yfinance")

            total = l1.score + l2.score + l3.score + l4.score + l5.score
            tier  = ("TRADE" if total >= SCORING.trade_threshold else
                     "WATCH" if total >= SCORING.watch_threshold else "SKIP")

            chg      = round(_best_change_pct(quote), 2)
            pm_vol   = int(float(quote.get("preMarketVolume") or
                                  quote.get("volume") or 0))
            pm_price = round(_best_price(quote), 2)
            p_close  = float(quote.get("previousClose") or pm_price)
            gap_pct  = round((pm_price - p_close) / p_close * 100, 2) if p_close else 0

            # Serialise DataSource objects for storage
            def _src_dicts(layer): return [
                {"field": s.field, "value": s.value,
                 "provider": s.provider, "endpoint": s.endpoint}
                for s in layer.sources
            ]

            return MomentumResult(
                ticker=ticker, name=profile.get("companyName", ticker),
                sector=sector, score=total, tier=tier, session=session,
                pm_change_pct=chg, pm_volume=pm_vol,
                premarket_price=pm_price, prev_close=round(p_close, 2),
                gap_pct=gap_pct,
                catalyst=l1.detail, volume_detail=l2.detail,
                price_detail=l3.detail, rs_detail=l4.detail,
                options_detail=l5.detail,
                raw_signals={
                    "l1": l1.raw, "l1_sources": _src_dicts(l1),
                    "l2": l2.raw, "l2_sources": _src_dicts(l2),
                    "l3": l3.raw, "l3_sources": _src_dicts(l3),
                    "l4": l4.raw, "l4_sources": _src_dicts(l4),
                    "l5": l5.raw, "l5_sources": _src_dicts(l5),
                },
            )
        except Exception as e:
            logger.error("score_ticker %s: %s", ticker, e, exc_info=True)
            return None

    # ── Main run ─────────────────────────────────────────────────────────────
    def run(self) -> pd.DataFrame:
        session = _session_label()
        logger.info("═══════ Pre-Market Momentum Screener ═══════")
        logger.info("Session: %s  ·  %s ET",
                    session, _et_now().strftime("%H:%M"))

        today = date.today().isoformat()
        yday  = (date.today() - timedelta(days=1)).isoformat()

        tickers = self.build_universe()
        if not tickers:
            return pd.DataFrame()

        # Profiles (sector mapping)
        logger.info("Loading company profiles…")
        self._profiles = self.data.get_profiles_batch(tickers)
        logger.info("Profiles loaded: %d", len(self._profiles))

        # Movers
        movers = self.get_movers(tickers, session)
        if not movers:
            return pd.DataFrame()

        # Shared data
        logger.info("Fetching shared catalyst data…")
        analyst_changes = []  # fetched per-ticker inside score_ticker
        recent_8ks      = self.edgar.get_recent_8k(hours_back=20)

        # Bulk FMP news feed — ONE call covers all movers; filtered per ticker at score time
        # Endpoint: /stable/news/stock-latest?page=0&limit=100
        if self.data._fmp_ok:
            self._news_cache = self.fmp.get_stock_news_latest(limit=100, page=0)
            logger.info("FMP news feed: %d articles loaded (news/stock-latest)",
                        len(self._news_cache))
        else:
            self._news_cache = []
            logger.info("FMP unavailable — news will use Benzinga fallback per ticker")

        # Sector + index quotes (route through DataRouter too)
        all_etfs    = list(set(SECTOR_ETF_MAP.values())) + ["SPY", "QQQ"]
        etf_quotes  = self.data.get_batch_quotes(all_etfs)
        index_quote = etf_quotes.get("SPY")

        # Score in parallel
        logger.info("Scoring %d movers…", len(movers))
        results: List[MomentumResult] = []

        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {
                ex.submit(self.score_ticker, sym, q,
                          analyst_changes, recent_8ks,
                          etf_quotes, index_quote, session): sym
                for sym, q in movers.items()
            }
            for fut in as_completed(futures):
                res = fut.result()
                if res:
                    results.append(res)

        tier_order = {"TRADE": 0, "WATCH": 1, "SKIP": 2}
        results.sort(key=lambda r: (tier_order[r.tier], -r.score, -abs(r.gap_pct)))

        df = pd.DataFrame([r.to_dict() for r in results])
        results_map = {r.ticker: r.raw_signals for r in results}

        logger.info("Done. %d results  TRADE=%d  WATCH=%d  SKIP=%d",
                    len(results),
                    sum(1 for r in results if r.tier == "TRADE"),
                    sum(1 for r in results if r.tier == "WATCH"),
                    sum(1 for r in results if r.tier == "SKIP"))
        return df, results_map
