"""Tests for metrics_lib.checkpoints - age-to-label mapping for the snapshot CSV.

Table-driven against the windows in the design spec's checkpoint table. Every
case runs with both date objects and "YYYY-MM-DD" strings, since checkpoint_for
must accept either.
"""

from datetime import date, timedelta

import pytest

from metrics_lib.checkpoints import checkpoint_for, is_milestone, should_record

PUBLISH_DATE = date(2026, 1, 1)

CASES = [
    # age_days, expected label
    (1, "24h"),  # exact hit
    (7, "7d"),  # exact hit
    (30, "30d"),  # exact hit
    (90, "90d"),  # exact hit
    (180, "180d"),  # exact hit
    (8, "7d"),  # near hit
    (92, "90d"),  # near hit
    (15, "weekly"),  # between checkpoints
    (45, "weekly"),  # between checkpoints
    (130, "weekly"),  # between checkpoints
    (300, "weekly"),  # past 180d
    (-2, "weekly"),  # run before publish
]


@pytest.mark.parametrize("age_days, expected", CASES)
def test_checkpoint_for_with_date_objects(age_days, expected):
    run_date = PUBLISH_DATE + timedelta(days=age_days)
    assert checkpoint_for(PUBLISH_DATE, run_date) == expected


@pytest.mark.parametrize("age_days, expected", CASES)
def test_checkpoint_for_with_string_inputs(age_days, expected):
    run_date = PUBLISH_DATE + timedelta(days=age_days)
    assert checkpoint_for(PUBLISH_DATE.isoformat(), run_date.isoformat()) == expected


def test_checkpoint_for_accepts_mixed_date_and_string_inputs():
    run_date = PUBLISH_DATE + timedelta(days=7)
    assert checkpoint_for(PUBLISH_DATE, run_date.isoformat()) == "7d"
    assert checkpoint_for(PUBLISH_DATE.isoformat(), run_date) == "7d"


# --- is_milestone -----------------------------------------------------------


@pytest.mark.parametrize("label", ["24h", "7d", "30d", "90d", "180d"])
def test_is_milestone_true_for_named_checkpoints(label):
    assert is_milestone(label) is True


def test_is_milestone_false_for_weekly():
    assert is_milestone("weekly") is False


# --- should_record ----------------------------------------------------------
# The daily cron runs every day but records sparingly: a milestone is written
# once, and between milestones a video gets a fresh row about weekly.

RUN = date(2026, 7, 12)


def test_should_record_records_a_milestone_not_yet_on_file():
    # First day the run lands in the 7d window and no "7d" row exists yet.
    assert should_record("7d", set(), None, RUN) is True


def test_should_record_skips_a_milestone_already_on_file():
    # A later day still inside the same window: "7d" is already recorded -> skip,
    # so the milestone is never duplicated across the days of its window.
    assert should_record("7d", {"24h", "7d"}, RUN - timedelta(days=1), RUN) is False


def test_should_record_takes_a_baseline_row_when_nothing_recorded_yet():
    # Even on a non-milestone ("weekly") day, the very first row is taken so a
    # video that first appears between milestones still gets a baseline.
    assert should_record("weekly", set(), None, RUN) is True


def test_should_record_skips_weekly_before_the_gap_elapses():
    assert should_record("weekly", {"7d"}, RUN - timedelta(days=3), RUN) is False


def test_should_record_records_weekly_once_the_gap_elapses():
    assert should_record("weekly", {"7d"}, RUN - timedelta(days=7), RUN) is True


def test_should_record_accepts_string_dates():
    assert should_record("weekly", {"7d"}, "2026-07-05", "2026-07-12") is True   # 7 days
    assert should_record("weekly", {"7d"}, "2026-07-09", "2026-07-12") is False  # 3 days
