"""Tests for metrics_lib.reach_archive - the local reach-report archive.

No network: FakeReportingClient is a minimal stand-in for ReportingClient that
exposes exactly the three methods gather_reach_rows calls (ensure_reach_job,
list_reports, download_report_text), so these tests exercise archive/ledger
behavior in isolation from metrics_lib.reporting's own HTTP-shaped tests.

These tests lock in the fix for the impressions double-count / 60-day
rolling-window bug: a day's report must be summed at most once across any
number of runs, and a day must stay counted even after it falls out of the
API's ~60-day report listing, as long as it is still in our own archive.
"""

import json

import pytest

from metrics_lib.http import MetricsApiError
from metrics_lib.reach_archive import gather_reach_rows

CSV_HEADER = "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr"


def _csv(date_str, *value_rows):
    """Build reach-report CSV text: header + one line per "video_id,
    impressions,ctr" tuple-string, all sharing date_str."""
    lines = [CSV_HEADER]
    lines.extend(f"{date_str},{row}" for row in value_rows)
    return "\n".join(lines) + "\n"


class FakeReportingClient:
    """Minimal stand-in for ReportingClient exposing exactly the methods
    gather_reach_rows calls. Report bodies are keyed by downloadUrl; call
    lists let a test assert how many times (and which urls) were downloaded.
    """

    def __init__(self, job_id, reports, texts_by_url):
        self.job_id = job_id
        self._reports = reports
        self._texts_by_url = texts_by_url
        self.ensure_reach_job_calls = 0
        self.list_reports_calls = []
        self.download_calls = []

    def ensure_reach_job(self, printer=print):
        self.ensure_reach_job_calls += 1
        return self.job_id

    def list_reports(self, job_id):
        self.list_reports_calls.append(job_id)
        return list(self._reports)

    def download_report_text(self, download_url):
        self.download_calls.append(download_url)
        return self._texts_by_url[download_url]


def _report(report_id, start_time, create_time, download_url):
    return {"id": report_id, "startTime": start_time, "createTime": create_time, "downloadUrl": download_url}


# --- basic archive + combine behavior ---


def test_gather_reach_rows_downloads_and_returns_rows_on_first_run(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/rep1"
    text = _csv("2026-07-01", "vid001,1000,0.048", "vid002,850,0.0512")
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})

    rows = gather_reach_rows(client, archive_dir)

    assert rows == [
        {"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8},
        {"date": "2026-07-01", "video_id": "vid002", "impressions": 850, "ctr": 5.12},
    ]
    assert client.download_calls == [url]
    assert client.ensure_reach_job_calls == 1


def test_gather_reach_rows_parses_columns_by_header_not_position(tmp_path):
    # Same contract as metrics_lib.reporting.parse_reach_csv: column order in
    # the source CSV must not matter, only the header names.
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/scrambled"
    text = "video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr,date\nvid001,1000,0.048,2026-07-01\n"
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})

    rows = gather_reach_rows(client, archive_dir)

    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}]


def test_gather_reach_rows_creates_archive_dir_if_missing(tmp_path):
    archive_dir = tmp_path / "nested" / "_reach_reports"
    assert not archive_dir.exists()
    client = FakeReportingClient("job-abc", [], {})

    rows = gather_reach_rows(client, archive_dir)

    assert rows == []
    assert archive_dir.is_dir()


def test_gather_reach_rows_writes_day_file_and_ledger(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/rep1"
    text = _csv("2026-07-01", "vid001,1000,0.048")
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})

    gather_reach_rows(client, archive_dir)

    day_file = archive_dir / "20260701.csv"
    ledger_file = archive_dir / "ingest_ledger.json"
    assert day_file.read_text(encoding="utf-8") == text
    ledger = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert ledger == {"20260701": {"report_id": "rep1", "create_time": "2026-07-02T05:00:00Z"}}
    assert list(archive_dir.glob("*.tmp")) == []  # atomic writes leave no temp files behind


