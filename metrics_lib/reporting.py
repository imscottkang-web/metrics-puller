"""YouTube Reporting API client - the reach-report (impressions + CTR) half.

The Reporting API is JOB-BASED: a job must be created once (ensure_reach_job)
before YouTube starts compiling daily reports, and there is only a limited
historical backfill - so the job is created as early as possible and never
re-created. Every method here just builds a URL/body and shapes the parsed
response into small plain dicts on top of the shared Transport seam (see
metrics_lib/http.py); transport failures raise MetricsApiError and are left
to propagate uncaught.

Downloading and parsing a report body is split into two pieces on purpose
(download_report_text + parse_reach_csv, composed by fetch_reach_rows for
convenience): metrics_lib/reach_archive.py needs to archive the decoded text
to disk BEFORE parsing it, so it uses the two pieces separately.
"""

import csv
import gzip
import io
from datetime import datetime, timezone

from metrics_lib.http import MetricsApiError

REACH_REPORT_TYPE = "channel_reach_basic_a1"

# The raw column names below, and whether video_thumbnail_impressions_ctr is
# really a 0..1 ratio (vs already a percent), are copied from the design spec
# and have NOT been confirmed against a live API response - confirm both at
# real wire-up (Task 8). Parsing is header-driven (csv.DictReader, never a
# fixed column position) precisely so a column-order or naming surprise there
# is a one-place fix. The CTR-percent assumption is additionally guarded at
# parse time (see parse_reach_csv): if it is ever wrong, the tool refuses to
# write the resulting nonsense values rather than silently corrupting the CSV.
COL_DATE = "date"
COL_VIDEO_ID = "video_id"
COL_IMPRESSIONS = "video_thumbnail_impressions"
COL_CTR = "video_thumbnail_impressions_ctr"
_REQUIRED_COLUMNS = (COL_DATE, COL_VIDEO_ID, COL_IMPRESSIONS, COL_CTR)

# channel_id is NOT required (older archived days may not have it), but when present it is
# carried through to each row - metrics.py's channel-identity guard cross-checks it against
# the house's own configured channel id, as a check that still works with no live API call.
COL_CHANNEL_ID = "channel_id"

# A CTR (as a percent) above this is not a real click-through rate - it means
# the source ratio-to-percent assumption above is wrong, most likely because
# the API already returned a percent rather than a 0..1 ratio.
_CTR_PERCENT_SANITY_CEILING = 100

_GZIP_MAGIC = b"\x1f\x8b"


def ctr_ratio_to_percent(ratio: float) -> float:
    """Convert a 0..1 CTR ratio to a percent, rounded to 2 decimal places."""
    return round(ratio * 100, 2)


