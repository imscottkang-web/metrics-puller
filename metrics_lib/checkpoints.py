"""Map a video's age on the run date to a snapshot checkpoint label.

Pure date math, no I/O. checkpoint_for(publish_date, run_date) tells the
puller which named checkpoint (if any) this run lands on, per the design
spec's CSV contract for the `checkpoint` column:
docs/specs/2026-07-12-part-c-metrics-puller-design.md
"""

from datetime import date

# (label, min age_days, max age_days) - first matching window wins. Kept as a
# flat table so the windows are easy to see and adjust in one place. Negative
# age (the run happened before the video published) is handled separately in
# checkpoint_for and always returns "weekly", regardless of this table.
_CHECKPOINT_WINDOWS: list[tuple[str, int, int]] = [
    ("24h", 0, 1),
    ("7d", 5, 9),
    ("30d", 27, 33),
    ("90d", 86, 94),
    ("180d", 175, 185),
]

_DEFAULT_CHECKPOINT = "weekly"


def _as_date(value: date | str) -> date:
    """Accept either a date object or a "YYYY-MM-DD" string."""
    if isinstance(value, str):
        return date.fromisoformat(value)
    if isinstance(value, date):
        return value
    raise TypeError(f"expected a date or 'YYYY-MM-DD' string, got {type(value).__name__}")


def checkpoint_for(publish_date: date | str, run_date: date | str) -> str:
    """Return the checkpoint label for a video published on publish_date,
    as of run_date.

    Both arguments accept a datetime.date or a "YYYY-MM-DD" string.
    age_days = (run_date - publish_date).days. The first window in
    _CHECKPOINT_WINDOWS whose [min, max] contains age_days wins; a negative
    age, or an age that falls between named windows, returns "weekly".
    """
    age_days = (_as_date(run_date) - _as_date(publish_date)).days
    if age_days < 0:
        return _DEFAULT_CHECKPOINT

    for label, min_age, max_age in _CHECKPOINT_WINDOWS:
        if min_age <= age_days <= max_age:
            return label

    return _DEFAULT_CHECKPOINT


# Days between weekly-continuity rows. Under a daily cron a video that is not on
# a named milestone still gets a fresh row about once a week, so the scoreboard
# never goes stale during the long gaps between milestones (e.g. ages 10-26).
WEEKLY_CONTINUITY_DAYS = 7


def is_milestone(checkpoint: str) -> bool:
    """True when checkpoint is a named milestone (24h/7d/30d/90d/180d), not weekly."""
    return checkpoint != _DEFAULT_CHECKPOINT


def should_record(
    checkpoint: str,
    recorded_checkpoints: set[str],
    last_recorded: date | str | None,
    run_date: date | str,
    *,
    weekly_days: int = WEEKLY_CONTINUITY_DAYS,
) -> bool:
    """Decide whether to add a snapshot row for one video on run_date.

    The cron runs every day so a milestone is never missed, but this keeps the
    actual recording sparse:
      - a named milestone is written once - the first day the run lands in its
        window - and skipped on later days in that same window, since it is
        already on file (recorded_checkpoints), so it is never duplicated;
      - between milestones ("weekly"), a row is added only once at least
        weekly_days have passed since the video's last recorded row, or when the
        video has no row yet (last_recorded is None) so it still gets a baseline.

    recorded_checkpoints: the checkpoint labels already on file for this video.
    last_recorded: the video's most recent snapshot date (date or "YYYY-MM-DD"),
    or None if it has never been recorded.
    """
    if is_milestone(checkpoint):
        return checkpoint not in recorded_checkpoints
    if last_recorded is None:
        return True
    return (_as_date(run_date) - _as_date(last_recorded)).days >= weekly_days