def test_gather_reach_rows_skips_reports_missing_download_url_or_start_time(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    reports = [
        {"id": "rep-nostart", "createTime": "2026-07-02T05:00:00Z", "downloadUrl": "https://x/y"},
        {"id": "rep-nourl", "startTime": "2026-07-01T00:00:00Z", "createTime": "2026-07-02T05:00:00Z"},
    ]
    client = FakeReportingClient("job-abc", reports, {})

    rows = gather_reach_rows(client, archive_dir)

    assert rows == []
    assert client.download_calls == []


def test_gather_reach_rows_ignores_non_day_shaped_files_already_in_archive_dir(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    archive_dir.mkdir(parents=True)
    (archive_dir / "notes.csv").write_text("not a day file", encoding="utf-8")
    client = FakeReportingClient("job-abc", [], {})

    rows = gather_reach_rows(client, archive_dir)

    assert rows == []


# --- the double-count fix: same report re-listed across runs is summed once ---


def test_gather_reach_rows_does_not_redownload_or_double_count_on_second_run(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/rep1"
    text = _csv("2026-07-01", "vid001,1000,0.048")
    report = _report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)

    client1 = FakeReportingClient("job-abc", [report], {url: text})
    rows1 = gather_reach_rows(client1, archive_dir)

    client2 = FakeReportingClient("job-abc", [report], {url: text})
    rows2 = gather_reach_rows(client2, archive_dir)

    assert rows1 == rows2 == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}]
    assert client2.download_calls == []  # already archived with the same createTime -> no re-download


def test_gather_reach_rows_redownloads_and_replaces_when_create_time_is_newer(tmp_path):
    # YouTube re-issued the day's report after a data correction: same day,
    # newer createTime. The corrected version must REPLACE the old one, not
    # add to it (that is exactly the double-count bug).
    archive_dir = tmp_path / "_reach_reports"
    url_v1 = "https://reports.example/v1"
    url_v2 = "https://reports.example/v2"
    text_v1 = _csv("2026-07-01", "vid001,1000,0.04")
    text_v2 = _csv("2026-07-01", "vid001,1200,0.05")
    report_v1 = _report("rep-v1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url_v1)
    report_v2 = _report("rep-v2", "2026-07-01T00:00:00Z", "2026-07-03T05:00:00Z", url_v2)

    client1 = FakeReportingClient("job-abc", [report_v1], {url_v1: text_v1})
    rows1 = gather_reach_rows(client1, archive_dir)
    assert rows1 == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.0}]

    client2 = FakeReportingClient("job-abc", [report_v2], {url_v2: text_v2})
    rows2 = gather_reach_rows(client2, archive_dir)

    assert rows2 == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1200, "ctr": 5.0}]
    assert client2.download_calls == [url_v2]


def test_gather_reach_rows_does_not_redownload_when_create_time_is_older_or_equal(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url_v2 = "https://reports.example/v2"
    text_v2 = _csv("2026-07-01", "vid001,1200,0.05")
    report_v2 = _report("rep-v2", "2026-07-01T00:00:00Z", "2026-07-03T05:00:00Z", url_v2)
    client1 = FakeReportingClient("job-abc", [report_v2], {url_v2: text_v2})
    gather_reach_rows(client1, archive_dir)

    # A later run sees the SAME (or an older) createTime for the same day.
    url_stale = "https://reports.example/stale-resend"
    report_stale = _report("rep-stale", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", url_stale)
    client2 = FakeReportingClient("job-abc", [report_stale], {url_stale: "should never be read"})

    rows = gather_reach_rows(client2, archive_dir)

    assert client2.download_calls == []
    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1200, "ctr": 5.0}]


# --- the 60-day-truncation fix: archived days outlive the API's listing ---


