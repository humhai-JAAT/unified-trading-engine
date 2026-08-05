"""Verifies AccountRateLimiter is genuinely thread-safe under real concurrent
load — the exact bug class this project's design exists to avoid (see
Architecture.md's "Thread-safety design"). Run with: pytest tests/ -v
"""

import threading
import time

from engine.rate_limiter import AccountRateLimiter


def test_single_thread_respects_interval():
    limiter = AccountRateLimiter(min_interval_seconds=0.1)
    start = time.time()
    for _ in range(3):
        limiter.wait_for_turn()
    elapsed = time.time() - start
    # 3 calls, first is immediate, next 2 each wait ~0.1s => >= 0.2s total
    assert elapsed >= 0.19, f"expected >= ~0.2s of spacing, got {elapsed:.3f}s"


def test_concurrent_threads_never_violate_min_interval():
    """The actual race-condition test: many threads hammering wait_for_turn()
    at once must still never produce two 'calls' closer together than
    min_interval — if the lock weren't held for the whole check-sleep-update
    sequence, this would fail intermittently."""
    limiter = AccountRateLimiter(min_interval_seconds=0.05)
    call_times: list[float] = []
    lock = threading.Lock()

    def worker():
        limiter.wait_for_turn()
        with lock:
            call_times.append(time.time())

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    call_times.sort()
    gaps = [b - a for a, b in zip(call_times, call_times[1:])]
    assert len(call_times) == 10
    # Allow a tiny scheduling-jitter tolerance below the nominal interval.
    assert all(gap >= 0.045 for gap in gaps), f"found a gap below min_interval: {gaps}"


def test_two_independent_limiters_run_in_parallel():
    """Two AccountRateLimiter instances (simulating 2 different broker accounts)
    must NOT block each other — this is what makes cross-account parallelism
    real instead of accidentally serialized through shared state."""
    limiter_a = AccountRateLimiter(min_interval_seconds=0.2)
    limiter_b = AccountRateLimiter(min_interval_seconds=0.2)

    results = {}

    def worker(name, limiter, n_calls):
        start = time.time()
        for _ in range(n_calls):
            limiter.wait_for_turn()
        results[name] = time.time() - start

    t_a = threading.Thread(target=worker, args=("a", limiter_a, 3))
    t_b = threading.Thread(target=worker, args=("b", limiter_b, 3))
    overall_start = time.time()
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()
    overall_elapsed = time.time() - overall_start

    # Each account's own 3 calls take ~0.4s (3 calls, 2 gaps of 0.2s) — if the
    # two accounts were accidentally sharing state, overall would be ~0.8s+.
    assert overall_elapsed < 0.7, f"accounts appear to be blocking each other: {overall_elapsed:.3f}s"
