"""Dashboard sections shared between app.py (admin, full controls) and
viewer_app.py (read-only). Two NEW sections here that don't exist in the
sibling bots' dashboards — both are load-bearing design requirements from
Architecture.md/design.md, not cosmetic additions:

  render_warning_banner() — Stage 1/2 fallback usage or partial-fetch failures
  MUST be visible here, not just logged. A silent partial dataset producing a
  wrong/incomplete top-50 or candle-set was one of the 5 architecture bugs
  this project's design explicitly guards against.

  render_account_health() — since this project spans multiple broker accounts
  (2 Angel One + 2 Groww), it should be visible at a glance which accounts are
  actually configured, so a misconfigured/missing account isn't a silent gap.

render_variant_panel() renders ONE variant's full panel (metrics/open-position/
equity/trade-log), full width — the calling script picks universe-bot + variant
via a 2-level sidebar selector (12 variants is too many for bot-v3's flat
st.columns(2)/radio pattern — see design.md) and calls this once per rerun.
"""

from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import pytz
import streamlit as st

from common.helpers import format_currency, format_pct
from engine import config, db, metrics
from engine.broker_accounts import get_configured_accounts

IST = pytz.timezone("Asia/Kolkata")
REFRESH_SECONDS = 15

# Design tokens — mirrors the Figma file "Unified Trading Engine Dashboard"
# (Trading Dashboard Tokens variable collection), designed 2026-08-04. Keep
# these two in sync if the palette ever changes — Figma is the source of
# truth for the *design*, this dict is what actually ships.
TOKENS = {
    "bg_page": "#0B0F14",
    "bg_surface": "#141A21",
    "bg_surface_raised": "#1B232C",
    "border": "#232B34",
    "text_primary": "#E8EDF2",
    "text_secondary": "#8A96A3",
    "accent": "#3B82F6",
    "success": "#22C55E",
    "danger": "#EF4444",
    "warning": "#F59E0B",
    "radius": "10px",
}


def inject_custom_css() -> None:
    """Applies the Figma-designed dark theme on top of Streamlit's own
    `[theme]` (`.streamlit/config.toml`) — the config.toml sets the base
    palette so native widgets (inputs, radios, the sidebar) pick it up
    automatically; this CSS handles the pieces Streamlit's theme system
    doesn't reach (metric cards, alert banners, the open-position border).
    Call ONCE per page, near the top of app.py / viewer_app.py."""
    t = TOKENS
    st.markdown(f"""
    <style>
    div[data-testid="stMetric"] {{
        background: {t["bg_surface_raised"]};
        border: 1px solid {t["border"]};
        border-radius: {t["radius"]};
        padding: 14px 18px;
    }}
    div[data-testid="stMetricLabel"] {{
        color: {t["text_secondary"]};
        font-size: 0.72rem;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }}
    div[data-testid="stMetricValue"] {{
        color: {t["text_primary"]};
    }}
    div[data-testid="stAlert"] {{
        border-radius: {t["radius"]};
        border-width: 1px;
        border-style: solid;
    }}
    .ute-position-card {{
        background: {t["bg_surface_raised"]};
        border: 1px solid {t["accent"]};
        border-radius: {t["radius"]};
        padding: 18px 20px;
        margin-bottom: 8px;
    }}
    .ute-position-header {{
        color: {t["text_primary"]};
        font-weight: 700;
        font-size: 1.05rem;
        margin-bottom: 12px;
    }}
    .ute-position-row {{
        display: flex; gap: 32px; flex-wrap: wrap;
    }}
    .ute-position-field-label {{
        color: {t["text_secondary"]};
        font-size: 0.68rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        margin-bottom: 2px;
    }}
    .ute-position-field-value {{
        color: {t["text_primary"]};
        font-size: 0.92rem;
        font-weight: 600;
    }}
    .ute-chip-row {{
        display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 4px;
    }}
    .ute-chip {{
        display: flex; align-items: center; gap: 6px;
        background: {t["bg_surface_raised"]};
        border: 1px solid {t["border"]};
        border-radius: {t["radius"]};
        padding: 8px 12px;
        font-size: 0.8rem;
        font-weight: 600;
        color: {t["text_primary"]};
    }}
    .ute-chip-dot {{
        width: 8px; height: 8px; border-radius: 50%;
        background: {t["text_secondary"]};
        flex-shrink: 0;
    }}
    .ute-chip-dot.ute-chip-ok {{ background: {t["success"]}; }}
    </style>
    """, unsafe_allow_html=True)


