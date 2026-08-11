"""Config sanity + a synthetic SQLite round-trip for db.py — same verification
style the sibling bots used for their own Phase 0 builds (unit-level DB checks
before ever touching a live broker)."""

import os

import pytest

from engine import config


def test_exactly_8_variant_ids_with_new_naming():
    ids = config.all_variant_ids()
    assert len(ids) == 8
    assert len(set(ids)) == 8  # no duplicates
    for vid in ids:
        assert "/" in vid
        universe, variant = vid.split("/")
        assert universe in {"bot_300", "bot_400"}
        assert variant in {
            "subh30_trailing_ema", "subh30_trailing_atr",
            "puradin_trailing_ema", "puradin_trailing_atr",
        }
        # No sibling-bot-style vN/vN_M keys anywhere (see rules.md).
        assert not variant.startswith("v")
        assert "_fixed" not in variant


@pytest.fixture()
def sqlite_db(tmp_path, monkeypatch):
    """Points db.py at a throwaway SQLite file for this test only, and resets
    its cached engine so init_db() creates fresh tables there."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "common.helpers.PROJECT_ROOT", tmp_path, raising=False
    )
    import engine.db as db_module
    db_module._engine = None
    monkeypatch.setattr(db_module, "_sqlite_path", lambda: tmp_path / "test.db")
    db_module.init_db()
    yield db_module
    db_module._engine = None


def test_trade_open_close_round_trip(sqlite_db):
    db = sqlite_db
    variant_id = "bot_400/subh30_trailing_ema"

    trade_id = db.open_trade(
        variant_id=variant_id, symbol="RELIANCE", entry_price=2500.0, quantity=4,
        capital_used=10000.0, entry_charges=15.0, arm_cycle_id="2026-08-03T09:20:00", leverage=1.0,
    )
    open_trade = db.get_open_trade(variant_id)
    assert open_trade is not None
    assert open_trade["symbol"] == "RELIANCE"
    assert open_trade["status"] == "OPEN"

    db.mark_target_hit(variant_id, trade_id)
    reloaded = db.get_open_trade(variant_id)
    assert reloaded["target_hit"] == 1

    db.close_trade(variant_id, trade_id, exit_price=2600.0, exit_reason="EMA_TRAIL_EXIT", exit_charges=16.0)
    assert db.get_open_trade(variant_id) is None

    closed = db.get_closed_trades(variant_id)
    assert len(closed) == 1
    assert closed.iloc[0]["exit_reason"] == "EMA_TRAIL_EXIT"
    assert closed.iloc[0]["net_pnl"] == pytest.approx((2600 - 2500) * 4 - 15.0 - 16.0)


def test_get_setting_returns_default_when_unset(sqlite_db):
    """2026-08-11 live finding: admin (app.py) and viewer (viewer_app.py) are
    two SEPARATE Streamlit Cloud deployments with separate local filesystems
    — engine.config's settings.yaml can't be used to share a value between
    them, only the shared database (ute_settings) actually reaches both."""
    db = sqlite_db
    assert db.get_setting("public_variant", "") == ""
    assert db.get_setting("nonexistent_key") is None


def test_set_setting_then_get_returns_the_new_value(sqlite_db):
    db = sqlite_db
    db.set_setting("public_variant", "bot_400/puradin_trailing_ema")
    assert db.get_setting("public_variant") == "bot_400/puradin_trailing_ema"


def test_set_setting_overwrites_an_existing_value(sqlite_db):
    db = sqlite_db
    db.set_setting("public_variant", "bot_400/puradin_trailing_ema")
    db.set_setting("public_variant", "bot_400/subh30_trailing_atr")
    assert db.get_setting("public_variant") == "bot_400/subh30_trailing_atr"


def test_variant_isolation_capital_never_crosses(sqlite_db):
    db = sqlite_db
    v1 = "bot_400/subh30_trailing_ema"
    v2 = "bot_400/puradin_trailing_atr"

    tid = db.open_trade(v1, "TCS", 3000.0, 3, 9000.0, 10.0, None, 1.0)
    db.close_trade(v1, tid, 3300.0, "EMA_TRAIL_EXIT", 12.0)

    # v1 made a profit today; v2 (different variant, same universe-bot) must
    # still start from the plain default capital, unaffected by v1's P&L.
    assert db.get_starting_capital(v1, 10000) > 10000
    assert db.get_starting_capital(v2, 10000) == 10000


def test_checkpoint_tracking_is_per_variant(sqlite_db):
    db = sqlite_db
    v1 = "bot_400/subh30_trailing_ema"
    v2 = "bot_300/subh30_trailing_ema"

    db.mark_checkpoint_used(v1, "09:20", True, "INFY", None)
    assert db.get_checkpoints_used_today(v1) == {"09:20"}
    assert db.get_checkpoints_used_today(v2) == set()  # different universe-bot, untouched
