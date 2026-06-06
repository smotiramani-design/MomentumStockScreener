"""
utils/report.py — HTML report with data-source provenance panel per ticker row.
"""

from __future__ import annotations

import os
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text       import MIMEText
from pathlib               import Path
from typing                import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ── Design tokens ────────────────────────────────────────────────────────────
TIER_COLORS = {
    "TRADE": ("#064e3b", "#d1fae5"),
    "WATCH": ("#78350f", "#fef3c7"),
    "SKIP":  ("#4b5563", "#f3f4f6"),
}
SCORE_COLORS = ["#ef4444","#f97316","#eab308","#22c55e","#16a34a","#15803d"]

PROVIDER_BADGE = {
    "FMP":            ("#1e40af", "#dbeafe"),   # blue
    "yfinance":       ("#065f46", "#d1fae5"),   # green
    "SEC EDGAR":      ("#7c2d12", "#ffedd5"),   # orange
    "Benzinga":       ("#4c1d95", "#ede9fe"),   # purple
    "Unusual Whales": ("#831843", "#fce7f3"),   # pink
    "computed":       ("#374151", "#f3f4f6"),   # gray
    "N/A":            ("#9ca3af", "#f9fafb"),   # light gray
    "not configured": ("#9ca3af", "#f9fafb"),
}