def render_warning_banner() -> None:
    """Reads the most recent entry-scan cycle's warnings column — populated
    whenever Stage 1/2 used a fallback path or couldn't recover a chunk/symbol
    even after fallback (see Architecture.md's "Error visibility")."""
    logs = db.get_cycle_logs(limit=20)
    scan_logs = logs[logs["stage"] == "entry_scan"] if not logs.empty and "stage" in logs.columns else logs
    if scan_logs.empty:
        st.info("ℹ️ No entry-scan cycle has run yet — waiting for the first 5-min-boundary scan.")
        return

    latest = scan_logs.iloc[0]
    warnings_text = str(latest.get("warnings") or "").strip()
    if warnings_text:
        st.warning(
            f"⚠️ Last scan cycle ({latest['cycle_time']}) had incomplete data: {warnings_text}",
            icon="⚠️",
        )
    else:
        st.success(f"✅ Last scan cycle ({latest['cycle_time']}) fetched full data, no fallback needed.", icon="✅")


def render_account_health() -> None:
    """Shows which of the configured broker accounts are actually usable right
    now — a quick way to notice a real account-level outage (e.g. a Groww
    session expiring) instead of it hiding inside Stage 1/2's fallback chain.
    Rendered as compact status chips (matches the Figma "Account Chip"
    component), not full-size st.metric cards — this is a glance-level
    indicator, not a headline number."""
    accounts = get_configured_accounts()
    labels = [
        ("Angel One #1", accounts["angelone"], 0), ("Angel One #2", accounts["angelone"], 1),
        ("Groww #1", accounts["groww"], 0), ("Groww #2", accounts["groww"], 1),
    ]
    chips = "".join(
        f'<div class="ute-chip">'
        f'<span class="ute-chip-dot {"ute-chip-ok" if idx < len(pool) else ""}"></span>'
        f'{label}</div>'
        for label, pool, idx in labels
    )
    st.markdown(f'<div class="ute-chip-row">{chips}</div>', unsafe_allow_html=True)


