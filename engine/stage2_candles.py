"""Stage 2 — candle history layer. See Architecture.md's "Stage 2 — candle
history layer" for the full design this implements.

Merges the 3 universe-bots' top-N lists from Stage 1, removes duplicate
symbols, then fetches 1-symbol-per-call candle history ONLY for that
deduplicated set — shared read-only across whichever of the 12 variants need
each symbol, instead of every variant/universe re-fetching independently.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import pandas as pd

from common.helpers import get_logger
from engine.broker_accounts import BrokerAccount, get_configured_accounts

logger = get_logger(__name__)

CANDLE_WORKERS_PER_ACCOUNT = 3


@dataclass
class Stage2Result:
    candles_by_symbol: dict[str, pd.DataFrame] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    symbols_requested: int = 0
    symbols_fetched: int = 0


def merge_unique_symbols(top_lists: dict[str, pd.DataFrame]) -> list[str]:
    """Union of symbols across all universe-bots' top-N lists (e.g. 3 lists of
    50, typically ~100-150 unique after overlap) — the exact input to Stage 2's
    fetch, see Architecture.md's Stage 2 diagram."""
    seen: set[str] = set()
    ordered: list[str] = []
    for df in top_lists.values():
        if df is None or df.empty:
            continue
        for symbol in df["symbol"]:
            if symbol not in seen:
                seen.add(symbol)
                ordered.append(symbol)
    return ordered


def _fetch_one(account: BrokerAccount, symbol: str, interval: str, period_days: int) -> "pd.DataFrame | None":
    return account.fetch_candles(symbol, interval=interval, period_days=period_days)


def fetch_candle_history(symbols: list[str], interval: str = "5m", period_days: int = 5,
                          primary_accounts: list[BrokerAccount] | None = None,
                          fallback_accounts: list[BrokerAccount] | None = None) -> Stage2Result:
    """Fetches candle history for each of `symbols` exactly once, in parallel
    across `primary_accounts` (defaults to configured Groww accounts), with a
    per-symbol fallback to `fallback_accounts` (defaults to configured Angel
    One accounts) on failure — matches Stage 1's chunk-level fallback pattern,
    just at symbol granularity here since candle-history calls are already
    1-symbol-per-call (no batching to fail at a coarser level)."""
    accounts = get_configured_accounts()
    if primary_accounts is None:
        primary_accounts = accounts["groww"] or accounts["angelone"]
    if fallback_accounts is None:
        fallback_accounts = accounts["angelone"]

    result = Stage2Result(symbols_requested=len(symbols))
    if not primary_accounts or not symbols:
        if not primary_accounts:
            result.warnings.append("No broker accounts configured for Stage 2 candle fetch.")
        return result

    workers = []
    for account in primary_accounts:
        workers.extend([account] * CANDLE_WORKERS_PER_ACCOUNT)

    failed_symbols: list[str] = []
    with ThreadPoolExecutor(max_workers=max(len(workers), 1)) as executor:
        futures = {
            executor.submit(_fetch_one, workers[i % len(workers)], symbol, interval, period_days): symbol
            for i, symbol in enumerate(symbols)
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                df = future.result()
            except Exception as e:
                logger.warning(f"Stage 2 fetch for {symbol} raised: {e}")
                df = None
            if df is not None and not df.empty:
                result.candles_by_symbol[symbol] = df
            else:
                failed_symbols.append(symbol)

    if failed_symbols and fallback_accounts:
        # Parallel across CANDLE_WORKERS_PER_ACCOUNT workers on the fallback
        # account, same pattern as the primary pass above — was a plain
        # sequential loop until 2026-08-11 live testing found it: when the
        # PRIMARY broker degrades (rate-limited, etc.), its ENTIRE symbol
        # load cascades onto the fallback in one burst. A sequential retry of
        # 30-70+ symbols at the fallback account's own (typically slower,
        # e.g. Angel One's 0.4s/call) rate limit took long enough that some
        # calls started failing that succeeded fine when tested individually
        # in isolation — a "thundering herd on the fallback path" symptom,
        # not a problem with those specific symbols. Parallelizing shortens
        # the burst's wall-clock duration; the fallback account's existing
        # AccountRateLimiter still correctly throttles regardless of how
        # many workers share it (that's its whole design, see rate_limiter.py).
        fallback_account = fallback_accounts[0]
        fallback_workers = [fallback_account] * CANDLE_WORKERS_PER_ACCOUNT
        still_failed = []
        with ThreadPoolExecutor(max_workers=len(fallback_workers)) as executor:
            futures = {
                executor.submit(_fetch_one, fallback_workers[i % len(fallback_workers)],
                                 symbol, interval, period_days): symbol
                for i, symbol in enumerate(failed_symbols)
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    df = future.result()
                except Exception as e:
                    logger.warning(f"Stage 2 fallback fetch for {symbol} raised: {e}")
                    df = None
                if df is not None and not df.empty:
                    result.candles_by_symbol[symbol] = df
                else:
                    still_failed.append(symbol)
        if still_failed:
            result.warnings.append(
                f"{len(still_failed)}/{len(symbols)} symbols had no candle data even after "
                f"fallback ({fallback_account.account_id}): {still_failed[:10]}"
                f"{'...' if len(still_failed) > 10 else ''}"
            )
    elif failed_symbols:
        result.warnings.append(
            f"{len(failed_symbols)}/{len(symbols)} symbols had no candle data and no fallback "
            f"account was configured to retry them."
        )

    result.symbols_fetched = len(result.candles_by_symbol)
    return result
