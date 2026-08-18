"""Unified Trading Engine — Streamlit dashboard (admin, full controls).

2 universe-bots (bot_300/bot_400) x 4 variants each (subh30/puradin
entry-timing x ema/atr trailing-exit) = 8 total, all scanning/trading
independently in the background every cycle regardless of what's on screen —
see Architecture.md. A 2-level sidebar selector (universe-bot, then variant)
picks which ONE is shown in the main panel at a time — 8 is too many to lay
out flat (see design.md).
"""

import os
from datetime import datetime

import pytz
import streamlit as st

# Pull secrets into the environment — same pattern as the sibling bots.
# DATABASE_URL -> Postgres (falls back to local SQLite if unset).
# ANGELONE_{1,2}_* / GROWW_{1,2}_* -> broker account credentials (see
# engine/broker_accounts.py — an account with incomplete credentials is
# silently skipped, not an error).
try:
    if "DATABASE_URL" in st.secrets:
        os.environ.setdefault("DATABASE_URL", st.secrets["DATABASE_URL"])
    for prefix in ("ANGELONE_1", "ANGELONE_2"):
        for suffix in ("CLIENT_CODE", "PASSWORD", "TOTP_SECRET", "API_KEY"):
            key = f"{prefix}_{suffix}"
            if key in st.secrets:
                os.environ.setdefault(key, st.secrets[key])
    for prefix in ("GROWW_1", "GROWW_2"):
        for suffix in ("API_KEY", "API_SECRET", "TOTP_SECRET"):
            key = f"{prefix}_{suffix}"
            if key in st.secrets:
                os.environ.setdefault(key, st.secrets[key])
except Exception:
    pass

from engine import config, dashboard_view, db, scheduler

st.set_page_config(page_title="Unified Trading Engine", page_icon="🧩", layout="wide")
dashboard_view.inject_custom_css()

IST = pytz.timezone("Asia/Kolkata")
db.init_db()
settings = config.load_settings()

st.title("🧩 Unified Trading Engine")
st.caption(
    "2 universe-bots (Nifty500-minus-Nifty200 / Nifty500-minus-Nifty100) x 4 variants each "
    "(Subh-30-min or Pura-din entry timing x EMA9 or ATR trailing exit) = 8 independent paper-trading "
    "strategies, sharing one deduplicated ranking + candle-data fetch per cycle instead of each "
    "re-fetching from scratch — see Architecture.md."
)

# ---------------- Sidebar: controls ----------------
st.sidebar.title("⚙️ Controls")

running = scheduler.is_running()
st.sidebar.markdown(f"**Scheduler:** {'🟢 Running' if running else '🔴 Stopped'}")

col_a, col_b = st.sidebar.columns(2)
if col_a.button("Start", use_container_width=True, disabled=running):
    scheduler.start_scheduler()
    st.rerun()
if col_b.button("Stop", use_container_width=True, disabled=not running):
    scheduler.stop_scheduler()
    st.rerun()

st.sidebar.caption(
    f"Position management every {settings['position_management_interval_minutes']} min · "
    f"entry scan (Stage 1 + Stage 2 + all 8 variants) at 5-min-boundary+1 offsets "
    f"(09:21, 09:26, 09:31...) · awake only {settings['wake_time']}–{settings['sleep_time']} IST."
)

col_c, col_d = st.sidebar.columns(2)
if col_c.button("▶️ Run Position Mgmt Now", use_container_width=True):
    with st.spinner("Managing open positions..."):
        result = scheduler.run_position_management_cycle(settings)
    st.sidebar.success(f"Done: {result.get('status')}")
if col_d.button("▶️ Run Scan Now", use_container_width=True, type="primary"):
    with st.spinner("Running Stage 1 + Stage 2 + entry scan..."):
        result = scheduler.run_full_scan_cycle(settings)
    entered = result.get("entered_variants", [])
    st.sidebar.success(f"Done: {result.get('status')} · entered={len(entered)} variant(s)")
    if result.get("warnings"):
        st.sidebar.warning(f"{len(result['warnings'])} warning(s) — see banner below.")

st.sidebar.divider()
now_ist = datetime.now(IST)
bot_awake = scheduler.is_awake(settings, now_ist)
st.sidebar.markdown(f"**Bot:** {'🟢 Awake' if bot_awake else '💤 Asleep (outside wake/sleep window)'}")
status = scheduler.market_status(now_ist)
status_label = {"open": "🟢 Market Open", "closed_hours": "🔴 Market Closed (outside hours)",
                "closed_weekend": "🔴 Market Closed (weekend)"}.get(status, status)
st.sidebar.markdown(f"**{status_label}**")
st.sidebar.caption(f"IST time: {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")

st.sidebar.divider()
st.sidebar.markdown("### 👀 Viewing")
universe_bot_key = st.sidebar.selectbox(
    "Universe-bot", options=[b["key"] for b in config.UNIVERSE_BOTS],
    format_func=lambda k: config.UNIVERSE_BOTS_BY_KEY[k]["label"],
)
variant_key = st.sidebar.radio(
    "Variant", options=[v["key"] for v in config.VARIANTS],
    format_func=lambda k: k.replace("_", " "),
)
variant_cfg = config.VARIANTS_BY_KEY[variant_key]
st.sidebar.caption("All 8 keep scanning/trading independently regardless of which one you're viewing.")

