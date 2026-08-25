"""Tests for metrics_lib.snapshots - the append-only CSV writer + the joins.

Pure functions, no network, no client imports. Everything is exercised with
in-test data and tmp_path files, so this suite is independent of the API-client
modules the workers are building in parallel.
"""

import csv
from datetime import date

import pytest

from metrics_lib.snapshots import (
    COLUMNS,
    SnapshotError,
    aggregate_reach,
    append_snapshot,
    assemble_row,
    parse_clock_seconds,
    parse_hook_end_seconds,
    read_video_history,
    retention_at_hook_end,
)


# --- aggregate_reach --------------------------------------------------------

def test_aggregate_reach_sums_impressions_and_impression_weighted_ctr():
    daily = [
        {"video_id": "v1", "impressions": 1000, "ctr": 4.0},   # 40 clicks
        {"video_id": "v1", "impressions": 3000, "ctr": 6.0},   # 180 clicks
        {"video_id": "v2", "impressions": 500, "ctr": 9.0},    # other video, ignored
    ]
    agg = aggregate_reach(daily, "v1")
    assert agg["impressions"] == 4000
    # (40 + 180) / 4000 = 5.5%
    assert agg["ctr"] == 5.5


def test_aggregate_reach_zero_impressions_gives_none_ctr():
    assert aggregate_reach([], "v1") == {"impressions": 0, "ctr": None}


# --- parse_hook_end_seconds -------------------------------------------------

def test_parse_hook_end_seconds_reads_mmss(tmp_path):
    p = tmp_path / "delivered_diff.md"
    p.write_text("- **Real hook-end timestamp:** 1:23   <!-- comment -->\n", encoding="utf-8")
    assert parse_hook_end_seconds(p) == 83


def test_parse_hook_end_seconds_placeholder_or_missing_is_none(tmp_path):
    p = tmp_path / "delivered_diff.md"
    p.write_text("- **Real hook-end timestamp:** <MM:SS>\n", encoding="utf-8")
    assert parse_hook_end_seconds(p) is None
    assert parse_hook_end_seconds(tmp_path / "nope.md") is None


def test_parse_hook_end_seconds_reads_hms_with_hours(tmp_path):
    # Regression: the old regex only ever captured the first two colon-groups,
    # so "1:02:30" (1h 2m 30s) misparsed as "1:02" -> 62 seconds instead of 3750.
    p = tmp_path / "delivered_diff.md"
    p.write_text("- **Real hook-end timestamp:** 1:02:30\n", encoding="utf-8")
    assert parse_hook_end_seconds(p) == 3750


# --- parse_clock_seconds -----------------------------------------------------

def test_parse_clock_seconds_reads_plain_mmss():
    assert parse_clock_seconds("Video length: 07:45") == 465


def test_parse_clock_seconds_reads_hms_with_hours():
    assert parse_clock_seconds("Video length: 1:02:30") == 3750


def test_parse_clock_seconds_mmss_minutes_can_exceed_59():
    # A plain running time longer than 99 minutes must still read as MM:SS,
    # not be misread as an hours group.
    assert parse_clock_seconds("Video length: 92:15") == 5535


def test_parse_clock_seconds_no_digits_is_none():
    assert parse_clock_seconds("Video length: <MM:SS>") is None
    assert parse_clock_seconds("no time token here at all") is None


# --- retention_at_hook_end --------------------------------------------------

def test_retention_at_hook_end_interpolates_between_points():
    curve = [
        {"elapsed_ratio": 0.0, "audience_watch_ratio": 1.0},
        {"elapsed_ratio": 0.10, "audience_watch_ratio": 0.80},
        {"elapsed_ratio": 0.20, "audience_watch_ratio": 0.60},
    ]
    # duration 100s, hook ends at 10s -> ratio 0.10 -> 0.80 -> 80.0%
    assert retention_at_hook_end(curve, 10, 100) == 80.0
    # hook ends at 15s -> ratio 0.15 -> halfway between 0.80 and 0.60 -> 0.70 -> 70.0%
    assert retention_at_hook_end(curve, 15, 100) == 70.0


def test_retention_at_hook_end_none_when_inputs_missing():
    curve = [{"elapsed_ratio": 0.0, "audience_watch_ratio": 1.0}]
    assert retention_at_hook_end(curve, None, 100) is None
    assert retention_at_hook_end(curve, 10, None) is None
    assert retention_at_hook_end([], 10, 100) is None


# --- assemble_row -----------------------------------------------------------

def test_assemble_row_maps_all_columns_and_blanks_none():
    row = assemble_row(
        snapshot_date="2026-07-12",
        checkpoint="weekly",
        entry={"video_id": "v1", "slug": "seller-repairs"},
        reach={"impressions": 4000, "ctr": 5.5},
        core={"views": 1200, "avg_view_duration_sec": 210},
        traffic={"browse": 55.0, "search": 30.0, "suggested": 10.0},
        retention_pct=None,
        notes="",
    )
    assert list(row.keys()) == COLUMNS
    assert row["video_id"] == "v1"
    assert row["impressions"] == 4000
    assert row["ctr"] == 5.5
    assert row["retention_at_hook_end"] == ""  # None -> blank per the CSV README


# --- append_snapshot --------------------------------------------------------

def _row(date, checkpoint, video_id, **over):
    base = {c: "" for c in COLUMNS}
    base.update(snapshot_date=date, checkpoint=checkpoint, video_id=video_id, slug=video_id)
    base.update(over)
    return base


