"""
utils/email_builder.py — Rich HTML email for pre-market momentum alerts.

Email structure:
  • Subject  : session, TRADE count, date/time
  • Header   : run timestamp + market session
  • Summary  : TRADE / WATCH / TOTAL counts
  • Top-10   : conviction-ranked TRADE cards (configurable via email_top_n)
  • Table    : all TRADE + WATCH rows in ranked order
  • Footer   : data sources + disclaimer

Attachments (two files):
  1. top10_trade_YYYYMMDD_HHMM.csv  — top-N TRADE ideas with Rank + Conviction
  2. full_report_YYYYMMDD_HHMM.csv  — complete run (ALL tickers: TRADE+WATCH+SKIP)

Conviction ranking formula (within TRADE tier, signal score already 4–5):
  conviction = signal_score × 10         (40–50 base)
             + min(|gap_pct|, 10) × 2    (up to 20 pts — bigger gap = bigger move)
             + min(rvol, 10)             (up to 10 pts — extracted from L2 detail)
             + 5  if L1 catalyst fired   (catalyst presence bonus)
             + 3  if L4 RS fired         (market outperformance bonus)
  Max: 88.  Ranked descending.
"""

from __future__ import annotations

import io
import os
import logging
import smtplib
from datetime import datetime
from email                import encoders
from email.mime.base      import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text      import MIMEText
from typing               import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ── Conviction scorer ─────────────────────────────────────────────────────────

def compute_conviction(row: pd.Series) -> float:
    """
    Sub-rank TRADE-tier stocks beyond the 0–5 signal score.
    Returns a float 0–88; higher = stronger setup.
    """
    score = float(row.get("Score", 0))
    gap   = abs(float(row.get("Gap %", 0)))
    l1    = str(row.get("L1 Catalyst", ""))
    l2    = str(row.get("L2 Volume",   ""))
    l4    = str(row.get("L4 RS",       ""))

    # Extract RVOL from L2 detail string, e.g. "RVOL = 4.2×"
    rvol = 0.0
    if "RVOL" in l2:
        try:
            rvol = float(l2.split("RVOL")[1].split("=")[1].split("×")[0].strip())
        except Exception:
            pass

    pts  = score * 10
    pts += min(gap,  10) * 2
    pts += min(rvol, 10)
    pts += 5 if l1 and "No catalyst"   not in l1 else 0
    pts += 3 if l4 and "insufficient"  not in l4 else 0
    return round(pts, 2)