st.sidebar.divider()
st.sidebar.markdown("### 🌐 Public Viewer")
_public_options = [""] + config.all_variant_ids()
# db.get_setting, NOT engine.config.load_settings()/settings.get("public_variant") —
# admin and viewer are two SEPARATE Streamlit Cloud deployments with separate local
# filesystems; settings.yaml on admin's disk is invisible to viewer's container. The
# database is the only thing both apps actually share (2026-08-11 live finding — this
# control looked like it worked from the admin side but never reached the viewer app).
_public_current = db.get_setting("public_variant", "") or ""
public_variant = st.sidebar.selectbox(
    "Variant shown on the Viewer app",
    options=_public_options,
    index=_public_options.index(_public_current) if _public_current in _public_options else 0,
    format_func=lambda v: "— none (viewer shows nothing) —" if v == "" else v.replace("/", " · ").replace("_", " "),
)
if public_variant != _public_current:
    db.set_setting("public_variant", public_variant)
    st.sidebar.success("Viewer app updated.")
    st.rerun()
st.sidebar.caption("Controls what the separate read-only Viewer app shows — visitors there can't pick their own.")

st.sidebar.divider()
st.sidebar.markdown("### 🔍 Stock Pool Size")
# Pulled out of the Strategy Settings expander (2026-08-13, user request) into
# its own always-visible control that applies immediately — same auto-save
# pattern as the Public Viewer selector above, no separate Save click needed
# just to change how many stocks the bot scans.
pool_size = st.sidebar.number_input(
    "Top-N gainers per universe-bot", min_value=5, max_value=100, step=5,
    value=int(settings["gainers_pool_size"]),
    help="How many top %-gainers Stage 1 hands to each universe-bot (bot_300, bot_400) "
         "for entry scanning. Lower = fewer Stage 2 candle fetches (faster entry-scan "
         "cadence) but a narrower candidate set — a real mover outside this rank at scan "
         "time won't be checked for a signal.",
)
if pool_size != settings["gainers_pool_size"]:
    config.save_settings({**settings, "gainers_pool_size": pool_size})
    st.sidebar.success(f"Pool size updated to {pool_size}. Applies from the next entry-scan cycle.")
    st.rerun()

st.sidebar.divider()
with st.sidebar.expander("⚙️ Strategy Settings", expanded=False):
    capital = st.number_input("Capital per Variant (₹)", min_value=1000, step=1000,
                               value=int(settings["starting_capital"]))
    target_pct = st.number_input("Fixed Target % (trailing-activation trigger)", min_value=0.5, max_value=50.0,
                                  step=0.5, value=float(settings["profit_target_pct"]))
    sl_pct = st.number_input("Fixed Stop-Loss %", min_value=0.5, max_value=50.0, step=0.5,
                              value=float(settings["stop_loss_pct"]))
    atr_period = st.number_input("ATR Period", min_value=5, max_value=50, step=1,
                                  value=int(settings["atr_period"]))
    atr_multiplier = st.number_input("ATR Multiplier", min_value=0.5, max_value=5.0, step=0.1,
                                      value=float(settings["atr_multiplier"]))
    wake_time = st.text_input("Bot Wake Time (HH:MM)", value=settings["wake_time"])
    sleep_time = st.text_input("Bot Sleep Time (HH:MM)", value=settings["sleep_time"])
    square_off_time = st.text_input("Square-off Time (HH:MM)", value=settings["square_off_time"])

    if st.button("💾 Save Settings", use_container_width=True):
        new_settings = {
            **settings, "starting_capital": capital,
            "profit_target_pct": target_pct, "stop_loss_pct": sl_pct,
            "atr_period": atr_period, "atr_multiplier": atr_multiplier,
            "wake_time": wake_time, "sleep_time": sleep_time, "square_off_time": square_off_time,
        }
        config.save_settings(new_settings)
        st.success("Saved. Restart the scheduler for interval changes to take effect.")
        st.rerun()

st.sidebar.divider()
with st.sidebar.expander("🗑️ Danger Zone", expanded=False):
    st.caption("Permanently deletes ALL trades and cycle logs for ALL 8 variants. Cannot be undone.")
    confirm_reset = st.checkbox("I understand this deletes everything", key="confirm_reset")
    if st.button("Reset All Data", type="secondary", disabled=not confirm_reset, use_container_width=True):
        db.reset_all_data()
        st.success("All data reset.")
        st.rerun()

st.sidebar.caption(
    "⚠️ Groww MIS/intraday charges (brokerage + STT + stamp duty + exchange + SEBI + IPFT + GST) "
    "are simulated on every paper trade. Broker data is used for forward-testing only — treat as "
    "paper trading, not a live-execution signal."
)


@st.fragment(run_every=dashboard_view.get_refresh_interval())
def live_panel():
    dashboard_view.render_warning_banner()
    dashboard_view.render_account_health()
    st.divider()
    dashboard_view.render_variant_panel(universe_bot_key, variant_cfg, settings, show_force_exit=True)


live_panel()

st.divider()
st.subheader("📊 Variant Scoreboard")
st.caption("All 8 variants compared side by side — see which ones are actually earning their keep.")


@st.fragment(run_every=dashboard_view.get_refresh_interval())
def scoreboard_panel():
    dashboard_view.render_variant_scoreboard(settings)


scoreboard_panel()

st.divider()
st.subheader("Latest Cycle Log")
logs = db.get_cycle_logs(limit=20)
if logs.empty:
    st.info("No cycles run yet — click **Run Scan Now** or start the scheduler.")
else:
    st.dataframe(logs, use_container_width=True, hide_index=True)