def test_append_writes_header_when_empty_then_rows(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1")])
    assert n == 1
    text = csv_path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(COLUMNS)
    assert "v1" in text


def test_append_is_idempotent_per_date_checkpoint_video(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1")])
    # same key again -> nothing appended
    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1", impressions=999)])
    assert n == 0
    data_lines = csv_path.read_text(encoding="utf-8").strip().splitlines()[1:]  # drop header
    assert len(data_lines) == 1  # the duplicate key was skipped; original row untouched


def test_append_is_append_only_preserving_history(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    append_snapshot(csv_path, [_row("2026-07-05", "weekly", "v1")])
    append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1")])  # new date = new era row
    lines = [l for l in csv_path.read_text(encoding="utf-8").splitlines() if "v1" in l]
    assert len(lines) == 2  # both snapshots kept


def test_append_dedupes_within_one_batch(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1"), _row("2026-07-12", "weekly", "v1")])
    assert n == 1


# --- read_video_history -----------------------------------------------------

def test_read_video_history_missing_file_is_empty(tmp_path):
    assert read_video_history(tmp_path / "nope.csv") == {}


def test_read_video_history_collects_checkpoints_and_latest_date(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    append_snapshot(
        csv_path,
        [
            _row("2026-07-05", "24h", "v1"),
            _row("2026-07-12", "7d", "v1"),   # later date for v1
            _row("2026-07-05", "weekly", "v2"),
        ],
    )
    hist = read_video_history(csv_path)
    assert hist["v1"]["checkpoints"] == {"24h", "7d"}
    assert hist["v1"]["last_date"] == date(2026, 7, 12)  # the more recent of v1's rows
    assert hist["v2"]["checkpoints"] == {"weekly"}
    assert hist["v2"]["last_date"] == date(2026, 7, 5)


# --- append_snapshot hardening: header-drift guard --------------------------

def test_append_raises_snapshot_error_on_header_drift(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    csv_path.write_text("snapshot_date,checkpoint,video_id\n2026-07-05,weekly,v1\n", encoding="utf-8")

    with pytest.raises(SnapshotError) as ei:
        append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1")])

    message = str(ei.value)
    assert "snapshot_date" in message and "video_id" in message  # names the found header
    assert "notes" in message  # names the expected header (only COLUMNS has "notes")

    # Nothing was appended under the stale header.
    assert csv_path.read_text(encoding="utf-8").strip().splitlines() == [
        "snapshot_date,checkpoint,video_id",
        "2026-07-05,weekly,v1",
    ]


def test_append_does_not_check_header_when_file_is_new_or_empty(tmp_path):
    # A brand-new file has no header yet to drift from - must not raise.
    csv_path = tmp_path / "weekly_snapshots.csv"
    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v1")])
    assert n == 1

    # An existing, but zero-byte, file is likewise not a "drifted header".
    empty_path = tmp_path / "empty.csv"
    empty_path.write_text("", encoding="utf-8")
    n2 = append_snapshot(empty_path, [_row("2026-07-12", "weekly", "v1")])
    assert n2 == 1


# --- append_snapshot hardening: torn-row healing -----------------------------

def test_append_heals_torn_last_line_before_appending(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    append_snapshot(csv_path, [_row("2026-07-05", "weekly", "v1")])

    # Simulate a crash mid-write: the file ends abruptly with no newline at
    # the very end (whatever fragment of the last row's line terminator
    # survived).
    original = csv_path.read_bytes()
    torn = original.rstrip(b"\r\n")
    assert torn != original  # sanity: a trailing newline was actually removed
    csv_path.write_bytes(torn)

    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v2")])
    assert n == 1

    # The new row must be on its OWN line, not glued onto the torn one.
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[-1].startswith("2026-07-12,weekly,v2")

    # And it parses back out cleanly as its own well-formed row.
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows[-1]["video_id"] == "v2"


def test_append_does_not_touch_file_when_nothing_new_to_write(tmp_path):
    # No healing/rewriting should happen when every candidate row is already
    # a duplicate - the file must be left completely untouched.
    csv_path = tmp_path / "weekly_snapshots.csv"
    append_snapshot(csv_path, [_row("2026-07-05", "weekly", "v1")])
    before = csv_path.read_bytes()

    n = append_snapshot(csv_path, [_row("2026-07-05", "weekly", "v1")])  # exact duplicate

    assert n == 0
    assert csv_path.read_bytes() == before


# --- append_snapshot hardening: ragged/short rows in the dedupe scan --------

def test_append_skips_ragged_row_in_dedupe_scan_with_warning(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    header = ",".join(COLUMNS)
    good_row = ",".join(["2026-07-05", "weekly", "v1", "v1"] + [""] * 9)
    ragged_row = "2026-07-06,7d"  # hand-edited: missing video_id and everything after
    csv_path.write_text(f"{header}\n{good_row}\n{ragged_row}\n", encoding="utf-8")

    warnings = []
    n = append_snapshot(csv_path, [_row("2026-07-12", "weekly", "v2")], printer=warnings.append)

    assert n == 1  # the new, well-formed row is still appended - no crash
    assert len(warnings) == 1
    assert "row 3" in warnings[0]  # line 1 = header, line 2 = good_row, line 3 = ragged_row
    assert "v1" not in warnings[0]  # the warning is about the ragged row, not the good one


def test_append_ragged_row_does_not_block_deduping_the_good_rows(tmp_path):
    csv_path = tmp_path / "weekly_snapshots.csv"
    header = ",".join(COLUMNS)
    good_row = ",".join(["2026-07-05", "weekly", "v1", "v1"] + [""] * 9)
    ragged_row = "2026-07-06,7d"
    csv_path.write_text(f"{header}\n{good_row}\n{ragged_row}\n", encoding="utf-8")

    # Re-submitting the SAME key as the good row must still be recognized as
    # a duplicate and skipped, even with a ragged row elsewhere in the file.
    n = append_snapshot(csv_path, [_row("2026-07-05", "weekly", "v1")], printer=lambda _line: None)
    assert n == 0
