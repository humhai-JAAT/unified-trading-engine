"""Verifies the trailing-exit mechanisms (EMA9-close-below, ATR-pullback) and
the full manage_open_position flow: fixed-SL exit, target-hit -> trailing
flip, trailing exit with the hard floor, and square-off. Ported logic from
the sibling v2 bot — not previously unit-tested standalone in this project
(flagged as a Phase 4 gap in Phase.md).
"""

from datetime import datetime, time as dtime
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
import pytz

from engine.variant_engine import check_atr_trail_exit, check_ema9_trail_exit, manage_open_position

IST = pytz.timezone("Asia/Kolkata")


def _flat_5m(level=100.0, n=30):
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": level, "High": level, "Low": level, "Close": level}, index=idx)


def _dip_below_ema9_5m(n=30):
    """Flat at 100, then a sharp final-bar drop well below its own EMA9."""
    idx = pd.date_range("2026-08-03 09:15", periods=n, freq="5min", tz="Asia/Kolkata")
    closes = np.full(n, 100.0)
    closes[-1] = 90.0
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)


def test_check_ema9_trail_exit_none_when_above_ema9():
    df = _flat_5m()
    assert check_ema9_trail_exit(df) is None


def test_check_ema9_trail_exit_fires_on_close_below_ema9():
    df = _dip_below_ema9_5m()
    price = check_ema9_trail_exit(df)
    assert price == 90.0


def test_check_ema9_trail_exit_none_on_insufficient_data():
    assert check_ema9_trail_exit(_flat_5m(n=5)) is None
    assert check_ema9_trail_exit(None) is None
    assert check_ema9_trail_exit(pd.DataFrame()) is None


def test_check_atr_trail_exit_fires_beyond_multiplier():
    idx = pd.date_range("2026-08-03 09:15", periods=20, freq="5min", tz="Asia/Kolkata")
    closes = np.full(20, 100.0)
    df = pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1, "Close": closes}, index=idx)
    # ATR here is ~2 (High-Low range). Peak=110, last close=100 -> pullback=10,
    # which is well beyond atr_multiplier(1.5)*atr(~2)=3.
    price = check_atr_trail_exit(df, peak_price=110.0, atr_period=14, atr_multiplier=1.5)
    assert price == 100.0


def test_check_atr_trail_exit_none_when_pullback_small():
    idx = pd.date_range("2026-08-03 09:15", periods=20, freq="5min", tz="Asia/Kolkata")
    closes = np.full(20, 100.0)
    df = pd.DataFrame({"Open": closes, "High": closes + 1, "Low": closes - 1, "Close": closes}, index=idx)
    # Peak barely above current close -> pullback smaller than atr_multiplier*ATR.
    assert check_atr_trail_exit(df, peak_price=100.5, atr_period=14, atr_multiplier=1.5) is None


class FakeAccount:
    """Minimal stand-in for engine.broker_accounts.BrokerAccount, scripted to
    return whichever 1m/5m frame each test needs."""

    def __init__(self, df_1m, df_5m=None):
        self._df_1m = df_1m
        self._df_5m = df_5m

    def fetch_candles(self, symbol, interval, period_days):
        return self._df_1m if interval == "1m" else self._df_5m


def _open_trade(entry_price=100.0, quantity=10, capital_used=1000.0, target_hit=False, peak=None, trough=None):
    return {
        "id": 1, "symbol": "TESTSYM", "entry_price": entry_price, "quantity": quantity,
        "capital_used": capital_used, "entry_time": "2026-08-03T09:20:00+05:30",
        "peak_price": peak if peak is not None else entry_price,
        "trough_price": trough if trough is not None else entry_price,
        "target_hit": int(target_hit),
    }


def _settings(**overrides):
    base = {
        "profit_target_pct": 3.0, "stop_loss_pct": 1.5, "square_off_time": "15:15",
        "atr_period": 14, "atr_multiplier": 1.5,
    }
    base.update(overrides)
    return base


def _now(hh=10, mm=0):
    return IST.localize(datetime(2026, 8, 3, hh, mm))


@patch("engine.db.get_open_trade")
@patch("engine.broker.exit_position")
@patch("engine.broker.update_extremes")
@patch("engine.variant_engine._position_data_account")
def test_manage_open_position_exits_on_stop_loss(mock_account, mock_update_extremes, mock_exit,
                                                   mock_get_open_trade):
    # entry=100, capital_used=1000, SL=1.5% -> stop_price = 100 - (1000*1.5/100)/10 = 98.5
    idx = pd.date_range("2026-08-03 09:20", periods=5, freq="1min", tz="Asia/Kolkata")
    df_1m = pd.DataFrame({
        "Open": [100, 100, 99, 98.3, 98.6], "High": [100, 100, 99, 98.6, 98.8],
        "Low": [100, 100, 98.9, 98.0, 98.4],  # bar index 3 breaches stop_price (98.5)
        "Close": [100, 100, 99.0, 98.3, 98.6],
    }, index=idx)
    mock_account.return_value = FakeAccount(df_1m)
    mock_update_extremes.return_value = (100.0, 98.0)
    mock_exit.return_value = {"trade_id": 1, "exit_price": 98.5, "exit_reason": "STOP_LOSS", "exit_charges": 1.0}

    trade = _open_trade()
    mock_get_open_trade.return_value = trade
    result = manage_open_position("bot_751/subh30_trailing_ema",
                                   {"key": "subh30_trailing_ema", "exit_style": "ema"},
                                   trade, _settings(), _now())

    assert result["action"] == "exit"
    assert result["reason"] == "STOP_LOSS"
    mock_exit.assert_called_once()


