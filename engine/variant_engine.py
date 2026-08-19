"""Per-variant entry-timing + trailing-exit logic — the 8 variants (2
universe-bots x 4 variants, see engine/config.py) each run this against the
SHARED data Stage 1/Stage 2 already fetched once per cycle. No variant ever
fetches its own gainers ranking or candle history from scratch — that's the
whole point of this project's architecture (see Architecture.md).

Entry timing:
  'subh30'  — checkpoint-style, only evaluates entries at 09:20/09:25/09:30,
              each checked 1 min after its own candle closes (09:21/09:26/
              09:31) — resolved with the user 2026-08-02, see Phase.md.
  'puradin' — continuous, evaluates every cycle, no fixed checkpoints.

Exit: every variant's stop-loss and target are both fixed %, but reaching the
target does NOT exit immediately — it flips the position into trailing mode
(see mark_target_hit), and the trailing MECHANISM is what differs:
  'ema' — exits when a 5-min candle closes below its own EMA9.
  'atr' — exits when price pulls back more than atr_multiplier*ATR from the
          peak reached since entry.
Both share a hard floor once trailing is active: exit price can never be below
the original target level (see PRD.md's note — there is no standalone "fixed
exit" variant).
"""

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

import pandas as pd
import pytz

from common import indicators
from common.helpers import get_logger
from engine import broker, config, db, strategy
from engine.broker_accounts import BrokerAccount, get_configured_accounts
from engine.stage2_candles import CALENDAR_FETCH_DAYS, CANDLE_LOOKBACK_TRADING_DAYS, trim_to_last_n_trading_days

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_TRAIL_EXIT_REASON = {"ema": "EMA_TRAIL_EXIT", "atr": "ATR_TRAIL_EXIT"}


def _parse_time(hhmm: str) -> dtime:
    h, m = map(int, hhmm.split(":"))
    return dtime(h, m)


def _timestamp_ist(time_str: str) -> pd.Timestamp:
    ts = pd.Timestamp(time_str)
    return IST.localize(ts) if ts.tzinfo is None else ts.tz_convert(IST)


# ---------------------------------------------------------------------------
# Subh-30-min checkpoint gating
# ---------------------------------------------------------------------------

def next_due_subh30_checkpoint(used: set[str], now: datetime) -> str | None:
    """Returns the checkpoint time string (e.g. '09:20') that's currently due,
    or None if none is due right now. A checkpoint is 'due' starting 1 minute
    after its own clock time (giving the 5-min candle time to fully close and
    the data provider time to have it available — same safety buffer used
    elsewhere in this design), and stays due for SUBH30_CHECKPOINT_GRACE_MINUTES
    after that — if missed beyond the grace window, it's skipped permanently
    for the day, never fired late (mirrors the sibling v1 bot's fix for the
    JSWENERGY late-checkpoint bug, 2026-07-20)."""
    for cp in config.SUBH30_CHECKPOINTS:
        if cp in used:
            continue
        cp_time = _parse_time(cp)
        cp_dt = now.replace(hour=cp_time.hour, minute=cp_time.minute, second=0, microsecond=0)
        window_start = cp_dt + timedelta(minutes=1)  # e.g. 09:20 candle checked from 09:21
        window_end = cp_dt + timedelta(minutes=1 + config.SUBH30_CHECKPOINT_GRACE_MINUTES)
        if window_start <= now < window_end:
            return cp
    return None


# ---------------------------------------------------------------------------
# Trailing-exit checks (both mechanisms need 5-min candle data for the symbol)
# ---------------------------------------------------------------------------

def check_ema9_trail_exit(candle_df: pd.DataFrame) -> float | None:
    """Returns the exit price if the most recently closed 5-min candle closed
    below its own EMA9, else None."""
    if candle_df is None or candle_df.empty or len(candle_df) < 10:
        return None
    ema9 = indicators.ema(candle_df["Close"], 9)
    last_close = float(candle_df["Close"].iloc[-1])
    if last_close < float(ema9.iloc[-1]):
        return last_close
    return None


