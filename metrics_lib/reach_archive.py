"""Local archive for YouTube Reporting API reach reports.

This is the fix for two related bugs in a naive "download every listed report
and sum every run" approach:

1. Impressions double-count. YouTube re-issues a day's channel_reach_basic_a1
   report after data corrections - same startTime/endTime, a newer
   createTime - and Google's own guidance is to keep the newest by
   createTime. Summing every report the API's reports.list currently returns,
   every run, sums BOTH the original and the corrected version of a
   re-issued day.
2. 60-day rolling-window truncation. The Reporting API only retains reports
   for about 60 days. Once a day's report ages out of reports.list, a puller
   that only ever looks at "what does the API list right now" silently loses
   that day from its lifetime total.

The fix: keep our OWN local archive of one CSV file per day
(archive_dir/YYYYMMDD.csv), plus a ledger (archive_dir/ingest_ledger.json)
recording which report (report id + createTime) is currently archived for
each day. A report is downloaded only when its day is missing from the
ledger or its createTime is strictly newer than what is already archived; a
newer download OVERWRITES the day file (never appends), so a re-issued day
is replaced, not summed twice. The rows returned to the caller are always
built by re-parsing every file in the archive, so the archive - not the
API's current listing - is the source of truth for "what days do we have",
and an archived day survives falling out of the API's 60-day window.

A downloaded body is parsed for validity BEFORE it is archived: a body that
does not parse is never written to a day file or recorded in the ledger, so
a transiently bad download raises once and is retried on the next run,
instead of poisoning the archive permanently.
"""

import csv
import io
import json
import os
from datetime import date
from pathlib import Path

from metrics_lib.reporting import COL_CTR, ctr_ratio_to_percent, parse_reach_csv, parse_report_timestamp

LEDGER_FILENAME = "ingest_ledger.json"
_DAY_FILE_GLOB = "*.csv"
_FIRST_INGEST_SAMPLE_CAP = 3


def gather_reach_rows(client, archive_dir, name, printer=print) -> list:
    """Ensure the reach job exists, archive any new/updated daily reports
    under archive_dir, then return the COMBINED per-video-per-day rows parsed
    from the FULL local archive - not just the reports downloaded this run.

    client: a ReportingClient (or anything exposing its ensure_reach_job,
    list_reports, and download_report_text methods).
    archive_dir: pathlib.Path to archive into; created if missing.
    name: this house's reach-report job name (Config.reach_job_name) - required,
    since ensure_reach_job takes no default (see metrics_lib.reporting).
    printer: where plain-English warnings/diagnostics go (default print).
    Never receives a token, header, or URL - only day keys and CSV values
    already downloaded to local disk.

    Returns rows in exactly the shape metrics.py's old _gather_reach_rows
    returned ({"date", "video_id", "impressions", "ctr"} per row, ctr as a
    percent, plus "channel_id" when the archived CSV carried that column), so
    it is a drop-in replacement at the call site.
    """
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = archive_dir / LEDGER_FILENAME
    ledger = _load_ledger(ledger_path)
    ledger_was_empty_at_start = not ledger
    first_ingest_shown = False

    job_id = client.ensure_reach_job(name, printer=printer)
    for report in client.list_reports(job_id):
        download_url = report.get("downloadUrl")
        day = _day_key(report.get("startTime"))
        if not download_url or day is None:
            continue

        create_time = report.get("createTime")
        existing = ledger.get(day)
        if existing is not None and not _is_strictly_newer(create_time, existing.get("create_time")):
            continue  # already have this day's newest known version archived

        text = client.download_report_text(download_url)
        # Validate BEFORE archiving. A body that does not parse (empty,
        # missing columns, ctr in the wrong form) must never reach the day
        # file or the ledger: recording it would suppress the re-download
        # on every later run (same createTime), so the run would fail on
        # the same bad file forever until a human deleted it. Raising here
        # with nothing recorded means a transiently bad download is simply
        # retried on the next run.
        parse_reach_csv(text)
        _write_day_file(archive_dir, day, text)
        ledger[day] = {"report_id": report.get("id"), "create_time": create_time}
        _save_ledger(ledger_path, ledger)

        if ledger_was_empty_at_start and not first_ingest_shown:
            _print_first_ingest_diagnostic(printer, day, text)
            first_ingest_shown = True

    return _parse_archive(archive_dir)


