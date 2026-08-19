"""Port of the user's Pine Script "EMA-MACD V2.1.2" — entry logic only, unchanged
from the sibling bots (intraday-trading-bot v1/v2, bot-v3). Timeframe-agnostic:
works on whatever OHLC bars are passed in (this project evaluates it on 5-min
candles for all 8 variants — see Architecture.md).

Pine reference (entry side):
    fastEMA=9  slowEMA=30  trendEMA=100  trendSMA=9 (SMA of EMA100)
    MACD(12,26,9)
    emaSepMinPct = 0.6
    setupArmed: latched True on a bullish EMA9/EMA30 crossover, latched False on
                a bearish crossover, and consumed (set False) the instant an
                entry fires — so only one entry per arm cycle.
    entryCondition = setupArmed and emaFast>emaSlow and macd>signal and
                     ema100>sma(ema100,9) and emaSepPct>=0.6 and flat
"""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from common.indicators import ema, macd, sma

FAST_EMA = 9
SLOW_EMA = 30
TREND_EMA = 100
TREND_SMA = 9
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
EMA_SEP_MIN_PCT = 0.6

MIN_BARS_REQUIRED = TREND_EMA + TREND_SMA  # EMA100 + its SMA need this much warm-up

# How many calendar days old the ARM CYCLE (the EMA9/30 crossover bar) is
# allowed to be, relative to the bar being evaluated. NOT the same thing as
# the freshness guard below (which checks the TRIGGER bar, i.e. entry_signal
# itself). 3 days comfortably survives a Friday crossover carrying into a
# Monday trigger (3 calendar days apart) without letting a crossover from
# last week or earlier stay armed indefinitely — see check_entry's docstring.
MAX_ARM_CYCLE_AGE_DAYS = 3


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    close = out["Close"]

    out["ema_fast"] = ema(close, FAST_EMA)
    out["ema_slow"] = ema(close, SLOW_EMA)
    out["ema_trend"] = ema(close, TREND_EMA)
    out["ema_trend_sma"] = sma(out["ema_trend"], TREND_SMA)

    macd_df = macd(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL)
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["macd_signal"]

    out["ema_sep_pct"] = (out["ema_fast"] - out["ema_slow"]) / out["ema_slow"] * 100

    bull_cross = (out["ema_fast"] > out["ema_slow"]) & (out["ema_fast"].shift(1) <= out["ema_slow"].shift(1))
    bear_cross = (out["ema_fast"] < out["ema_slow"]) & (out["ema_fast"].shift(1) >= out["ema_slow"].shift(1))

    arm_signal = pd.Series(float("nan"), index=out.index, dtype="float64")
    arm_signal[bull_cross] = 1.0
    arm_signal[bear_cross] = 0.0
    out["armed"] = arm_signal.ffill().fillna(0.0).astype(bool)

    # Timestamp of the bull-cross bar that produced the *current* armed state —
    # used to make sure we only take one entry per arm cycle (mirrors Pine's
    # setupArmed being consumed the instant an entry fires).
    bull_cross_time = pd.Series(pd.NaT, index=out.index, dtype=out.index.dtype)
    bull_cross_time[bull_cross] = out.index[bull_cross]
    out["arm_cycle_id"] = bull_cross_time.ffill()

    out["macd_bullish"] = out["macd"] > out["macd_signal"]
    out["trend_bullish"] = out["ema_trend"] > out["ema_trend_sma"]
    out["ema_sep_ok"] = out["ema_sep_pct"] >= EMA_SEP_MIN_PCT

    out["entry_signal"] = (
        out["armed"] & (out["ema_fast"] > out["ema_slow"]) & out["macd_bullish"]
        & out["trend_bullish"] & out["ema_sep_ok"]
    )
    return out


@dataclass
class EntryCheck:
    signal: bool
    arm_cycle_id: "pd.Timestamp | None"
    close: float
    reason: str