def check_atr_trail_exit(candle_df: pd.DataFrame, peak_price: float, atr_period: int,
                          atr_multiplier: float) -> float | None:
    """Returns the exit price if price has pulled back more than
    atr_multiplier*ATR from the peak reached since entry, else None."""
    if candle_df is None or candle_df.empty or len(candle_df) < atr_period + 1:
        return None
    atr_series = indicators.atr(candle_df, atr_period)
    last_close = float(candle_df["Close"].iloc[-1])
    last_atr = float(atr_series.iloc[-1])
    if (peak_price - last_close) >= atr_multiplier * last_atr:
        return last_close
    return None


# ---------------------------------------------------------------------------
# Open-position management — small scale (<=12 open positions total), so this
# fetches its own 1m/5m data directly via one account rather than going
# through Stage 1/2's parallel machinery, which is sized for the 751-stock
# universe scan, not a handful of open-position lookups.
# ---------------------------------------------------------------------------

def _position_data_accounts() -> list[BrokerAccount]:
    """Primary (Groww) then fallback (Angel One) — same account priority as
    Stage 1/2, but previously this path had NO fallback at all (2026-08-12
    live finding: Groww hit its own rate limit, which silently froze
    position management for every open trade with zero visibility, since a
    Groww-only failure here fell straight through to the "no_price_data"
    hold below instead of trying the other broker)."""
    accounts = get_configured_accounts()
    return (accounts["groww"][:1] or []) + (accounts["angelone"][:1] or [])


def manage_open_position(variant_id: str, variant_cfg: dict, trade: dict, settings: dict,
                          now: datetime) -> dict:
    """Ported from the sibling v2 bot's _manage_open_position, adapted to this
    project's variant_id naming and account-based fetching. Tries each
    configured account in priority order (Groww, then Angel One) until one
    returns real candle data — see _position_data_accounts()."""
    accounts = _position_data_accounts()
    if not accounts:
        return {"action": "hold", "reason": "no_broker_account_configured", "symbol": trade["symbol"]}

    symbol = trade["symbol"]
    account = None
    df_1m = None
    failed_accounts = []
    for candidate in accounts:
        try:
            candidate_df = candidate.fetch_candles(symbol, interval="1m", period_days=1)
        except Exception as e:
            failed_accounts.append(f"{candidate.account_id}: {e}")
            continue
        if candidate_df is not None and not candidate_df.empty:
            account, df_1m = candidate, candidate_df
            break
        failed_accounts.append(f"{candidate.account_id}: empty response")

    if account is None:
        return {"action": "hold", "reason": "no_price_data", "symbol": symbol,
                "failed_accounts": failed_accounts}

    return locked_decide_and_exit(variant_id, variant_cfg, trade, settings, now, account, symbol, df_1m)


def locked_decide_and_exit(variant_id: str, variant_cfg: dict, trade: dict, settings: dict, now: datetime,
                            account: BrokerAccount, symbol: str, df_1m: pd.DataFrame) -> dict:
    """Shared entry point for BOTH manage_open_position (REST, every 2 min) and
    engine.live_feed (websocket tick-driven, sub-second) — both must go through
    this so db.acquire_trade_lock() actually prevents the two concurrent paths
    from double-exiting the same trade. Re-checks the trade is still open under
    the lock rather than trusting the caller's possibly-stale snapshot. No-ops
    on SQLite (see db.acquire_trade_lock's docstring), so local dev/tests are
    unaffected. Locked per-variant_id (2026-08-10 code review) — a different
    variant's trailing-exit REST candle fetch (which happens while THIS lock
    is held, see _decide_and_exit) no longer blocks unrelated variants."""
    with db.acquire_trade_lock(variant_id):
        fresh_trade = db.get_open_trade(variant_id)
        if fresh_trade is None or fresh_trade["id"] != trade["id"]:
            return {"action": "hold", "reason": "already_closed", "symbol": symbol}

        return _decide_and_exit(variant_id, variant_cfg, fresh_trade, settings, now, account, symbol, df_1m)


