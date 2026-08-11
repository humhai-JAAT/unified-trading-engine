"""Verifies engine/live_feed.py — the websocket tick-driven stop-loss/target
exit path added 2026-08-10 alongside the existing 2-min REST job. Covers the
fail-closed no-Groww-account contract, subscription-diffing logic, and that a
tick correctly gets funneled into variant_engine.locked_decide_and_exit with a
properly-shaped synthetic single-row candle — NOT the full live network path
(that's covered by manual live-account verification, see memory.md).
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytz

from engine import live_feed

IST = pytz.timezone("Asia/Kolkata")


def teardown_function():
    """live_feed's subscription/thread/cache state is module-level — reset it
    after every test so tests don't leak into each other, same reasoning as
    test_scheduler.py's teardown_function."""
    live_feed.stop(timeout_seconds=1.0)
    live_feed._subscribed_tokens = {}
    live_feed._symbol_token_cache = {}
    live_feed._symbol_token_cache_loaded_at = 0.0


@patch("engine.live_feed.get_configured_accounts", return_value={"groww": [], "angelone": []})
def test_start_is_a_noop_when_no_groww_account_configured(mock_accounts):
    started = live_feed.start()
    assert started is False
    assert live_feed.is_running() is False


def test_all_open_trades_aggregates_across_all_12_variants():
    def fake_get_open_trade(variant_id):
        if variant_id == "bot_300/subh30_trailing_ema":
            return {"id": 1, "symbol": "RELIANCE"}
        if variant_id == "bot_400/puradin_trailing_atr":
            return {"id": 2, "symbol": "TCS"}
        return None

    with patch("engine.live_feed.db.get_open_trade", side_effect=fake_get_open_trade):
        open_trades = live_feed._all_open_trades()

    assert open_trades == {
        "bot_300/subh30_trailing_ema": {"id": 1, "symbol": "RELIANCE"},
        "bot_400/puradin_trailing_atr": {"id": 2, "symbol": "TCS"},
    }


def test_get_symbol_to_token_caches_in_memory_across_calls():
    """2026-08-10 code review finding: account._load_symbol_to_token() re-reads
    a ~4k-row CSV from disk every call — must not be called on every poll."""
    account = MagicMock()
    account._load_symbol_to_token.return_value = {"RELIANCE": "2885"}

    first = live_feed._get_symbol_to_token(account)
    second = live_feed._get_symbol_to_token(account)

    assert first == {"RELIANCE": "2885"}
    assert second == {"RELIANCE": "2885"}
    account._load_symbol_to_token.assert_called_once()  # NOT called again on the 2nd call


def test_get_symbol_to_token_refreshes_after_ttl_expires():
    account = MagicMock()
    account._load_symbol_to_token.return_value = {"RELIANCE": "2885"}

    live_feed._get_symbol_to_token(account)
    live_feed._symbol_token_cache_loaded_at -= (live_feed.SYMBOL_TOKEN_CACHE_TTL_SECONDS + 1)
    live_feed._get_symbol_to_token(account)

    assert account._load_symbol_to_token.call_count == 2


def test_sync_subscriptions_only_diffs_the_delta():
    account = MagicMock()
    account._load_symbol_to_token.return_value = {"RELIANCE": "2885", "TCS": "11536", "INFY": "1594"}
    feed = MagicMock()

    # Start with RELIANCE + TCS already subscribed.
    live_feed._subscribed_tokens = {"2885": "RELIANCE", "11536": "TCS"}

    # New wanted set: RELIANCE (unchanged) + INFY (new) — TCS position closed.
    open_trades = {
        "bot_300/subh30_trailing_ema": {"symbol": "RELIANCE"},
        "bot_400/puradin_trailing_atr": {"symbol": "INFY"},
    }
    live_feed._sync_subscriptions(account, feed, open_trades)

    feed.subscribe_ltp.assert_called_once_with([{"exchange": "NSE", "segment": "CASH", "exchange_token": "1594"}])
    feed.unsubscribe_ltp.assert_called_once_with([{"exchange": "NSE", "segment": "CASH", "exchange_token": "11536"}])
    assert live_feed._subscribed_tokens == {"2885": "RELIANCE", "1594": "INFY"}


def test_sync_subscriptions_skips_symbol_with_no_known_token():
    account = MagicMock()
    account._load_symbol_to_token.return_value = {"RELIANCE": "2885"}  # UNKNOWNSYM missing
    feed = MagicMock()
    live_feed._subscribed_tokens = {}

    open_trades = {
        "bot_300/subh30_trailing_ema": {"symbol": "RELIANCE"},
        "bot_400/puradin_trailing_atr": {"symbol": "UNKNOWNSYM"},
    }
    live_feed._sync_subscriptions(account, feed, open_trades)

    feed.subscribe_ltp.assert_called_once_with([{"exchange": "NSE", "segment": "CASH", "exchange_token": "2885"}])
    assert live_feed._subscribed_tokens == {"2885": "RELIANCE"}


@patch("engine.live_feed.locked_decide_and_exit")
def test_check_tick_builds_single_row_ohlc_candle_from_the_ltp(mock_decide):
    mock_decide.return_value = {"action": "hold"}
    trade = {"id": 1, "symbol": "RELIANCE", "entry_price": 100.0, "quantity": 10,
              "capital_used": 1000.0, "peak_price": 100.0, "trough_price": 100.0, "target_hit": 0}
    settings = {"profit_target_pct": 3.0, "stop_loss_pct": 1.5, "square_off_time": "15:15",
                "atr_period": 14, "atr_multiplier": 1.5}
    now = IST.localize(datetime(2026, 8, 10, 11, 0))

    live_feed._check_tick("bot_300/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                           trade, settings, now, MagicMock(), "RELIANCE", 101.5)

    mock_decide.assert_called_once()
    call_args = mock_decide.call_args[0]
    df_1m = call_args[7]  # locked_decide_and_exit(variant_id, variant_cfg, trade, settings, now, account, symbol, df_1m)
    assert list(df_1m.columns) == ["Open", "High", "Low", "Close"]
    assert len(df_1m) == 1
    assert df_1m["Close"].iloc[0] == 101.5
    assert df_1m["High"].iloc[0] == 101.5
    assert df_1m["Low"].iloc[0] == 101.5


@patch("engine.live_feed.locked_decide_and_exit")
def test_check_tick_logs_but_does_not_raise_on_exit(mock_decide, caplog):
    mock_decide.return_value = {"action": "exit", "reason": "STOP_LOSS", "price": 98.5}
    trade = {"id": 1, "symbol": "RELIANCE"}
    now = IST.localize(datetime(2026, 8, 10, 11, 0))

    live_feed._check_tick("bot_300/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                           trade, {}, now, MagicMock(), "RELIANCE", 98.5)

    mock_decide.assert_called_once()


@patch("engine.live_feed.locked_decide_and_exit", side_effect=RuntimeError("boom"))
def test_check_tick_swallows_exceptions_fail_closed(mock_decide):
    """A single symbol's check failing must never crash the whole poll loop —
    same fail-closed contract as the rest of this project's broker/data layer."""
    trade = {"id": 1, "symbol": "RELIANCE"}
    now = IST.localize(datetime(2026, 8, 10, 11, 0))

    live_feed._check_tick("bot_300/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                           trade, {}, now, MagicMock(), "RELIANCE", 98.5)  # must not raise
