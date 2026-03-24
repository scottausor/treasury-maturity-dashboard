#!/usr/bin/env python3
"""
Treasury MSPD Scraper
=====================
Downloads the Monthly Statement of Public Debt (MSPD) from Treasury Direct,
extracts all outstanding marketable securities with their maturity dates,
aggregates month-by-month debt maturities, and generates a standalone
interactive HTML dashboard.

Usage:
    python treasury_scraper.py                    # Auto-detect latest month
    python treasury_scraper.py --year 2026 --month 2   # Specific month
    python treasury_scraper.py --pdf path/to/file.pdf  # Use local PDF
"""

import requests
import pdfplumber
import pandas as pd
import json
import re
import logging
import argparse
import sys
import os
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from pathlib import Path

# ── Logging ─────────────────────────────────────────────────────────────────

LOG_FILE = Path(__file__).parent / "treasury_scraper.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL = (
    "https://fiscaldata.treasury.gov/static-data/published-reports/"
    "mspd-entire/MonthlyStatementPublicDebt_Entire_{YYYYMM}.pdf"
)

SECURITY_TYPES = {
    "bill":  ["treasury bill", "t-bill", "cash management bill", "cmb"],
    "note":  ["treasury note", "t-note"],
    "bond":  ["treasury bond", "t-bond"],
    "tips":  ["inflation-protected", "tips", "inflation indexed"],
    "frn":   ["floating rate", "frn"],
    "strip": ["strip", "stripped"],
}

# Columns we expect in the parsed securities table
EXPECTED_COLUMNS = ["type", "rate", "issue_date", "maturity_date", "amount_millions"]


# ── URL helpers ───────────────────────────────────────────────────────────────

def build_url(year: int, month: int) -> str:
    return BASE_URL.format(YYYYMM=f"{year}{month:02d}")


def get_candidate_months() -> list[tuple[int, int]]:
    """
    Returns (year, month) pairs to try, starting from last month
    (MSPD is usually published ~5th of the following month).
    """
    today = date.today()
    candidates = []
    for delta in range(0, 4):
        d = today - relativedelta(months=delta)
        candidates.append((d.year, d.month))
    return candidates


# ── PDF download ──────────────────────────────────────────────────────────────

def download_pdf(url: str, save_path: Path) -> Path:
    """Download a PDF to disk. Returns the local path."""
    log.info(f"Downloading: {url}")
    resp = requests.get(url, timeout=60, stream=True)
    resp.raise_for_status()

    save_path.parent.mkdir(parents=True, exist_ok=True)
    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    size_mb = save_path.stat().st_size / 1_048_576
    log.info(f"Saved {size_mb:.1f} MB → {save_path}")
    return save_path


def find_or_download_pdf(year: int = None, month: int = None) -> tuple[Path, str]:
    """
    Find a cached PDF or download the latest available one.
    Returns (local_path, yyyymm_label).
    """
    cache_dir = Path(__file__).parent / "pdf_cache"
    cache_dir.mkdir(exist_ok=True)

    if year and month:
        candidates = [(year, month)]
    else:
        candidates = get_candidate_months()

    for y, m in candidates:
        label = f"{y}{m:02d}"
        cached = cache_dir / f"MSPD_{label}.pdf"
        if cached.exists():
            log.info(f"Using cached PDF: {cached}")
            return cached, label

        url = build_url(y, m)
        try:
            r = requests.head(url, timeout=15)
            if r.status_code == 200:
                return download_pdf(url, cached), label
            else:
                log.warning(f"HTTP {r.status_code} for {label}, trying earlier month…")
        except requests.RequestException as e:
            log.warning(f"Could not reach {url}: {e}")

    raise RuntimeError("Could not find or download any MSPD PDF. "
                       "Try passing --year and --month explicitly, or --pdf.")


# ── PDF parsing ───────────────────────────────────────────────────────────────