def read_archived_reach_rows(archive_dir) -> list:
    """Read every row already archived locally, without touching the network.

    Used by metrics.py's channel-identity guard (Net B, the cross-check half): that
    check must still work even when the live API cannot be reached at all, so it
    reads only what is already saved to disk in archive_dir - the same files
    gather_reach_rows above keeps up to date on every successful run.
    """
    return _parse_archive(Path(archive_dir))


def _parse_archive(archive_dir: Path) -> list:
    """Parse every archived day file (oldest filename first) into one list."""
    rows = []
    for day_file in sorted(archive_dir.glob(_DAY_FILE_GLOB)):
        if not _is_day_filename(day_file.stem):
            continue
        rows.extend(parse_reach_csv(day_file.read_text(encoding="utf-8")))
    return rows


def _is_day_filename(stem: str) -> bool:
    return len(stem) == 8 and stem.isdigit()


def _day_key(start_time):
    """Derive a YYYYMMDD ledger/filename key from a report's startTime.

    Returns None when start_time is missing or its first 10 characters are
    not a real calendar date, so a malformed report is skipped instead of
    archived under a nonsense key.
    """
    if not start_time or len(start_time) < 10:
        return None
    try:
        return date.fromisoformat(start_time[:10]).strftime("%Y%m%d")
    except ValueError:
        return None


def _is_strictly_newer(candidate, existing) -> bool:
    """True if the candidate createTime is strictly newer than the ledger's.

    Parses both as RFC3339 timestamps when possible; falls back to a plain
    string comparison if either fails to parse, so a malformed createTime
    can never crash the run - it just makes the comparison less precise.
    """
    candidate_dt = parse_report_timestamp(candidate)
    existing_dt = parse_report_timestamp(existing)
    if candidate_dt is not None and existing_dt is not None:
        return candidate_dt > existing_dt
    return (candidate or "") > (existing or "")


def _load_ledger(path: Path) -> dict:
    """Load the ingest ledger, degrading any damage to "treat as absent".

    An unreadable/non-dict ledger loads as {}, and an entry whose value is
    not a dict (e.g. hand-edited to a bare string) is dropped: the day then
    counts as never ingested, so it is simply re-downloaded and re-recorded
    rather than crashing the newer-createTime check later.
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {day: entry for day, entry in data.items() if isinstance(entry, dict)}


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text to path atomically: write to a temp file, then os.replace
    it into place, so a run killed mid-write never leaves a corrupt or
    partial file at `path` - only ever the old version or the complete new
    one.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def _save_ledger(path: Path, ledger: dict) -> None:
    _atomic_write_text(path, json.dumps(ledger, indent=2, sort_keys=True))


def _write_day_file(archive_dir: Path, day: str, text: str) -> None:
    _atomic_write_text(archive_dir / f"{day}.csv", text)


def _print_first_ingest_diagnostic(printer, day: str, text: str) -> None:
    """Eyeball aid for a supervised first run: up to 3 raw ctr values next to
    their stored percent values, the first time a day is ingested into what
    was an empty ledger at the start of this call. Reads only the CSV text
    already downloaded to local disk - never a token or URL.
    """
    samples = []
    for record in csv.DictReader(io.StringIO(text)):
        raw_value = record.get(COL_CTR)
        if raw_value is None:
            continue
        try:
            raw_ctr = float(raw_value)
        except ValueError:
            continue
        samples.append((raw_ctr, ctr_ratio_to_percent(raw_ctr)))
        if len(samples) == _FIRST_INGEST_SAMPLE_CAP:
            break

    shown = ", ".join(f"raw {raw:g} -> percent {percent:g}" for raw, percent in samples)
    printer(f"First real reach report ingested (day {day}): {shown}")