@patch("engine.db.get_open_trade")
@patch("engine.db.mark_target_hit")
@patch("engine.broker.update_extremes")
@patch("engine.variant_engine._position_data_account")
def test_manage_open_position_flips_to_trailing_on_target_hit_but_no_trail_signal_yet(
    mock_account, mock_update_extremes, mock_mark_target_hit, mock_get_open_trade
):
    # target_price = 100 + (1000*3/100)/10 = 103. High reaches 103.5 -> target hit.
    idx_1m = pd.date_range("2026-08-03 09:20", periods=3, freq="1min", tz="Asia/Kolkata")
    df_1m = pd.DataFrame({
        "Open": [100, 102, 103], "High": [100, 102.5, 103.5], "Low": [100, 101.5, 102.8],
        "Close": [100, 102, 103.2],
    }, index=idx_1m)
    df_5m = _flat_5m(level=103.2)  # above its own EMA9 -> no EMA trail signal yet
    mock_account.return_value = FakeAccount(df_1m, df_5m)
    mock_update_extremes.return_value = (103.2, 100.0)

    trade = _open_trade()
    mock_get_open_trade.return_value = trade
    result = manage_open_position("bot_751/subh30_trailing_ema",
                                   {"key": "subh30_trailing_ema", "exit_style": "ema"},
                                   trade, _settings(), _now())

    assert result["action"] == "hold"
    assert result["target_hit"] is True
    mock_mark_target_hit.assert_called_once_with("bot_751/subh30_trailing_ema", 1)


@patch("engine.db.get_open_trade")
@patch("engine.broker.exit_position")
@patch("engine.broker.update_extremes")
@patch("engine.variant_engine._position_data_account")
def test_manage_open_position_trailing_exit_never_goes_below_target_hard_floor(
    mock_account, mock_update_extremes, mock_exit, mock_get_open_trade
):
    # Already past target (target_hit=True in the trade record). 1m data shows
    # no new SL/target breach this cycle; 5m data shows a fresh EMA9 trail-exit
    # signal at a price BELOW the original target -> hard floor must clamp it
    # up to the target price, never letting a winner give back below target.
    idx_1m = pd.date_range("2026-08-03 09:30", periods=2, freq="1min", tz="Asia/Kolkata")
    df_1m = pd.DataFrame({
        "Open": [104, 103.5], "High": [104, 103.6], "Low": [103.4, 103.3], "Close": [104, 103.5],
    }, index=idx_1m)
    df_5m = _dip_below_ema9_5m()  # last close = 90, well below target (103)
    mock_account.return_value = FakeAccount(df_1m, df_5m)
    mock_update_extremes.return_value = (110.0, 100.0)
    mock_exit.return_value = {"trade_id": 1, "exit_price": 103.0, "exit_reason": "EMA_TRAIL_EXIT", "exit_charges": 1.0}

    trade = _open_trade(target_hit=True, peak=110.0)
    mock_get_open_trade.return_value = trade
    manage_open_position("bot_751/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                          trade, _settings(), _now())

    # exit_position must have been called with a price >= the original target (103),
    # never with the raw trail price (90) that fell below it.
    call_args = mock_exit.call_args
    exit_price_used = call_args[0][3]  # exit_position(variant_id, trade_id, quantity, price, reason)
    assert exit_price_used >= 103.0


@patch("engine.db.get_open_trade")
@patch("engine.broker.exit_position")
@patch("engine.broker.update_extremes")
@patch("engine.variant_engine._position_data_account")
def test_manage_open_position_square_off_after_hours(mock_account, mock_update_extremes, mock_exit,
                                                       mock_get_open_trade):
    idx_1m = pd.date_range("2026-08-03 15:10", periods=2, freq="1min", tz="Asia/Kolkata")
    df_1m = pd.DataFrame({
        "Open": [101, 101], "High": [101.2, 101.2], "Low": [100.9, 100.9], "Close": [101, 101],
    }, index=idx_1m)
    mock_account.return_value = FakeAccount(df_1m, _flat_5m(level=101))
    mock_update_extremes.return_value = (101.2, 100.9)
    mock_exit.return_value = {"trade_id": 1, "exit_price": 101.0, "exit_reason": "SQUARE_OFF", "exit_charges": 1.0}

    trade = _open_trade()
    mock_get_open_trade.return_value = trade
    result = manage_open_position("bot_751/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                                   trade, _settings(square_off_time="15:15"), _now(15, 20))

    assert result["action"] == "exit"
    assert result["reason"] == "SQUARE_OFF"


@patch("engine.variant_engine._position_data_account", return_value=None)
def test_manage_open_position_holds_when_no_account_configured(mock_account):
    trade = _open_trade()
    result = manage_open_position("bot_751/subh30_trailing_ema", {"key": "subh30_trailing_ema", "exit_style": "ema"},
                                   trade, _settings(), _now())
    assert result == {"action": "hold", "reason": "no_broker_account_configured", "symbol": "TESTSYM"}
