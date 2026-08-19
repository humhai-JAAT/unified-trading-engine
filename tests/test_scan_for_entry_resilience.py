"""Verifies the 2026-08-17 crash-isolation fix: before this, an exception
raised inside strategy.check_entry() for ANY single symbol (e.g. the
still-unroot-caused intermittent RecursionError seen live in production)
propagated all the way up and aborted run_full_scan_cycle() entirely - all
8 variants, every other symbol, for that whole cycle. scan_for_entry() now
catches per-symbol so one bad symbol can't starve the rest of the scan, and
records an "error: ..." reason so the failure is visible in the cycle log's
warnings instead of silently vanishing."""

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytz

from engine.variant_engine import scan_for_entry
from engine.strategy import EntryCheck

IST = pytz.timezone("Asia/Kolkata")

VARIANT_CFG = {"key": "puradin_trailing_ema", "entry_timing": "puradin", "exit_style": "ema"}


def _now():
    return IST.localize(datetime(2026, 8, 17, 10, 0))


def _candle_df():
    idx = pd.date_range("2026-08-17 09:15", periods=30, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0}, index=idx)


def test_a_crash_on_one_symbol_does_not_abort_the_whole_scan():
    top_n_df = pd.DataFrame({"symbol": ["CRASHY", "FINE"]})
    candles_by_symbol = {"CRASHY": _candle_df(), "FINE": _candle_df()}

    def fake_check_entry(df, used_arm_cycles=frozenset(), today=None):
        if df is candles_by_symbol["CRASHY"]:
            raise RecursionError("maximum recursion depth exceeded")
        return EntryCheck(signal=False, arm_cycle_id=None, close=100.0, reason="no_signal")

    with patch("engine.variant_engine.db.get_arm_cycles_used_today", return_value=set()), \
         patch("engine.variant_engine.strategy.check_entry", side_effect=fake_check_entry):
        result = scan_for_entry(
            "bot_400", VARIANT_CFG, settings={"starting_capital": 10000, "leverage_multiplier": 1.0},
            now=_now(), top_n_df=top_n_df, candles_by_symbol=candles_by_symbol, was_flat=True,
        )

    # The crash must not propagate - scan_for_entry returns normally.
    assert result["action"] == "no_signal"
    candidates = {c["symbol"]: c for c in result["candidates"]}

    # The crashing symbol is recorded with an "error: ..." reason, not lost.
    assert candidates["CRASHY"]["reason"].startswith("error: RecursionError")
    assert candidates["CRASHY"]["signal"] is False

    # The scan continued past the crash and still evaluated the next symbol.
    assert candidates["FINE"]["reason"] == "no_signal"


def test_a_crash_does_not_block_a_later_real_entry_signal():
    top_n_df = pd.DataFrame({"symbol": ["CRASHY", "SIGNAL"]})
    candles_by_symbol = {"CRASHY": _candle_df(), "SIGNAL": _candle_df()}

    def fake_check_entry(df, used_arm_cycles=frozenset(), today=None):
        if df is candles_by_symbol["CRASHY"]:
            raise RecursionError("maximum recursion depth exceeded")
        return EntryCheck(signal=True, arm_cycle_id=pd.Timestamp("2026-08-17 09:30", tz="Asia/Kolkata"),
                           close=105.0, reason="entry")

    with patch("engine.variant_engine.db.get_arm_cycles_used_today", return_value=set()), \
         patch("engine.variant_engine.broker.enter_position",
               return_value={"trade_id": 1, "symbol": "SIGNAL"}) as mock_enter:
        with patch("engine.variant_engine.strategy.check_entry", side_effect=fake_check_entry):
            result = scan_for_entry(
                "bot_400", VARIANT_CFG, settings={"starting_capital": 10000, "leverage_multiplier": 1.0},
                now=_now(), top_n_df=top_n_df, candles_by_symbol=candles_by_symbol, was_flat=True,
            )

    assert result["action"] == "enter"
    mock_enter.assert_called_once()


def test_a_crash_on_one_symbol_does_not_abort_the_whole_scan_via_indicator_cache():
    """Same crash-isolation guarantee as the first test above, but through the
    indicator_cache code path (2026-08-19) - production now always passes a
    shared cache built once per cycle via strategy.build_indicator_cache, so
    this path (not the check_entry fallback) is what actually runs live."""
    top_n_df = pd.DataFrame({"symbol": ["CRASHY", "FINE"]})
    candles_by_symbol = {"CRASHY": _candle_df(), "FINE": _candle_df()}
    indicator_cache = {"CRASHY": _candle_df(), "FINE": _candle_df()}  # stand-in "enriched" dfs

    def fake_decide_entry(enriched, used_arm_cycles=frozenset(), today=None):
        if enriched is indicator_cache["CRASHY"]:
            raise RecursionError("maximum recursion depth exceeded")
        return EntryCheck(signal=False, arm_cycle_id=None, close=100.0, reason="no_signal")

    with patch("engine.variant_engine.db.get_arm_cycles_used_today", return_value=set()), \
         patch("engine.variant_engine.strategy.decide_entry", side_effect=fake_decide_entry):
        result = scan_for_entry(
            "bot_400", VARIANT_CFG, settings={"starting_capital": 10000, "leverage_multiplier": 1.0},
            now=_now(), top_n_df=top_n_df, candles_by_symbol=candles_by_symbol, was_flat=True,
            indicator_cache=indicator_cache,
        )

    assert result["action"] == "no_signal"
    candidates = {c["symbol"]: c for c in result["candidates"]}
    assert candidates["CRASHY"]["reason"].startswith("error: RecursionError")
    assert candidates["FINE"]["reason"] == "no_signal"