DATE_PATTERNS = [
    r"\d{2}/\d{2}/\d{4}",   # 02/15/2026
    r"\d{2}-\d{2}-\d{4}",   # 02-15-2026
    r"[A-Z][a-z]{2}[\s\-]\d{1,2},?\s+\d{4}",  # Feb 15, 2026
]
DATE_RE = re.compile("|".join(DATE_PATTERNS))
AMOUNT_RE = re.compile(r"[\d,]{4,}")  # numbers like 38,400 or 125000


def parse_date(s: str) -> date | None:
    if not s:
        return None
    s = s.strip()
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%b %d, %Y", "%b %d %Y", "%b-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_amount(s: str) -> float | None:
    """Parse a number string like '38,400' → 38400.0"""
    if not s:
        return None
    cleaned = re.sub(r"[^\d.]", "", s.replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def classify_security(text: str) -> str:
    """Return the security type given a row's text."""
    t = text.lower()
    for sec_type, keywords in SECURITY_TYPES.items():
        if any(k in t for k in keywords):
            return sec_type
    return "other"


def extract_tables_from_pdf(pdf_path: Path) -> pd.DataFrame:
    """
    Main extraction function.
    Strategy:
      1. Use pdfplumber table extraction (structured rows/columns)
      2. Fall back to regex scanning of raw text per page
    Returns a DataFrame with columns: type, rate, issue_date, maturity_date, amount_millions
    """
    log.info(f"Opening PDF: {pdf_path}")
    records = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        log.info(f"PDF has {len(pdf.pages)} pages")
        current_type = "unknown"

        for page_num, page in enumerate(pdf.pages, 1):
            page_text = page.extract_text() or ""

            # Track which security type we're in based on section headers
            for sec_type, keywords in SECURITY_TYPES.items():
                for kw in keywords:
                    if kw in page_text.lower():
                        current_type = sec_type
                        break

            # ── Strategy 1: pdfplumber table extraction ───────────────────
            tables = page.extract_tables()
            for table in tables:
                if not table:
                    continue
                rows_parsed = _parse_table(table, current_type)
                records.extend(rows_parsed)

            # ── Strategy 2: line-by-line regex fallback ───────────────────
            if not tables:
                line_records = _parse_text_lines(page_text, current_type)
                records.extend(line_records)

            if page_num % 20 == 0:
                log.info(f"  Processed {page_num}/{len(pdf.pages)} pages, "
                         f"{len(records)} records so far…")

    df = pd.DataFrame(records, columns=EXPECTED_COLUMNS) if records else pd.DataFrame(columns=EXPECTED_COLUMNS)
    log.info(f"Raw extraction: {len(df)} rows")
    return df


def _parse_table(table: list, current_type: str) -> list[dict]:
    """Parse a pdfplumber table into security records."""
    records = []
    # Try to detect header row
    header_idx = 0
    for i, row in enumerate(table[:4]):
        if row and any(
            cell and any(k in str(cell).lower() for k in ["rate", "interest", "maturity", "issue", "amount", "coupon"])
            for cell in row
        ):
            header_idx = i
            break

    for row in table[header_idx + 1:]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue
        row_text = " ".join(str(c) for c in row if c)
        rec = _try_parse_row(row, row_text, current_type)
        if rec:
            records.append(rec)
    return records


def _parse_text_lines(text: str, current_type: str) -> list[dict]:
    """Regex-based fallback: scan each line for date + amount patterns."""
    records = []
    for line in text.splitlines():
        dates = DATE_RE.findall(line)
        if len(dates) >= 2:  # Expect at least issue_date + maturity_date
            rec = _try_parse_row(None, line, current_type)
            if rec:
                records.append(rec)
    return records


def _try_parse_row(row: list | None, row_text: str, current_type: str) -> dict | None:
    """
    Try to extract (type, rate, issue_date, maturity_date, amount) from a row.
    Returns None if we can't find the essential fields.
    """
    # Extract dates
    raw_dates = DATE_RE.findall(row_text)
    parsed_dates = [parse_date(d) for d in raw_dates]
    parsed_dates = [d for d in parsed_dates if d]

    if len(parsed_dates) < 2:
        return None

    # Heuristic: issue_date < maturity_date
    parsed_dates.sort()
    issue_date = parsed_dates[0]
    maturity_date = parsed_dates[-1]

    # Skip if maturity is in the past (more than 6 months ago)
    if maturity_date < date.today() - relativedelta(months=6):
        return None

    # Extract interest rate
    rate_match = re.search(r"(\d+\.\d+)\s*%?", row_text)
    rate = float(rate_match.group(1)) if rate_match else None

    # Extract amount (last large number = outstanding amount)
    amounts = AMOUNT_RE.findall(row_text)
    amounts_clean = []
    for a in amounts:
        v = parse_amount(a)
        if v and v > 100:  # filter out year numbers, small codes
            amounts_clean.append(v)
    amount = amounts_clean[-1] if amounts_clean else None

    if amount is None:
        return None

    # Classify type (override if row text is more specific)
    sec_type = classify_security(row_text) if classify_security(row_text) != "other" else current_type

    return {
        "type":            sec_type,
        "rate":            rate,
        "issue_date":      issue_date.isoformat(),
        "maturity_date":   maturity_date.isoformat(),
        "amount_millions": amount,
    }


# ── Data validation & cleaning ────────────────────────────────────────────────

def validate_and_clean(df: pd.DataFrame) -> pd.DataFrame:
    """
    Validate extracted data and log any anomalies.
    Returns cleaned DataFrame.
    """
    initial = len(df)

    if df.empty:
        log.warning("No securities extracted — check PDF structure or parsing logic.")
        return df

    df = df.copy()
    df["maturity_date"] = pd.to_datetime(df["maturity_date"])
    df["issue_date"]    = pd.to_datetime(df["issue_date"])

    # Drop rows where maturity is before issue (clearly wrong)
    bad_dates = df["maturity_date"] < df["issue_date"]
    if bad_dates.sum():
        log.warning(f"Dropping {bad_dates.sum()} rows where maturity < issue date")
        df = df[~bad_dates]

    # Drop rows with suspiciously small or large amounts
    median_amt = df["amount_millions"].median()
    outliers = (df["amount_millions"] < 1) | (df["amount_millions"] > 2_000_000)
    if outliers.sum():
        log.warning(f"Dropping {outliers.sum()} rows with outlier amounts "
                    f"(median={median_amt:.0f}M)")
        df = df[~outliers]

    # Sanity check: total outstanding should be in trillions
    total_bn = df["amount_millions"].sum() / 1000
    log.info(f"Total outstanding: ${total_bn:,.0f}B  ({len(df)} securities)")
    if total_bn < 10_000 or total_bn > 100_000:
        log.warning(f"Total ${total_bn:,.0f}B looks unusual — verify parsing is correct")

    log.info(f"After validation: {len(df)} rows (dropped {initial - len(df)})")
    return df


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_by_month(df: pd.DataFrame) -> dict:
    """
    Aggregate maturities by calendar month.
    Returns a dict with 'monthly' list and 'summary' stats.
    """
    if df.empty:
        return {"monthly": [], "summary": {}, "by_type": {}}

    df = df.copy()
    df["maturity_ym"] = df["maturity_date"].dt.to_period("M")

    # Only look ahead (don't include already-matured debt)
    today = pd.Timestamp(date.today())
    future = df[df["maturity_date"] >= today]

    # Group by month + type
    monthly = (
        future.groupby(["maturity_ym", "type"])["amount_millions"]
        .sum()
        .reset_index()
    )
    monthly["maturity_ym"] = monthly["maturity_ym"].astype(str)

    # Pivot to wide format: one column per type
    pivot = monthly.pivot_table(
        index="maturity_ym", columns="type", values="amount_millions", aggfunc="sum"
    ).fillna(0).reset_index()
    pivot.columns.name = None
    pivot["total_millions"] = pivot.drop(columns="maturity_ym").sum(axis=1)
    pivot = pivot.sort_values("maturity_ym")

    monthly_records = pivot.to_dict(orient="records")

    # Summary stats
    next_12 = pivot[pivot["maturity_ym"] <= (pd.Timestamp(date.today()) + relativedelta(months=12)).strftime("%Y-%m")]
    summary = {
        "total_next_12m_billions": round(next_12["total_millions"].sum() / 1000, 1),
        "peak_month":              next_12.loc[next_12["total_millions"].idxmax(), "maturity_ym"] if not next_12.empty else None,
        "peak_month_billions":     round(next_12["total_millions"].max() / 1000, 1) if not next_12.empty else 0,
        "avg_monthly_billions":    round(next_12["total_millions"].mean() / 1000, 1) if not next_12.empty else 0,
        "types_found":             sorted(df["type"].unique().tolist()),
        "extracted_at":            datetime.utcnow().isoformat() + "Z",
    }

    log.info(f"Summary: ${summary['total_next_12m_billions']}T maturing in next 12 months")
    log.info(f"Peak month: {summary['peak_month']} (${summary['peak_month_billions']}B)")

    return {
        "monthly":  monthly_records,
        "summary":  summary,
        "raw_count": len(df),
    }


# ── Dashboard generation ──────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>US Treasury Debt Maturity Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {{
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff;
    --red: #f85149; --green: #3fb950; --yellow: #d29922;
    --orange: #f0883e;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; padding: 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 600; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.875rem; margin-bottom: 24px; }}
  .kpi-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 24px; }}
  .kpi {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
  .kpi-label {{ font-size: 0.75rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 6px; }}
  .kpi-value {{ font-size: 1.75rem; font-weight: 700; }}
  .kpi-value.warn {{ color: var(--orange); }}
  .kpi-value.danger {{ color: var(--red); }}
  .kpi-value.ok {{ color: var(--green); }}
  .kpi-sub {{ font-size: 0.75rem; color: var(--muted); margin-top: 4px; }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 20px; }}
  .card h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 16px; }}
  .chart-container {{ position: relative; height: 320px; }}
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }}
  .filter-btn {{ padding: 4px 12px; border-radius: 20px; border: 1px solid var(--border); background: transparent; color: var(--text); cursor: pointer; font-size: 0.8rem; transition: all 0.15s; }}
  .filter-btn.active {{ background: var(--accent); border-color: var(--accent); color: #000; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ text-align: left; padding: 8px 12px; color: var(--muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; border-bottom: 1px solid var(--border); }}
  td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); }}
  tr:hover td {{ background: rgba(88,166,255,0.05); }}
  .bar-inline {{ height: 6px; border-radius: 3px; background: var(--accent); opacity: 0.7; display: inline-block; margin-top: 4px; }}
  .note {{ background: rgba(210,153,34,0.1); border: 1px solid rgba(210,153,34,0.3); border-radius: 6px; padding: 12px 16px; font-size: 0.8rem; color: var(--yellow); margin-bottom: 20px; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.7rem; font-weight: 600; text-transform: uppercase; }}
  .tag-note {{ background: rgba(88,166,255,0.15); color: var(--accent); }}
  .tag-bond {{ background: rgba(63,185,80,0.15); color: var(--green); }}
  .tag-bill {{ background: rgba(240,136,62,0.15); color: var(--orange); }}
  .tag-tips {{ background: rgba(248,81,73,0.15); color: var(--red); }}
  .tag-frn  {{ background: rgba(139,148,158,0.15); color: var(--muted); }}
  footer {{ color: var(--muted); font-size: 0.75rem; margin-top: 24px; text-align: center; }}