def _decide_and_exit(variant_id: str, variant_cfg: dict, trade: dict, settings: dict, now: datetime,
                      account: BrokerAccount, symbol: str, df_1m: pd.DataFrame) -> dict:
    """The actual stop-loss/target/trailing/square-off decision + DB write —
    split out of manage_open_position so it always runs under db.acquire_trade_lock()."""
    entry_time = _timestamp_ist(trade["entry_time"])
    quantity = trade["quantity"]
    capital_used = trade["capital_used"]
    target_price = trade["entry_price"] + (capital_used * settings["profit_target_pct"] / 100) / quantity
    stop_price = trade["entry_price"] - (capital_used * settings["stop_loss_pct"] / 100) / quantity

    since_entry = df_1m[df_1m.index >= entry_time]
    if since_entry.empty:
        since_entry = df_1m.tail(1)
    price = float(since_entry["Close"].iloc[-1])
    recent_high = float(since_entry["High"].max())
    recent_low = float(since_entry["Low"].min())
    peak, trough = broker.update_extremes(variant_id, trade["id"], trade["peak_price"], trade["trough_price"],
                                           recent_high, recent_low)

    target_hit = bool(trade.get("target_hit"))
    reason = None
    exit_price = price

    if not target_hit:
        for _, row in since_entry.iterrows():
            if row["Low"] <= stop_price:
                reason, exit_price = "STOP_LOSS", stop_price
                break
            if row["High"] >= target_price:
                target_hit = True
                break
        if target_hit and reason is None:
            db.mark_target_hit(variant_id, trade["id"])

    if reason is None and target_hit:
        df_5m = account.fetch_candles(symbol, interval="5m", period_days=CALENDAR_FETCH_DAYS)
        df_5m = trim_to_last_n_trading_days(df_5m, CANDLE_LOOKBACK_TRADING_DAYS)
        exit_style = variant_cfg["exit_style"]
        if exit_style == "ema":
            trail_price = check_ema9_trail_exit(df_5m)
        else:
            trail_price = check_atr_trail_exit(df_5m, peak, settings["atr_period"], settings["atr_multiplier"])
        if trail_price is not None:
            reason = _TRAIL_EXIT_REASON[exit_style]
            # Hard floor: once trailing, never exit below the original target —
            # there is no standalone "fixed exit" here, but a winning trade can
            # also never round-trip into a loss once target was reached.
            exit_price = max(trail_price, target_price)

    square_off_time = _parse_time(settings["square_off_time"])
    if reason is None and now.time() >= square_off_time:
        reason = "SQUARE_OFF"
        exit_price = max(price, target_price) if target_hit else price

    pnl_pct = (exit_price - trade["entry_price"]) * quantity / capital_used * 100

    if reason:
        result = broker.exit_position(variant_id, trade["id"], trade["quantity"], exit_price, reason)
        return {"action": "exit", "symbol": symbol, "price": exit_price, "pnl_pct": pnl_pct,
                "reason": reason, "target_hit": target_hit, **result}

    return {"action": "hold", "symbol": symbol, "price": price, "pnl_pct": pnl_pct,
            "peak": peak, "trough": trough, "target_hit": target_hit}


# ---------------------------------------------------------------------------
# Entry scanning — consumes Stage 1/2's shared data, never fetches its own.
# ---------------------------------------------------------------------------

