"""Persistent Groww websocket (GrowwFeed) listener for tick-driven stop-loss/
target/trailing-exit reaction on OPEN positions — much faster than waiting for
the 2-min REST position-management job (engine.scheduler's _position_job).
See memory.md's 2026-08-10 entry for the design discussion this implements.

Only covers positions whose data can come from a configured Groww account —
GrowwFeed is Groww-specific, there is no websocket equivalent for Angel One in
this codebase. If no Groww account is configured, start() is a no-op and the
2-min REST job (unchanged, always runs regardless) remains the only path —
a fail-closed degrade, never a hard requirement.

Runs as a single daemon background thread, started/stopped alongside
engine.scheduler.start_scheduler()/stop_scheduler(). Every poll_interval_seconds
(default 2s):
  1. re-syncs the websocket subscription set to whichever symbols currently
     have an open position across all 8 variants (subscribes new, unsubscribes
     closed) — NOT the full 400-stock universe, only actual open positions (at
     most 8 symbols), so subscription-count is never a concern here (see the
     2026-08-10 test that subscribed to the full 751-stock universe with no
     error at all — 12 is trivially smaller).
  2. reads the latest LTP for each subscribed symbol via GrowwFeed.get_all_feed()
  3. for each open position with a fresh tick, builds a synthetic single-row
     "candle" (Open=High=Low=Close=tick price) and runs it through
     engine.variant_engine.locked_decide_and_exit() — reusing the EXACT same
     tested stop-loss/target/trailing/square-off/hard-floor decision logic the
     2-min REST path uses, under the same db.acquire_trade_lock(), so the two
     concurrent paths can never double-exit the same trade.

Deliberately does NOT touch: Stage 1/2 (REST, unaffected — historical candle
data isn't available over this websocket at all, see Architecture.md) or the
2-min REST job itself (kept running unconditionally as a safety net — if this
thread disconnects or was never started, behavior silently degrades back to
2-min-only reaction, never below that).

NOT continuous protection, despite the name — 2026-08-10 code review note:
each poll only checks the price AT THAT INSTANT (one tick, not every tick
that arrived since the last poll). A stop-loss/target level that gets
touched and recovers again within a single poll_interval_seconds window
(default 2s) can be missed here. The 2-min REST job's manage_open_position()
doesn't have this gap — it scans every 1-min candle's actual High/Low since
entry, not just the latest price — so it will still catch it retroactively,
just up to 2 minutes later. Acceptable given how narrow the window is, but a
real gap, not a hypothetical one — don't describe this module as guaranteeing
sub-second SL/target enforcement, only sub-second REACTION to whatever price
was live at each poll.
"""

import threading
import time
from datetime import datetime

import pandas as pd
import pytz

from common.helpers import get_logger
from engine import config, db
from engine.broker_accounts import GrowwAccount, get_configured_accounts
from engine.variant_engine import locked_decide_and_exit

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
# account._load_symbol_to_token() re-reads+parses its ~4k-row instrument-master
# CSV from disk on every call (its own cache is file-based, 7-day TTL) — fine
# for occasional use, too expensive to call on every ~2s poll. Cache the
# resulting dict here IN MEMORY for the life of this thread, refreshed hourly
# — well under the account's own 7-day file-cache TTL, just avoids redundant
# disk I/O within that window (2026-08-10 code review finding).
SYMBOL_TOKEN_CACHE_TTL_SECONDS = 3600

_thread: threading.Thread | None = None
_stop_event = threading.Event()
_feed = None  # growwapi.GrowwFeed instance, set once connected
_subscribed_tokens: dict[str, str] = {}  # exchange_token -> symbol, currently subscribed
# Guards _subscribed_tokens — stop() (called from whatever thread calls
# stop_scheduler(), usually the main/Streamlit thread) and _sync_subscriptions()
# (the background _run_loop thread) both read-then-write it; without this,
# a stop() landing mid-_sync_subscriptions could race a stale value back in
# (2026-08-10 code review — benign under the GIL, self-heals next poll, but
# worth closing properly rather than relying on that).
_subscribed_tokens_lock = threading.Lock()
_symbol_token_cache: dict[str, str] = {}
_symbol_token_cache_loaded_at = 0.0