</style>
</head>
<body>

<h1>🏛️ US Treasury — Debt Maturity Schedule</h1>
<p class="subtitle" id="subtitle">Monthly Statement of Public Debt · Loading…</p>

<div id="stale-note" class="note" style="display:none">
  ⚠️ This data was extracted from an older MSPD. Re-run <code>treasury_scraper.py</code> to fetch the latest.
</div>

<div class="kpi-row" id="kpis"></div>

<div class="card">
  <h2>Debt Maturing by Month <span style="color:var(--muted);font-weight:400;font-size:0.8rem">(USD billions)</span></h2>
  <div class="filters" id="type-filters"></div>
  <div class="chart-container">
    <canvas id="mainChart"></canvas>
  </div>
</div>

<div class="card">
  <h2>Refinancing Cost Sensitivity</h2>
  <p style="color:var(--muted);font-size:0.8rem;margin-bottom:16px">
    Annual interest cost on maturing debt if rolled over at different yield levels.
    Each 1% difference in yields = <span id="cost-delta" style="color:var(--orange)">—</span> in additional annual interest cost.
  </p>
  <div class="chart-container" style="height:220px">
    <canvas id="costChart"></canvas>
  </div>
</div>

<div class="card">
  <h2>Monthly Breakdown</h2>
  <table id="detail-table">
    <thead><tr>
      <th>Month</th><th>Total ($B)</th><th>Bills</th><th>Notes</th><th>Bonds</th><th>TIPS</th><th>FRNs</th><th>Scale</th>
    </tr></thead>
    <tbody id="table-body"></tbody>
  </table>
