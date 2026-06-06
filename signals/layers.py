"""
signals/layers.py — Five scoring layers for the pre-market momentum screener.

Each layer returns SignalResult(score, detail, sources, raw).
  • score   : 0 or 1
  • detail  : human-readable verdict
  • sources : list of DataSource(field, value, provider, endpoint)
              — every data point tagged with where it came from
  • raw     : raw numeric values for audit
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Data provenance tag ──────────────────────────────────────────────────────

@dataclass
class DataSource:
    field:    str    # logical field name, e.g. "EPS surprise"
    value:    str    # formatted value shown in UI
    provider: str    # "FMP" | "yfinance" | "SEC EDGAR" | "Benzinga" | "Unusual Whales"
    endpoint: str    # specific API path / method used


@dataclass
class SignalResult:
    score:   int           # 0 or 1
    detail:  str           # human-readable verdict
    sources: List[DataSource] = field(default_factory=list)
    raw:     Dict             = field(default_factory=dict)


# Provider constants
FMP  = "FMP"
YF   = "yfinance"
EDGR = "SEC EDGAR"
BENZ = "Benzinga"
UW   = "Unusual Whales"


# ── Layer 1 — Catalyst ───────────────────────────────────────────────────────

def score_catalyst(
    ticker:               str,
    quote:                Dict,
    earnings_surprises:   List[Dict],
    press_releases:       List[Dict],
    analyst_changes:      List[Dict],
    has_8k:               bool,
    benzinga_news:        List[Dict],
    analyst_estimates:    Optional[List[Dict]] = None,
    # provenance flags
    quote_provider:       str = FMP,
    earnings_provider:    str = FMP,
    press_provider:       str = FMP,
    analyst_provider:     str = FMP,
    earnings_surprise_pct: float = 5.0,
    price_reaction_pct:   float  = 2.0,
) -> SignalResult:
    raw: Dict = {}
    reasons: List[str] = []
    sources: List[DataSource] = []

    # (a) Earnings surprise — actual vs estimated (historical)
    if earnings_surprises:
        surprise = earnings_surprises[0]
        actual   = surprise.get("actualEarningResult") or 0
        est      = surprise.get("estimatedEarning") or 1e-6
        pct      = ((actual - est) / abs(est)) * 100
        raw["eps_surprise_pct"] = round(pct, 2)
        sources.append(DataSource(
            field="EPS surprise (actual vs est)",
            value=f"{pct:+.1f}%  (actual={actual:.2f}, est={est:.2f})",
            provider=earnings_provider,
            endpoint="stable/earnings" if earnings_provider == FMP
                     else "yfinance / earnings_history",
        ))
        if abs(pct) >= earnings_surprise_pct:
            reasons.append(f"EPS surprise {pct:+.1f}%")

    # (a2) Forward analyst EPS consensus — analyst-estimates endpoint
    # Fields used (all confirmed from live API response):
    #   epsAvg, epsHigh, epsLow, numAnalystsEps  → EPS consensus + conviction
    #   revenueAvg, revenueHigh, revenueLow, numAnalystsRevenue → revenue outlook
    #   date → period end date (e.g. "2026-09-27")
    est_list = analyst_estimates or []
    if est_list:
        nearest      = est_list[0]    # soonest upcoming period
        eps_avg      = nearest.get("epsAvg",  0) or 0
        eps_high     = nearest.get("epsHigh", 0) or 0
        eps_low      = nearest.get("epsLow",  0) or 0
        n_eps        = nearest.get("numAnalystsEps", 0) or 0
        rev_avg      = nearest.get("revenueAvg", 0) or 0
        n_rev        = nearest.get("numAnalystsRevenue", 0) or 0
        est_date     = nearest.get("date", "")

        # EPS dispersion: (high - low) / avg * 100  — tight = high conviction
        eps_dispersion = ((eps_high - eps_low) / abs(eps_avg)) * 100 if eps_avg else 0

        # Revenue growth vs prior period
        rev_growth_pct = 0.0
        if len(est_list) >= 2:
            prev_rev = est_list[1].get("revenueAvg", 0) or 0
            if prev_rev:
                rev_growth_pct = ((rev_avg - prev_rev) / abs(prev_rev)) * 100

        raw["forward_eps_avg"]         = round(float(eps_avg), 4)
        raw["forward_eps_high"]        = round(float(eps_high), 4)
        raw["forward_eps_low"]         = round(float(eps_low), 4)
        raw["forward_eps_dispersion"]  = round(eps_dispersion, 1)
        raw["forward_num_analysts"]    = int(n_eps)
        raw["forward_rev_avg_bn"]      = round(float(rev_avg) / 1e9, 2)
        raw["forward_rev_growth_pct"]  = round(rev_growth_pct, 1)
        raw["forward_est_date"]        = est_date

        sources.append(DataSource(
            field=f"Forward EPS consensus ({est_date})",
            value=(f"avg=${eps_avg:.2f}  "
                   f"range=[${eps_low:.2f}–${eps_high:.2f}]  "
                   f"dispersion={eps_dispersion:.0f}%  "
                   f"n={n_eps} analysts"),
            provider=analyst_provider,
            endpoint="stable/analyst-estimates?symbol={ticker}&period=quarter"
                     if analyst_provider == FMP
                     else "yfinance / earnings_estimate",
        ))
        sources.append(DataSource(
            field=f"Forward revenue consensus ({est_date})",
            value=(f"avg=${rev_avg/1e9:.1f}B  "
                   f"growth={rev_growth_pct:+.1f}% vs prior period  "
                   f"n={n_rev} analysts"),
            provider=analyst_provider,
            endpoint="stable/analyst-estimates?symbol={ticker}&period=quarter"
                     if analyst_provider == FMP
                     else "yfinance / revenue_estimate",
        ))

        # Score: tight consensus with good analyst coverage
        if n_eps >= 10 and eps_dispersion < 10:
            reasons.append(
                f"Strong analyst consensus: {n_eps} analysts, "
                f"EPS ${eps_avg:.2f} tight range (±{eps_dispersion:.0f}%)"
            )
        # Score: revenue growth expected
        if rev_growth_pct >= 5:
            reasons.append(
                f"Revenue estimate +{rev_growth_pct:.1f}% vs prior period "
                f"(${rev_avg/1e9:.1f}B avg, {n_rev} analysts)"
            )
        # Flag (not score): wide dispersion signals high uncertainty
        if eps_dispersion > 20:
            raw["forward_high_uncertainty"] = True
            reasons.append(
                f"High analyst uncertainty: EPS range "
                f"${eps_low:.2f}–${eps_high:.2f} ({eps_dispersion:.0f}% spread)"
            )

    # (b) Pre-market price move
    pm_chg = quote.get("preMarketChangePercent") or 0
    raw["preMarket_change_pct"] = round(float(pm_chg), 2)
    sources.append(DataSource(
        field="Pre-market Δ%",
        value=f"{float(pm_chg):+.2f}%",
        provider=quote_provider,
        endpoint="stable/quote" if quote_provider == FMP else "yfinance / fast_info",
    ))
    if abs(float(pm_chg)) >= price_reaction_pct:
        reasons.append(f"Pre-market move {float(pm_chg):+.2f}%")

    # (c) 8-K filing
    raw["has_8k"] = has_8k
    sources.append(DataSource(
        field="8-K filing",
        value="Yes" if has_8k else "None",
        provider=EDGR,
        endpoint="efts.sec.gov / search-index (8-K)",
    ))
    if has_8k:
        reasons.append("Recent 8-K filing (SEC EDGAR)")

    # (d) Analyst action today
    ticker_changes = [a for a in analyst_changes
                      if a.get("symbol", "").upper() == ticker.upper()]
    raw["analyst_actions_today"] = len(ticker_changes)
    sources.append(DataSource(
        field="Analyst action",
        value=f"{len(ticker_changes)} today" if ticker_changes else "None",
        provider=analyst_provider,
        endpoint="stable/upgrades-downgrades" if analyst_provider == FMP else "N/A",
    ))
    if ticker_changes:
        action = ticker_changes[0].get("action", "change")
        reasons.append(f"Analyst {action} today")

    # (e) Press releases (last 20 h)
    now = datetime.now(timezone.utc)
    fresh_pr = [p for p in press_releases if _hours_ago(p.get("date", ""), now) <= 20]
    raw["fresh_press_releases"] = len(fresh_pr)
    sources.append(DataSource(
        field="Press releases",
        value=f"{len(fresh_pr)} in last 20h",
        provider=press_provider,
        endpoint="stable/news/press-releases?symbol={ticker}" if press_provider == FMP else "N/A",
    ))
    if fresh_pr:
        reasons.append(f"{len(fresh_pr)} press release(s) filed")

    # (f) News articles — FMP stock-latest feed (primary) or Benzinga (fallback)
    #
    # FMP news/stock-latest response fields used here:
    #   symbol        : ticker (already filtered upstream)
    #   publishedDate : "2026-05-30 15:31:10"
    #   publisher     : "Fox Business" / "Reuters" / "Benzinga" etc.
    #   title         : headline
    #   text          : snippet / lede paragraph
    #   url           : full article URL
    #   site          : domain name
    now = datetime.now(timezone.utc)
    fresh_news = [
        a for a in benzinga_news
        if _hours_ago(a.get("publishedDate", a.get("created", "")), now) <= 20
    ]
    raw["news_articles"]   = len(fresh_news)
    raw["news_titles"]     = [a.get("title", "")[:80] for a in fresh_news[:3]]
    raw["news_publishers"] = list({a.get("publisher", a.get("site", "")) for a in fresh_news})

    # Detect news source — FMP articles have 'publishedDate'; Benzinga have 'created'
    is_fmp_news = any("publishedDate" in a for a in fresh_news)
    news_provider = FMP if is_fmp_news else BENZ
    news_endpoint = (
        "stable/news/stock-latest?page=0&limit=100 (bulk, filtered by symbol)"
        if is_fmp_news else
        "api.benzinga.com/v2/news" if fresh_news else "not configured"
    )
    sources.append(DataSource(
        field="News headlines",
        value=(f"{len(fresh_news)} article(s) — "
               f"{', '.join(raw['news_publishers'][:2])}")
              if fresh_news else "None in last 20h",
        provider=news_provider,
        endpoint=news_endpoint,
    ))
    if fresh_news:
        top = fresh_news[0]
        title_snip = top.get("title", "")[:60]
        pub = top.get("publisher", top.get("site", ""))
        reasons.append(f"{len(fresh_news)} article(s) — \"{title_snip}\" ({pub})")

    score  = 1 if reasons else 0
    detail = "; ".join(reasons) if reasons else "No catalyst detected"
    return SignalResult(score=score, detail=detail, sources=sources, raw=raw)


def _hours_ago(date_str: str, now: datetime) -> float:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return (now - datetime.strptime(date_str, fmt).replace(
                        tzinfo=timezone.utc)).total_seconds() / 3600
        except ValueError:
            continue
    return 9999.0


# ── Layer 2 — Volume ─────────────────────────────────────────────────────────

def score_volume(
    quote:            Dict,
    historical_ohlcv: List[Dict],
    premarket_vol_pct: float = 20.0,
    rvol_floor:        float = 2.0,
    quote_provider:    str   = FMP,
    history_provider:  str   = FMP,
) -> SignalResult:
    raw: Dict = {}
    sources: List[DataSource] = []

    # ADV from historical
    adv_source = history_provider
    if historical_ohlcv:
        vols = [d.get("volume", 0) for d in historical_ohlcv[:20]]
        adv  = statistics.mean(v for v in vols if v > 0)
        sources.append(DataSource(
            field="20-day ADV",
            value=f"{int(adv):,}",
            provider=adv_source,
            endpoint="stable/historical-price-eod/full" if adv_source == FMP
                     else "yfinance / Ticker.history",
        ))
    else:
        adv = quote.get("avgVolume") or 1
        sources.append(DataSource(
            field="20-day ADV (fallback)",
            value=f"{int(adv):,}",
            provider=quote_provider,
            endpoint="stable/quote.avgVolume" if quote_provider == FMP
                     else "yfinance / fast_info",
        ))

    pm_vol_raw = quote.get("preMarketVolume")
    if pm_vol_raw:
        pm_vol = float(pm_vol_raw)
        vol_note = "pre-market volume"
        vol_ep   = "stable/quote.preMarketVolume" if quote_provider == FMP \
                   else "yfinance / fast_info.pre_market_volume"
    else:
        pm_vol   = float(quote.get("volume") or 0) * 0.10
        vol_note = "10% of day volume (proxy)"
        vol_ep   = "stable/quote.volume (proxy)" if quote_provider == FMP \
                   else "yfinance / download.Volume (proxy)"

    sources.append(DataSource(
        field=f"Pre-mkt volume ({vol_note})",
        value=f"{int(pm_vol):,}",
        provider=quote_provider,
        endpoint=vol_ep,
    ))

    raw["adv_20d"]       = int(adv)
    raw["premarket_vol"] = int(pm_vol)
    raw["pm_pct_of_adv"] = round(pm_vol / adv * 100, 1) if adv else 0
    raw["rvol"]          = round(pm_vol / (adv / 6.5), 2) if adv else 0

    sources.append(DataSource(
        field="RVOL",
        value=f"{raw['rvol']:.1f}×",
        provider="computed",
        endpoint=f"pm_vol / (ADV/6.5)  =  {int(pm_vol):,} / {int(adv/6.5):,}",
    ))

    # Bid/ask spread from aftermarket-quote — liquidity quality signal
    # Injected into quote dict by screener when available (bid > 0)
    # Wide spread (>0.5%) = thin pre-market liquidity, reduces conviction
    # Tight spread (<0.1%) = institutional-grade liquidity, increases conviction
    spread_pct = float(quote.get("aftermarket_spread_pct") or 0)
    bid        = float(quote.get("aftermarket_bid") or 0)
    ask        = float(quote.get("aftermarket_ask") or 0)
    raw["aftermarket_bid"]        = round(bid, 2)
    raw["aftermarket_ask"]        = round(ask, 2)
    raw["aftermarket_spread_pct"] = round(spread_pct, 3)

    if bid > 0:
        sources.append(DataSource(
            field="Bid/ask spread (extended hours)",
            value=(f"bid=${bid:.2f}  ask=${ask:.2f}  "
                   f"spread={spread_pct:.3f}% "
                   f"({'tight' if spread_pct < 0.1 else 'normal' if spread_pct < 0.5 else 'wide'})"),
            provider=FMP,
            endpoint="stable/aftermarket-quote?symbol={ticker}",
        ))

    reasons = []
    if raw["pm_pct_of_adv"] >= premarket_vol_pct:
        reasons.append(f"Pre-mkt vol = {raw['pm_pct_of_adv']:.1f}% of ADV")
    if raw["rvol"] >= rvol_floor:
        reasons.append(f"RVOL = {raw['rvol']:.1f}×")
    # Tight spread = institutional liquidity = add signal
    if bid > 0 and spread_pct < 0.1:
        reasons.append(f"Tight bid/ask spread ({spread_pct:.3f}%) — institutional liquidity")
    # Wide spread = warn in detail even if volume thresholds met
    if bid > 0 and spread_pct > 0.5:
        raw["liquidity_warning"] = f"Wide spread {spread_pct:.2f}% — thin pre-mkt liquidity"

    score  = 1 if reasons else 0
    detail = "; ".join(reasons) if reasons else (
        f"Pre-mkt vol {raw['pm_pct_of_adv']:.1f}% of ADV (need ≥{premarket_vol_pct:.0f}%)"
    )
    if raw.get("liquidity_warning"):
        detail += f" ⚠ {raw['liquidity_warning']}"
    return SignalResult(score=score, detail=detail, sources=sources, raw=raw)


# ── Layer 3 — Price Action ────────────────────────────────────────────────────

def score_price_action(
    quote:            Dict,
    historical_ohlcv: List[Dict],
    sma50:            Optional[float] = None,
    sma200:           Optional[float] = None,
    gap_pct_min:      float = 1.5,
    ma_buffer_pct:    float = 0.5,
    quote_provider:   str   = FMP,
    history_provider: str   = FMP,
    sma_provider:     str   = FMP,
) -> SignalResult:
    raw: Dict = {}
    reasons: List[str] = []
    sources: List[DataSource] = []

    pm_price   = float(quote.get("preMarketPrice") or quote.get("price") or 0)
    prev_close = float(quote.get("previousClose") or quote.get("price") or pm_price)
    prev_high  = float(historical_ohlcv[1]["high"]) if len(historical_ohlcv) > 1 else prev_close

    sources.append(DataSource(
        field="Pre-mkt price",
        value=f"${pm_price:.2f}",
        provider=quote_provider,
        endpoint="stable/quote.preMarketPrice" if quote_provider == FMP
                 else "yfinance / fast_info.pre_market_price → download.Close",
    ))
    sources.append(DataSource(
        field="Prev close",
        value=f"${prev_close:.2f}",
        provider=quote_provider,
        endpoint="stable/quote.previousClose" if quote_provider == FMP
                 else "yfinance / download.Close[-2]",
    ))
    sources.append(DataSource(
        field="Prev day high",
        value=f"${prev_high:.2f}",
        provider=history_provider,
        endpoint="stable/historical-price-eod/full[1].high" if history_provider == FMP
                 else "yfinance / Ticker.history.High[-2]",
    ))

    gap_pct = ((pm_price - prev_close) / prev_close * 100) if prev_close else 0
    raw["gap_pct"] = round(gap_pct, 2)
    raw["prev_close"] = prev_close
    raw["prev_high"]  = prev_high
    raw["premarket_px"] = pm_price

    if abs(gap_pct) >= gap_pct_min:
        reasons.append(f"Gap {gap_pct:+.1f}% vs prior close")
    if pm_price > prev_high:
        reasons.append(f"Above prior day high ({prev_high:.2f})")

    # SMA reclaims
    for ma_val, label, period in [(sma50, "50-day MA", 50), (sma200, "200-day MA", 200)]:
        if ma_val and ma_val > 0:
            sources.append(DataSource(
                field=label,
                value=f"${ma_val:.2f}",
                provider=sma_provider,
                endpoint=f"stable/technical-indicators/daily?type=SMA&period={period}"
                         if sma_provider == FMP
                         else f"yfinance / Ticker.history.Close.rolling({period}).mean()",
            ))
            dist_pct = (pm_price - ma_val) / ma_val * 100
            raw[f"dist_{label.replace(' ','')}"] = round(dist_pct, 2)
            if -ma_buffer_pct <= dist_pct <= 5.0:
                reasons.append(f"Testing/reclaiming {label} ({ma_val:.2f})")

    # VWAP proxy
    if historical_ohlcv:
        prev = historical_ohlcv[0]
        vwap_proxy = (prev.get("high",0) + prev.get("low",0) + prev.get("close",0)) / 3
        raw["vwap_proxy"] = round(vwap_proxy, 2)
        sources.append(DataSource(
            field="Prior VWAP proxy",
            value=f"${vwap_proxy:.2f}",
            provider=history_provider,
            endpoint="(H+L+C)/3 from historical-price-eod/full[0]"
                     if history_provider == FMP
                     else "(H+L+C)/3 from yfinance / Ticker.history[-1]",
        ))
        if pm_price > vwap_proxy:
            reasons.append(f"Pre-mkt price above prior VWAP proxy ({vwap_proxy:.2f})")

    score  = 1 if reasons else 0
    detail = "; ".join(reasons) if reasons else f"Gap {gap_pct:+.1f}% — no key level break"
    return SignalResult(score=score, detail=detail, sources=sources, raw=raw)


# ── Layer 4 — Relative Strength ───────────────────────────────────────────────

def score_relative_strength(
    ticker:           str,
    quote:            Dict,
    sector_etf_quote: Optional[Dict],
    index_quote:      Optional[Dict],
    min_outperformance: float = 1.0,
    quote_provider:     str   = FMP,
    etf_provider:       str   = FMP,
) -> SignalResult:
    raw: Dict = {}
    reasons: List[str] = []
    sources: List[DataSource] = []

    stock_chg = float(quote.get("preMarketChangePercent") or
                      quote.get("changesPercentage") or 0)
    raw["stock_pm_chg_pct"] = round(stock_chg, 2)
    sources.append(DataSource(
        field=f"{ticker} change",
        value=f"{stock_chg:+.2f}%",
        provider=quote_provider,
        endpoint="stable/quote.preMarketChangePercent → changesPercentage"
                 if quote_provider == FMP
                 else "yfinance / download (close-to-close Δ%)",
    ))

    # vs sector ETF
    if sector_etf_quote:
        etf_sym    = sector_etf_quote.get("symbol", "ETF")
        sector_chg = float(sector_etf_quote.get("preMarketChangePercent") or
                           sector_etf_quote.get("changesPercentage") or 0)
        outperf    = stock_chg - sector_chg
        raw["sector_chg_pct"]        = round(sector_chg, 2)
        raw["sector_outperformance"] = round(outperf, 2)
        sources.append(DataSource(
            field=f"{etf_sym} (sector ETF)",
            value=f"{sector_chg:+.2f}%  →  outperf {outperf:+.2f}%",
            provider=etf_provider,
            endpoint="stable/quote (batch, sector ETF symbols)"
                     if etf_provider == FMP
                     else "yfinance / download (sector ETF)",
        ))
        if outperf >= min_outperformance:
            reasons.append(f"Outperforming {etf_sym} by {outperf:+.1f}%")

    # vs SPY
    if index_quote:
        idx_sym     = index_quote.get("symbol", "SPY")
        idx_chg     = float(index_quote.get("preMarketChangePercent") or
                            index_quote.get("changesPercentage") or 0)
        idx_outperf = stock_chg - idx_chg
        raw["index_chg_pct"]        = round(idx_chg, 2)
        raw["index_outperformance"] = round(idx_outperf, 2)
        sources.append(DataSource(
            field=f"{idx_sym} (index)",
            value=f"{idx_chg:+.2f}%  →  outperf {idx_outperf:+.2f}%",
            provider=etf_provider,
            endpoint="stable/quote (batch, SPY/QQQ)"
                     if etf_provider == FMP
                     else "yfinance / download (SPY/QQQ)",
        ))
        if idx_outperf >= min_outperformance:
            reasons.append(f"Outperforming {idx_sym} by {idx_outperf:+.1f}%")
        elif idx_chg <= 0 and stock_chg > 0:
            reasons.append(f"Positive while {idx_sym} is flat/down")

    score  = 1 if reasons else 0
    detail = "; ".join(reasons) if reasons else f"Stock {stock_chg:+.1f}% — insufficient RS edge"
    return SignalResult(score=score, detail=detail, sources=sources, raw=raw)


# ── Layer 5 — Options ─────────────────────────────────────────────────────────

def score_options(
    yf_options:              Dict,
    iv_rank:                 Optional[float],
    uw_flow:                 List[Dict],
    fmp_iv:                  Optional[Dict],
    iv_rank_floor:           float = 50.0,
    unusual_call_multiplier: float = 3.0,
    iv_provider:             str   = YF,
    options_provider:        str   = YF,
) -> SignalResult:
    raw: Dict = {}
    reasons: List[str] = []
    sources: List[DataSource] = []

    # IV rank
    iv_rank_val = iv_rank
    if iv_rank_val is None and fmp_iv:
        iv_rank_val = fmp_iv.get("ivRank") or fmp_iv.get("ivPercentile")
        iv_provider = FMP

    raw["iv_rank"] = round(float(iv_rank_val), 1) if iv_rank_val is not None else None
    sources.append(DataSource(
        field="IV rank",
        value=f"{iv_rank_val:.0f}th pct" if iv_rank_val is not None else "N/A",
        provider=iv_provider,
        endpoint="stable/stock/implied-volatility" if iv_provider == FMP
                 else "yfinance / rolling 30-day realised vol (52-week rank)",
    ))
    if iv_rank_val is not None and iv_rank_val >= iv_rank_floor:
        reasons.append(f"IV rank {iv_rank_val:.0f}th percentile")

    # Call/Put ratio
    cp_ratio = yf_options.get("cp_ratio", 0)
    raw["cp_ratio"]    = cp_ratio
    raw["call_volume"] = yf_options.get("call_volume", 0)
    raw["put_volume"]  = yf_options.get("put_volume", 0)
    sources.append(DataSource(
        field="Call/Put vol ratio",
        value=f"{cp_ratio:.1f}×  (calls {int(raw['call_volume']):,} / puts {int(raw['put_volume']):,})",
        provider=options_provider,
        endpoint="yfinance / Ticker.option_chain (nearest expiry)",
    ))
    if cp_ratio >= unusual_call_multiplier:
        reasons.append(f"Call/Put vol ratio {cp_ratio:.1f}×")

    # ATM IV
    atm_iv = yf_options.get("atm_iv", 0)
    raw["atm_iv_annualised"] = round(float(atm_iv) * 100, 1)
    sources.append(DataSource(
        field="ATM implied vol",
        value=f"{atm_iv*100:.0f}%" if atm_iv else "N/A",
        provider=options_provider,
        endpoint="yfinance / option_chain.impliedVolatility (ATM strike)",
    ))
    if atm_iv > 0.5:
        reasons.append(f"ATM IV {atm_iv*100:.0f}% (elevated)")

    # Unusual Whales
    bullish_sweeps = [f for f in uw_flow if f.get("sentiment", "").lower() == "bullish"]
    raw["uw_bullish_sweeps"] = len(bullish_sweeps)
    sources.append(DataSource(
        field="Options sweeps",
        value=f"{len(bullish_sweeps)} bullish" if bullish_sweeps else "None",
        provider=UW,
        endpoint="unusualwhales.com/api/stock/{ticker}/options-flow"
                 if uw_flow else "not configured",
    ))
    if bullish_sweeps:
        reasons.append(f"{len(bullish_sweeps)} bullish sweep(s) via Unusual Whales")

    score  = 1 if reasons else 0
    detail = "; ".join(reasons) if reasons else "No notable options signal"
    return SignalResult(score=score, detail=detail, sources=sources, raw=raw)