def _groww_account() -> GrowwAccount | None:
    pool = get_configured_accounts().get("groww") or []
    return pool[0] if pool else None


def _all_open_trades() -> dict[str, dict]:
    """{variant_id: trade} for every one of the 8 variants with an open position."""
    open_trades = {}
    for universe_bot in config.UNIVERSE_BOTS:
        for variant_cfg in config.VARIANTS:
            variant_id = f"{universe_bot['key']}/{variant_cfg['key']}"
            trade = db.get_open_trade(variant_id)
            if trade:
                open_trades[variant_id] = trade
    return open_trades


def _instrument(token: str) -> dict:
    return {"exchange": "NSE", "segment": "CASH", "exchange_token": token}


def _get_symbol_to_token(account: GrowwAccount) -> dict[str, str]:
    """In-memory-cached wrapper around account._load_symbol_to_token() — see
    SYMBOL_TOKEN_CACHE_TTL_SECONDS' comment for why this exists."""
    global _symbol_token_cache, _symbol_token_cache_loaded_at
    if not _symbol_token_cache or time.time() - _symbol_token_cache_loaded_at > SYMBOL_TOKEN_CACHE_TTL_SECONDS:
        _symbol_token_cache = account._load_symbol_to_token()
        _symbol_token_cache_loaded_at = time.time()
    return _symbol_token_cache


def _sync_subscriptions(account: GrowwAccount, feed, open_trades: dict[str, dict]) -> None:
    """Diffs the current subscription set against `open_trades`' symbols and
    subscribes/unsubscribes only the delta. Symbols with no known
    exchange_token are logged and skipped, not treated as fatal — matches this
    project's established fail-closed contract (see broker_accounts.py)."""
    global _subscribed_tokens

    wanted_symbols = {trade["symbol"] for trade in open_trades.values()}
    symbol_to_token = _get_symbol_to_token(account) if wanted_symbols else {}
    wanted_tokens: dict[str, str] = {}
    for sym in wanted_symbols:
        token = symbol_to_token.get(sym)
        if token:
            wanted_tokens[token] = sym
        else:
            logger.warning(f"live_feed: no exchange_token found for {sym!r}, cannot subscribe")

    # Lock brackets only the dict read/write, never the network calls below —
    # same "don't hold a lock across I/O" principle as db.acquire_trade_lock's
    # 2026-08-10 fix (see that commit).
    with _subscribed_tokens_lock:
        current = dict(_subscribed_tokens)
    to_add = {tok: sym for tok, sym in wanted_tokens.items() if tok not in current}
    to_remove = {tok: sym for tok, sym in current.items() if tok not in wanted_tokens}

    if to_remove:
        try:
            feed.unsubscribe_ltp([_instrument(tok) for tok in to_remove])
        except Exception as e:
            logger.warning(f"live_feed: unsubscribe failed for {list(to_remove.values())}: {e}")

    if to_add:
        try:
            feed.subscribe_ltp([_instrument(tok) for tok in to_add])
        except Exception as e:
            logger.warning(f"live_feed: subscribe failed for {list(to_add.values())}: {e}")
            # Don't claim tokens we're not sure actually subscribed — retry next poll.
            wanted_tokens = {tok: sym for tok, sym in wanted_tokens.items() if tok not in to_add}

    with _subscribed_tokens_lock:
        _subscribed_tokens = wanted_tokens


