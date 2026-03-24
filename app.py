import streamlit as st
import plotly.graph_objects as go
import pandas as pd
import json
from pathlib import Path
from datetime import datetime

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="US Treasury Debt Maturity",
    page_icon="🏛️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── Dark theme override ───────────────────────────────────────────────────────
st.markdown("""
<style>
  [data-testid="stAppViewContainer"] { background: #0d1117; }
  [data-testid="stHeader"] { background: #0d1117; }
  .block-container { padding-top: 2rem; }
  .kpi-card {
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 20px 24px;
  }
  .kpi-label { font-size: 0.72rem; color: #8b949e; text-transform: uppercase;
               letter-spacing: 0.06em; margin-bottom: 6px; }
  .kpi-value { font-size: 2rem; font-weight: 700; line-height: 1.1; }
  .kpi-sub   { font-size: 0.75rem; color: #8b949e; margin-top: 4px; }
  .warn   { color: #f0883e; }
  .danger { color: #f85149; }
  .ok     { color: #3fb950; }
  .neutral{ color: #58a6ff; }
  hr { border-color: #30363d; }
  .stale-banner {
    background: rgba(210,153,34,0.1);
    border: 1px solid rgba(210,153,34,0.3);
    border-radius: 8px;
    padding: 10px 16px;
    color: #d29922;
    font-size: 0.85rem;
    margin-bottom: 16px;
  }
</style>
""", unsafe_allow_html=True)


# ── Data loading ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_data() -> dict | None:
    path = Path(__file__).parent / "data" / "maturity_data_latest.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def is_stale(extracted_at: str, days: int = 45) -> bool:
    try:
        extracted = datetime.fromisoformat(extracted_at.replace("Z", "+00:00"))
        return (datetime.now(extracted.tzinfo) - extracted).days > days
    except Exception:
        return False


# ── Color map ─────────────────────────────────────────────────────────────────
TYPE_COLORS = {
    "bill":  "#f0883e",
    "note":  "#58a6ff",
    "bond":  "#3fb950",
    "tips":  "#f85149",
    "frn":   "#8b949e",
    "other": "#6e7681",
}
TYPE_ORDER = ["bill", "note", "bond", "tips", "frn", "other"]


# ── Main ──────────────────────────────────────────────────────────────────────
data = load_data()

st.markdown("## 🏛️ US Treasury — Debt Maturity Schedule")

if data is None:
    st.error(
        "No data found. Run `treasury_scraper.py` first to generate "
        "`data/maturity_data_latest.json`."
    )
    st.stop()

summary = data["summary"]
monthly = data["monthly"]
extracted_at = summary.get("extracted_at", "")

# Stale data warning
if is_stale(extracted_at):
    st.markdown(
        f'<div class="stale-banner">⚠️ Data was extracted on '
        f'{extracted_at[:10]}. Re-run the scraper to update.</div>',
        unsafe_allow_html=True,
    )

st.markdown(
    f"<p style='color:#8b949e;font-size:0.85rem;margin-bottom:1.5rem'>"
    f"Monthly Statement of Public Debt · {summary.get('raw_count', '—')} securities · "
    f"Extracted {extracted_at[:10]}</p>",
    unsafe_allow_html=True,
)


# ── KPI row ───────────────────────────────────────────────────────────────────
total_t   = summary["total_next_12m_billions"] / 1000
peak_m    = summary.get("peak_month", "—")
peak_b    = summary.get("peak_month_billions", 0)
avg_b     = summary.get("avg_monthly_billions", 0)
cost_45   = total_t * 0.045
cost_35   = total_t * 0.035
saving    = cost_45 - cost_35

col1, col2, col3, col4 = st.columns(4)