def decide_entry(enriched: pd.DataFrame, used_arm_cycles: set[str] = frozenset(),
                  today: "pd.Timestamp | None" = None) -> EntryCheck:
    """The variant-specific decision half of check_entry, split out 2026-08-19
    so build_indicators() (the expensive, purely symbol-dependent half - EMA9/
    30/100, MACD) can be computed ONCE per unique symbol and shared across all
    8 variants, instead of every variant recomputing identical indicator math
    for the same symbol. Only this decision half genuinely differs per variant
    (used_arm_cycles is each variant's own trade history). `enriched` must
    already be build_indicators()'s output - see check_entry() below for the
    single-call convenience wrapper that does both steps.

    Live-verified 2026-08-19: caching indicators once per unique symbol
    (~40/cycle) instead of once per (variant, symbol) slot (~240/cycle, since
    scan_for_entry evaluates each of the 2 universe-bots' top-30 across all 4
    of that universe-bot's variants) cut real measured scan time from
    ~1198ms to ~185ms (6.5x) with 0 result mismatches across all 240 slots.

    today (the timestamp of the bar being evaluated, IST) bounds how stale the ARM
    CYCLE (the EMA9/30 crossover bar) is allowed to be — without this, a bullish
    crossover from a PREVIOUS day that never got a bearish crossunder since stays
    "armed" indefinitely, and could fire a "fresh" entry_signal transition weeks
    later using that stale precondition. Real bug hit in production on the sibling
    bots (2026-07-14), ported here as a permanent guard.

    2026-08-13 correction: this used to require the arm cycle be from THE EXACT
    SAME calendar day as the trigger bar — but the trigger bar itself is already
    guaranteed fresh by the signal_not_fresh check above (entry_signal just turned
    True on THIS bar). Requiring the crossover ALSO be same-day was stricter than
    the original bug needed and blocked real, valid trades whose crossover simply
    happened the previous trading day (confirmed live: ASTRAL and NETWEB both had
    a 1-day-old crossover with a genuinely fresh trigger this morning, and got
    wrongly rejected). Now bounded by MAX_ARM_CYCLE_AGE_DAYS instead of same-day."""
    if len(enriched) < MIN_BARS_REQUIRED:
        return EntryCheck(False, None, float(enriched["Close"].iloc[-1]) if len(enriched) else 0.0,
                           "insufficient_history")

    last = enriched.iloc[-1]
    close = float(last["Close"])

    if not bool(last["entry_signal"]):
        return EntryCheck(False, last["arm_cycle_id"], close, "no_signal")

    # Only take the trade if entry_signal *just* turned true on this exact bar (false
    # on the previous bar) — same freshness guard the sibling bots use (their NUVOCO
    # fix, 2026-07-15). Still only looks 1 bar back — the sibling bots' known-unfixed
    # flicker edge case (a signal flickering True->False->True within one arm cycle)
    # applies here too; not addressed by this project, see intraday_bot_v2's memory.
    if len(enriched) > 1 and bool(enriched.iloc[-2]["entry_signal"]):
        return EntryCheck(False, last["arm_cycle_id"], close, "signal_not_fresh")

    if today is not None and pd.notna(last["arm_cycle_id"]):
        age_days = (today.date() - pd.Timestamp(last["arm_cycle_id"]).date()).days
        if age_days > MAX_ARM_CYCLE_AGE_DAYS:
            return EntryCheck(False, last["arm_cycle_id"], close, "arm_cycle_stale")

    arm_id_str = str(last["arm_cycle_id"]) if pd.notna(last["arm_cycle_id"]) else None
    if arm_id_str is not None and arm_id_str in used_arm_cycles:
        return EntryCheck(False, last["arm_cycle_id"], close, "arm_cycle_already_used")

    return EntryCheck(True, last["arm_cycle_id"], close, "entry")


def build_indicator_cache(candles_by_symbol: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Computes build_indicators() ONCE per unique symbol in Stage 2's shared
    candle dict — the cache scan_for_entry() should build once per cycle and
    reuse across all 8 variants, instead of each variant recomputing the same
    symbol's indicators independently. A symbol with fewer than
    MIN_BARS_REQUIRED bars is simply omitted (decide_entry's caller then sees
    it as a cache miss and reports "insufficient_history", same as before)."""
    return {
        symbol: build_indicators(df)
        for symbol, df in candles_by_symbol.items()
        if df is not None and len(df) >= MIN_BARS_REQUIRED
    }


def check_entry(df: pd.DataFrame, used_arm_cycles: set[str] = frozenset(),
                 today: "pd.Timestamp | None" = None) -> EntryCheck:
    """Single-call convenience wrapper: build_indicators() + decide_entry() in
    one step. Prefer build_indicator_cache() + decide_entry() directly when
    evaluating the SAME symbol across multiple variants in one cycle (see
    decide_entry's docstring) — this wrapper recomputes indicators every call,
    which is correct but wasteful for that case."""
    if len(df) < MIN_BARS_REQUIRED:
        return EntryCheck(False, None, float(df["Close"].iloc[-1]) if len(df) else 0.0, "insufficient_history")
    return decide_entry(build_indicators(df), used_arm_cycles, today)
