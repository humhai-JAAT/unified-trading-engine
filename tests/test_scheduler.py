"""Verifies engine/scheduler.py actually registers the two jobs correctly
with a REAL APScheduler instance (not just calling run_*_cycle() directly, as
every other test so far has done) — the other Phase 4 gap flagged in
Phase.md: the two-job split (fast position management + 5-min-boundary-
aligned entry scan) is a deliberate, load-bearing design decision (see
Architecture.md), so its actual wiring deserves its own test.
"""

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from engine import scheduler


def teardown_function():
    """APScheduler is a module-level singleton in scheduler.py — reset it
    after every test so tests don't leak scheduler state into each other.
    wait=True so any job that already fired (start_scheduler's
    next_run_time=now() means the position job can fire almost immediately)
    finishes before the test process moves on — wait=False here raced with
    pytest's own log-stream teardown and produced spurious 'I/O on closed
    file' noise from the background thread, harmless but distracting."""
    if scheduler._scheduler is not None and scheduler._scheduler.running:
        scheduler._scheduler.shutdown(wait=True)
    scheduler._scheduler = None


def test_start_scheduler_registers_both_jobs_with_correct_trigger_types():
    scheduler.start_scheduler()
    sched = scheduler.get_scheduler()

    position_job = sched.get_job(scheduler.POSITION_JOB_ID)
    scan_job = sched.get_job(scheduler.SCAN_JOB_ID)

    assert position_job is not None
    assert scan_job is not None
    # Position management is a plain N-minute interval (fast, frequent).
    assert isinstance(position_job.trigger, IntervalTrigger)
    # Entry scan is 5-min-boundary-aligned via cron, NOT a plain interval —
    # this is the specific design decision this test exists to protect
    # against silently regressing back to (see Architecture.md's scheduler
    # docstring: "Do NOT collapse these back into one job").
    assert isinstance(scan_job.trigger, CronTrigger)


def test_scan_job_fires_at_five_minute_boundary_plus_one_offset():
    scheduler.start_scheduler()
    sched = scheduler.get_scheduler()
    scan_job = sched.get_job(scheduler.SCAN_JOB_ID)

    trigger_str = str(scan_job.trigger)
    # CronTrigger's minute field should be the 1,6,11...56 offset schedule,
    # not a bare "*/5" (which would fire ON the boundary, before the 1-min
    # candle-close safety buffer this design relies on).
    for minute in ("1", "6", "11", "16", "21", "26", "31", "36", "41", "46", "51", "56"):
        assert minute in trigger_str, f"expected minute {minute} in scan trigger: {trigger_str}"
    assert "*/5" not in trigger_str


def test_restarting_scheduler_does_not_duplicate_jobs():
    scheduler.start_scheduler()
    scheduler.start_scheduler()  # simulates clicking "Start" twice, or a Streamlit rerun
    sched = scheduler.get_scheduler()

    assert len(sched.get_jobs()) == 2  # still exactly 2, not 4


def test_stop_scheduler_actually_stops_it():
    scheduler.start_scheduler()
    assert scheduler.is_running() is True
    scheduler.stop_scheduler()
    assert scheduler.is_running() is False


def test_is_awake_respects_wake_sleep_window():
    from datetime import datetime
    import pytz
    ist = pytz.timezone("Asia/Kolkata")
    settings = {"wake_time": "09:00", "sleep_time": "16:00"}

    assert scheduler.is_awake(settings, ist.localize(datetime(2026, 8, 3, 10, 0))) is True
    assert scheduler.is_awake(settings, ist.localize(datetime(2026, 8, 3, 8, 59))) is False
    assert scheduler.is_awake(settings, ist.localize(datetime(2026, 8, 3, 16, 0))) is False


def test_market_status_detects_weekend_and_hours():
    from datetime import datetime
    import pytz
    ist = pytz.timezone("Asia/Kolkata")

    saturday = ist.localize(datetime(2026, 8, 8, 10, 0))  # a Saturday
    assert scheduler.market_status(saturday) == "closed_weekend"

    before_open = ist.localize(datetime(2026, 8, 3, 9, 0))  # Monday, before 09:15
    assert scheduler.market_status(before_open) == "closed_hours"

    during_market = ist.localize(datetime(2026, 8, 3, 11, 0))
    assert scheduler.market_status(during_market) == "open"