def scan_for_entry(universe_bot_key: str, variant_cfg: dict, settings: dict, now: datetime,
                    top_n_df: pd.DataFrame, candles_by_symbol: dict[str, pd.DataFrame],
                    was_flat: bool, indicator_cache: dict[str, pd.DataFrame] | None = None) -> dict:
    """Scans `top_n_df` (this universe-bot's own top-N from Stage 1) against
    `candles_by_symbol` (Stage 2's shared candle-history dict) for a fresh
    entry signal, gated by this variant's entry-timing (subh30 checkpoint /
    puradin continuous).

    `indicator_cache` (build once per cycle via strategy.build_indicator_cache,
    shared across all 8 variants' scan_for_entry calls — see scheduler.py) lets
    every variant reuse the SAME symbol's already-computed EMA/MACD instead of
    recomputing it per variant — build_indicators() is purely symbol-dependent,
    only the final decide_entry() step is genuinely variant-specific (each
    variant tracks its own used_arm_cycles). Falls back to computing indicators
    inline (the old per-call behavior) if no cache is supplied, e.g. from a
    test calling this directly."""
    variant_id = f"{universe_bot_key}/{variant_cfg['key']}"

    if variant_cfg["entry_timing"] == "subh30":
        used = db.get_checkpoints_used_today(variant_id)
        checkpoint = next_due_subh30_checkpoint(used, now)
        if checkpoint is None:
            reason = "all_checkpoints_used" if used >= set(config.SUBH30_CHECKPOINTS) else "no_checkpoint_due_yet"
            return {"action": "skip_scan", "reason": reason}
    else:
        checkpoint = None  # puradin: no checkpoint concept, always eligible to scan

    if not was_flat:
        return {"action": "skip_scan", "reason": "already_in_position"}

    candidates_checked = []
    for symbol in top_n_df["symbol"].tolist() if not top_n_df.empty else []:
        candle_df = candles_by_symbol.get(symbol)
        if candle_df is None or candle_df.empty:
            candidates_checked.append({"symbol": symbol, "signal": False, "reason": "no_candle_data"})
            continue

        try:
            used_arm_cycles = db.get_arm_cycles_used_today(variant_id, symbol)
            if indicator_cache is not None:
                enriched = indicator_cache.get(symbol)
                check = (strategy.decide_entry(enriched, used_arm_cycles=used_arm_cycles, today=now)
                         if enriched is not None
                         else strategy.EntryCheck(False, None, float(candle_df["Close"].iloc[-1]),
                                                   "insufficient_history"))
            else:
                check = strategy.check_entry(candle_df, used_arm_cycles=used_arm_cycles, today=now)
        except Exception as e:
            # 2026-08-17: check_entry has been crashing the ENTIRE cycle (all 8
            # variants, every symbol) with an intermittent, still-unroot-caused
            # RecursionError — isolating it to just this one symbol so the rest
            # of the scan survives, and surfacing exactly which symbol/variant
            # triggered it (see scheduler.py's warnings aggregation) instead of
            # losing that breadcrumb the way the old whole-cycle crash did.
            logger.error(f"check_entry crashed for {variant_id}/{symbol}: {type(e).__name__}: {e}")
            candidates_checked.append({"symbol": symbol, "signal": False,
                                        "reason": f"error: {type(e).__name__}: {e}"})
            continue
        candidates_checked.append({"symbol": symbol, "signal": bool(check.signal), "reason": check.reason})

        if check.signal:
            starting_capital = settings["starting_capital"]
            leverage = settings.get("leverage_multiplier", 1.0)
            trade = broker.enter_position(variant_id, symbol, check.close, starting_capital,
                                           check.arm_cycle_id, leverage)
            if checkpoint is not None:
                db.mark_checkpoint_used(variant_id, checkpoint, True, symbol, trade["trade_id"])
            return {"action": "enter", "checkpoint": checkpoint, "candidates": candidates_checked,
                    "entered": trade}

    if checkpoint is not None:
        db.mark_checkpoint_used(variant_id, checkpoint, False, None)
    return {"action": "no_signal", "checkpoint": checkpoint, "candidates": candidates_checked}
