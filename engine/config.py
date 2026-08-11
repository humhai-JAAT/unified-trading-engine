"""Load/save this bot's tunable settings, and define the core UNIVERSE_BOTS /
VARIANTS structure — see Architecture.md and PRD.md for the full design.

New naming convention (breaking change from the sibling bots' `v1`/`v2_3_2`-style
keys) — never reintroduce those here. Every variant is identified by
`{universe_bot}/{variant_key}`, e.g. `bot_400/subh30_trailing_ema`.
"""

import yaml

from common.helpers import PROJECT_ROOT

SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"

# 2 universe-bots, both defined relative to Nifty500 (not Total Market — that
# universe was dropped 2026-08-11 to cut Stage 1's fetch size). `bot_300` is
# ALWAYS a subset of `bot_400`'s universe (see
# engine.nse_universe.get_universe_symbols: Nifty500-Nifty200 ⊂
# Nifty500-Nifty100) — this is what makes Stage 1's shared-ranking-fetch valid
# (fetch bot_400's 400 symbols once, bot_300 filters its 300 from that same
# list). Don't add a 3rd universe-bot whose symbol set isn't provably a subset
# of bot_400's without revisiting Stage 1's design (see Architecture.md).
UNIVERSE_BOTS = [
    {"key": "bot_300", "label": "Bot 300 - Nifty500 minus Nifty200", "universe": "n500_minus_200"},
    {"key": "bot_400", "label": "Bot 400 - Nifty500 minus Nifty100", "universe": "n500_minus_100"},
]
UNIVERSE_BOTS_BY_KEY = {b["key"]: b for b in UNIVERSE_BOTS}

# 4 variants per universe-bot: entry-timing (subh30 / puradin) x trailing-exit
# mechanism (ema / atr). There is NO standalone "fixed exit" variant — every
# variant's target is a fixed %, but reaching it flips the position into
# trailing mode instead of exiting immediately (same as the sibling v2 bot).
# See PRD.md's "Core structure" section for why this is 4, not 6.
VARIANTS = [
    {"key": "subh30_trailing_ema", "entry_timing": "subh30", "exit_style": "ema"},
    {"key": "subh30_trailing_atr", "entry_timing": "subh30", "exit_style": "atr"},
    {"key": "puradin_trailing_ema", "entry_timing": "puradin", "exit_style": "ema"},
    {"key": "puradin_trailing_atr", "entry_timing": "puradin", "exit_style": "atr"},
]
VARIANTS_BY_KEY = {v["key"]: v for v in VARIANTS}


def all_variant_ids() -> list[str]:
    """Every `{universe_bot}/{variant_key}` combination — 2 x 4 = 8."""
    return [f"{b['key']}/{v['key']}" for b in UNIVERSE_BOTS for v in VARIANTS]


# "Subh 30 min" checkpoint schedule — resolved with the user 2026-08-02: same
# 3 clock-times as the sibling v1 bot's original design, but each is evaluated
# 1 minute after its own 5-min candle closes (not exactly on the checkpoint
# minute), to avoid acting on a still-forming candle.
SUBH30_CHECKPOINTS = ["09:20", "09:25", "09:30"]
SUBH30_CHECKPOINT_GRACE_MINUTES = 10  # a missed checkpoint is skipped, not fired late

DEFAULTS = {
    "starting_capital": 10000,       # per variant (8 independent pools)
    "leverage_multiplier": 1.0,
    "profit_target_pct": 3.0,        # trailing-activation trigger, not a final exit
    "stop_loss_pct": 1.5,
    "wake_time": "09:00",
    "sleep_time": "16:00",
    "square_off_time": "15:15",
    "gainers_pool_size": 50,         # per universe-bot, after Stage 1 filtering
    "stage1_scan_interval_minutes": 5,   # aligned to 5-min candle boundaries (Stage 1 only)
    "position_management_interval_minutes": 2,  # open-position monitoring stays fast
    "atr_period": 14,
    "atr_multiplier": 1.5,
    "candle_cache_ttl_seconds": 60,  # deprecated in this design — Stage 2 dedupes
                                      # per-cycle instead of via a time-based cache,
                                      # kept only for any code that still wants a TTL
    "public_variant": "",  # "" = no variant shown on the read-only viewer app yet
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return {**DEFAULTS, **cfg}


def save_settings(settings: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(settings, f, sort_keys=False, default_flow_style=False)