for col, label, value, sub, cls in [
    (col1, "Maturing Next 12 Months", f"${total_t:.1f}T",
     "Total face value rolling over", "danger" if total_t > 8 else "warn"),
    (col2, "Peak Month",    peak_m,
     f"${peak_b:.0f}B maturing", "danger"),
    (col3, "Avg Monthly",  f"${avg_b:.0f}B",
     "Over next 12 months", "warn"),
    (col4, "Interest Cost Spread",  f"${saving:.2f}T/yr",
     f"Saved at 3.5% vs 4.5%  ·  ${cost_45:.2f}T vs ${cost_35:.2f}T", "ok"),
]:
    col.markdown(
        f'<div class="kpi-card"><div class="kpi-label">{label}</div>'
        f'<div class="kpi-value {cls}">{value}</div>'
        f'<div class="kpi-sub">{sub}</div></div>',
        unsafe_allow_html=True,
    )

st.markdown("<br>", unsafe_allow_html=True)


# ── Filters ───────────────────────────────────────────────────────────────────
df = pd.DataFrame(monthly)
df["maturity_ym"] = pd.to_datetime(df["maturity_ym"])

types_available = [t for t in TYPE_ORDER if t in df.columns and df[t].sum() > 0]

with st.expander("⚙️ Filters", expanded=False):
    fcol1, fcol2 = st.columns([2, 1])
    with fcol1:
        selected_types = st.multiselect(
            "Security types", types_available,
            default=types_available,
            format_func=str.upper,
        )
    with fcol2:
        months_ahead = st.slider("Months to show", 6, 36, 18)

df_filtered = df[df["maturity_ym"] <= df["maturity_ym"].min() + pd.DateOffset(months=months_ahead)]


# ── Main stacked bar chart ────────────────────────────────────────────────────
st.markdown("### Debt Maturing by Month")

fig_bar = go.Figure()
for t in selected_types:
    if t not in df_filtered.columns:
        continue
    fig_bar.add_trace(go.Bar(
        name=t.upper(),
        x=df_filtered["maturity_ym"].dt.strftime("%Y-%m"),
        y=(df_filtered[t].fillna(0) / 1000).round(0),
        marker_color=TYPE_COLORS.get(t, "#6e7681"),
        hovertemplate=f"<b>{t.upper()}</b>: $%{{y:.0f}}B<extra></extra>",
    ))

fig_bar.update_layout(
    barmode="stack",
    paper_bgcolor="#0d1117",
    plot_bgcolor="#0d1117",
    font=dict(color="#e6edf3", family="system-ui"),
    height=380,
    margin=dict(l=0, r=0, t=10, b=0),
    legend=dict(
        orientation="h", yanchor="bottom", y=1.01,
        xanchor="left", x=0,
        bgcolor="rgba(0,0,0,0)",
    ),
    xaxis=dict(gridcolor="#30363d", tickfont=dict(color="#8b949e")),
    yaxis=dict(
        gridcolor="#30363d",
        tickfont=dict(color="#8b949e"),
        tickprefix="$", ticksuffix="B",
        title=dict(text="USD Billions", font=dict(color="#8b949e")),
    ),
    hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
)
st.plotly_chart(fig_bar, use_container_width=True)


# ── Two-column lower section ──────────────────────────────────────────────────
left, right = st.columns([1.6, 1])