def test_gather_reach_rows_keeps_days_that_later_fall_out_of_the_api_listing(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url_day1 = "https://reports.example/day1"
    url_day2 = "https://reports.example/day2"
    text_day1 = _csv("2026-05-01", "vid001,500,0.03")
    text_day2 = _csv("2026-07-01", "vid001,1000,0.04")
    report_day1 = _report("rep1", "2026-05-01T00:00:00Z", "2026-05-02T00:00:00Z", url_day1)
    report_day2 = _report("rep2", "2026-07-01T00:00:00Z", "2026-07-02T00:00:00Z", url_day2)

    client1 = FakeReportingClient("job-abc", [report_day1, report_day2], {url_day1: text_day1, url_day2: text_day2})
    gather_reach_rows(client1, archive_dir)

    # Simulate report_day1 aging out of YouTube's ~60-day retention: a later
    # run's list_reports no longer includes it at all.
    client2 = FakeReportingClient("job-abc", [report_day2], {url_day2: text_day2})
    rows = gather_reach_rows(client2, archive_dir)

    assert len(rows) == 2
    assert {"date": "2026-05-01", "video_id": "vid001", "impressions": 500, "ctr": 3.0} in rows
    assert {"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.0} in rows
    assert client2.download_calls == []  # day2 unchanged -> not re-downloaded either


# --- validate BEFORE archiving: a bad download must never poison the archive ---


def test_gather_reach_rows_refuses_and_does_not_archive_ctr_that_looks_like_a_percent(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/badctr"
    text = _csv("2026-07-01", "vid001,1000,4.8")
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})

    with pytest.raises(MetricsApiError):
        gather_reach_rows(client, archive_dir)

    # The refused body must not have been archived or recorded: archiving it
    # would suppress the re-download and poison every later run.
    assert list(archive_dir.glob("*.csv")) == []
    assert not (archive_dir / "ingest_ledger.json").exists()


def test_gather_reach_rows_does_not_archive_a_bad_body_and_retries_next_run(tmp_path):
    # A transiently bad download (here: an empty body) must raise WITHOUT
    # writing the day file or the ledger entry. If it were recorded, the next
    # run would see the same createTime, skip the re-download, and fail on
    # the same bad file forever - a permanent failure a human would have to
    # clean up by hand. With nothing recorded, the next run simply retries.
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/flaky"
    report = _report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)

    client_bad = FakeReportingClient("job-abc", [report], {url: ""})
    printed_bad = []
    with pytest.raises(MetricsApiError):
        gather_reach_rows(client_bad, archive_dir, printer=printed_bad.append)

    assert list(archive_dir.glob("*.csv")) == []
    assert not (archive_dir / "ingest_ledger.json").exists()
    assert printed_bad == []  # no first-ingest diagnostic for a failed ingest

    # Same report, same createTime, good body this time: retried and archived.
    good_text = _csv("2026-07-01", "vid001,1000,0.048")
    client_good = FakeReportingClient("job-abc", [report], {url: good_text})
    printed_good = []

    rows = gather_reach_rows(client_good, archive_dir, printer=printed_good.append)

    assert client_good.download_calls == [url]  # nothing was recorded -> re-downloaded
    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}]
    assert (archive_dir / "20260701.csv").read_text(encoding="utf-8") == good_text
    assert len(printed_good) == 1  # the first SUCCESSFUL ingest carries the diagnostic
    assert printed_good[0].startswith("First real reach report ingested")


# --- ledger robustness: hand-edited damage degrades to a re-download, not a crash ---


