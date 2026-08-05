"""Verifies Stage 1's per-universe filtering (no new API calls, just filtering
the shared rank_list) and the subset-safety warning when a universe symbol is
missing from that shared list (see Architecture.md's "Subset-safety check").
"""

from unittest.mock import patch

import pandas as pd

from engine.nse_universe import filter_to_universe


def _rank_list(rows):
    return pd.DataFrame(rows, columns=["symbol", "last_price", "pct_change"])


def test_filter_keeps_only_universe_members_and_takes_top_n():
    rank_list = _rank_list([
        ("AAA", 100, 5.0), ("BBB", 100, 4.0), ("CCC", 100, 3.0), ("ZZZ", 100, 99.0),
    ])
    with patch("engine.nse_universe.get_universe_symbols", return_value={"AAA", "BBB", "CCC"}):
        top, missing = filter_to_universe(rank_list, "n500_minus_100", top_n=2)

    # ZZZ is the highest %-change overall but NOT in this universe — must be excluded.
    assert "ZZZ" not in set(top["symbol"])
    assert list(top["symbol"]) == ["AAA", "BBB"]  # top 2 of the 3 universe members
    assert missing == []


def test_filter_reports_missing_symbols_not_silently():
    rank_list = _rank_list([("AAA", 100, 5.0)])
    # Universe expects AAA and BBB, but BBB never made it into the shared rank_list —
    # simulates the index-CSV cache-timing mismatch scenario from Architecture.md.
    with patch("engine.nse_universe.get_universe_symbols", return_value={"AAA", "BBB"}):
        top, missing = filter_to_universe(rank_list, "total_market_minus_200", top_n=50)

    assert missing == ["BBB"]
    assert list(top["symbol"]) == ["AAA"]  # doesn't crash, just proceeds with what it has


def test_filter_handles_empty_rank_list():
    with patch("engine.nse_universe.get_universe_symbols", return_value={"AAA", "BBB"}):
        top, missing = filter_to_universe(_rank_list([]), "total_market", top_n=50)

    assert top.empty
    assert sorted(missing) == ["AAA", "BBB"]