# ── Cost sensitivity line chart ───────────────────────────────────────────────
with left:
    st.markdown("### Refinancing Cost Sensitivity")
    st.markdown(
        "<p style='color:#8b949e;font-size:0.8rem;margin-top:-8px'>"
        f"Annual interest cost if the ${total_t:.1f}T maturing next 12 months "
        f"rolls over at each yield level. "
        f"Each 1% = <b style='color:#f0883e'>${total_t*10:.0f}B/yr</b>.</p>",
        unsafe_allow_html=True,
    )

    yields = [y / 10 for y in range(20, 65, 5)]  # 2.0 → 6.0 in 0.5 steps
    costs  = [total_t * y / 100 for y in yields]
    colors = ["#3fb950" if y <= 3.5 else "#d29922" if y <= 4.5 else "#f85149"
              for y in yields]

    fig_cost = go.Figure()
    fig_cost.add_trace(go.Scatter(
        x=[f"{y:.1f}%" for y in yields],
        y=costs,
        mode="lines+markers",
        line=dict(color="#f85149", width=2),
        fill="tozeroy",
        fillcolor="rgba(248,81,73,0.08)",
        marker=dict(color=colors, size=9, line=dict(color="#0d1117", width=1)),
        hovertemplate="Yield: %{x}<br>Annual cost: $%{y:.2f}T<extra></extra>",
    ))
    # Reference bands
    fig_cost.add_hrect(y0=0, y1=total_t * 0.035, fillcolor="rgba(63,185,80,0.05)",
                       line_width=0, annotation_text="Below 3.5%",
                       annotation_font_color="#3fb950", annotation_font_size=10)

    fig_cost.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#e6edf3"), height=280,
        margin=dict(l=0, r=0, t=10, b=0),
        xaxis=dict(gridcolor="#30363d", tickfont=dict(color="#8b949e"),
                   title=dict(text="Yield at Rollover", font=dict(color="#8b949e"))),
        yaxis=dict(gridcolor="#30363d", tickfont=dict(color="#8b949e"),
                   tickprefix="$", ticksuffix="T",
                   title=dict(text="Annual Interest Cost", font=dict(color="#8b949e"))),
        showlegend=False,
        hoverlabel=dict(bgcolor="#161b22", bordercolor="#30363d"),
    )
    st.plotly_chart(fig_cost, use_container_width=True)


# ── Maturity composition donut ────────────────────────────────────────────────
with right:
    st.markdown("### 12-Month Composition")

    totals = {t: df_filtered[t].sum() / 1000 for t in types_available if t in df_filtered.columns}
    totals = {k: v for k, v in totals.items() if v > 0}

    fig_pie = go.Figure(go.Pie(
        labels=[k.upper() for k in totals.keys()],
        values=list(totals.values()),
        hole=0.55,
        marker=dict(colors=[TYPE_COLORS[k] for k in totals.keys()],
                    line=dict(color="#0d1117", width=2)),
        textfont=dict(color="#e6edf3"),
        hovertemplate="<b>%{label}</b>: $%{value:.0f}B (%{percent})<extra></extra>",
    ))
    fig_pie.update_layout(
        paper_bgcolor="#0d1117", plot_bgcolor="#0d1117",
        font=dict(color="#e6edf3"), height=280,
        margin=dict(l=0, r=0, t=10, b=30),
        legend=dict(font=dict(color="#8b949e"), bgcolor="rgba(0,0,0,0)"),
        annotations=[dict(
            text=f"${sum(totals.values()):.0f}B",
            x=0.5, y=0.5, font_size=18,
            font_color="#e6edf3", showarrow=False,
        )],
    )
    st.plotly_chart(fig_pie, use_container_width=True)


# ── Detail table ──────────────────────────────────────────────────────────────
st.markdown("### Monthly Breakdown")

display_df = df_filtered.copy()
display_df["Month"] = display_df["maturity_ym"].dt.strftime("%Y-%m")
display_df["Total ($B)"] = (display_df["total_millions"] / 1000).round(1)
for t in types_available:
    display_df[t.upper()] = (display_df[t].fillna(0) / 1000).round(1).apply(
        lambda v: f"${v:.0f}B" if v > 0 else "—"
    )

cols_show = ["Month", "Total ($B)"] + [t.upper() for t in types_available]
st.dataframe(
    display_df[cols_show].set_index("Month"),
    use_container_width=True,
    height=320,
)

st.markdown(
    "<p style='color:#6e7681;font-size:0.72rem;text-align:right;margin-top:8px'>"
    "Source: US Treasury fiscaldata.treasury.gov · Monthly Statement of Public Debt</p>",
    unsafe_allow_html=True,
)
