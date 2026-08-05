"""APScheduler wiring — TWO separate jobs, not one shared cycle like the
sibling bots:

  1. Position management (fast, every position_management_interval_minutes,
     default 2) — manages any of the 12 variants' open positions. Lightweight
     (at most 12 symbols), so stays fast and frequent regardless of how heavy
     entry-scanning is.
  2. Entry scanning (Stage 1 + Stage 2 + all 12 variants' scan_for_entry),
     aligned to 5-min candle boundaries with a 1-min safety offset (e.g.
     09:21, 09:26, 09:31...) instead of a plain N-minute interval — because
     the underlying candle data only changes every 5 minutes regardless, so
     scanning faster than that just re-checks stale data (see this project's
     own design discussion, and the sibling v2 bot's unfixed 12-minute-cycle
     bug that motivated this whole project).

This split was an explicit design decision — see Architecture.md's Stage 1/2
sections and this project's memory.md. Do NOT collapse these back into one job.
"""

from datetime import datetime, time as dtime

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from common.helpers import get_logger
from engine import config, db, nse_universe, stage1_ranking, stage2_candles, variant_engine
from engine.broker_accounts import get_configured_accounts

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

_scheduler: BackgroundScheduler | None = None
POSITION_JOB_ID = "ute_position_management"
SCAN_JOB_ID = "ute_entry_scan"

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def _parse_time(hhmm: str) -> dtime:
    h, m = map(int, hhmm.split(":"))
    return dtime(h, m)


def _now_ist() -> datetime:
    return datetime.now(IST)


def market_status(now: datetime | None = None) -> str:
    now = now or _now_ist()
    if now.weekday() >= 5:
        return "closed_weekend"
    if now.time() < MARKET_OPEN or now.time() >= MARKET_CLOSE:
        return "closed_hours"
    return "open"


def is_awake(settings: dict, now: datetime) -> bool:
    wake = _parse_time(settings.get("wake_time", "09:00"))
    sleep = _parse_time(settings.get("sleep_time", "16:00"))
    return wake <= now.time() < sleep


def run_position_management_cycle(settings: dict | None = None) -> dict:
    """Manages every one of the 12 variants' open positions, if any. Runs
    independently of, and more frequently than, the entry-scan cycle."""
    settings = settings or config.load_settings()
    db.init_db()
    now = _now_ist()

    if market_status(now) != "open":
        return {"status": market_status(now)}

    results = {}
    for universe_bot in config.UNIVERSE_BOTS:
        for variant_cfg in config.VARIANTS:
            variant_id = f"{universe_bot['key']}/{variant_cfg['key']}"
            trade = db.get_open_trade(variant_id)
            if trade:
                results[variant_id] = variant_engine.manage_open_position(
                    variant_id, variant_cfg, trade, settings, now
                )

    db.log_cycle(status="OK", stage="position_management",
                  message=f"managed={len([r for r in results.values() if r.get('action') != 'hold'])}")
    return {"status": "open", "managed": results}