def _check_tick(variant_id: str, variant_cfg: dict, trade: dict, settings: dict, now: datetime,
                account: GrowwAccount, symbol: str, ltp: float) -> None:
    tick_df = pd.DataFrame(
        {"Open": [ltp], "High": [ltp], "Low": [ltp], "Close": [ltp]},
        index=pd.DatetimeIndex([now]).tz_localize(IST) if now.tzinfo is None else pd.DatetimeIndex([now]),
    )
    try:
        result = locked_decide_and_exit(variant_id, variant_cfg, trade, settings, now, account, symbol, tick_df)
        if result.get("action") == "exit":
            logger.info(f"live_feed: [{variant_id}] tick-driven exit "
                        f"{result.get('reason')} @ {result.get('price')}")
    except Exception as e:
        logger.warning(f"live_feed: check failed for {variant_id}/{symbol}: {e}")


def _poll_once(account: GrowwAccount, feed) -> None:
    settings = config.load_settings()
    now = datetime.now(IST)
    open_trades = _all_open_trades()

    _sync_subscriptions(account, feed, open_trades)
    if not open_trades:
        return

    try:
        feed_data = feed.get_all_feed()
    except Exception as e:
        logger.warning(f"live_feed: get_all_feed failed: {e}")
        return
    ltp_by_token = feed_data.get("ltp", {}).get("NSE", {}).get("CASH", {})

    token_by_symbol = {sym: tok for tok, sym in _subscribed_tokens.items()}
    for variant_id, trade in open_trades.items():
        universe_key, variant_key = variant_id.split("/", 1)
        variant_cfg = config.VARIANTS_BY_KEY[variant_key]
        symbol = trade["symbol"]
        token = token_by_symbol.get(symbol)
        if token is None:
            continue
        tick = ltp_by_token.get(token)
        if not tick or not tick.get("ltp"):
            continue  # no trade on this symbol yet this poll — not an error, just quiet
        _check_tick(variant_id, variant_cfg, trade, settings, now, account, symbol, float(tick["ltp"]))


def _run_loop(poll_interval_seconds: float) -> None:
    global _feed
    account = _groww_account()
    if account is None:
        logger.info("live_feed: no Groww account configured, thread exiting "
                     "(2-min REST position-management job remains active)")
        return

    try:
        from growwapi import GrowwFeed  # lazy import — mirrors broker_accounts.py's pattern
        client = account.get_client()
        if client is None:
            logger.warning("live_feed: Groww client unavailable, thread exiting")
            return
        _feed = GrowwFeed(client)
    except Exception as e:
        logger.warning(f"live_feed: could not connect GrowwFeed: {e}")
        return

    logger.info("live_feed: connected, entering poll loop")
    while not _stop_event.is_set():
        try:
            _poll_once(account, _feed)
        except Exception as e:
            logger.warning(f"live_feed: poll loop iteration failed: {e}")
        _stop_event.wait(poll_interval_seconds)

    logger.info("live_feed: stopped")


def start(poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS) -> bool:
    """Idempotent — safe to call multiple times. Returns True if a thread is
    (now or already) running, False if no Groww account is configured (the
    2-min REST job remains the only path in that case, unaffected)."""
    global _thread
    if _groww_account() is None:
        logger.info("live_feed.start(): no Groww account configured, skipping")
        return False
    if _thread is not None and _thread.is_alive():
        return True
    _stop_event.clear()
    _thread = threading.Thread(target=_run_loop, args=(poll_interval_seconds,),
                                daemon=True, name="ute-live-feed")
    _thread.start()
    return True


def stop(timeout_seconds: float = 5.0) -> None:
    """Best-effort — if the thread is currently blocked inside GrowwFeed's own
    (non-cancellable) connection retry logic, it can't respond to the stop
    signal until that call returns; live-verified 2026-08-10, the initial
    connect alone can take several seconds. The thread is a daemon (see
    start()), so it can never outlive the process even if this timeout is hit
    — just log it so a stuck connect isn't silently confusing."""
    global _feed, _subscribed_tokens
    _stop_event.set()
    if _thread is not None:
        _thread.join(timeout=timeout_seconds)
        if _thread.is_alive():
            logger.warning("live_feed.stop(): thread still alive after timeout "
                            "(likely blocked in GrowwFeed's connect) — will stop on its own once unblocked")
    _feed = None
    with _subscribed_tokens_lock:
        _subscribed_tokens = {}


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()
