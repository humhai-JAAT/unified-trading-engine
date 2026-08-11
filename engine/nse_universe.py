"""Index-constituent lists for the 2 universe-bots (see config.UNIVERSE_BOTS).

Ported from the sibling bots' nse_universe.py — the actual gainers-RANKING logic
(fetching %-change and picking top-N) is NOT here in this project; that's
engine/stage1_ranking.py's job, fetched once and shared across both universes.
This module only answers "which symbols belong to universe X" — a cheap,
CSV-based lookup, independent of live market data.

'bot_300' is constructed as, and must always remain, a SUBSET of 'bot_400'
(both defined relative to Nifty500, not Total Market — that universe was
dropped 2026-08-11 to cut Stage 1's fetch from 751 to 400 symbols) — this is a
structural requirement of Stage 1's design (see Architecture.md), not just
documentation. If this ever needs a 3rd universe whose relationship to
bot_400 isn't a clean subset, Stage 1 needs to change too.
"""

import time
from pathlib import Path

import pandas as pd
import requests

from common.helpers import PROJECT_ROOT, get_logger

logger = get_logger(__name__)

_INDEX_URLS = {
    "nifty100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "nifty200": "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "nifty500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

INDEX_CACHE_TTL_DAYS = 7


def _fetch_index_symbols(index_key: str, force_refresh: bool = False) -> list[str]:
    """Returns the constituent symbols of one named index (see _INDEX_URLS),
    cached to disk per index, refreshed every INDEX_CACHE_TTL_DAYS days.

    NOTE (subset-safety, see Architecture.md): each of the 3 indices caches and
    refreshes INDEPENDENTLY of the others. Around a rare NSE index-rebalance
    day, this could momentarily mean e.g. nifty500's cache reflects a newer
    rebalance than nifty200's does — see get_universe_symbols's subset-safety
    warning below for how this is handled defensively."""
    url = _INDEX_URLS[index_key]
    cache_path = PROJECT_ROOT / "data" / f"{index_key}_list.csv"
    if not force_refresh and cache_path.exists():
        age_days = (time.time() - cache_path.stat().st_mtime) / 86400
        if age_days < INDEX_CACHE_TTL_DAYS:
            return pd.read_csv(cache_path)["Symbol"].tolist()

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
        return pd.read_csv(cache_path)["Symbol"].tolist()
    except Exception as e:
        logger.warning(f"{index_key} list fetch failed ({e}), using cached copy if any")
        if cache_path.exists():
            return pd.read_csv(cache_path)["Symbol"].tolist()
        raise


def get_universe_symbols(universe: str, force_refresh: bool = False) -> set[str]:
    """Resolves one of the 2 configured universes to its symbol set. Returns a
    set (not a list) since Stage 1 only ever needs membership tests, and
    set-difference is how bot_300/bot_400 are constructed in the first place."""
    if universe == "n500_minus_200":
        n500 = set(_fetch_index_symbols("nifty500", force_refresh))
        n200 = set(_fetch_index_symbols("nifty200", force_refresh))
        return n500 - n200
    if universe == "n500_minus_100":
        n500 = set(_fetch_index_symbols("nifty500", force_refresh))
        n100 = set(_fetch_index_symbols("nifty100", force_refresh))
        return n500 - n100
    raise ValueError(f"Unknown universe: {universe!r}")


def filter_to_universe(rank_list: pd.DataFrame, universe: str, top_n: int,
                        force_refresh: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Stage 1's per-universe filter step: given the SHARED, already-fetched
    rank_list (columns: symbol, last_price, pct_change — see stage1_ranking.py),
    keep only rows whose symbol belongs to `universe`, then take the top_n by
    pct_change. No network call here — this is why both universes only need ONE
    upstream fetch (see Architecture.md's Stage 1).

    Returns (top_n_dataframe, missing_symbols) — missing_symbols is the
    subset-safety check: universe symbols that SHOULD be in rank_list (by the
    subset construction above) but weren't found in it, most likely because the
    3 underlying index CSVs refreshed at slightly different times around an NSE
    rebalance. Callers must surface a warning if this is non-empty — see
    Architecture.md's "Error visibility" section — never silently ignore it."""
    universe_symbols = get_universe_symbols(universe, force_refresh)
    present = set(rank_list["symbol"]) if not rank_list.empty else set()
    missing = sorted(universe_symbols - present)

    filtered = rank_list[rank_list["symbol"].isin(universe_symbols)]
    top = filtered.sort_values("pct_change", ascending=False).head(top_n).reset_index(drop=True)
    return top, missing