</div>

<footer id="footer">Generated by treasury_scraper.py</footer>

<script>
// ── Data injected by scraper ──────────────────────────────────────────────────
const DATA = {DATA_JSON};

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmt = (n, dec=1) => n == null ? '—' : '$' + (n/1000).toFixed(dec) + 'T';
const fmtB = (n) => n == null || n === 0 ? '—' : '$' + (n/1000).toFixed(0) + 'B';
const fmtBd = (n, dec=1) => n == null ? '—' : (n/1000).toFixed(dec);

function tagHtml(type) {{
  const map = {{bill:'tag-bill', note:'tag-note', bond:'tag-bond', tips:'tag-tips', frn:'tag-frn'}};
  const cls = map[type] || '';
  return `<span class="tag ${{cls}}">${{type}}</span>`;
}}

// ── KPIs ──────────────────────────────────────────────────────────────────────
function renderKPIs() {{
  const s = DATA.summary;
  const kpis = [
    {{ label: 'Maturing Next 12 Months', value: '$' + (s.total_next_12m_billions/1000).toFixed(1) + 'T',
       sub: 'Total face value rolling over', cls: s.total_next_12m_billions > 8000 ? 'danger' : 'warn' }},
    {{ label: 'Peak Month', value: s.peak_month || '—',
       sub: '$' + (s.peak_month_billions||0).toFixed(0) + 'B maturing', cls: 'danger' }},
    {{ label: 'Avg Monthly Maturity', value: '$' + (s.avg_monthly_billions||0).toFixed(0) + 'B',
       sub: 'Over next 12 months', cls: 'warn' }},
    {{ label: 'Annual Interest (at 4.5%)', value: '$' + ((s.total_next_12m_billions||0) * 0.045 / 1000).toFixed(1) + 'T',
       sub: 'vs $' + ((s.total_next_12m_billions||0) * 0.035 / 1000).toFixed(1) + 'T at 3.5%', cls: 'ok' }},
  ];
  const el = document.getElementById('kpis');
  el.innerHTML = kpis.map(k => `
    <div class="kpi">
      <div class="kpi-label">${{k.label}}</div>
      <div class="kpi-value ${{k.cls}}">${{k.value}}</div>
      <div class="kpi-sub">${{k.sub}}</div>
    </div>`).join('');
}}

