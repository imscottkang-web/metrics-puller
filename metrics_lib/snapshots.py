"""Assemble and append weekly_snapshots.csv rows - the append-only writer.

Pure functions: they take already-fetched plain data and produce/append rows.
The API clients (reporting, analytics) and the registry are called by the CLI
(metrics.py); this module imports none of them, so it stays trivially testable
and never touches the network.

Two joins live here because they combine sources:
- aggregate_reach: many daily reach rows -> one video's total impressions +
  impression-weighted CTR.
- retention_at_hook_end: the Analytics retention CURVE + the REAL hook-end
  timestamp (from a video's delivered_diff.md) -> the single % remaining.
"""

import csv
import re
from datetime import date
from pathlib import Path

COLUMNS = [
    "snapshot_date",
    "checkpoint",
    "video_id",
    "slug",
    "impressions",
    "ctr",
    "views",
    "retention_at_hook_end",
    "avg_view_duration_sec",
    "traffic_browse",
    "traffic_search",
    "traffic_suggested",
    "notes",
]

# Matches a "MM:SS" or "H:MM:SS" clock-style token. Two colon-separated parts
# are minutes:seconds; three are hours:minutes:seconds - the extra group is
# what tells the formats apart, so minutes are never misread as hours (or
# vice versa). The first part accepts up to 3 digits so a plain running time
# longer than 99 minutes (e.g. "125:00") still parses as MM:SS.
_CLOCK_RE = re.compile(r"(\d{1,3}):(\d{2})(?::(\d{2}))?")


class SnapshotError(Exception):
    """weekly_snapshots.csv is not shaped the way this writer expects.

    Raised - never silently worked around - when an existing, non-empty
    file's header no longer matches COLUMNS, so a stale or hand-edited header
    can never cause new rows to be appended under the wrong column names.
    """


def parse_clock_seconds(text):
    """Parse the first clock-style token in text to whole seconds, or None.

    Accepts "MM:SS" (e.g. "92:15" -> 5535 - minutes are not capped at 59, so
    a long video's plain running time still parses correctly) and "H:MM:SS"
    (e.g. "1:02:30" -> 3750). Returns None when no such token is found (e.g.
    a still-unfilled "<MM:SS>" placeholder, which has no digits at all).
    """
    match = _CLOCK_RE.search(text)
    if not match:
        return None
    first, second, third = match.groups()
    if third is not None:
        return int(first) * 3600 + int(second) * 60 + int(third)
    return int(first) * 60 + int(second)


