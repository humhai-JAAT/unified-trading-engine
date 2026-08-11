"""Unified Trading Engine — READ-ONLY viewer dashboard.

Shows exactly ONE variant — whichever the admin app's "🌐 Public Viewer"
control currently points `db.get_setting("public_variant")` at (see app.py).
Read from the DATABASE, not engine.config.load_settings() — admin and viewer
are two SEPARATE Streamlit Cloud deployments with separate local
filesystems, so a value admin writes to its own settings.yaml never reaches
viewer's container; the database is the only thing both apps actually share
(2026-08-11 live finding — the original file-based version of this control
looked like it worked from the admin side but silently never reached here).
NO variant selector here — visitors can't pick their own, that choice is
admin-controlled only (2026-08-10, per the user: viewer picking any of the
8 variants itself was never the intent). No other controls either — no
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

st.title("🧩 Unified Trading Engine — Viewer")
st.caption("Live view only — no controls here.")

public_variant_id = db.get_setting("public_variant", "") or ""
if not public_variant_id or public_variant_id not in config.all_variant_ids():
    st.info("No variant is currently public. Check back later.")
    st.stop()

universe_bot_key, variant_key = public_variant_id.split("/", 1)
variant_cfg = config.VARIANTS_BY_KEY[variant_key]
st.caption(f"Showing: **{config.UNIVERSE_BOTS_BY_KEY[universe_bot_key]['label']}** · "
           f"{variant_key.replace('_', ' ')}")


@st.fragment(run_every=dashboard_view.get_refresh_interval())
def live_panel():
    dashboard_view.render_warning_banner()
    dashboard_view.render_account_health()
    st.divider()
    dashboard_view.render_variant_panel(universe_bot_key, variant_cfg, settings, show_force_exit=False)


live_panel()