// ── Main bar chart ─────────────────────────────────────────────────────────────
const TYPES = ['bill','note','bond','tips','frn'];
const TYPE_COLORS = {{
  bill:  'rgba(240,136,62,0.8)',
  note:  'rgba(88,166,255,0.8)',
  bond:  'rgba(63,185,80,0.8)',
  tips:  'rgba(248,81,73,0.7)',
  frn:   'rgba(139,148,158,0.6)',
  other: 'rgba(100,100,100,0.5)',
}};

let mainChart, activeTypes = new Set(TYPES);

function buildMainChart() {{
  const months = DATA.monthly.map(r => r.maturity_ym);
  const ctx = document.getElementById('mainChart').getContext('2d');

  const datasets = TYPES.filter(t => DATA.monthly.some(r => r[t] > 0)).map(t => ({{
    label: t.toUpperCase(),
    data: DATA.monthly.map(r => +((r[t]||0)/1000).toFixed(2)),
    backgroundColor: TYPE_COLORS[t],
    stack: 'stack',
  }}));

  mainChart = new Chart(ctx, {{
    type: 'bar',
    data: {{ labels: months, datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => ` ${{ctx.dataset.label}}: $${{ctx.parsed.y.toFixed(0)}}B`,
            footer: items => ` Total: $${{items.reduce((s,i)=>s+i.parsed.y,0).toFixed(0)}}B`,
          }}
        }}
      }},
      scales: {{
        x: {{ stacked: true, grid: {{ color: '#30363d' }}, ticks: {{ color: '#8b949e', maxRotation: 45 }} }},
        y: {{ stacked: true, grid: {{ color: '#30363d' }}, ticks: {{ color: '#8b949e',
              callback: v => '$' + v + 'B' }} }},
      }}
    }}
  }});

  // Filter buttons
  const fEl = document.getElementById('type-filters');
  fEl.innerHTML = TYPES.map(t =>
    `<button class="filter-btn active" data-type="${{t}}" onclick="toggleType('${{t}}')">${{t.toUpperCase()}}</button>`
  ).join('');
}}

