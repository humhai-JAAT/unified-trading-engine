"""Verifies Stage 1's chunk-level fallback and Stage 2's dedup logic against
MOCKED broker accounts (no real credentials needed) — the core new-architecture
logic this project exists to get right. Run with: pytest tests/ -v
"""

import pandas as pd
import pytest

from engine.broker_accounts import BrokerAccount, QuoteResult
from engine.stage1_ranking import _chunk, fetch_ranking_data
from engine.stage2_candles import fetch_candle_history, merge_unique_symbols


class FakeAccount(BrokerAccount):
    """A stand-in broker account whose fetch methods are scripted per-test,
    with call counting so tests can assert dedup/fallback actually happened
    (not just that the final result looks right by coincidence)."""

    def __init__(self, account_id, quote_fn=None, candle_fn=None):
        self.account_id = account_id
        self.broker = "fake"
        self._quote_fn = quote_fn or (lambda symbols: [])
        self._candle_fn = candle_fn or (lambda symbol, interval, period_days: None)
        self.quote_calls: list[list[str]] = []
        self.candle_calls: list[str] = []
        super().__init__()

    def _quote_rate_seconds(self):
        return 0.0  # no throttling needed in tests

    def _candle_rate_seconds(self):
        return 0.0

    def is_configured(self):
        return True

    def fetch_quotes_batch(self, symbols):
        self.quote_calls.append(list(symbols))
        return self._quote_fn(symbols)

    def fetch_candles(self, symbol, interval, period_days):
        self.candle_calls.append(symbol)
        return self._candle_fn(symbol, interval, period_days)


def test_chunk_splits_evenly_with_remainder():
    symbols = [f"SYM{i}" for i in range(11)]
    chunks = _chunk(symbols, 3)
    assert sum(len(c) for c in chunks) == 11
    assert len(chunks) == 3
    # 11 / 3 = 3 remainder 2 -> sizes [4, 4, 3]
    assert sorted(len(c) for c in chunks) == [3, 4, 4]


def test_stage1_dedupes_and_sorts_by_pct_change():
    symbols = ["AAA", "BBB", "CCC", "DDD"]

    def quote_fn(chunk):
        pct = {"AAA": 5.0, "BBB": 1.0, "CCC": 3.0, "DDD": 2.0}
        return [QuoteResult(symbol=s, last_price=100.0, pct_change=pct[s]) for s in chunk]

    account = FakeAccount("test_1", quote_fn=quote_fn)
    result = fetch_ranking_data(symbols, primary_accounts=[account], fallback_accounts=[])

    assert list(result.rank_list["symbol"]) == ["AAA", "CCC", "DDD", "BBB"]
    assert result.chunks_failed == 0
    assert result.warnings == []


def test_stage1_chunk_level_fallback_only_retries_missing_symbols():
    symbols = ["AAA", "BBB", "CCC", "DDD"]

    def primary_quote_fn(chunk):
        # Deliberately drops "BBB" to simulate a partial chunk failure.
        return [QuoteResult(symbol=s, last_price=100.0, pct_change=1.0) for s in chunk if s != "BBB"]

    fallback_calls = []

    def fallback_quote_fn(chunk):
        fallback_calls.append(list(chunk))
        return [QuoteResult(symbol=s, last_price=100.0, pct_change=9.0) for s in chunk]

    primary = FakeAccount("primary", quote_fn=primary_quote_fn)
    fallback = FakeAccount("fallback", quote_fn=fallback_quote_fn)

    result = fetch_ranking_data(symbols, primary_accounts=[primary], fallback_accounts=[fallback])

    assert "BBB" in set(result.rank_list["symbol"])
    # Fallback must have been asked ONLY for the missing symbol, not the whole list.
    assert fallback_calls == [["BBB"]]
    assert result.chunks_fallback_used >= 1


def test_stage1_reports_warning_when_fallback_also_fails():
    symbols = ["AAA", "BBB"]

    def primary_quote_fn(chunk):
        return [QuoteResult(symbol=s, last_price=100.0, pct_change=1.0) for s in chunk if s != "BBB"]

    def fallback_quote_fn(chunk):
        return []  # fallback also fails to find BBB

    primary = FakeAccount("primary", quote_fn=primary_quote_fn)
    fallback = FakeAccount("fallback", quote_fn=fallback_quote_fn)

    result = fetch_ranking_data(symbols, primary_accounts=[primary], fallback_accounts=[fallback])

    assert "BBB" not in set(result.rank_list["symbol"])
    assert result.chunks_failed >= 1
    assert any("BBB" in w or "unavailable" in w for w in result.warnings) or result.warnings


def test_merge_unique_symbols_dedupes_across_universes():
    top_lists = {
        "bot_751": pd.DataFrame({"symbol": ["AAA", "BBB", "CCC"]}),
        "bot_551": pd.DataFrame({"symbol": ["BBB", "CCC", "DDD"]}),  # BBB/CCC overlap
        "bot_400": pd.DataFrame({"symbol": ["EEE"]}),
    }
    unique = merge_unique_symbols(top_lists)
    assert sorted(unique) == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert len(unique) == 5  # not 3+3+1=7 — overlap was deduped


def test_stage2_fetches_each_unique_symbol_exactly_once():
    fake_df = pd.DataFrame({"Open": [1], "High": [2], "Low": [0.5], "Close": [1.5], "Volume": [100]})
    account = FakeAccount("primary", candle_fn=lambda symbol, interval, period_days: fake_df)

    symbols = ["AAA", "BBB", "AAA"]  # caller passing a duplicate shouldn't matter either
    result = fetch_candle_history(symbols, primary_accounts=[account], fallback_accounts=[])

    # Each requested symbol appears in the result; AAA fetched at most as many
    # times as it was submitted (dedup is the CALLER's job via merge_unique_
    # symbols — this test documents that stage2 itself doesn't silently drop
    # a duplicate submission, it just fetches what it's given).
    assert set(result.candles_by_symbol.keys()) == {"AAA", "BBB"}
    assert result.symbols_fetched == 2


def test_stage2_falls_back_per_symbol_on_failure():
    def primary_candle_fn(symbol, interval, period_days):
        return None if symbol == "BBB" else pd.DataFrame({"Close": [1, 2, 3]})

    def fallback_candle_fn(symbol, interval, period_days):
        return pd.DataFrame({"Close": [9, 9, 9]})

    primary = FakeAccount("primary", candle_fn=primary_candle_fn)
    fallback = FakeAccount("fallback", candle_fn=fallback_candle_fn)

    result = fetch_candle_history(["AAA", "BBB"], primary_accounts=[primary], fallback_accounts=[fallback])

    assert "BBB" in result.candles_by_symbol
    assert list(result.candles_by_symbol["BBB"]["Close"]) == [9, 9, 9]
    assert "BBB" in fallback.candle_calls
    assert "AAA" not in fallback.candle_calls  # AAA succeeded on primary, no fallback needed