LAYER_META = {
    "l1": ("L1", "Catalyst",        "#f59e0b"),
    "l2": ("L2", "Volume",          "#3b82f6"),
    "l3": ("L3", "Price Action",    "#8b5cf6"),
    "l4": ("L4", "Relative Strength","#10b981"),
    "l5": ("L5", "Options",         "#f43f5e"),
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _score_bar(score: int) -> str:
    color = SCORE_COLORS[min(score, 5)]
    return "".join(
        f'<span style="display:inline-block;width:13px;height:13px;border-radius:3px;'
        f'margin-right:2px;background:{color if i < score else "#e5e7eb"}"></span>'
        for i in range(5)
    )

def _tier_badge(tier: str) -> str:
    tc, bg = TIER_COLORS.get(tier, TIER_COLORS["SKIP"])
    return (f'<span style="background:{bg};color:{tc};padding:2px 9px;'
            f'border-radius:12px;font-size:11px;font-weight:700">{tier}</span>')

def _provider_badge(provider: str) -> str:
    tc, bg = PROVIDER_BADGE.get(provider, ("#374151","#f3f4f6"))
    return (f'<span style="background:{bg};color:{tc};padding:1px 7px;'
            f'border-radius:10px;font-size:10px;font-weight:600;'
            f'white-space:nowrap">{provider}</span>')

def _chg_span(chg: float) -> str:
    color = "#16a34a" if chg >= 0 else "#dc2626"
    return f'<span style="color:{color};font-weight:600">{chg:+.2f}%</span>'


# ── Source provenance panel (expandable per ticker) ──────────────────────────

def _source_panel(ticker: str, raw_signals: dict) -> str:
    """
    Renders a full per-ticker data-source table showing every data point,
    its value, which provider supplied it, and the exact endpoint called.
    Hidden by default — toggled with a button.
    """
    panel_id = f"src-{ticker}"
    rows = []
    for key, (short, label, color) in LAYER_META.items():
        slist = raw_signals.get(f"{key}_sources", [])
        if not slist:
            continue
        # Layer header row
        rows.append(
            f'<tr><td colspan="4" style="background:{color}18;padding:6px 10px;'
            f'font-size:11px;font-weight:700;color:{color};border-top:2px solid {color}33">'
            f'{short} — {label}</td></tr>'
        )
        for s in slist:
            rows.append(
                f'<tr style="border-bottom:1px solid #f3f4f6">'
                f'<td style="padding:4px 10px 4px 20px;font-size:11px;color:#374151;'
                f'width:160px">{s["field"]}</td>'
                f'<td style="padding:4px 10px;font-size:11px;font-weight:600;'
                f'color:#111827;width:120px">{s["value"]}</td>'
                f'<td style="padding:4px 10px;width:130px">{_provider_badge(s["provider"])}</td>'
                f'<td style="padding:4px 10px;font-size:10px;color:#6b7280;'
                f'font-family:monospace">{s["endpoint"]}</td>'
                f'</tr>'
            )

    if not rows:
        return ""

    table = (
        f'<table style="width:100%;border-collapse:collapse;background:#fafafa;'
        f'border:1px solid #e5e7eb;border-radius:6px;overflow:hidden">'
        f'<thead><tr style="background:#f1f5f9">'
        f'<th style="padding:6px 10px;text-align:left;font-size:11px;color:#374151">Field</th>'
        f'<th style="padding:6px 10px;text-align:left;font-size:11px;color:#374151">Value</th>'
        f'<th style="padding:6px 10px;text-align:left;font-size:11px;color:#374151">Provider</th>'
        f'<th style="padding:6px 10px;text-align:left;font-size:11px;color:#374151">Endpoint / Method</th>'
        f'</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )

    return (
        f'<tr id="{panel_id}" style="display:none">'
        f'<td colspan="14" style="padding:0 12px 12px 12px;background:#f8fafc">'
        f'<div style="font-size:11px;font-weight:600;color:#6b7280;'
        f'margin:8px 0 6px">Data provenance — {ticker}</div>'
        f'{table}</td></tr>'
    )


# ── Main row ─────────────────────────────────────────────────────────────────

def _row_html(row: pd.Series) -> str:
    ticker = row.get("Ticker", "")
    score  = int(row.get("Score", 0))
    chg    = float(row.get("PM Change %", 0))

    # Provider badges for the summary column — derived from Data Sources string
    src_str  = str(row.get("Data Sources", ""))
    providers_used = sorted(set(
        p for part in src_str.split("  ")
        for p in part[part.find("[")+1:part.find("]")].split("|")
        if p
    ))
    provider_badges = " ".join(_provider_badge(p) for p in providers_used)

    def cell(v, bold=False, mono=False, small=False):
        s = ""
        if bold:  s += "font-weight:600;"
        if mono:  s += "font-family:monospace;"
        if small: s += "font-size:11px;color:#374151;"
        return f'<td style="padding:8px 10px;{s}">{v}</td>'

    toggle_btn = (
        f'<button onclick="toggleRow(\'{ticker}\')" '
        f'style="font-size:10px;background:#f1f5f9;border:1px solid #e2e8f0;'
        f'border-radius:4px;padding:2px 7px;cursor:pointer;color:#64748b" '
        f'id="btn-{ticker}">▶ sources</button>'
    )

    return (
        f"<tr style='border-bottom:1px solid #e5e7eb'>"
        f"{cell(f'{ticker} {toggle_btn}', bold=True)}"
        f"{cell(str(row.get('Name',''))[:28])}"
        f"{cell(str(row.get('Sector',''))[:22])}"
        f"{cell(_tier_badge(str(row.get('Tier','SKIP'))))}"
        f"{cell(_score_bar(score))}"
        f"{cell(_chg_span(chg))}"
        f"<td style='padding:8px 10px'>{row.get('PM Price','')}</td>"
        f"<td style='padding:8px 10px'>{row.get('Prev Close','')}</td>"
        f"<td style='padding:8px 10px;font-size:11px'>{provider_badges}</td>"
        f"{cell(str(row.get('L1 Catalyst',''))[:60], small=True)}"
        f"{cell(str(row.get('L2 Volume',''))[:55], small=True)}"
        f"{cell(str(row.get('L3 Price',''))[:55], small=True)}"
        f"{cell(str(row.get('L4 RS',''))[:55], small=True)}"
        f"{cell(str(row.get('L5 Options',''))[:55], small=True)}"
        f"</tr>"
    )


# ── Full HTML report ─────────────────────────────────────────────────────────

def build_html_report(df: pd.DataFrame, run_time: Optional[datetime] = None) -> str:
    run_time = run_time or datetime.now()
    ts = run_time.strftime("%A %B %d, %Y  ·  %I:%M %p ET")

    n_trade = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
    n_watch = len(df[df["Tier"] == "WATCH"]) if "Tier" in df.columns else 0

    def badge(label, n, bg, tc):
        return (f'<span style="background:{bg};color:{tc};padding:5px 16px;'
                f'border-radius:20px;margin-right:8px;font-size:13px;font-weight:700">'
                f'{label}: {n}</span>')

    summary = (
        badge("TRADE", n_trade, "#d1fae5", "#065f46") +
        badge("WATCH", n_watch, "#fef3c7", "#78350f") +
        badge("TOTAL", len(df),  "#e0e7ff", "#3730a3")
    )

    # Legend for provider badges
    legend_items = "".join(
        f'<span style="margin-right:12px">{_provider_badge(p)} '
        f'<span style="font-size:11px;color:#6b7280">'
        + {
            "FMP":            "financialmodelingprep.com/stable",
            "yfinance":       "finance.yahoo.com (via yfinance lib)",
            "SEC EDGAR":      "efts.sec.gov / EDGAR full-text search",
            "Benzinga":       "api.benzinga.com (paid key required)",
            "Unusual Whales": "unusualwhales.com (paid key required)",
            "computed":       "derived / calculated in-process",
        }.get(p, p) +
        f'</span></span>'
        for p in ["FMP","yfinance","SEC EDGAR","Benzinga","Unusual Whales","computed"]
    )

    headers = ["Ticker","Name","Sector","Tier","Score","PM Chg%",
               "PM Price","Prev Close","Sources",
               "L1 Catalyst","L2 Volume","L3 Price","L4 RS","L5 Options"]
    thead = "".join(
        f'<th style="padding:10px;text-align:left;background:#1e293b;'
        f'color:#f1f5f9;font-size:11px;white-space:nowrap">{h}</th>'
        for h in headers
    )

    if df.empty:
        body_rows = ('<tr><td colspan="14" style="padding:40px;text-align:center;'
                     'color:#9ca3af">No momentum stocks found.</td></tr>')
    else:
        body_rows = ""
        for _, row in df.iterrows():
            ticker = row.get("Ticker", "")
            body_rows += _row_html(row)
            # Inject hidden source panel row immediately after the data row
            rs = {}
            # raw_signals not in DataFrame rows — source panel uses Data Sources string
            # For full panel we need the raw_signals dict; skip panel if not available
            body_rows += ""   # placeholder; full panel added by save_outputs via _results

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pre-Market Momentum Screener</title>
<style>
* {{ box-sizing:border-box;margin:0;padding:0 }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f8fafc;color:#0f172a;padding:20px }}
h1   {{ font-size:21px;font-weight:700 }}
.sub {{ font-size:13px;color:#64748b;margin-top:4px }}
.card{{ background:#fff;border-radius:10px;border:1px solid #e2e8f0;
       padding:16px 20px;margin:14px 0;overflow-x:auto }}
table{{ border-collapse:collapse;width:100%;min-width:960px }}
tr:hover td {{ background:#f8fafc }}
</style>
<script>
function toggleRow(ticker) {{
  var row = document.getElementById('src-' + ticker);
  var btn = document.getElementById('btn-' + ticker);
  if (row.style.display === 'none') {{
    row.style.display = '';
    btn.textContent = '▼ sources';
    btn.style.background = '#dbeafe';
    btn.style.color = '#1e40af';
  }} else {{
    row.style.display = 'none';
    btn.textContent = '▶ sources';
    btn.style.background = '#f1f5f9';
    btn.style.color = '#64748b';
  }}
}}
</script>
</head>
<body>
  <h1>🔍 Pre-Market Momentum Screener</h1>
  <p class="sub">{ts}</p>

  <div class="card" style="margin-top:14px">{summary}</div>

  <div class="card" style="padding:12px 20px">
    <div style="font-size:11px;font-weight:600;color:#374151;margin-bottom:8px">
      DATA SOURCES LEGEND</div>
    <div style="display:flex;flex-wrap:wrap;gap:4px">{legend_items}</div>
  </div>

  <div class="card">
    <p style="font-size:11px;color:#64748b;margin-bottom:10px">
      Click <strong>▶ sources</strong> on any row to expand the full data provenance
      — every field, its value, which provider supplied it, and the exact API endpoint.
    </p>
    <table>
      <thead><tr>{thead}</tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>

  <p style="font-size:10px;color:#94a3b8;margin-top:10px">
    Score 4–5 = TRADE · 3 = WATCH · &lt;3 = SKIP. Not financial advice.
  </p>
</body>
</html>"""


def build_html_report_with_sources(
    df: pd.DataFrame,
    results_map: dict,          # ticker → MomentumResult.raw_signals
    run_time: Optional[datetime] = None,
) -> str:
    """
    Full report with per-ticker expandable source panels.
    results_map is passed from save_outputs to inject raw_signals.
    """
    run_time = run_time or datetime.now()
    ts = run_time.strftime("%A %B %d, %Y  ·  %I:%M %p ET")

    n_trade = len(df[df["Tier"] == "TRADE"]) if "Tier" in df.columns else 0
    n_watch = len(df[df["Tier"] == "WATCH"]) if "Tier" in df.columns else 0

    def badge(label, n, bg, tc):
        return (f'<span style="background:{bg};color:{tc};padding:5px 16px;'
                f'border-radius:20px;margin-right:8px;font-size:13px;font-weight:700">'
                f'{label}: {n}</span>')

    summary = (
        badge("TRADE", n_trade, "#d1fae5", "#065f46") +
        badge("WATCH", n_watch, "#fef3c7", "#78350f") +
        badge("TOTAL", len(df),  "#e0e7ff", "#3730a3")
    )

    legend_items = "".join(
        f'<span style="margin-right:12px">{_provider_badge(p)} '
        f'<span style="font-size:11px;color:#6b7280">'
        + {
            "FMP":            "financialmodelingprep.com/stable",
            "yfinance":       "finance.yahoo.com (via yfinance lib)",
            "SEC EDGAR":      "efts.sec.gov / EDGAR full-text search",
            "Benzinga":       "api.benzinga.com (paid key required)",
            "Unusual Whales": "unusualwhales.com (paid key required)",
            "computed":       "derived / calculated in-process",
        }.get(p, p) + f'</span></span>'
        for p in ["FMP","yfinance","SEC EDGAR","Benzinga","Unusual Whales","computed"]
    )

    headers = ["Ticker","Name","Sector","Tier","Score","PM Chg%",
               "PM Price","Prev Close","Sources",
               "L1 Catalyst","L2 Volume","L3 Price","L4 RS","L5 Options"]
    thead = "".join(
        f'<th style="padding:10px;text-align:left;background:#1e293b;'
        f'color:#f1f5f9;font-size:11px;white-space:nowrap">{h}</th>'
        for h in headers
    )

    body_rows = ""
    if df.empty:
        body_rows = ('<tr><td colspan="14" style="padding:40px;text-align:center;'
                     'color:#9ca3af">No momentum stocks found.</td></tr>')
    else:
        for _, row in df.iterrows():
            ticker = str(row.get("Ticker", ""))
            body_rows += _row_html(row)
            raw_sig = results_map.get(ticker, {})
            body_rows += _source_panel(ticker, raw_sig)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pre-Market Momentum Screener</title>
<style>
* {{ box-sizing:border-box;margin:0;padding:0 }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#f8fafc;color:#0f172a;padding:20px }}
h1   {{ font-size:21px;font-weight:700 }}
.sub {{ font-size:13px;color:#64748b;margin-top:4px }}
.card{{ background:#fff;border-radius:10px;border:1px solid #e2e8f0;
       padding:16px 20px;margin:14px 0;overflow-x:auto }}
table {{ border-collapse:collapse;width:100%;min-width:960px }}
tr:hover > td {{ background:#f8fafc }}
</style>
<script>
function toggleRow(ticker) {{
  var row = document.getElementById('src-' + ticker);
  var btn = document.getElementById('btn-' + ticker);
  if (!row) return;
  if (row.style.display === 'none' || row.style.display === '') {{
    row.style.display = 'table-row';
    btn.textContent = '▼ sources';
    btn.style.background = '#dbeafe';
    btn.style.color = '#1e40af';
  }} else {{
    row.style.display = 'none';
    btn.textContent = '▶ sources';
    btn.style.background = '#f1f5f9';
    btn.style.color = '#64748b';
  }}
}}
</script>
</head>
<body>
  <h1>🔍 Pre-Market Momentum Screener</h1>
  <p class="sub">{ts}</p>
  <div class="card" style="margin-top:14px">{summary}</div>
  <div class="card" style="padding:12px 20px">
    <div style="font-size:11px;font-weight:600;color:#374151;margin-bottom:8px">
      DATA SOURCES LEGEND</div>
    <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
      {legend_items}
    </div>
  </div>
  <div class="card">
    <p style="font-size:11px;color:#64748b;margin-bottom:10px">
      Click <strong>▶ sources</strong> on any row to expand full data provenance
      — field, value, provider, and exact API endpoint for every signal input.
    </p>
    <table>
      <thead><tr>{thead}</tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>
  <p style="font-size:10px;color:#94a3b8;margin-top:10px">
    Score 4–5 = TRADE · 3 = WATCH · &lt;3 = SKIP. Not financial advice.
  </p>
</body>
</html>"""


# ── Save outputs ─────────────────────────────────────────────────────────────

def save_outputs(df: pd.DataFrame, output_dir: str = "./output",
                 results_map: Optional[dict] = None,
                 send_email: bool = False,
                 email_cfg: Optional[dict] = None,
                 email_top_n: int = 5,
                 **kwargs) -> dict:
    from utils.email_builder import send_momentum_email

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    run_time = datetime.now()
    stamp    = run_time.strftime("%Y%m%d_%H%M")
    paths: dict = {}

    # CSV — always saved; also attached to email
    csv_path = os.path.join(output_dir, f"momentum_{stamp}.csv")
    df.to_csv(csv_path, index=False)
    paths["csv"] = csv_path
    logger.info("CSV saved: %s", csv_path)

    # Full HTML report (browser view with expandable source panels)
    html = build_html_report_with_sources(df, results_map or {}, run_time)
    html_path = os.path.join(output_dir, f"momentum_{stamp}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    paths["html"] = html_path
    logger.info("HTML report saved: %s", html_path)

    # Email — top-10 cards in body + two CSV attachments generated inline
    # Diagnostic logging so silent failures are immediately visible in the console
    if not send_email:
        logger.info("Email: disabled  (set send_email=True in config.py, "
                    "or use --email flag)")
    elif not email_cfg:
        logger.warning("Email: skipped  (email_cfg is None — internal error)")
    elif not email_cfg.get("smtp_user"):
        logger.warning("Email: skipped  ✗ smtp_user is empty  "
                       "→ run: export SMTP_USER='you@gmail.com'")
    elif not email_cfg.get("smtp_password"):
        logger.warning("Email: skipped  ✗ smtp_password is empty  "
                       "→ run: export SMTP_PASSWORD='your_16_char_app_password'")
    elif not email_cfg.get("recipients"):
        logger.warning("Email: skipped  ✗ email_recipients is empty  "
                       "→ add your address to OUTPUT.email_recipients in config.py")
    else:
        logger.info("Email: sending to %s…", email_cfg["recipients"])
        ok = send_momentum_email(
            df       = df,
            csv_path = csv_path,
            run_time = run_time,
            cfg      = email_cfg,
            top_n    = email_top_n,
        )
        paths["email"] = "sent" if ok else "failed"
        if ok:
            logger.info("Email: ✓ sent → %s", email_cfg["recipients"])
        else:
            logger.error("Email: ✗ failed — run: python test_email.py")

    # SMS alert — short summary + top-3 TRADE ideas
    # Triggered when send_sms=True in config or --sms CLI flag
    # Handled via sms_cfg passed from run_screener (None if not enabled)
    sms_cfg = kwargs.get("sms_cfg")
    if sms_cfg and sms_cfg.get("send_sms") and sms_cfg.get("to_numbers"):
        from utils.sms_builder import send_momentum_sms
        logger.info("SMS: sending to %s…", sms_cfg["to_numbers"])
        ok_sms = send_momentum_sms(
            df       = df,
            run_time = run_time,
            sms_cfg  = sms_cfg,
            top_n    = sms_cfg.get("sms_top_n", 3),
        )
        paths["sms"] = "sent" if ok_sms else "failed"
        if ok_sms:
            logger.info("SMS: ✓ sent → %s", sms_cfg["to_numbers"])
        else:
            logger.error("SMS: ✗ failed — check sms credentials in config.py")
    elif sms_cfg and sms_cfg.get("send_sms") and not sms_cfg.get("to_numbers"):
        logger.warning("SMS: skipped — to_numbers is empty in SMSConfig")

    return paths