function toggleType(type) {{
  if (activeTypes.has(type)) {{ activeTypes.delete(type); }}
  else {{ activeTypes.add(type); }}
  document.querySelectorAll('.filter-btn').forEach(b => {{
    b.classList.toggle('active', activeTypes.has(b.dataset.type));
  }});
  mainChart.data.datasets.forEach(ds => {{
    ds.hidden = !activeTypes.has(ds.label.toLowerCase());
  }});
  mainChart.update();
}}

// ── Cost sensitivity chart ─────────────────────────────────────────────────────
function buildCostChart() {{
  const s = DATA.summary;
  const total12 = (s.total_next_12m_billions || 0) * 1e9; // in USD
  const yields  = [2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0];
  const costs   = yields.map(y => +(total12 * y / 100 / 1e12).toFixed(2)); // in $T

  // Cost delta per 1%
  const perPct = +(total12 * 0.01 / 1e9).toFixed(0);
  document.getElementById('cost-delta').textContent = '$' + perPct.toLocaleString() + 'B/year per 1%';

  const ctx = document.getElementById('costChart').getContext('2d');
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: yields.map(y => y.toFixed(1) + '%'),
      datasets: [{{
        label: 'Annual Interest Cost ($T)',
        data: costs,
        borderColor: '#f85149',
        backgroundColor: 'rgba(248,81,73,0.1)',
        fill: true,
        tension: 0.3,
        pointBackgroundColor: yields.map(y => y <= 3.5 ? '#3fb950' : y <= 4.5 ? '#d29922' : '#f85149'),
        pointRadius: 5,
      }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: c => ` Annual cost: $${{c.parsed.y.toFixed(2)}}T` }} }} }},
      scales: {{
        x: {{ grid: {{ color: '#30363d' }}, ticks: {{ color: '#8b949e' }} }},
        y: {{ grid: {{ color: '#30363d' }}, ticks: {{ color: '#8b949e', callback: v => '$' + v + 'T' }} }},
      }}
    }}
  }});
}}

