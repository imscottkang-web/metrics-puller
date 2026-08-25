"""Tests for metrics_lib.reporting - the YouTube Reporting API (reach) client.

No network: a small FakeTransport records every call and returns a canned
fixture response (or raises a canned MetricsApiError), so nothing here
constructs the real Transport or opens a socket. Mirrors the fake-response
style in tests/test_http.py, but injected at the transport seam instead of
monkeypatching urlopen, per ReportingClient's constructor contract.
"""

import gzip

import pytest

from metrics_lib.http import MetricsApiError
from metrics_lib.reporting import ReportingClient


class PagedResponses:
    """Serves a sequence of list-endpoint pages keyed by the pageToken received.

    The first request arrives with no pageToken (keyed by None); each page's
    nextPageToken is the key the transport uses for the following request. Lets
    a test stand up a real multi-page list without touching the network.
    """

    def __init__(self, pages_by_token):
        self._pages_by_token = pages_by_token

    def for_token(self, token):
        return self._pages_by_token[token]


class SequentialResponses:
    """Serves a fixed sequence of GET responses for repeated calls to the SAME
    url - one response per call, and the last one repeats once the list is
    exhausted. PagedResponses branches on the pageToken; this branches on call
    order instead, which is what a test needs to simulate state changing
    between two GETs to the same url (e.g. a job appearing only after it is
    created by a POST in between).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._index = 0

    def next(self):
        if self._index < len(self._responses):
            value = self._responses[self._index]
            self._index += 1
            return value
        return self._responses[-1]


class FakeTransport:
    """Records calls; returns a canned response or raises a canned error.

    A get response may be a plain dict (single page), a PagedResponses (served
    by the pageToken in params), a SequentialResponses (served by call order),
    or an Exception (raised on access).
    """

    def __init__(self, get_responses=None, post_responses=None, bytes_responses=None):
        self.get_responses = get_responses or {}
        self.post_responses = post_responses or {}
        self.bytes_responses = bytes_responses or {}
        self.get_calls = []
        self.post_calls = []
        self.get_bytes_calls = []

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        response = self.get_responses[url]
        if isinstance(response, PagedResponses):
            response = response.for_token((params or {}).get("pageToken"))
        elif isinstance(response, SequentialResponses):
            response = response.next()
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, body=None):
        self.post_calls.append((url, body))
        response = self.post_responses[url]
        if isinstance(response, Exception):
            raise response
        return response

    def get_bytes(self, url):
        self.get_bytes_calls.append(url)
        response = self.bytes_responses[url]
        if isinstance(response, Exception):
            raise response
        return response


JOBS_URL = f"{ReportingClient.BASE}/jobs"
JOB_NAME = "Anchor and Ivy reach"  # sanctioned fake; this tool names no real business


def test_ensure_reach_job_is_idempotent_when_job_exists(load_fixture):
    jobs = load_fixture("reporting_jobs_list.json")
    transport = FakeTransport(get_responses={JOBS_URL: jobs})
    client = ReportingClient(transport)

    job_id = client.ensure_reach_job(JOB_NAME)

    assert job_id == "job-abc"
    assert transport.post_calls == []
    # Single page (no nextPageToken) -> exactly one GET, carrying no pageToken.
    assert transport.get_calls == [(JOBS_URL, None)]


def test_ensure_reach_job_finds_existing_job_on_second_page():
    pages = PagedResponses(
        {
            None: {
                "jobs": [{"id": "job-other", "reportTypeId": "channel_subscribers_a1"}],
                "nextPageToken": "PAGE2",
            },
            "PAGE2": {"jobs": [{"id": "job-abc", "reportTypeId": "channel_reach_basic_a1"}]},
        }
    )
    transport = FakeTransport(get_responses={JOBS_URL: pages})
    client = ReportingClient(transport)

    job_id = client.ensure_reach_job(JOB_NAME)

    assert job_id == "job-abc"
    assert transport.post_calls == []
    assert transport.get_calls == [
        (JOBS_URL, None),
        (JOBS_URL, {"pageToken": "PAGE2"}),
    ]


def test_ensure_reach_job_creates_job_when_none_exists():
    transport = FakeTransport(
        get_responses={JOBS_URL: {"jobs": []}},
        post_responses={JOBS_URL: {"id": "job-new", "reportTypeId": "channel_reach_basic_a1"}},
    )
    client = ReportingClient(transport)

    job_id = client.ensure_reach_job(JOB_NAME)

    assert job_id == "job-new"
    assert transport.post_calls == [
        (JOBS_URL, {"reportTypeId": "channel_reach_basic_a1", "name": JOB_NAME})
    ]


def test_ensure_reach_job_treats_missing_jobs_key_as_empty():
    transport = FakeTransport(
        get_responses={JOBS_URL: {}},
        post_responses={JOBS_URL: {"id": "job-new2"}},
    )
    client = ReportingClient(transport)

    assert client.ensure_reach_job(JOB_NAME) == "job-new2"


def test_list_reports_returns_parsed_reports(load_fixture):
    reports = load_fixture("reporting_reports_list.json")
    reports_url = f"{ReportingClient.BASE}/jobs/job-abc/reports"
    transport = FakeTransport(get_responses={reports_url: reports})
    client = ReportingClient(transport)

    result = client.list_reports("job-abc")

    assert result == reports["reports"]


def test_list_reports_returns_empty_list_when_reports_key_missing():
    reports_url = f"{ReportingClient.BASE}/jobs/job-empty/reports"
    transport = FakeTransport(get_responses={reports_url: {}})
    client = ReportingClient(transport)

    assert client.list_reports("job-empty") == []


def test_list_reports_concatenates_across_pages():
    reports_url = f"{ReportingClient.BASE}/jobs/job-abc/reports"
    pages = PagedResponses(
        {
            None: {"reports": [{"id": "rep1"}], "nextPageToken": "P2"},
            "P2": {"reports": [{"id": "rep2"}, {"id": "rep3"}]},
        }
    )
    transport = FakeTransport(get_responses={reports_url: pages})
    client = ReportingClient(transport)

    result = client.list_reports("job-abc")

    assert result == [{"id": "rep1"}, {"id": "rep2"}, {"id": "rep3"}]
    assert transport.get_calls == [
        (reports_url, None),
        (reports_url, {"pageToken": "P2"}),
    ]


def test_fetch_reach_rows_parses_scrambled_csv(load_fixture):
    csv_text = load_fixture("reporting_reach.csv")
    download_url = "https://reports.example/rep1"
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    rows = client.fetch_reach_rows(download_url)

    # This fixture's CSV carries a channel_id column (see the fixture file), which the
    # channel-identity guard's local cross-check (metrics.py's _check_channel_identity)
    # relies on - so it must survive parsing, not just the four original columns.
    assert rows == [
        {"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8,
         "channel_id": "UC_fake_channel"},
        {"date": "2026-07-01", "video_id": "vid002", "impressions": 850, "ctr": 5.12,
         "channel_id": "UC_fake_channel"},
        {"date": "2026-07-02", "video_id": "vid001", "impressions": 1100, "ctr": 5.0,
         "channel_id": "UC_fake_channel"},
    ]
    assert transport.get_bytes_calls == [download_url]


def test_fetch_reach_rows_raises_metrics_api_error_on_missing_column():
    # A missing required column must surface as the house MetricsApiError
    # (plain-English, handled at the CLI), never a bare ValueError traceback.
    # The message names the missing column AND lists the columns actually
    # present (column names are data-shape facts, not secrets), so a renamed
    # column at first real wire-up is diagnosable from the one line.
    download_url = "https://reports.example/bad"
    csv_text = "date,video_id,video_thumbnail_impressions_ctr\n2026-07-01,vid001,0.048\n"
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    with pytest.raises(MetricsApiError) as excinfo:
        client.fetch_reach_rows(download_url)

    message = str(excinfo.value)
    assert excinfo.value.status == 0
    assert "'video_thumbnail_impressions'" in message  # the missing column, named exactly
    assert "date, video_id, video_thumbnail_impressions_ctr" in message  # the present columns, listed
    assert "http" not in message.lower()
    assert "reports.example" not in message


def test_fetch_reach_rows_strips_leading_utf8_bom():
    download_url = "https://reports.example/bom"
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,0.048\n"
    )
    # A leading UTF-8 BOM would, under a plain utf-8 decode, corrupt the first
    # header name so the "date" column is not found; utf-8-sig must strip it.
    raw = b"\xef\xbb\xbf" + csv_text.encode("utf-8")
    transport = FakeTransport(bytes_responses={download_url: raw})
    client = ReportingClient(transport)

    rows = client.fetch_reach_rows(download_url)

    assert rows == [
        {"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}
    ]


def test_transport_error_propagates_from_ensure_reach_job():
    transport = FakeTransport(get_responses={JOBS_URL: MetricsApiError(403, "Insufficient permission")})
    client = ReportingClient(transport)

    with pytest.raises(MetricsApiError):
        client.ensure_reach_job(JOB_NAME)


# --- defect 2: gzip-compressed report bodies must not crash the decoder ---


def test_download_report_text_decompresses_gzip_body():
    download_url = "https://reports.example/gz"
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,0.048\n"
    )
    compressed = gzip.compress(csv_text.encode("utf-8"))
    transport = FakeTransport(bytes_responses={download_url: compressed})
    client = ReportingClient(transport)

    text = client.download_report_text(download_url)

    assert text == csv_text


def test_fetch_reach_rows_handles_gzip_compressed_body():
    download_url = "https://reports.example/gz2"
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,0.048\n"
    )
    compressed = gzip.compress(csv_text.encode("utf-8"))
    transport = FakeTransport(bytes_responses={download_url: compressed})
    client = ReportingClient(transport)

    rows = client.fetch_reach_rows(download_url)

    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}]


def test_download_report_text_still_works_for_plain_uncompressed_body():
    # A non-gzip body's first two bytes almost never collide with the gzip
    # magic number, but nail it down with a real example so the sniff never
    # false-positives on ordinary CSV text.
    download_url = "https://reports.example/plain"
    csv_text = "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    assert client.download_report_text(download_url) == csv_text


# --- defect 3: a ctr column that is already a percent must be refused ---


def test_fetch_reach_rows_raises_when_ctr_already_looks_like_a_percent():
    download_url = "https://reports.example/badctr"
    # 4.8 treated as a 0..1 ratio becomes 480% after the x100 conversion - a
    # clear tell that the API is already handing back a percent, not a ratio.
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,4.8\n"
    )
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    with pytest.raises(MetricsApiError) as excinfo:
        client.fetch_reach_rows(download_url)

    message = str(excinfo.value)
    assert "percent" in message.lower()
    assert "http" not in message.lower()
    assert "reports.example" not in message


def test_fetch_reach_rows_allows_ctr_at_exactly_one_hundred_percent():
    # A boundary check: exactly 100% is a legitimate (if unusual) value and
    # must not trip the guard - only values that convert to MORE than 100%.
    download_url = "https://reports.example/boundary"
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,1.0\n"
    )
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    rows = client.fetch_reach_rows(download_url)

    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 100.0}]


# --- defect 4: empty-body edge cases ---


def test_fetch_reach_rows_header_only_csv_parses_to_empty_list():
    download_url = "https://reports.example/headeronly"
    csv_text = "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    assert client.fetch_reach_rows(download_url) == []


def test_fetch_reach_rows_raises_clean_error_on_zero_byte_body():
    download_url = "https://reports.example/empty"
    transport = FakeTransport(bytes_responses={download_url: b""})
    client = ReportingClient(transport)

    with pytest.raises(MetricsApiError) as excinfo:
        client.fetch_reach_rows(download_url)

    assert excinfo.value.status == 0
    assert "reports.example" not in str(excinfo.value)


# --- defect 5: ensure_reach_job create-race guard ---


def test_ensure_reach_job_warns_and_keeps_oldest_on_race_created_duplicate():
    jobs_after_post = {
        "jobs": [
            {"id": "job-mine", "reportTypeId": "channel_reach_basic_a1", "createTime": "2026-07-12T10:00:05Z"},
            {"id": "job-theirs", "reportTypeId": "channel_reach_basic_a1", "createTime": "2026-07-12T10:00:01Z"},
        ]
    }
    transport = FakeTransport(
        get_responses={JOBS_URL: SequentialResponses([{"jobs": []}, jobs_after_post])},
        post_responses={JOBS_URL: {"id": "job-mine", "reportTypeId": "channel_reach_basic_a1"}},
    )
    client = ReportingClient(transport)
    warnings = []

    job_id = client.ensure_reach_job(JOB_NAME, printer=warnings.append)

    assert job_id == "job-theirs"  # the older of the two, not the one just created
    assert len(warnings) == 1
    assert "job-theirs" in warnings[0]
    assert "API console" in warnings[0]
    assert "http" not in warnings[0].lower()


def test_ensure_reach_job_no_warning_when_only_one_job_exists_after_create():
    transport = FakeTransport(
        get_responses={
            JOBS_URL: SequentialResponses(
                [{"jobs": []}, {"jobs": [{"id": "job-new", "reportTypeId": "channel_reach_basic_a1"}]}]
            )
        },
        post_responses={JOBS_URL: {"id": "job-new", "reportTypeId": "channel_reach_basic_a1"}},
    )
    client = ReportingClient(transport)
    warnings = []

    job_id = client.ensure_reach_job(JOB_NAME, printer=warnings.append)

    assert job_id == "job-new"
    assert warnings == []


def test_ensure_reach_job_never_issues_a_delete_call():
    # No automatic cleanup: FakeTransport exposes no delete method at all, so
    # any attempt to delete a duplicate job would itself blow up the test.
    jobs_after_post = {
        "jobs": [
            {"id": "job-mine", "reportTypeId": "channel_reach_basic_a1", "createTime": "2026-07-12T10:00:05Z"},
            {"id": "job-theirs", "reportTypeId": "channel_reach_basic_a1", "createTime": "2026-07-12T10:00:01Z"},
        ]
    }
    transport = FakeTransport(
        get_responses={JOBS_URL: SequentialResponses([{"jobs": []}, jobs_after_post])},
        post_responses={JOBS_URL: {"id": "job-mine", "reportTypeId": "channel_reach_basic_a1"}},
    )
    client = ReportingClient(transport)

    client.ensure_reach_job(JOB_NAME, printer=lambda _msg: None)

    assert not hasattr(transport, "delete_calls")


# --- channel-identity guard support: channel_id is optional on parse -------

def test_parse_reach_csv_omits_channel_id_key_when_column_absent():
    # Older archived days (or any report body without the column) keep the exact
    # same 4-key row shape as before - the channel-identity cross-check treats a
    # missing key as "nothing to compare", never as a crash.
    download_url = "https://reports.example/nochannel"
    csv_text = (
        "date,video_id,video_thumbnail_impressions,video_thumbnail_impressions_ctr\n"
        "2026-07-01,vid001,1000,0.048\n"
    )
    transport = FakeTransport(bytes_responses={download_url: csv_text.encode("utf-8")})
    client = ReportingClient(transport)

    rows = client.fetch_reach_rows(download_url)

    assert rows == [{"date": "2026-07-01", "video_id": "vid001", "impressions": 1000, "ctr": 4.8}]
    assert "channel_id" not in rows[0]