def parse_report_timestamp(value):
    """Parse an RFC3339 timestamp (e.g. a report's or job's createTime) into a
    comparable, timezone-aware datetime, or None if missing/unparseable.

    Used wherever two createTime values need to be ordered (which day-report
    is newer, which job is oldest) without assuming a fixed fractional-second
    format - `datetime.fromisoformat` handles that, once the trailing "Z" is
    swapped for an explicit UTC offset.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_reach_csv(text: str) -> list:
    """Parse a reach-report CSV's decoded text into per-video-per-day rows.

    Each returned row is
    {"date": str, "video_id": str, "impressions": int, "ctr": float}
    where ctr is a PERCENT (the source ratio times 100, rounded to 2 decimal
    places) - see the module-level column-name note above.

    Edge cases are handled explicitly rather than left to crash, and every
    refusal raises the house MetricsApiError so the CLI can print it in
    plain English instead of a raw traceback:
    - A header-only body (no data rows) parses to [] - that is a legitimate
      "nothing happened that day" report.
    - A truly empty body (no header at all) raises, since that shape means
      the download came back blank, not "zero rows".
    - A missing required column raises with the missing name and the names
      actually present, so a renamed column at first real wire-up is
      diagnosable from the one line (see the module-level note above).
    """
    if not text:
        raise MetricsApiError(0, "reach report body was empty; refusing to treat that as zero rows")

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    for column in _REQUIRED_COLUMNS:
        if column not in fieldnames:
            present = ", ".join(fieldnames) if fieldnames else "(none)"
            raise MetricsApiError(
                0,
                f"reach report CSV is missing the required column '{column}'; columns present: "
                f"{present}. If YouTube renamed a column, update the COL_ names in "
                "metrics_lib/reporting.py",
            )

    has_channel_id = COL_CHANNEL_ID in fieldnames
    rows = []
    for record in reader:
        percent = ctr_ratio_to_percent(float(record[COL_CTR]))
        if percent > _CTR_PERCENT_SANITY_CEILING:
            raise MetricsApiError(
                0,
                "reach report ctr column looks like it is already a percent, not a 0-1 ratio "
                "(converting it as a ratio produced a value over 100%); refusing to write "
                "incorrect data",
            )
        row = {
            "date": record[COL_DATE],
            "video_id": record[COL_VIDEO_ID],
            "impressions": int(record[COL_IMPRESSIONS]),
            "ctr": percent,
        }
        if has_channel_id:
            row["channel_id"] = record.get(COL_CHANNEL_ID)
        rows.append(row)
    return rows


def _oldest_job(jobs: list) -> dict:
    """Pick the job with the earliest createTime.

    A job with no parseable createTime sorts last (never picked as "oldest"),
    so a missing or malformed timestamp can never masquerade as the original
    job.
    """

    def sort_key(job):
        parsed = parse_report_timestamp(job.get("createTime"))
        return (parsed is None, parsed or datetime.max.replace(tzinfo=timezone.utc))

    return min(jobs, key=sort_key)


class ReportingClient:
    """Thin client for the YouTube Reporting API (job-based reach reports)."""

    BASE = "https://youtubereporting.googleapis.com/v1"

    def __init__(self, transport) -> None:
        self._transport = transport

    def ensure_reach_job(self, name: str, printer=print) -> str:
        """Return the id of the channel_reach_basic_a1 job, creating it once.

        name is required and never defaulted - this tool is shared by more than one
        channel, and a shared default job name would make one house's job
        indistinguishable from another's in the API console. Callers supply the
        house's own Config.reach_job_name.

        Idempotent: if a job of this report type already exists, its id is
        returned and no POST is made. This is what serves "create the reach
        job early, only once" - impressions/CTR history only starts accruing
        from job creation, so re-creating it on a later run would lose the
        history collected in between. The job listing is paginated (see
        _list_all), so an existing reach job on a later page is still found
        and never duplicated.

        Race guard: if two runs both see no existing job and both POST,
        YouTube ends up with more than one channel_reach_basic_a1 job. After
        a POST, jobs are re-listed; if more than one now exists, the OLDEST
        is kept (it has the longest accumulated history) and a plain-English
        warning naming the extras is printed via `printer` - nothing is ever
        auto-deleted, since deleting a job is a write beyond this tool's
        read-only scope.
        """
        jobs = self._reach_jobs()
        if jobs:
            return _oldest_job(jobs)["id"]

        created = self._transport.post(
            f"{self.BASE}/jobs",
            {"reportTypeId": REACH_REPORT_TYPE, "name": name},
        )

        after = self._reach_jobs()
        if len(after) > 1:
            oldest = _oldest_job(after)
            printer(
                f"Warning: {len(after)} reach-report jobs exist (expected 1) - most likely two "
                f"runs raced to create one. Keeping the oldest (id: {oldest['id']}); the extra "
                "job(s) can be deleted by hand in the API console (YouTube Reporting API > Jobs). "
                "This tool never deletes a job itself."
            )
            return oldest["id"]
        if after:
            return after[0]["id"]
        # The re-list came back empty (e.g. eventual-consistency lag right
        # after the POST) - the id we just got back is still good.
        return created["id"]

    def _reach_jobs(self) -> list:
        """List every existing channel_reach_basic_a1 job (usually 0 or 1)."""
        return [
            job
            for job in self._list_all(f"{self.BASE}/jobs", "jobs")
            if job.get("reportTypeId") == REACH_REPORT_TYPE
        ]

    def list_reports(self, job_id: str) -> list:
        """Return the reach job's available daily reports as plain dicts.

        Follows nextPageToken so reports spread across pages are all returned.
        """
        return self._list_all(f"{self.BASE}/jobs/{job_id}/reports", "reports")

    def _list_all(self, url: str, key: str) -> list:
        """GET a Reporting list endpoint, following nextPageToken to the end.

        Returns every item under `key` concatenated across all pages. The
        first request carries no pageToken; each page's nextPageToken drives
        the next request via params={"pageToken": token}. A response with no
        nextPageToken (the common single-page case) means exactly one GET.
        """
        items = []
        params = None
        while True:
            data = self._transport.get(url, params)
            items.extend(data.get(key, []))
            token = data.get("nextPageToken")
            if not token:
                return items
            params = {"pageToken": token}

    def download_report_text(self, download_url: str) -> str:
        """Download a report body and decode it to text.

        The download endpoint may serve the CSV gzip-compressed; the magic
        number (the two bytes \\x1f\\x8b) is sniffed and the body is
        decompressed first if present, so a gzip response never reaches the
        decoder as if it were raw text. utf-8-sig then strips a leading UTF-8
        byte-order mark if present, so the first header name is not
        corrupted; a BOM left in front of "date" would otherwise fail the
        required-column check in parse_reach_csv.
        """
        raw = self._transport.get_bytes(download_url)
        if raw[:2] == _GZIP_MAGIC:
            raw = gzip.decompress(raw)
        return raw.decode("utf-8-sig")

    def fetch_reach_rows(self, download_url: str) -> list:
        """Download a reach-report CSV and shape it into per-video-per-day rows.

        Thin composition of download_report_text + parse_reach_csv, kept as
        one call for callers that do not need the archive-then-parse split
        (see metrics_lib/reach_archive.py, which uses the two pieces
        separately so it can archive the decoded text before parsing it).
        """
        return parse_reach_csv(self.download_report_text(download_url))
