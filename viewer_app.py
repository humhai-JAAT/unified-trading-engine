"""Unified Trading Engine — READ-ONLY viewer dashboard.

Same metrics/position/equity/trade-log as app.py, and the same 2-level
universe-bot -> variant sidebar selector, but with NO other controls — no
Start/Stop, no Settings, no Reset, no Force Exit, no cycle log. Deploy as its
OWN separate Streamlit Cloud app (same repo, main file path "viewer_app.py"
instead of "app.py") so this link can be shared without exposing controls.
Needs its own DATABASE_URL secret configured too (same value as app.py's).
"""

import os

import streamlit as st

try:
    if "DATABASE_URL" in st.secrets:
        os.environ.setdefault("DATABASE_URL", st.secrets["DATABASE_URL"])
except Exception:
    pass

from engine import config, dashboard_view, db

st.set_page_config(page_title="Unified Trading Engine — Viewer", page_icon="🧩", layout="wide")
dashboard_view.inject_custom_css()

db.init_db()
settings = config.load_settings()

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

st.title("🧩 Unified Trading Engine — Viewer")
st.caption("Live view only — no controls here. Use the sidebar to pick a universe-bot and variant.")


@st.fragment(run_every=dashboard_view.get_refresh_interval())
def live_panel():
    dashboard_view.render_warning_banner()
    dashboard_view.render_account_health()
    st.divider()
    dashboard_view.render_variant_panel(universe_bot_key, variant_cfg, settings, show_force_exit=False)


live_panel()
