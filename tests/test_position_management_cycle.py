"""Regression tests for run_position_management_cycle's 2026-08-12 fixes:
a single variant's crash must not abort every other variant's management
this cycle, a total crash must still leave a visible ERROR cycle_log row
(matching run_full_scan_cycle's existing crash-visibility pattern), and any
abnormal hold (no_price_data, no_broker_account_configured, error) must be
surfaced in the cycle log's warnings column so
dashboard_view.render_warning_banner() can show it — previously these were
silent, which is exactly how a real open position (HINDCOPPER, live
production) went unmonitored for hours with zero visibility anywhere.
"""

from unittest.mock import patch

import pandas as pd
import pytest

from engine import config


@pytest.fixture()
def wired_db(tmp_path, monkeypatch):
    import engine.db as db_module
    db_module._engine = None
    monkeypatch.setattr(db_module, "_sqlite_path", lambda: tmp_path / "pm.db")
    db_module.init_db()
    yield db_module
    db_module._engine = None


def _open_a_trade(db_module, variant_id, symbol):
    db_module.open_trade(
        variant_id=variant_id, symbol=symbol, entry_price=100.0, quantity=10,
        capital_used=1000.0, entry_charges=10.0, arm_cycle_id=None, leverage=1.0,
    )


def test_normal_cycle_counts_a_real_exit_and_reports_no_warnings(wired_db):
    from engine import scheduler

    variant_id = f"{config.UNIVERSE_BOTS[0]['key']}/{config.VARIANTS[0]['key']}"
    _open_a_trade(wired_db, variant_id, "RELIANCE")
    fixed_now = pd.Timestamp("2026-08-03 10:00", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"), \
         patch("engine.variant_engine.manage_open_position",
               return_value={"action": "exit", "symbol": "RELIANCE", "reason": "STOP_LOSS"}):
        result = scheduler.run_position_management_cycle(config.load_settings())

    assert result["status"] == "open"
    logs = wired_db.get_cycle_logs()
    latest = logs.iloc[0]
    assert latest["status"] == "OK"
    assert latest["message"] == "managed=1"
    assert latest["warnings"] == ""


def test_one_variant_crashing_does_not_stop_others_from_being_managed(wired_db):
    """The real 2026-08-12 bug: an exception managing ONE variant used to
    abort the whole for-loop, silently starving every variant later in
    iteration order too — not just the one that actually failed."""
    from engine import scheduler

    crashing_variant = f"{config.UNIVERSE_BOTS[0]['key']}/{config.VARIANTS[0]['key']}"
    healthy_variant = f"{config.UNIVERSE_BOTS[0]['key']}/{config.VARIANTS[1]['key']}"
    _open_a_trade(wired_db, crashing_variant, "HINDCOPPER")
    _open_a_trade(wired_db, healthy_variant, "TCS")
    fixed_now = pd.Timestamp("2026-08-03 10:00", tz="Asia/Kolkata").to_pydatetime()

    def fake_manage(variant_id, variant_cfg, trade, settings, now):
        if variant_id == crashing_variant:
            raise RuntimeError("simulated broker crash")
        return {"action": "hold", "symbol": trade["symbol"]}

    with patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"), \
         patch("engine.variant_engine.manage_open_position", side_effect=fake_manage):
        result = scheduler.run_position_management_cycle(config.load_settings())

    # Cycle completes cleanly overall (status OK), not an unhandled crash.
    assert result["status"] == "open"
    assert healthy_variant in result["managed"]  # reached and processed despite the earlier crash
    assert result["managed"][healthy_variant]["action"] == "hold"

    logs = wired_db.get_cycle_logs()
    latest = logs.iloc[0]
    assert latest["status"] == "OK"
    assert crashing_variant in latest["warnings"]
    assert "simulated broker crash" in latest["warnings"]


def test_abnormal_hold_reason_is_surfaced_as_a_warning(wired_db):
    from engine import scheduler

    variant_id = f"{config.UNIVERSE_BOTS[0]['key']}/{config.VARIANTS[0]['key']}"
    _open_a_trade(wired_db, variant_id, "HINDCOPPER")
    fixed_now = pd.Timestamp("2026-08-03 10:00", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"), \
         patch("engine.variant_engine.manage_open_position",
               return_value={"action": "hold", "reason": "no_price_data", "symbol": "HINDCOPPER"}):
        result = scheduler.run_position_management_cycle(config.load_settings())

    assert result["status"] == "open"
    logs = wired_db.get_cycle_logs()
    latest = logs.iloc[0]
    assert latest["message"] == "managed=0"  # a hold, correctly not counted as "managed"
    assert "no_price_data" in latest["warnings"]  # but NOT silent — this is the actual fix


def test_a_total_crash_before_the_loop_still_logs_an_error_row(wired_db):
    """Mirrors run_full_scan_cycle's own crash-visibility regression test
    (test_end_to_end_cycle.py) — a crash must never vanish without a trace."""
    from engine import scheduler

    variant_id = f"{config.UNIVERSE_BOTS[0]['key']}/{config.VARIANTS[0]['key']}"
    _open_a_trade(wired_db, variant_id, "HINDCOPPER")
    fixed_now = pd.Timestamp("2026-08-03 10:00", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"), \
         patch("engine.db.get_open_trade", side_effect=RuntimeError("db connection lost")):
        with pytest.raises(RuntimeError, match="db connection lost"):
            scheduler.run_position_management_cycle(config.load_settings())

    logs = wired_db.get_cycle_logs()
    latest = logs.iloc[0]
    assert latest["status"] == "ERROR"
    assert latest["stage"] == "position_management"
    assert "db connection lost" in latest["error"]