// ── Detail table ──────────────────────────────────────────────────────────────
function buildTable() {{
  const maxTotal = Math.max(...DATA.monthly.map(r => r.total_millions || 0));
  const tbody = document.getElementById('table-body');
  tbody.innerHTML = DATA.monthly.map(r => {{
    const pct = ((r.total_millions || 0) / maxTotal * 180).toFixed(0);
    return `<tr>
      <td><strong>${{r.maturity_ym}}</strong></td>
      <td><strong>$${{(+(r.total_millions||0)/1000).toFixed(0)}}B</strong><br>
          <span class="bar-inline" style="width:${{pct}}px"></span></td>
      <td>${{fmtB(r.bill)}}</td>
      <td>${{fmtB(r.note)}}</td>
      <td>${{fmtB(r.bond)}}</td>
      <td>${{fmtB(r.tips)}}</td>
      <td>${{fmtB(r.frn)}}</td>
      <td><span class="bar-inline" style="width:${{pct}}px;background:${{
            r.total_millions > maxTotal*0.8 ? 'var(--red)' :
            r.total_millions > maxTotal*0.5 ? 'var(--orange)' : 'var(--accent)'}};opacity:0.8"></span></td>
    </tr>`;
  }}).join('');
}}

// ── Init ──────────────────────────────────────────────────────────────────────
document.getElementById('subtitle').textContent =
  'Monthly Statement of Public Debt · Extracted ' + (DATA.summary.extracted_at || '').slice(0,10);

renderKPIs();
buildMainChart();
buildCostChart();
buildTable();

document.getElementById('footer').textContent =
  'Generated by treasury_scraper.py · Source: US Treasury fiscaldata.treasury.gov · ' +
  DATA.raw_count + ' securities parsed';
</script>
</body>
</html>
"""


def generate_dashboard(data: dict, output_path: Path) -> Path:
    """Generate standalone HTML dashboard with embedded data."""
    data_json = json.dumps(data, default=str, indent=2)
    html = DASHBOARD_HTML.format(DATA_JSON=data_json)
    output_path.write_text(html, encoding="utf-8")
    log.info(f"Dashboard saved → {output_path}")
    return output_path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Treasury MSPD Scraper")
    parser.add_argument("--year",  type=int, help="MSPD year (e.g. 2026)")
    parser.add_argument("--month", type=int, help="MSPD month (e.g. 2)")
    parser.add_argument("--pdf",   type=str, help="Path to a local PDF (skips download)")
    parser.add_argument("--out",   type=str, default=str(Path(__file__).parent),
                        help="Output directory (default: script directory)")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Get PDF ────────────────────────────────────────────────────────────
    if args.pdf:
        pdf_path = Path(args.pdf)
        label = pdf_path.stem
        if not pdf_path.exists():
            log.error(f"PDF not found: {pdf_path}")
            sys.exit(1)
    else:
        pdf_path, label = find_or_download_pdf(args.year, args.month)

    # ── 2. Extract ────────────────────────────────────────────────────────────
    raw_df = extract_tables_from_pdf(pdf_path)
    clean_df = validate_and_clean(raw_df)

    if clean_df.empty:
        log.error("No data extracted — cannot generate outputs. "
                  "The PDF structure may have changed; check treasury_scraper.log.")
        sys.exit(1)

    # ── 3. Aggregate ──────────────────────────────────────────────────────────
    agg = aggregate_by_month(clean_df)

    # ── 4. Save JSON ──────────────────────────────────────────────────────────
    json_path = out_dir / f"maturity_data_{label}.json"
    json_path.write_text(json.dumps(agg, default=str, indent=2), encoding="utf-8")
    log.info(f"JSON saved → {json_path}")

    # Save latest copy for dashboard auto-load
    latest_json = out_dir / "maturity_data_latest.json"
    latest_json.write_text(json.dumps(agg, default=str, indent=2), encoding="utf-8")

    # ── 5. Generate dashboard ─────────────────────────────────────────────────
    dash_path = out_dir / f"treasury_dashboard_{label}.html"
    generate_dashboard(agg, dash_path)

    # ── 6. Done ───────────────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("✅  TREASURY SCRAPER COMPLETE")
    print("="*60)
    print(f"  MSPD period:  {label}")
    print(f"  Securities:   {agg['raw_count']}")
    print(f"  Next 12m:     ${agg['summary']['total_next_12m_billions']:.1f}T maturing")
    print(f"  Peak month:   {agg['summary']['peak_month']}  (${agg['summary']['peak_month_billions']:.0f}B)")
    print(f"  JSON →  {json_path}")
    print(f"  Dashboard → {dash_path}")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
