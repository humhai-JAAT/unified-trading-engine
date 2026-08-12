"""Regression coverage for engine/strategy.check_entry's freshness guard —
previously entirely untested despite being the exact mechanism that stops
the bot from entering on a STALE/ongoing buy signal (one that's already been
true for one or more prior bars) instead of only a signal that just turned
true on the current bar. User-reported concern 2026-08-12: "purane buy
signal pr trade nahi lena chahiye, sirf fresh candle pr" — this is the code
path responsible for that guarantee; these tests prove it actually holds.
"""

import numpy as np
import pandas as pd

from engine.strategy import MIN_BARS_REQUIRED, build_indicators, check_entry


def _rising_breakout_candles(warmup=None, rise_bars=8):
    """Flat/declining warm-up (so EMA100/trend-SMA are stable), then a sharp
    breakout that keeps rising for `rise_bars` bars in a row — long enough
    for entry_signal to stay True across MULTIPLE consecutive bars, so the
    freshness guard actually has something real to distinguish."""
    warmup = warmup if warmup is not None else MIN_BARS_REQUIRED + 5
    flat = np.linspace(100, 98, warmup)
    rise = np.linspace(98, 98 + rise_bars * 3, rise_bars + 1)[1:]  # steady climb
    prices = np.concatenate([flat, rise])
    idx = pd.date_range("2026-08-12 09:15", periods=len(prices), freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({
        "Open": prices, "High": prices * 1.002, "Low": prices * 0.998, "Close": prices,
        "Volume": 2000,
    }, index=idx)


def test_entry_signal_stays_true_across_several_bars_in_a_sustained_breakout():
    """Sanity check on the test data itself: confirms the synthetic breakout
    genuinely produces a multi-bar-True entry_signal window (not just one
    bar), otherwise the freshness test below wouldn't be testing anything."""
    df = _rising_breakout_candles()
    enriched = build_indicators(df)
    true_count = int(enriched["entry_signal"].tail(8).sum())
    assert true_count >= 2, "test fixture needs entry_signal True for 2+ consecutive bars"


def test_check_entry_fires_only_on_the_bar_the_signal_first_turns_true():
    df = _rising_breakout_candles()
    enriched = build_indicators(df)
    true_bars = enriched.index[enriched["entry_signal"]]
    assert len(true_bars) >= 2  # from the sanity check above
    first_true_bar = true_bars[0]

    # As of the FIRST bar where the signal is true, it just turned true this bar.
    df_at_first = df.loc[:first_true_bar]
    result = check_entry(df_at_first, today=first_true_bar)
    assert result.signal is True
    assert result.reason == "entry"


def test_check_entry_rejects_a_stale_signal_already_true_on_the_prior_bar():
    df = _rising_breakout_candles()
    enriched = build_indicators(df)
    true_bars = enriched.index[enriched["entry_signal"]]
    assert len(true_bars) >= 2
    second_true_bar = true_bars[1]  # signal was ALSO true one bar earlier — stale

    df_at_second = df.loc[:second_true_bar]
    result = check_entry(df_at_second, today=second_true_bar)
    assert result.signal is False
    assert result.reason == "signal_not_fresh"


def test_check_entry_no_signal_when_conditions_never_met():
    idx = pd.date_range("2026-08-12 09:15", periods=MIN_BARS_REQUIRED + 5, freq="5min", tz="Asia/Kolkata")
    flat = np.full(len(idx), 100.0)
    df = pd.DataFrame({"Open": flat, "High": flat, "Low": flat, "Close": flat, "Volume": 1000}, index=idx)
    result = check_entry(df)
    assert result.signal is False
    assert result.reason == "no_signal"


def test_check_entry_insufficient_history_before_warmup_completes():
    idx = pd.date_range("2026-08-12 09:15", periods=10, freq="5min", tz="Asia/Kolkata")
    flat = np.full(10, 100.0)
    df = pd.DataFrame({"Open": flat, "High": flat, "Low": flat, "Close": flat, "Volume": 1000}, index=idx)
    result = check_entry(df)
    assert result.signal is False
    assert result.reason == "insufficient_history"


def test_check_entry_blocks_an_arm_cycle_already_used_today():
    df = _rising_breakout_candles()
    enriched = build_indicators(df)
    first_true_bar = enriched.index[enriched["entry_signal"]][0]
    df_at_first = df.loc[:first_true_bar]

    fresh_check = check_entry(df_at_first, today=first_true_bar)
    assert fresh_check.signal is True
    arm_id = str(fresh_check.arm_cycle_id)

    # Same bar, but this arm_cycle_id was already used (e.g. this variant
    # already entered once for this exact crossover) — must not re-enter.
    reused_check = check_entry(df_at_first, used_arm_cycles={arm_id}, today=first_true_bar)
    assert reused_check.signal is False
    assert reused_check.reason == "arm_cycle_already_used"


def test_check_entry_blocks_an_arm_cycle_from_a_previous_day():
    """Real bug hit in production on the sibling bots (2026-07-14): without
    this guard, a bullish crossover from a PREVIOUS day that never got a
    bearish crossunder since stays 'armed' indefinitely."""
    df = _rising_breakout_candles()
    enriched = build_indicators(df)
    first_true_bar = enriched.index[enriched["entry_signal"]][0]
    df_at_first = df.loc[:first_true_bar]

    a_later_day = first_true_bar + pd.Timedelta(days=1)
    result = check_entry(df_at_first, today=a_later_day)
    assert result.signal is False
    assert result.reason == "arm_cycle_not_today"