def test_gather_reach_rows_treats_non_dict_ledger_entries_as_absent(tmp_path):
    # A hand-edited ledger entry like {"20260702": "oops"} must not crash the
    # run (the newer-createTime check would call .get on a string). Dropping
    # the bad entry at load treats that day as never ingested, so it is
    # simply re-downloaded and the ledger heals itself.
    archive_dir = tmp_path / "_reach_reports"
    archive_dir.mkdir(parents=True)
    day1_text = _csv("2026-07-01", "vid001,1000,0.048")
    (archive_dir / "20260701.csv").write_text(day1_text, encoding="utf-8")
    (archive_dir / "ingest_ledger.json").write_text(
        json.dumps(
            {
                "20260701": {"report_id": "rep1", "create_time": "2026-07-02T05:00:00Z"},
                "20260702": "oops",
            }
        ),
        encoding="utf-8",
    )

    url = "https://reports.example/day2"
    day2_text = _csv("2026-07-02", "vid001,900,0.04")
    report_day2 = _report("rep2", "2026-07-02T00:00:00Z", "2026-07-03T05:00:00Z", url)
    client = FakeReportingClient("job-abc", [report_day2], {url: day2_text})

    rows = gather_reach_rows(client, archive_dir, printer=lambda _msg: None)

    assert client.download_calls == [url]  # the corrupted day re-downloads
    assert len(rows) == 2
    assert {"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8} in rows
    assert {"date": "2026-07-02", "video_id": "vid001", "impressions": 900, "ctr": 4.0} in rows
    healed = json.loads((archive_dir / "ingest_ledger.json").read_text(encoding="utf-8"))
    assert healed["20260701"] == {"report_id": "rep1", "create_time": "2026-07-02T05:00:00Z"}
    assert healed["20260702"] == {"report_id": "rep2", "create_time": "2026-07-03T05:00:00Z"}


# --- defect-3: first-real-ingestion diagnostic ---


def test_gather_reach_rows_prints_first_ingest_diagnostic_when_ledger_starts_empty(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/rep1"
    text = _csv("2026-07-01", "vid001,1000,0.048", "vid002,850,0.0512")
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})
    printed = []

    gather_reach_rows(client, archive_dir, printer=printed.append)

    assert len(printed) == 1
    assert printed[0].startswith("First real reach report ingested")
    assert "0.048" in printed[0]
    assert "4.8" in printed[0]
    assert "http" not in printed[0].lower()


def test_gather_reach_rows_first_ingest_diagnostic_caps_at_three_samples(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url = "https://reports.example/rep1"
    value_rows = [f"vid{i:03d},100,0.0{i + 1}" for i in range(5)]
    text = _csv("2026-07-01", *value_rows)
    client = FakeReportingClient("job-abc", [_report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url)], {url: text})
    printed = []

    gather_reach_rows(client, archive_dir, printer=printed.append)

    assert len(printed) == 1
    assert printed[0].count("raw ") == 3


def test_gather_reach_rows_no_diagnostic_on_a_later_run(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    url1 = "https://reports.example/rep1"
    text1 = _csv("2026-07-01", "vid001,1000,0.048")
    report1 = _report("rep1", "2026-07-01T00:00:00Z", "2026-07-02T05:00:00Z", url1)
    client1 = FakeReportingClient("job-abc", [report1], {url1: text1})
    gather_reach_rows(client1, archive_dir, printer=lambda _msg: None)

    url2 = "https://reports.example/rep2"
    text2 = _csv("2026-07-02", "vid001,900,0.04")
    report2 = _report("rep2", "2026-07-02T00:00:00Z", "2026-07-03T05:00:00Z", url2)
    client2 = FakeReportingClient("job-abc", [report2], {url2: text2})
    printed = []

    gather_reach_rows(client2, archive_dir, printer=printed.append)

    assert printed == []


def test_gather_reach_rows_no_diagnostic_when_nothing_new_to_ingest(tmp_path):
    archive_dir = tmp_path / "_reach_reports"
    client = FakeReportingClient("job-abc", [], {})
    printed = []

    gather_reach_rows(client, archive_dir, printer=printed.append)

    assert printed == []


# --- printer plumbing: ensure_reach_job's own warnings surface through gather_reach_rows ---


def test_gather_reach_rows_passes_printer_through_to_ensure_reach_job(tmp_path):
    archive_dir = tmp_path / "_reach_reports"

    class WarningClient(FakeReportingClient):
        def ensure_reach_job(self, printer=print):
            printer("Warning: 2 reach-report jobs exist (expected 1) - example passthrough warning.")
            return self.job_id

    client = WarningClient("job-abc", [], {})
    printed = []

    gather_reach_rows(client, archive_dir, printer=printed.append)

    assert any("2 reach-report jobs" in msg for msg in printed)