def run_full_scan_cycle(settings: dict | None = None) -> dict:
    """Stage 1 (shared ranking fetch) -> per-universe filter -> Stage 2
    (shared, deduped candle fetch) -> all 12 variants' scan_for_entry. See
    Architecture.md for the full pipeline this implements."""
    settings = settings or config.load_settings()
    db.init_db()
    db.prune_cycle_logs(retention_days=7)
    now = _now_ist()

    status = market_status(now)
    if status != "open":
        db.log_cycle(status="MARKET_CLOSED", stage="entry_scan", message=f"market_status={status}")
        return {"status": status}

    # Stage 1 — one shared fetch for bot_751's full universe.
    all_symbols = list(nse_universe.get_universe_symbols("total_market"))
    stage1_result = stage1_ranking.fetch_ranking_data(all_symbols)
    if stage1_result.warnings:
        logger.warning(f"Stage 1 warnings: {stage1_result.warnings}")

    # Per-universe filter — no new API calls, just filtering the shared list.
    top_lists: dict[str, "pd.DataFrame"] = {}
    subset_warnings: list[str] = []
    for universe_bot in config.UNIVERSE_BOTS:
        top_df, missing = nse_universe.filter_to_universe(
            stage1_result.rank_list, universe_bot["universe"], settings["gainers_pool_size"]
        )
        top_lists[universe_bot["key"]] = top_df
        if missing:
            subset_warnings.append(
                f"{universe_bot['key']}: {len(missing)} constituent symbol(s) missing from "
                f"Stage 1's rank list (likely index-CSV cache timing mismatch): {missing[:10]}"
            )
    if subset_warnings:
        logger.warning(f"Subset-safety warnings: {subset_warnings}")

    # Stage 2 — merge/dedup all 3 universes' top-N lists, fetch candles once each.
    unique_symbols = stage2_candles.merge_unique_symbols(top_lists)
    stage2_result = stage2_candles.fetch_candle_history(unique_symbols)
    if stage2_result.warnings:
        logger.warning(f"Stage 2 warnings: {stage2_result.warnings}")

    # All 12 variants scan against the shared data — was_flat determined fresh
    # per variant right before its own scan (each variant is independent).
    scan_results = {}
    for universe_bot in config.UNIVERSE_BOTS:
        for variant_cfg in config.VARIANTS:
            variant_id = f"{universe_bot['key']}/{variant_cfg['key']}"
            was_flat = db.get_open_trade(variant_id) is None
            scan_results[variant_id] = variant_engine.scan_for_entry(
                universe_bot["key"], variant_cfg, settings, now,
                top_lists[universe_bot["key"]], stage2_result.candles_by_symbol, was_flat,
            )

    all_warnings = stage1_result.warnings + subset_warnings + stage2_result.warnings
    entered = [v for v, r in scan_results.items() if r.get("action") == "enter"]
    db.log_cycle(
        status="OK", stage="entry_scan",
        symbols_scanned=stage2_result.symbols_fetched,
        message=f"entered={entered}",
        warnings="; ".join(all_warnings) if all_warnings else "",
    )
    return {
        "status": "open", "stage1_chunks_total": stage1_result.chunks_total,
        "stage1_chunks_fallback_used": stage1_result.chunks_fallback_used,
        "stage1_chunks_failed": stage1_result.chunks_failed,
        "stage2_symbols_requested": stage2_result.symbols_requested,
        "stage2_symbols_fetched": stage2_result.symbols_fetched,
        "warnings": all_warnings, "scan": scan_results, "entered_variants": entered,
    }


def _position_job():
    settings = config.load_settings()
    now = _now_ist()
    if not is_awake(settings, now):
        return
    try:
        run_position_management_cycle(settings)
    except Exception as e:
        logger.error(f"Position management cycle failed: {e}")


def _scan_job():
    settings = config.load_settings()
    now = _now_ist()
    if not is_awake(settings, now):
        return
    try:
        result = run_full_scan_cycle(settings)
        logger.info(f"Entry scan cycle: {result.get('status')}, entered={result.get('entered_variants')}")
    except Exception as e:
        logger.error(f"Entry scan cycle failed: {e}")


def get_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    return _scheduler


def start_scheduler() -> None:
    settings = config.load_settings()
    scheduler = get_scheduler()

    for job_id in (POSITION_JOB_ID, SCAN_JOB_ID):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    position_minutes = settings["position_management_interval_minutes"]
    scheduler.add_job(
        _position_job,
        trigger=IntervalTrigger(minutes=position_minutes, timezone="Asia/Kolkata"),
        id=POSITION_JOB_ID, replace_existing=True,
        next_run_time=datetime.now(IST),
    )

    # 5-min-boundary-aligned, +1 min offset: fires at :01, :06, :11, ... :56 —
    # matches the candle-close cadence instead of an arbitrary N-minute poll.
    scheduler.add_job(
        _scan_job,
        trigger=CronTrigger(minute="1,6,11,16,21,26,31,36,41,46,51,56", timezone="Asia/Kolkata"),
        id=SCAN_JOB_ID, replace_existing=True,
    )

    if not scheduler.running:
        scheduler.start()
    logger.info(f"Scheduler started: position management every {position_minutes} min, "
                f"entry scan at 5-min-boundary+1 offsets")


def stop_scheduler() -> None:
    scheduler = get_scheduler()
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")


def is_running() -> bool:
    return get_scheduler().running