def rank_trade_tier(df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    """
    Score and rank all TRADE-tier rows by conviction.
    Returns ALL TRADE rows ranked (caller slices to top_n for cards).
    Adds columns: Conviction (float), Rank (int, 1-based).
    """
    if df.empty or "Tier" not in df.columns:
        return pd.DataFrame()
    trade = df[df["Tier"] == "TRADE"].copy()
    if trade.empty:
        return pd.DataFrame()
    trade["Conviction"] = trade.apply(compute_conviction, axis=1)
    trade = trade.sort_values("Conviction", ascending=False).reset_index(drop=True)
    trade["Rank"] = range(1, len(trade) + 1)
    return trade


# ── CSV builders ──────────────────────────────────────────────────────────────

def build_top10_csv(df: pd.DataFrame, top_n: int = 10) -> str:
    """
    Build CSV string for the top-N TRADE ideas with Rank + Conviction columns.
    Columns ordered for readability: Rank first, then identity, then scores,
    then all five layer details, then data sources.
    """
    ranked = rank_trade_tier(df, top_n=top_n)
    if ranked.empty:
        return "No TRADE-tier stocks found in this session.\n"

    top = ranked.head(top_n).copy()

    # Column order — move Rank + Conviction to front, drop raw internal cols
    front_cols   = ["Rank", "Conviction", "Ticker", "Name", "Sector", "Tier",
                     "Score", "Session"]
    price_cols   = ["PM Change %", "Gap %", "PM Price", "Prev Close", "PM Volume"]
    signal_cols  = ["L1 Catalyst", "L2 Volume", "L3 Price", "L4 RS", "L5 Options"]
    source_cols  = ["Data Sources"]

    ordered = front_cols + price_cols + signal_cols + source_cols
    # Keep only columns that exist in the dataframe
    ordered = [c for c in ordered if c in top.columns]
    # Add any remaining columns not already included
    remaining = [c for c in top.columns if c not in ordered]
    ordered += remaining

    return top[ordered].to_csv(index=False)


def build_full_report_csv(df: pd.DataFrame) -> str:
    """
    Build CSV string for the complete run — ALL tickers (TRADE + WATCH + SKIP).
    Adds Conviction and Rank columns for TRADE tier; blank for others.
    """
    if df.empty:
        return "No results.\n"

    full = df.copy()

    # Add Conviction + Rank for all rows (TRADE gets real values, others get blanks)
    ranked_trade = rank_trade_tier(df)
    if not ranked_trade.empty:
        conv_map = dict(zip(ranked_trade["Ticker"], ranked_trade["Conviction"]))
        rank_map = dict(zip(ranked_trade["Ticker"], ranked_trade["Rank"]))
        full["Conviction"] = full["Ticker"].map(conv_map)
        full["Rank"]       = full["Ticker"].map(rank_map)
    else:
        full["Conviction"] = None
        full["Rank"]       = None

    # Column order
    front_cols  = ["Rank", "Conviction", "Ticker", "Name", "Sector", "Tier",
                   "Score", "Session"]
    price_cols  = ["PM Change %", "Gap %", "PM Price", "Prev Close", "PM Volume"]
    signal_cols = ["L1 Catalyst", "L2 Volume", "L3 Price", "L4 RS", "L5 Options"]
    source_cols = ["Data Sources"]

    ordered = front_cols + price_cols + signal_cols + source_cols
    ordered = [c for c in ordered if c in full.columns]
    remaining = [c for c in full.columns if c not in ordered]
    ordered += remaining

    return full[ordered].to_csv(index=False)


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _chg_color(pct: float) -> str:
    return "#16a34a" if pct >= 0 else "#dc2626"


def _score_dots(score: int) -> str:
    filled = "#16a34a" if score >= 4 else "#f59e0b"
    return "".join(
        f'<span style="display:inline-block;width:12px;height:12px;border-radius:50%;'
        f'margin-right:3px;background:{filled if i < score else "#e5e7eb"}"></span>'
        for i in range(5)
    )


def _conviction_bar(conviction: float, max_val: float = 88) -> str:
    pct   = min(conviction / max_val * 100, 100)
    color = "#16a34a" if pct >= 75 else "#f59e0b" if pct >= 50 else "#6b7280"
    label = "Strong" if pct >= 75 else "Moderate" if pct >= 50 else "Developing"
    return (
        f'<div style="background:#e5e7eb;border-radius:4px;height:6px;'
        f'width:100%;margin-top:4px">'
        f'<div style="background:{color};border-radius:4px;height:6px;'
        f'width:{pct:.0f}%"></div></div>'
        f'<div style="font-size:10px;color:#6b7280;margin-top:2px">'
        f'{label} · {conviction:.0f}/88</div>'
    )


def _layer_pills(row: pd.Series) -> str:
    layers = [
        ("L1", row.get("L1 Catalyst", ""), "#f59e0b", "#fffbeb"),
        ("L2", row.get("L2 Volume",   ""), "#3b82f6", "#eff6ff"),
        ("L3", row.get("L3 Price",    ""), "#8b5cf6", "#f5f3ff"),
        ("L4", row.get("L4 RS",       ""), "#10b981", "#ecfdf5"),
        ("L5", row.get("L5 Options",  ""), "#ef4444", "#fff1f2"),
    ]
    out = ""
    for label, detail, color, bg in layers:
        scored  = detail and "No " not in detail and "insufficient" not in detail
        opacity = "1" if scored else "0.35"
        out += (
            f'<span style="display:inline-block;background:{bg};color:{color};'
            f'border:1px solid {color};border-radius:12px;padding:2px 8px;'
            f'font-size:10px;font-weight:600;margin-right:4px;opacity:{opacity}">'
            f'{label}</span>'
        )
    return out


# ── Trade card (used for each top-N entry) ────────────────────────────────────

_RANK_COLORS = {1:"#f59e0b", 2:"#9ca3af", 3:"#b45309",
                4:"#6b7280", 5:"#6b7280", 6:"#6b7280",
                7:"#6b7280", 8:"#6b7280", 9:"#6b7280", 10:"#6b7280"}


def _trade_card(row: pd.Series, rank: int) -> str:
    ticker     = str(row.get("Ticker",     ""))
    name       = str(row.get("Name",       ""))[:34]
    sector     = str(row.get("Sector",     ""))
    score      = int(row.get("Score",       0))
    conviction = float(row.get("Conviction", 0))
    gap        = float(row.get("Gap %",      0))
    pm_price   = row.get("PM Price",    "—")
    prev_close = row.get("Prev Close",  "—")
    pm_vol     = row.get("PM Volume",    0)
    chg        = float(row.get("PM Change %", 0))
    catalyst   = str(row.get("L1 Catalyst",  ""))[:95]
    l2_detail  = str(row.get("L2 Volume",    ""))[:70]
    l4_detail  = str(row.get("L4 RS",        ""))[:70]
    session    = str(row.get("Session",       ""))

    rank_color = _RANK_COLORS.get(rank, "#6b7280")
    vol_fmt    = f"{int(pm_vol):,}" if pm_vol else "—"
    rank_bg    = rank_color + "22"   # 13% opacity background

    return f"""
<table width="100%" cellpadding="0" cellspacing="0"
       style="border:1px solid #e5e7eb;border-radius:10px;
              margin-bottom:14px;background:#ffffff;
              border-collapse:separate;border-spacing:0">
  <tr>
    <td style="padding:14px 18px">

      <!-- Row 1: rank badge + ticker/name + price change -->
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="38" style="vertical-align:middle">
            <div style="width:34px;height:34px;border-radius:50%;
                        background:{rank_bg};border:2px solid {rank_color};
                        color:{rank_color};font-size:13px;font-weight:700;
                        text-align:center;line-height:30px">#{rank}</div>
          </td>
          <td style="padding-left:10px;vertical-align:middle">
            <span style="font-size:17px;font-weight:700;color:#111827;
                         font-family:monospace">{ticker}</span>
            <span style="font-size:12px;color:#6b7280;margin-left:6px">{name}</span>
            <div style="font-size:11px;color:#9ca3af;margin-top:1px">{sector}</div>
          </td>
          <td align="right" style="vertical-align:top">
            <div style="font-size:20px;font-weight:700;
                        color:{_chg_color(chg)}">{chg:+.2f}%</div>
            <div style="font-size:11px;color:#6b7280">
              ${pm_price} vs ${prev_close}
            </div>
          </td>
        </tr>
      </table>

      <!-- Divider -->
      <div style="border-top:1px solid #f3f4f6;margin:10px 0"></div>

      <!-- Row 2: score dots + conviction bar -->
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td width="48%">
            <div style="font-size:10px;color:#6b7280;margin-bottom:3px">
              Signal score</div>
            {_score_dots(score)}
            <div style="font-size:10px;color:#6b7280;margin-top:2px">
              {score}/5 layers confirmed</div>
          </td>
          <td width="52%" style="padding-left:14px">
            <div style="font-size:10px;color:#6b7280;margin-bottom:3px">
              Conviction</div>
            {_conviction_bar(conviction)}
          </td>
        </tr>
      </table>

      <!-- Row 3: layer pills -->
      <div style="margin-top:8px">{_layer_pills(row)}</div>

      <!-- Row 4: catalyst callout -->
      <div style="margin-top:10px;padding:8px 12px;background:#fffbeb;
                  border-radius:6px;border-left:3px solid #f59e0b">
        <div style="font-size:10px;font-weight:600;color:#92400e;
                    margin-bottom:2px">KEY CATALYST</div>
        <div style="font-size:11px;color:#374151;line-height:1.5">{catalyst}</div>
      </div>

      <!-- Row 5: volume + RS detail -->
      <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:8px">
        <tr>
          <td width="50%" style="font-size:10px;color:#6b7280;
                                  padding-right:8px;vertical-align:top">
            <strong>Volume:</strong> {l2_detail[:55]}
          </td>
          <td width="50%" style="font-size:10px;color:#6b7280;
                                  vertical-align:top">
            <strong>RS:</strong> {l4_detail[:55]}
          </td>
        </tr>
      </table>

      <!-- Row 6: stat chips -->
      <table cellpadding="0" cellspacing="0" style="margin-top:10px">
        <tr>
          <td style="padding-right:6px">
            <div style="background:#f0fdf4;border:1px solid #bbf7d0;
                        border-radius:5px;padding:3px 9px;
                        font-size:11px;color:#166534;white-space:nowrap">
              Gap {gap:+.1f}%
            </div>
          </td>
          <td style="padding-right:6px">
            <div style="background:#eff6ff;border:1px solid #bfdbfe;
                        border-radius:5px;padding:3px 9px;
                        font-size:11px;color:#1e40af;white-space:nowrap">
              Vol {vol_fmt}
            </div>
          </td>
          <td>
            <div style="background:#f5f3ff;border:1px solid #ddd6fe;
                        border-radius:5px;padding:3px 9px;
                        font-size:11px;color:#5b21b6;white-space:nowrap">
              {session.title()}
            </div>
          </td>
        </tr>
      </table>

    </td>
  </tr>
</table>"""


# ── Summary table (all TRADE + WATCH) ────────────────────────────────────────

def _summary_table(df: pd.DataFrame) -> str:
    if df.empty:
        return '<p style="color:#6b7280;font-size:13px">No results.</p>'

    show = df[df["Tier"].isin(["TRADE","WATCH"])].copy() \
           if "Tier" in df.columns else df
    if show.empty:
        return '<p style="color:#6b7280;font-size:13px">No TRADE or WATCH tickers.</p>'

    # Add conviction for TRADE rows
    ranked = rank_trade_tier(df)
    if not ranked.empty:
        conv_map = dict(zip(ranked["Ticker"], ranked["Conviction"]))
        rank_map = dict(zip(ranked["Ticker"], ranked["Rank"]))
        show["_conv"] = show["Ticker"].map(conv_map)
        show["_rank"] = show["Ticker"].map(rank_map)
    else:
        show["_conv"] = None
        show["_rank"] = None

    tier_colors = {
        "TRADE": ("#065f46","#d1fae5"),
        "WATCH": ("#78350f","#fef3c7"),
    }
    rows_html = ""
    for _, r in show.iterrows():
        tier   = str(r.get("Tier",""))
        tc, bg = tier_colors.get(tier, ("#374151","#f3f4f6"))
        chg    = float(r.get("PM Change %", 0))
        gap    = float(r.get("Gap %",       0))
        score  = int(r.get("Score",          0))
        cat    = str(r.get("L1 Catalyst",   ""))[:55]
        conv   = r.get("_conv")
        rank_n = r.get("_rank")
        rank_str = f"#{int(rank_n)}" if pd.notna(rank_n) else "—"
        conv_str = f"{conv:.0f}" if pd.notna(conv) else "—"

        rows_html += f"""
        <tr style="border-bottom:1px solid #f3f4f6">
          <td style="padding:7px 8px;font-size:12px;color:#9ca3af;
                     text-align:center">{rank_str}</td>
          <td style="padding:7px 8px;font-weight:600;
                     font-family:monospace;font-size:13px">{r.get('Ticker','')}</td>
          <td style="padding:7px 8px;font-size:11px;
                     color:#374151">{str(r.get('Sector',''))[:16]}</td>
          <td style="padding:7px 8px">
            <span style="background:{bg};color:{tc};padding:2px 7px;
                         border-radius:10px;font-size:10px;
                         font-weight:600">{tier}</span>
          </td>
          <td style="padding:7px 8px;font-weight:600;
                     color:{_chg_color(chg)}">{chg:+.2f}%</td>
          <td style="padding:7px 8px;color:#374151;
                     font-size:12px">{gap:+.1f}%</td>
          <td style="padding:7px 8px;font-size:12px">{score}/5</td>
          <td style="padding:7px 8px;font-size:11px;
                     color:#6b7280;font-weight:500">{conv_str}</td>
          <td style="padding:7px 8px;font-size:10px;
                     color:#6b7280">{cat}</td>
        </tr>"""

    return f"""
    <table width="100%" cellpadding="0" cellspacing="0"
           style="border-collapse:collapse;font-family:-apple-system,sans-serif">
      <thead>
        <tr style="background:#1e293b">
          <th style="padding:7px 8px;text-align:center;color:#f1f5f9;font-size:10px">Rank</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Ticker</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Sector</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Tier</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Δ%</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Gap%</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Score</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Conv.</th>
          <th style="padding:7px 8px;text-align:left;color:#f1f5f9;font-size:10px">Catalyst</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>"""


# ── Full email HTML builder ───────────────────────────────────────────────────

def build_email_html(df: pd.DataFrame, run_time: Optional[datetime] = None,
                     top_n: int = 10) -> str:
    run_time = run_time or datetime.now()
    ts       = run_time.strftime("%A, %B %d %Y  ·  %I:%M %p ET")
    session  = _infer_session(run_time)

    n_trade  = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
    n_watch  = len(df[df["Tier"] == "WATCH"]) if "Tier" in df.columns else 0
    n_total  = len(df)

    # Rank ALL trade stocks; take top_n for cards
    all_ranked = rank_trade_tier(df)
    top_cards  = all_ranked.head(top_n) if not all_ranked.empty else pd.DataFrame()
    actual_n   = len(top_cards)

    if top_cards.empty:
        cards_html = """
        <div style="padding:24px;text-align:center;background:#f9fafb;
                    border-radius:8px;color:#6b7280;font-size:13px">
          No TRADE-tier stocks found in this session.
        </div>"""
    else:
        cards_html = "".join(
            _trade_card(row, int(row["Rank"]))
            for _, row in top_cards.iterrows()
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pre-Market Momentum Alert</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9">
<tr><td align="center" style="padding:24px 16px">
<table width="620" cellpadding="0" cellspacing="0" style="max-width:620px;width:100%">

  <!-- HEADER -->
  <tr><td style="background:#0f172a;border-radius:12px 12px 0 0;padding:22px 28px">
    <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                letter-spacing:0.1em;margin-bottom:4px">Pre-Market Momentum Screener</div>
    <div style="font-size:22px;font-weight:700;color:#f8fafc">{session} Alert</div>
    <div style="font-size:12px;color:#94a3b8;margin-top:3px">{ts}</div>
  </td></tr>

  <!-- SUMMARY BAR -->
  <tr><td style="background:#1e293b;padding:14px 28px">
    <table cellpadding="0" cellspacing="0"><tr>
      <td style="padding-right:28px">
        <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                    letter-spacing:0.05em">TRADE</div>
        <div style="font-size:30px;font-weight:700;color:#34d399">{n_trade}</div>
      </td>
      <td style="padding-right:28px">
        <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                    letter-spacing:0.05em">WATCH</div>
        <div style="font-size:30px;font-weight:700;color:#fbbf24">{n_watch}</div>
      </td>
      <td>
        <div style="font-size:10px;color:#64748b;text-transform:uppercase;
                    letter-spacing:0.05em">TOTAL MOVERS</div>
        <div style="font-size:30px;font-weight:700;color:#e2e8f0">{n_total}</div>
      </td>
    </tr></table>
  </td></tr>

  <!-- ATTACHMENTS NOTE -->
  <tr><td style="background:#1e3a5f;padding:10px 28px">
    <div style="font-size:11px;color:#93c5fd">
      📎 <strong>2 files attached:</strong>
      &nbsp; top{actual_n}_trade_{run_time.strftime('%Y%m%d_%H%M')}.csv
      &nbsp;·&nbsp;
      full_report_{run_time.strftime('%Y%m%d_%H%M')}.csv
    </div>
  </td></tr>

  <!-- TOP-N SECTION -->
  <tr><td style="background:#ffffff;padding:22px 28px">
    <div style="font-size:16px;font-weight:600;color:#0f172a;margin-bottom:3px">
      🏆 Top {actual_n} TRADE Ideas — Conviction Ranked
    </div>
    <div style="font-size:11px;color:#64748b;margin-bottom:16px">
      Ranked by conviction: signal strength · gap size · relative volume · catalyst quality
    </div>
    {cards_html}
  </td></tr>

  <!-- FULL RANKED TABLE -->
  <tr><td style="background:#ffffff;padding:0 28px 22px">
    <div style="border-top:1px solid #e5e7eb;padding-top:18px;margin-bottom:10px">
      <div style="font-size:14px;font-weight:600;color:#0f172a">
        All TRADE &amp; WATCH Stocks
      </div>
      <div style="font-size:11px;color:#64748b">
        Complete session results — sorted by conviction
      </div>
    </div>
    {_summary_table(df)}
  </td></tr>

  <!-- FOOTER -->
  <tr><td style="background:#f8fafc;border-radius:0 0 12px 12px;
                  padding:14px 28px;border-top:1px solid #e5e7eb">
    <div style="font-size:10px;color:#94a3b8;line-height:1.7">
      <strong style="color:#64748b">Data:</strong>
      FMP (quotes, earnings, news, grades) · yfinance (options, IV)
      · SEC EDGAR (8-K filings) · GitHub (universe)<br>
      <strong style="color:#64748b">Attached CSVs:</strong>
      top-{actual_n} TRADE ideas with conviction scores ·
      full report with all TRADE/WATCH/SKIP results<br>
      <strong style="color:#64748b">Score:</strong>
      4–5 = TRADE &nbsp;·&nbsp; 3 = WATCH &nbsp;·&nbsp; &lt;3 = SKIP
      &nbsp;·&nbsp;
      <strong style="color:#94a3b8">Not financial advice.</strong>
    </div>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def _infer_session(dt: datetime) -> str:
    h = dt.hour
    if 4 <= h < 9:   return "Pre-Market"
    if 9 <= h < 16:  return "Market Hours"
    return "After-Hours"


# ── Email sender ──────────────────────────────────────────────────────────────

def send_momentum_email(df: pd.DataFrame,
                        csv_path: str,          # full report CSV (already saved to disk)
                        run_time: datetime,
                        cfg: dict,
                        top_n: int = 10) -> bool:
    """
    Send the momentum email with two CSV attachments:
      1. top{N}_trade_YYYYMMDD_HHMM.csv  — top-N TRADE ideas (Rank + Conviction)
      2. full_report_YYYYMMDD_HHMM.csv   — complete run (all tickers)

    cfg keys: smtp_host, smtp_port, smtp_user, smtp_password, recipients
    Returns True on success.
    """
    if not cfg.get("recipients"):
        logger.warning("Email skipped — no recipients configured")
        return False

    n_trade = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
    session = _infer_session(run_time)
    stamp   = run_time.strftime("%Y%m%d_%H%M")

    subject = (
        f"[Pre-Mkt] {session} · {n_trade} TRADE "
        f"{'idea' if n_trade == 1 else 'ideas'} · "
        f"{run_time.strftime('%b %d %Y %I:%M %p ET')}"
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = cfg["smtp_user"]
    msg["To"]      = ", ".join(cfg["recipients"])

    # HTML body
    html_body = build_email_html(df, run_time=run_time, top_n=top_n)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # ── Attachment 1: top-N TRADE ideas CSV ────────────────────────────────
    try:
        top_csv_str   = build_top10_csv(df, top_n=top_n)
        top_csv_bytes = top_csv_str.encode("utf-8")
        top_fname     = f"top{top_n}_trade_{stamp}.csv"
        part1 = MIMEBase("text", "csv")
        part1.set_payload(top_csv_bytes)
        encoders.encode_base64(part1)
        part1.add_header("Content-Disposition", f'attachment; filename="{top_fname}"')
        part1.add_header("Content-Type", 'text/csv; charset=utf-8')
        msg.attach(part1)
        logger.info("Attachment 1: %s (%d bytes, %d rows)",
                    top_fname, len(top_csv_bytes),
                    top_csv_str.count("\n") - 1)
    except Exception as e:
        logger.warning("Could not build top-N CSV: %s", e)

    # ── Attachment 2: full report CSV (all tickers) ─────────────────────────
    try:
        full_csv_str   = build_full_report_csv(df)
        full_csv_bytes = full_csv_str.encode("utf-8")
        full_fname     = f"full_report_{stamp}.csv"
        part2 = MIMEBase("text", "csv")
        part2.set_payload(full_csv_bytes)
        encoders.encode_base64(part2)
        part2.add_header("Content-Disposition", f'attachment; filename="{full_fname}"')
        part2.add_header("Content-Type", 'text/csv; charset=utf-8')
        msg.attach(part2)
        logger.info("Attachment 2: %s (%d bytes, %d rows)",
                    full_fname, len(full_csv_bytes),
                    full_csv_str.count("\n") - 1)
    except Exception as e:
        logger.warning("Could not build full report CSV: %s", e)

    # ── Send ────────────────────────────────────────────────────────────────
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.sendmail(cfg["smtp_user"], cfg["recipients"], msg.as_string())
        logger.info("✓ Email sent → %s  |  subject: %s",
                    cfg["recipients"], subject)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.error(
            "✗ SMTP auth failed.\n"
            "  Gmail: use an App Password (not account password).\n"
            "  Generate: https://myaccount.google.com/apppasswords\n"
            "  Set: export SMTP_PASSWORD='xxxx xxxx xxxx xxxx'"
        )
        return False
    except Exception as e:
        logger.error("✗ Email failed: %s", e)
        return False