def _parse_iso_date(value):
    """Parse a 'YYYY-MM-DD' string to a date, or None if blank/unparseable."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def read_video_history(csv_path):
    """Per-video recording history, for deciding what to record on a daily run.

    Reads weekly_snapshots.csv once and returns
      {video_id: {"checkpoints": set(labels on file), "last_date": date|None}}.
    "checkpoints" lets a daily cron skip a milestone it has already written;
    "last_date" (the most recent snapshot_date seen for that video) drives the
    weekly-continuity spacing. A missing file returns {}; a row with a blank or
    unparseable snapshot_date still contributes its checkpoint label but no date.
    """
    path = Path(csv_path)
    history: dict[str, dict] = {}
    if not path.is_file():
        return history
    with path.open(newline="", encoding="utf-8") as f:
        for record in csv.DictReader(f):
            video_id = record.get("video_id")
            if not video_id:
                continue
            entry = history.setdefault(video_id, {"checkpoints": set(), "last_date": None})
            checkpoint = record.get("checkpoint")
            if checkpoint:
                entry["checkpoints"].add(checkpoint)
            snapshot_date = _parse_iso_date(record.get("snapshot_date"))
            if snapshot_date and (entry["last_date"] is None or snapshot_date > entry["last_date"]):
                entry["last_date"] = snapshot_date
    return history


def aggregate_reach(daily_rows, video_id):
    """Sum one video's daily reach rows into total impressions + weighted CTR.

    daily_rows: list of {"video_id","impressions","ctr"(percent), ...}.
    Returns {"impressions": int, "ctr": float|None}. CTR is impression-weighted
    (total clicks / total impressions, as a percent, 2 dp); None when there are
    no impressions, so a video with no reach data reads blank rather than 0%.
    """
    rows = [r for r in daily_rows if r.get("video_id") == video_id]
    total_impressions = sum(int(r["impressions"]) for r in rows)
    if total_impressions == 0:
        return {"impressions": 0, "ctr": None}
    total_clicks = sum(int(r["impressions"]) * float(r["ctr"]) / 100.0 for r in rows)
    return {"impressions": total_impressions, "ctr": round(100.0 * total_clicks / total_impressions, 2)}


def parse_hook_end_seconds(delivered_diff_path):
    """Read 'Real hook-end timestamp: MM:SS' (or 'H:MM:SS') from a delivered_diff.md.

    Returns whole seconds, or None if the file is missing or the value is still
    a placeholder (e.g. '<MM:SS>') - retention_at_hook_end stays blank until a
    real timestamp is logged, exactly as the CSV README specifies.
    """
    path = Path(delivered_diff_path)
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if "Real hook-end timestamp" in line:
            return parse_clock_seconds(line)
    return None


def retention_at_hook_end(curve, hook_end_seconds, duration_s):
    """Read the retention curve at the real hook-end point, as a percent.

    curve: list of {"elapsed_ratio": float, "audience_watch_ratio": float}.
    Linearly interpolates the watch ratio at hook_end_seconds/duration_s and
    returns it as a percent (1 dp). Returns None when any input is missing, so
    the column is blank until every piece is known.
    """
    if not curve or not hook_end_seconds or not duration_s:
        return None
    target = max(0.0, min(1.0, hook_end_seconds / duration_s))
    points = sorted(
        (float(p["elapsed_ratio"]), float(p["audience_watch_ratio"])) for p in curve
    )
    if target <= points[0][0]:
        ratio = points[0][1]
    elif target >= points[-1][0]:
        ratio = points[-1][1]
    else:
        ratio = points[-1][1]
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if x0 <= target <= x1:
                span = x1 - x0
                ratio = y0 if span == 0 else y0 + (y1 - y0) * (target - x0) / span
                break
    return round(ratio * 100, 1)


def assemble_row(*, snapshot_date, checkpoint, entry, reach, core, traffic, retention_pct, notes=""):
    """Map the fetched pieces into one 13-column snapshot row (dict).

    None values become blank (""), per the CSV README - a blank cell means "not
    known yet", never a real zero.
    """

    def blank(value):
        return "" if value is None else value

    return {
        "snapshot_date": snapshot_date,
        "checkpoint": checkpoint,
        "video_id": entry["video_id"],
        "slug": entry["slug"],
        "impressions": blank(reach.get("impressions")),
        "ctr": blank(reach.get("ctr")),
        "views": blank(core.get("views")),
        "retention_at_hook_end": blank(retention_pct),
        "avg_view_duration_sec": blank(core.get("avg_view_duration_sec")),
        "traffic_browse": blank(traffic.get("browse")),
        "traffic_search": blank(traffic.get("search")),
        "traffic_suggested": blank(traffic.get("suggested")),
        "notes": notes,
    }


def _heal_torn_last_line(path):
    """If path (known non-empty) does not end with a newline, append one.

    A process crash mid-write can truncate the CSV's last line so it has no
    trailing newline; without this, the next appended row's text would be
    glued directly onto that torn line instead of starting on its own line.
    Only ever called when the file is known to be non-empty.
    """
    with path.open("rb") as f:
        f.seek(-1, 2)
        last_byte = f.read(1)
    if last_byte != b"\n":
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")


def append_snapshot(csv_path, rows, *, printer=print):
    """Append rows (13-key dicts) to the CSV: append-only and idempotent.

    A row whose (snapshot_date, checkpoint, video_id) already exists in the
    file is skipped, so re-running the same day adds nothing; existing history
    is never rewritten. Duplicates within one batch are also collapsed. The
    header is written only when the file is new/empty (in the repo it already
    ships). Returns the number of rows actually appended.

    Hardening for a hand-edited CSV (the data README documents a manual-paste
    ritual, so ragged legacy rows are realistic):
    - a row missing snapshot_date, checkpoint, or video_id is skipped in the
      duplicate-check scan (with a one-line warning naming the row number, via
      `printer`) instead of raising, so a human can find and fix it by hand;
    - a crash-truncated last line (no trailing newline) is healed with a
      newline before any new rows are appended, so they are never glued onto
      it (see _heal_torn_last_line);
    - a header that no longer matches COLUMNS raises SnapshotError rather than
      silently appending rows under the wrong column names.
    """
    path = Path(csv_path)
    file_has_content = path.is_file() and path.stat().st_size > 0

    seen = set()
    if file_has_content:
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = reader.fieldnames or []
            if header != COLUMNS:
                raise SnapshotError(
                    "weekly_snapshots.csv header does not match the columns this "
                    f"writer expects.\nFound:    {header}\nExpected: {COLUMNS}\n"
                    "Fix the file's header by hand (or reconcile it with the code) "
                    "before appending more rows."
                )
            for line_no, record in enumerate(reader, start=2):  # line 1 is the header
                snapshot_date = record.get("snapshot_date")
                checkpoint = record.get("checkpoint")
                video_id = record.get("video_id")
                if not snapshot_date or not checkpoint or not video_id:
                    printer(
                        f"WARNING: weekly_snapshots.csv row {line_no} is missing "
                        "snapshot_date, checkpoint, or video_id - skipped in the "
                        "duplicate check. Fix the row by hand if that is unexpected."
                    )
                    continue
                seen.add((snapshot_date, checkpoint, video_id))

    to_write = []
    for row in rows:
        key = (row["snapshot_date"], row["checkpoint"], row["video_id"])
        if key in seen:
            continue
        seen.add(key)
        to_write.append(row)

    if to_write:
        if file_has_content:
            _heal_torn_last_line(path)
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            if not file_has_content:
                writer.writeheader()
            writer.writerows(to_write)
    return len(to_write)
