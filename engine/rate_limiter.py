"""Per-account thread-safe rate limiting — see Architecture.md's "Thread-safety
design" section for the full rationale.

The sibling bots' Angel One rate-limiter (`_last_candle_call_at` in their
angelone_client.py) is a plain module-level dict, read-then-sleep-then-written
with NO lock — safe there only because those bots never called it from multiple
threads at once. This project's whole design *requires* multiple worker threads
sharing one broker account concurrently (e.g. 3 Stage 1 workers on one Groww
account), so that unlocked pattern would be a real race condition here: two
threads could both read "elapsed time is enough" before either updates the
timestamp, and both fire near-simultaneously — exceeding the broker's real safe
rate and risking an account-level block.

One `AccountRateLimiter` instance per broker ACCOUNT (never one shared globally,
never one per worker) — see engine/broker_accounts.py for how instances are
created and assigned.
"""

import threading
import time


class AccountRateLimiter:
    """Ensures calls made through this instance are spaced at least
    `min_interval_seconds` apart, no matter how many threads share it."""

    def __init__(self, min_interval_seconds: float):
        self._lock = threading.Lock()
        self._last_call_at = 0.0
        self._min_interval = min_interval_seconds

    def wait_for_turn(self) -> None:
        """Blocks the calling thread until it's safe to make the next call, then
        reserves that slot. The whole check-sleep-update sequence happens while
        holding the lock, so two threads can never both decide it's their turn."""
        with self._lock:
            elapsed = time.time() - self._last_call_at
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call_at = time.time()

    def call(self, fn, *args, **kwargs):
        """Convenience wrapper: waits for a safe turn, then calls fn(*args, **kwargs)
        and returns its result. Use this (rather than wait_for_turn + a separate
        call) wherever possible, so the "reserve the slot" and "actually make the
        network call" steps stay next to each other in the caller's code."""
        self.wait_for_turn()
        return fn(*args, **kwargs)
