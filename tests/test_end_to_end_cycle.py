"""Synthetic end-to-end test: Stage 1 -> per-universe filter -> Stage 2 ->
all 8 variants' entry scan, fully mocked (no real broker/DB network calls
except a throwaway local SQLite file) — mirrors the sibling bots' own
"synthetic end-to-end engine test" verification pattern before ever touching a
live account.
"""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from engine import config
from engine.broker_accounts import BrokerAccount, QuoteResult


def _make_breakout_candles(n=150):
    prices = np.concatenate([np.linspace(100, 98, n - 3), [98, 101, 106]])
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({
        "Open": prices, "High": prices * 1.002, "Low": prices * 0.998, "Close": prices,
        "Volume": 2000,
    }, index=idx)


def _make_flat_candles(n=150, level=50.0):
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    prices = np.full(n, level)
    return pd.DataFrame({
        "Open": prices, "High": prices, "Low": prices, "Close": prices, "Volume": 1000,
    }, index=idx)


class MockAccount(BrokerAccount):
    """One symbol ('BREAKOUT') has an engineered fresh buy signal; every other
    symbol is flat (no signal) — lets the test assert exactly one variant per
    universe-bot enters, and that it's the right symbol."""

    def __init__(self):
        self.account_id = "mock"
        self.broker = "mock"
        super().__init__()

    def _quote_rate_seconds(self):
        return 0.0

    def _candle_rate_seconds(self):
        return 0.0

    def is_configured(self):
        return True

    def fetch_quotes_batch(self, symbols):
        return [
            QuoteResult(symbol=s, last_price=100.0, pct_change=(50.0 if s == "BREAKOUT" else 1.0))
            for s in symbols
        ]

    def fetch_candles(self, symbol, interval, period_days):
        return _make_breakout_candles() if symbol == "BREAKOUT" else _make_flat_candles()


@pytest.fixture()
def wired_db(tmp_path, monkeypatch):
    import engine.db as db_module
    db_module._engine = None
    monkeypatch.setattr(db_module, "_sqlite_path", lambda: tmp_path / "e2e.db")
    db_module.init_db()
    yield db_module
    db_module._engine = None


def test_full_scan_cycle_enters_the_breakout_symbol_for_every_universe_bot(wired_db):
    from engine import scheduler

    universe_symbols = {"BREAKOUT", "FLAT1", "FLAT2", "FLAT3"}
    account = MockAccount()

    settings = config.load_settings()
    settings["gainers_pool_size"] = 10

    fixed_now = pd.Timestamp("2026-08-03 09:21", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.nse_universe.get_universe_symbols", return_value=universe_symbols), \
         patch("engine.stage1_ranking.get_configured_accounts",
               return_value={"groww": [account], "angelone": []}), \
         patch("engine.stage2_candles.get_configured_accounts",
               return_value={"groww": [account], "angelone": []}), \
         patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"):

        result = scheduler.run_full_scan_cycle(settings)

    assert result["status"] == "open"
    assert result["stage2_symbols_fetched"] > 0

    # subh30 variants are gated to the 09:20 checkpoint at 09:21 (fixed_now) —
    # puradin variants scan every cycle regardless. Both entry-timings should
    # find BREAKOUT and enter, for both universe-bots (8 variants total).
    entered = result["entered_variants"]
    assert len(entered) == 8, f"expected all 8 variants to enter, got: {entered}"
    for variant_id in entered:
        assert result["scan"][variant_id]["entered"]["symbol"] == "BREAKOUT"

    # Confirm it's actually reflected in the DB, per-variant, independently.
    for universe_bot in config.UNIVERSE_BOTS:
        for variant_cfg in config.VARIANTS:
            variant_id = f"{universe_bot['key']}/{variant_cfg['key']}"
            trade = wired_db.get_open_trade(variant_id)
            assert trade is not None, f"{variant_id} should have an open trade"
            assert trade["symbol"] == "BREAKOUT"


def test_full_scan_cycle_reports_stage1_warnings_when_no_accounts_configured(wired_db):
    from engine import scheduler

    fixed_now = pd.Timestamp("2026-08-03 09:21", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.nse_universe.get_universe_symbols", return_value={"AAA"}), \
         patch("engine.stage1_ranking.get_configured_accounts",
               return_value={"groww": [], "angelone": []}), \
         patch("engine.stage2_candles.get_configured_accounts",
               return_value={"groww": [], "angelone": []}), \
         patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"):

        result = scheduler.run_full_scan_cycle(config.load_settings())

    assert result["status"] == "open"
    assert any("No broker accounts configured" in w for w in result["warnings"])
    assert result["entered_variants"] == []


def test_full_scan_cycle_logs_error_and_reraises_on_mid_cycle_crash(wired_db):
    """2026-08-11 live-market finding: a crash inside run_full_scan_cycle (after
    real trades had already been opened for some variants) left ZERO cycle_log
    row — the dashboard showed "no cycles run yet" despite live trading having
    actually happened. Live-verified the fix against real Postgres the same
    day; this is the SQLite-backed regression test for it."""
    from engine import scheduler

    fixed_now = pd.Timestamp("2026-08-03 09:21", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.nse_universe.get_universe_symbols", return_value={"AAA"}), \
         patch("engine.stage1_ranking.fetch_ranking_data", side_effect=RuntimeError("simulated crash")), \
         patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"):

        with pytest.raises(RuntimeError, match="simulated crash"):
            scheduler.run_full_scan_cycle(config.load_settings())

    logs = wired_db.get_cycle_logs(limit=1)
    assert not logs.empty, "a crash must still leave a cycle_log row, not vanish silently"
    assert logs.iloc[0]["status"] == "ERROR"
    assert logs.iloc[0]["stage"] == "entry_scan"
    assert "simulated crash" in logs.iloc[0]["error"]


def test_full_scan_cycle_skips_cleanly_when_another_cycle_already_holds_the_scan_lock(wired_db):
    """2026-08-11 live-market finding: a scheduled cron run and a manual "Run
    Scan Now" click overlapped in production, producing an inconsistent
    'entered' list in the cycle log (was_flat still correctly prevented an
    actual duplicate trade row — verified against real data — but the log
    became misleading and Stage 1/2 API load doubled up needlessly)."""
    from engine import scheduler

    fixed_now = pd.Timestamp("2026-08-03 09:21", tz="Asia/Kolkata").to_pydatetime()

    with patch("engine.scheduler.db.try_acquire_scan_lock") as mock_lock, \
         patch("engine.scheduler._now_ist", return_value=fixed_now), \
         patch("engine.scheduler.market_status", return_value="open"):
        mock_lock.return_value.__enter__.return_value = False  # another cycle already holds it

        result = scheduler.run_full_scan_cycle(config.load_settings())

    assert result["status"] == "skipped"
    assert result["reason"] == "scan_already_in_progress"

    logs = wired_db.get_cycle_logs(limit=1)
    assert logs.iloc[0]["status"] == "SKIPPED"