def render_variant_panel(universe_bot_key: str, variant_cfg: dict, settings: dict,
                          show_force_exit: bool = False) -> None:
    variant_id = f"{universe_bot_key}/{variant_cfg['key']}"
    universe_label = config.UNIVERSE_BOTS_BY_KEY[universe_bot_key]["label"]
    timing_label = "Subh 30 min (checkpoints)" if variant_cfg["entry_timing"] == "subh30" else "Pura din (continuous)"
    exit_label = "EMA9 trailing" if variant_cfg["exit_style"] == "ema" else "ATR trailing"

    summary = metrics.get_summary(variant_id, settings["starting_capital"])

    st.markdown(f"### {universe_label}")
    st.caption(
        f"**{timing_label}** entry · **{exit_label}** exit (activates once price crosses the "
        f"{settings['profit_target_pct']:.1f}% fixed target — stop-loss is fixed at "
        f"{settings['stop_loss_pct']:.1f}% throughout)"
    )

    m1, m2 = st.columns(2)
    m1.metric("Current Capital", format_currency(summary["current_capital"]))
    m2.metric("Total P&L", format_currency(summary["total_pnl"]), format_pct(summary["total_pnl_pct"]))
    m3, m4 = st.columns(2)
    m3.metric("Total Trades", summary["total_trades"])
    m4.metric("Win Rate", f"{summary['win_rate']:.1f}%")
    m5, m6 = st.columns(2)
    pf = summary["profit_factor"]
    m5.metric("Profit Factor", f"{pf:.2f}" if pf != float("inf") else "∞")
    m6.metric("Max Drawdown", format_currency(summary["max_drawdown"]))
    st.caption(f"Open: {summary['open_positions']} / 1 · Charges paid: {format_currency(summary['total_charges'])}")

    st.divider()

    trade = db.get_open_trade(variant_id)
    if not trade:
        st.info("No open position — scanning for a fresh entry signal.")
    else:
        # 2026-08-11 live finding: this card only ever showed static entry-time
        # fields — no current price, no live P&L — unlike the sibling bots
        # (see bot-v3's dashboard_view.py), which fetch a live quote and show
        # Current + Unrealized P&L. Same pattern here, plus this project's own
        # peak/trough (unique to the trailing-exit hard-floor design).
        current_price = trade["entry_price"]
        accounts = get_configured_accounts()
        pool = accounts["groww"] or accounts["angelone"]
        if pool:
            try:
                quotes = pool[0].fetch_quotes_batch([trade["symbol"]])
                if quotes:
                    current_price = quotes[0].last_price
            except Exception:
                pass  # fail closed — show entry price rather than crash the panel

        unrealized_pnl = (current_price - trade["entry_price"]) * trade["quantity"]
        unrealized_pct = unrealized_pnl / trade["capital_used"] * 100 if trade["capital_used"] else 0.0

        target_hit = bool(trade.get("target_hit"))
        mode_label = "🎯 Trailing (target hit)" if target_hit else "🎯 Fixed SL / target"
        fields = [
            ("Symbol", trade["symbol"]), ("Entry", format_currency(trade["entry_price"])),
            ("Current", format_currency(current_price)),
            ("Unrealized P&L", f"{format_currency(unrealized_pnl)} ({format_pct(unrealized_pct)})"),
            ("Mode", mode_label), ("Qty", trade["quantity"]),
            ("Peak", format_currency(trade["peak_price"])), ("Trough", format_currency(trade["trough_price"])),
            ("Capital Used", format_currency(trade["capital_used"])), ("Entered", trade["entry_time"]),
        ]
        fields_html = "".join(
            f'<div><div class="ute-position-field-label">{label}</div>'
            f'<div class="ute-position-field-value">{value}</div></div>'
            for label, value in fields
        )
        st.markdown(
            f'<div class="ute-position-card">'
            f'<div class="ute-position-header">Open Position — {trade["symbol"]}</div>'
            f'<div class="ute-position-row">{fields_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if show_force_exit:
            if st.button(f"🛑 Force Exit {trade['symbol']}", key=f"force_exit_{variant_id}"):
                from engine import broker
                result = broker.exit_position(variant_id, trade["id"], trade["quantity"],
                                                trade["entry_price"], "MANUAL_EXIT")
                st.warning(f"Force exited {trade['symbol']}")
                st.rerun()

    with st.expander("📈 Equity Curve", expanded=False):
        equity = metrics.get_equity_curve(variant_id)
        if equity.empty:
            st.info("No closed trades yet.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=equity["exit_time"], y=equity["cum_pnl"], mode="lines+markers",
                name="Cumulative P&L", line=dict(color="#00D46A", width=2),
            ))
            fig.update_layout(template="plotly_dark", height=300, margin=dict(l=20, r=20, t=20, b=20),
                               yaxis_title="Cumulative P&L (₹)")
            st.plotly_chart(fig, use_container_width=True)

    with st.expander("📋 Trade Log", expanded=False):
        all_trades = db.get_all_trades(variant_id)
        if all_trades.empty:
            st.info("No trades yet.")
        else:
            display_cols = [
                "symbol", "status", "entry_time", "entry_price", "quantity", "capital_used",
                "target_hit", "exit_time", "exit_price", "exit_reason",
                "gross_pnl", "total_charges", "net_pnl", "net_pnl_pct",
            ]
            st.dataframe(all_trades[display_cols], use_container_width=True, hide_index=True)

    now_str = datetime.now(IST).strftime("%H:%M:%S")
    st.caption(f"🔄 Last updated {now_str}")


def get_refresh_interval():
    """15s while ANY of the 12 variants has an open position, so live P&L is
    visible without a manual refresh; None (no auto-refresh) while all 12 are
    flat. The scheduler keeps managing/scanning on its own cadence regardless
    of what's showing on screen."""
    for universe_bot in config.UNIVERSE_BOTS:
        for variant_cfg in config.VARIANTS:
            variant_id = f"{universe_bot['key']}/{variant_cfg['key']}"
            if db.get_open_trade(variant_id):
                return REFRESH_SECONDS
    return None
