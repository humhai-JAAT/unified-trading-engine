"""Verifies the subh30 checkpoint-timing gate (resolved design: checkpoints at
09:20/09:25/09:30, each evaluated 1 min after candle close, i.e. checks
actually happen at 09:21/09:26/09:31, with a grace window after that).
"""

from datetime import datetime

import pytz

from engine.variant_engine import next_due_subh30_checkpoint

IST = pytz.timezone("Asia/Kolkata")


def _ist(hh, mm):
    return IST.localize(datetime(2026, 8, 3, hh, mm))  # a Monday


def test_checkpoint_not_due_before_the_one_minute_offset():
    # 09:20 exactly — candle hasn't had its 1-min safety buffer yet.
    assert next_due_subh30_checkpoint(used=set(), now=_ist(9, 20)) is None


def test_checkpoint_due_right_at_the_one_minute_offset():
    assert next_due_subh30_checkpoint(used=set(), now=_ist(9, 21)) == "09:20"


def test_checkpoint_still_due_within_grace_window():
    assert next_due_subh30_checkpoint(used=set(), now=_ist(9, 28)) == "09:20"


def test_checkpoint_skipped_permanently_after_grace_window_even_if_unused():
    # Grace window is 10 minutes from the 1-min offset (09:21-09:31) — by 09:35
    # the 09:20 checkpoint should be gone, not fired late.
    result = next_due_subh30_checkpoint(used=set(), now=_ist(9, 35))
    assert result != "09:20"


def test_already_used_checkpoint_is_skipped():
    assert next_due_subh30_checkpoint(used={"09:20"}, now=_ist(9, 21)) is None


def test_second_checkpoint_becomes_due_after_first_is_used():
    assert next_due_subh30_checkpoint(used={"09:20"}, now=_ist(9, 26)) == "09:25"


def test_no_checkpoint_due_outside_all_windows():
    assert next_due_subh30_checkpoint(used=set(), now=_ist(10, 0)) is None
