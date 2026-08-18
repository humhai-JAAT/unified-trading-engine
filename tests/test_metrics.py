"""Regression tests for the Profit-Factor/SQN/Wilson-win-rate scoring added
2026-08-18 to close the evaluation-framework gap flagged in memory.md's
2026-08-13 tangent (proposed then, never built until now)."""

import pandas as pd
import pytest

from common.metrics import sqn, wilson_lower_bound_win_rate
from engine import metrics


def test_sqn_needs_at_least_two_trades():
    assert sqn(pd.Series([100.0])) == 0.0
    assert sqn(pd.Series([])) == 0.0


def test_sqn_zero_variance_does_not_divide_by_zero():
    assert sqn(pd.Series([100.0, 100.0, 100.0])) == 0.0


def test_sqn_positive_for_a_consistent_edge():
    # Small, steady wins beat one huge win + one huge loss on SQN even though
    # both series can have the same total P&L — that's the whole point of
    # using SQN instead of raw P&L to rank variants.
    steady = pd.Series([50.0, 60.0, 55.0, 45.0, 52.0])
    erratic = pd.Series([500.0, -450.0, 500.0, -450.0, 162.0])
    assert steady.sum() == pytest.approx(erratic.sum())
    assert sqn(steady) > sqn(erratic)


def test_wilson_lower_bound_discounts_small_samples():
    # 3 wins out of 3 reads as literal 100% from the naive win_rate(), but a
    # 3-trade sample shouldn't be trusted that far — Wilson pulls it down hard.
    three_wins = pd.Series([100.0, 100.0, 100.0])
    lb = wilson_lower_bound_win_rate(three_wins)
    assert 30.0 < lb < 55.0

    # Same 100% win rate over 100 trades should be trusted much more.
    hundred_wins = pd.Series([100.0] * 100)
    assert wilson_lower_bound_win_rate(hundred_wins) > 95.0


def test_wilson_lower_bound_empty_series():
    assert wilson_lower_bound_win_rate(pd.Series([])) == 0.0


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("common.helpers.PROJECT_ROOT", tmp_path, raising=False)
    import engine.db as db_module
    db_module._engine = None
    monkeypatch.setattr(db_module, "_sqlite_path", lambda: tmp_path / "test.db")
    db_module.init_db()
    yield db_module
    db_module._engine = None


def test_get_variant_rankings_covers_all_8_even_with_no_trades(sqlite_db):
    settings = {"starting_capital": 10000}
    df = metrics.get_variant_rankings(settings)
    assert len(df) == 8
    assert set(df["variant_id"]) == set(
        __import__("engine.config", fromlist=["config"]).all_variant_ids()
    )
    assert (df["total_trades"] == 0).all()


def test_get_variant_rankings_ranks_a_profitable_variant_above_an_untraded_one(sqlite_db):
    db = sqlite_db
    winning_variant = "bot_400/subh30_trailing_ema"

    for entry, exit_ in [(100.0, 110.0), (100.0, 108.0), (100.0, 112.0)]:
        trade_id = db.open_trade(
            variant_id=winning_variant, symbol="RELIANCE", entry_price=entry, quantity=10,
            capital_used=1000.0, entry_charges=1.0, arm_cycle_id=None, leverage=1.0,
        )
        db.close_trade(winning_variant, trade_id, exit_price=exit_, exit_reason="TARGET", exit_charges=1.0)

    df = metrics.get_variant_rankings({"starting_capital": 10000})
    assert len(df) == 8

    winner_row = df[df["variant_id"] == winning_variant].iloc[0]
    assert winner_row["total_trades"] == 3
    assert winner_row["profit_factor"] == float("inf")  # all 3 trades won, zero gross loss
    assert winner_row["sqn"] > 0

    # SQN-descending sort: the winning variant should rank above every
    # untraded variant (sqn=0.0 for those), i.e. it's row 0.
    assert df.iloc[0]["variant_id"] == winning_variant
