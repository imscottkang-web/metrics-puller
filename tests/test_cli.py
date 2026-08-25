"""End-to-end tests for the metrics CLI orchestration - fakes only, no network.

run_pull / run_setup_reach_job / dispatch take injected clients, so the whole
flow (registry -> reach aggregation -> analytics -> CSV) is exercised without
Google, credentials, or a socket. main() (which builds the real clients) is not
called here. metrics_lib.registry is real (these tests write real bet_card.md
fixtures); metrics_lib.reach_archive's gather_reach_rows is faked directly
(see _patch_reach_rows) - it has its own local-archive/ledger logic and its
own test suite, so these tests only care that metrics.py calls it correctly.
"""

import csv
from datetime import date

import pytest

import metrics as cli
from metrics_lib import snapshots as snapshots_mod
from metrics_lib.auth import REAUTH_MESSAGE, CredentialFileError, ReauthRequired
from metrics_lib.config import Config
from metrics_lib.http import MetricsApiError


class FakeReporting:
    """A minimal reporting-client double.

    metrics.py's only direct use of the reporting client is ensure_reach_job
    (for the setup-reach-job command, and once per pull via reach_archive).
    The reach-gathering step's own archiving/dedup logic lives in
    metrics_lib.reach_archive and is faked directly in these tests (see
    _patch_reach_rows) rather than driven through a simulated Reporting API -
    that module has its own test suite.
    """

    def __init__(self, job_id="job1", raise_reauth=False, raise_api_error=None):
        self.job_id = job_id
        self.raise_reauth = raise_reauth
        self.raise_api_error = raise_api_error
        self.ensure_calls = 0

    def ensure_reach_job(self, name="five-and-dime reach", printer=print):
        self.ensure_calls += 1
        if self.raise_reauth:
            raise ReauthRequired(REAUTH_MESSAGE)
        if self.raise_api_error:
            raise self.raise_api_error
        return self.job_id


class FakeAnalytics:
    def __init__(self, core=None, traffic=None, curve=None, fail_video_id=None, fail_exc=None):
        self.core = core if core is not None else {"views": 1200, "avg_view_duration_sec": 210}
        self.traffic = traffic if traffic is not None else {"browse": 55.0, "search": 30.0, "suggested": 10.0}
        self.curve = curve if curve is not None else [
            {"elapsed_ratio": 0.0, "audience_watch_ratio": 1.0, "relative_retention": None},
            {"elapsed_ratio": 0.1, "audience_watch_ratio": 0.8, "relative_retention": None},
        ]
        # When set, core_metrics raises fail_exc for exactly this video_id, so
        # tests can exercise per-video error isolation without a real API.
        self.fail_video_id = fail_video_id
        self.fail_exc = fail_exc if fail_exc is not None else MetricsApiError(500, "backend blip")

    def core_metrics(self, vid, start, end):
        if vid == self.fail_video_id:
            raise self.fail_exc
        return dict(self.core)

    def traffic_mix(self, vid, start, end):
        return dict(self.traffic)

    def retention_curve(self, vid, start, end):
        return [dict(p) for p in self.curve]


@pytest.fixture(autouse=True)
def _no_reach_rows_by_default(monkeypatch):
    """Default metrics_lib.reach_archive.gather_reach_rows to no rows.

    metrics.py delegates all reach-report gathering (impressions/CTR) to that
    module, which has its own local-archive/ledger logic and its own tests;
    these CLI tests are about run_pull's ORCHESTRATION, so this seam defaults
    to empty and a test that cares about specific numbers overrides it (see
    _patch_reach_rows).
    """
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows", lambda client, archive_dir, printer=print: [])


def _patch_reach_rows(monkeypatch, rows):
    monkeypatch.setattr(
        cli.reach_archive_mod, "gather_reach_rows", lambda client, archive_dir, printer=print: list(rows)
    )


def _config(tmp_path):
    root = tmp_path / "second-brain" / "tools" / "metrics"
    root.mkdir(parents=True)
    return Config(root=root, client_secret_path=root / "secrets" / "cs.json", token_path=root / "secrets" / "token.json")


def _write_bet_card(config, slug, video_id, date_str="2026-07-05", fmt="longform"):
    folder = config.scripts_dir / slug
    folder.mkdir(parents=True, exist_ok=True)
    folder.joinpath("bet_card.md").write_text(
        "# Bet Card\n\n| Field | Content |\n|---|---|\n"
        f"| date | {date_str} |\n| video id / slug | {video_id} / {slug} |\n| format | {fmt} |\n",
        encoding="utf-8",
    )
    return folder


def _rows(config):
    with config.snapshots_csv.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_pull_appends_one_row_for_published_video(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    _patch_reach_rows(monkeypatch, [
        {"video_id": "vidREAL01", "impressions": 1000, "ctr": 4.0, "date": "2026-07-06"},
        {"video_id": "vidREAL01", "impressions": 3000, "ctr": 6.0, "date": "2026-07-07"},
    ])
    summary = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    assert summary["appended"] == 1
    row = _rows(config)[0]
    assert row["video_id"] == "vidREAL01"
    assert row["slug"] == "seller-repairs"
    assert row["snapshot_date"] == "2026-07-12"
    assert row["checkpoint"] == "7d"  # published 07-05, run 07-12 -> age 7
    assert row["impressions"] == "4000"  # 1000 + 3000
    assert row["ctr"] == "5.5"  # impression-weighted
    assert row["views"] == "1200"
    assert row["avg_view_duration_sec"] == "210"
    assert row["traffic_browse"] == "55.0"
    assert row["retention_at_hook_end"] == ""  # no delivered_diff -> blank


def test_pull_skips_drafted_video_with_placeholder_id(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "draft-x", "<youtube id>")
    summary = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    assert summary["appended"] == 0
    # Still-drafted (no video id yet) is a healthy, informational state -
    # registry.py files it under "notes", never "warnings" (see registry.py).
    assert any("draft-x" in note for note in summary["registry_notes"])
    assert summary["registry_warnings"] == []
    assert not config.snapshots_csv.exists() or _rows(config) == []


def test_pull_archives_full_retention_curve(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    archived = config.retention_dir / "seller-repairs.csv"
    assert archived.is_file()
    with archived.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2  # the FakeAnalytics default curve has exactly two points
    assert rows[0] == {"elapsed_ratio": "0.0", "audience_watch_ratio": "1.0", "relative_retention": ""}
    assert rows[1] == {"elapsed_ratio": "0.1", "audience_watch_ratio": "0.8", "relative_retention": ""}


def test_pull_fills_retention_when_hookend_and_duration_known(tmp_path):
    config = _config(tmp_path)
    folder = _write_bet_card(config, "seller-repairs", "vidREAL01")
    # hook ends 0:10, length 1:40 (100s) -> ratio 0.10 -> curve value 0.8 -> 80.0%
    folder.joinpath("delivered_diff.md").write_text(
        "- **Real hook-end timestamp:** 0:10\n- **Video length:** 1:40\n", encoding="utf-8"
    )
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    assert _rows(config)[0]["retention_at_hook_end"] == "80.0"


def test_pull_retention_duration_parses_hours_in_video_length(tmp_path):
    config = _config(tmp_path)
    folder = _write_bet_card(config, "seller-repairs", "vidREAL01")
    # Same 100s duration as above, but written as H:MM:SS ("0:01:40") instead
    # of plain MM:SS - proves the hours-capable parser is actually wired in
    # (the old regex misread "H:MM:SS" strings, see metrics_lib.snapshots).
    folder.joinpath("delivered_diff.md").write_text(
        "- **Real hook-end timestamp:** 0:10\n- **Video length:** 0:01:40\n", encoding="utf-8"
    )
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    assert _rows(config)[0]["retention_at_hook_end"] == "80.0"


def test_pull_does_not_duplicate_a_milestone_across_days_in_its_window(tmp_path):
    # 7d window is ages 5-9. A daily cron lands in it several days running; the
    # "7d" row must be written once, not once per day.
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")
    s1 = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 11))  # age 6
    assert s1["appended"] == 1
    s2 = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))  # age 7
    assert s2["appended"] == 0  # "7d" already on file -> nothing added
    rows = _rows(config)
    assert len(rows) == 1
    assert rows[0]["checkpoint"] == "7d"
    assert rows[0]["snapshot_date"] == "2026-07-11"  # the first day in the window


def test_pull_records_weekly_continuity_only_after_the_gap(tmp_path):
    # Between milestones the checkpoint is "weekly"; a daily cron should add a
    # fresh row about once a week, not every day.
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-06-01")
    s1 = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 6, 20))  # age 19 -> baseline
    assert s1["appended"] == 1
    s2 = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 6, 23))  # +3 days -> skip
    assert s2["appended"] == 0
    s3 = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 6, 27))  # +7 days -> record
    assert s3["appended"] == 1
    assert len(_rows(config)) == 2


def test_pull_reports_only_the_videos_it_recorded(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")  # age 7 -> due
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")  # age 27 -> "30d" due
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    # Re-run same day: both already recorded -> nothing due, so nothing reported.
    summary = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12))
    assert summary["appended"] == 0
    assert summary["videos"] == []


def test_setup_reach_job_reports_job_id(tmp_path):
    config = _config(tmp_path)
    reporting = FakeReporting(job_id="job-XYZ")
    out = []
    rc = cli.dispatch(
        "setup-reach-job",
        config=config, reporting_client=reporting, analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 0
    assert reporting.ensure_calls == 1
    assert any("job-XYZ" in line for line in out)


def test_dispatch_reauth_prints_plain_english(tmp_path):
    config = _config(tmp_path)
    out = []
    rc = cli.dispatch(
        "setup-reach-job",
        config=config, reporting_client=FakeReporting(raise_reauth=True), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 2
    assert any(REAUTH_MESSAGE in line for line in out)  # the plain-English fix, not a traceback


# --- defect 1/2: per-video error isolation + dispatch exit codes ------------

def test_pull_isolates_one_videos_api_failure_from_the_rest(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")  # age 7 -> due
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")  # age 27 -> due
    analytics = FakeAnalytics(fail_video_id="vidOLD02", fail_exc=MetricsApiError(500, "backend blip"))
    summary = cli.run_pull(config, FakeReporting(), analytics, today=date(2026, 7, 12))
    assert summary["appended"] == 1
    assert summary["videos"] == ["seller-repairs"]
    assert summary["failures"] == [("old-video", "YouTube API error (status 500): backend blip")]
    rows = _rows(config)
    assert len(rows) == 1
    assert rows[0]["slug"] == "seller-repairs"


def test_pull_isolates_missing_field_key_error_with_readable_reason(tmp_path):
    # An HTTP-200 response missing an expected column surfaces as KeyError in
    # the analytics client's row["..."] reads, not MetricsApiError; it must be
    # isolated per-video the same way, with a readable reason instead of
    # KeyError's quoted-key repr.
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")  # age 7 -> due
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")  # age 27 -> due
    analytics = FakeAnalytics(fail_video_id="vidOLD02", fail_exc=KeyError("views"))
    summary = cli.run_pull(config, FakeReporting(), analytics, today=date(2026, 7, 12))
    assert summary["appended"] == 1
    assert summary["videos"] == ["seller-repairs"]
    assert summary["failures"] == [("old-video", "response missing expected field views")]
    rows = _rows(config)
    assert len(rows) == 1
    assert rows[0]["slug"] == "seller-repairs"


def test_pull_isolates_value_error_from_a_malformed_response(tmp_path):
    # int()/float() on a junk cell in an otherwise-200 response raises
    # ValueError in the analytics client - also per-video, never a whole-run
    # loss of every already-fetched row.
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")
    analytics = FakeAnalytics(
        fail_video_id="vidOLD02", fail_exc=ValueError("could not convert string to float: 'n/a'")
    )
    summary = cli.run_pull(config, FakeReporting(), analytics, today=date(2026, 7, 12))
    assert summary["appended"] == 1
    assert summary["failures"] == [("old-video", "could not convert string to float: 'n/a'")]


def test_dispatch_unexpected_error_prints_one_plain_line_and_exits_1(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def boom(scripts_dir):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli.registry_mod, "read_registry", boom)
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 1
    assert "Unexpected error: RuntimeError: boom" in out  # one plain line, never a traceback


def test_dispatch_pull_partial_failure_exits_4_and_prints_summary(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")
    analytics = FakeAnalytics(fail_video_id="vidOLD02", fail_exc=MetricsApiError(500, "backend blip"))
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=analytics,
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 4
    assert any(
        "WARNING: 1 video(s) not recorded this run" in line and "old-video" in line for line in out
    )
    # the video that DID succeed is still on file, not lost
    assert any(row["slug"] == "seller-repairs" for row in _rows(config))


def test_dispatch_pull_registry_warning_exits_4_even_with_no_due_videos(tmp_path):
    config = _config(tmp_path)
    # An invalid (non-YYYY-MM-DD) date is an ACTIONABLE registry problem
    # (registry.py's warnings, not notes) - never due, but still a red run.
    _write_bet_card(config, "bad-date-video", "vidBAD0001", date_str="not-a-date")
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 4
    assert any("not-a-date" in line for line in out)


def test_dispatch_pull_all_success_exits_0(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 0


def test_dispatch_credential_file_error_exits_2(tmp_path):
    config = _config(tmp_path)
    reporting = FakeReporting()

    def boom(name="five-and-dime reach", printer=print):
        raise CredentialFileError("client_secret.json is missing - see runbooks/metrics-api-setup.md")

    reporting.ensure_reach_job = boom
    out = []
    rc = cli.dispatch(
        "setup-reach-job", config=config, reporting_client=reporting, analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 2
    assert any("client_secret.json" in line for line in out)


# Adopted design decision: a reach-gather failure DEGRADES the run (views/
# retention/traffic still recorded, impressions/ctr blank, exit 4) instead of
# erasing the whole day with exit 3 - a missed 24h milestone is permanent.

def _reach_gather_boom(client, archive_dir, printer=print):
    raise MetricsApiError(500, "reach backend down")


def test_pull_degrades_to_blank_reach_when_gather_fails(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-07-05")  # age 7 -> due
    _write_bet_card(config, "old-video", "vidOLD02", date_str="2026-06-15")  # age 27 -> due
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows", _reach_gather_boom)
    out = []
    summary = cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12), out=out.append)
    assert summary["appended"] == 2  # BOTH rows written, not zero
    assert summary["reach_failure"] == "YouTube API error (status 500): reach backend down"
    rows = _rows(config)
    assert len(rows) == 2
    for row in rows:
        assert row["impressions"] == ""  # blank-until-known, not 0
        assert row["ctr"] == ""
        assert row["notes"] == "reach data unavailable this run"
        assert row["views"] == "1200"  # analytics data still recorded
    assert any("WARNING" in line and "reach backend down" in line for line in out)


def test_dispatch_pull_reach_failure_exits_4_and_names_the_reason(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows", _reach_gather_boom)
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 4
    assert any("WARNING" in line and "reach backend down" in line for line in out)
    # The snapshot row itself was still written this run.
    assert any(row["slug"] == "seller-repairs" for row in _rows(config))


def test_pull_reach_failure_note_joins_the_empty_curve_note(tmp_path, monkeypatch):
    # Both data-gap notes apply to the same row: they must be combined, not
    # one overwriting the other.
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows", _reach_gather_boom)
    cli.run_pull(config, FakeReporting(), FakeAnalytics(curve=[]), today=date(2026, 7, 12), out=lambda _line: None)
    assert _rows(config)[0]["notes"] == "retention curve empty this run; reach data unavailable this run"


def test_dispatch_setup_reach_job_top_level_api_error_exits_3(tmp_path):
    config = _config(tmp_path)
    reporting = FakeReporting(raise_api_error=MetricsApiError(503, "service unavailable"))
    out = []
    rc = cli.dispatch(
        "setup-reach-job", config=config, reporting_client=reporting, analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 3
    assert any("503" in line and "service unavailable" in line for line in out)


def test_dispatch_snapshot_error_exits_5(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")

    def boom(csv_path, rows, printer=print):
        raise snapshots_mod.SnapshotError(
            "weekly_snapshots.csv header does not match - found [...], expected [...]"
        )

    monkeypatch.setattr(cli.snapshots_mod, "append_snapshot", boom)
    out = []
    rc = cli.dispatch(
        "pull", config=config, reporting_client=FakeReporting(), analytics_client=FakeAnalytics(),
        today=date(2026, 7, 12), out=out.append,
    )
    assert rc == 5
    assert any("header does not match" in line for line in out)


# --- defect 3: retention archive must never be clobbered by an empty curve --

def test_pull_keeps_previous_retention_archive_when_curve_empty(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01", date_str="2026-06-01")
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 6, 20))  # baseline, real curve
    archived = config.retention_dir / "seller-repairs.csv"
    previous_content = archived.read_text(encoding="utf-8")
    assert "0.8" in previous_content  # sanity: the first run's real curve is on file

    # Second run, later: the API returns an EMPTY curve this time (a blip).
    cli.run_pull(config, FakeReporting(), FakeAnalytics(curve=[]), today=date(2026, 6, 27))  # +7 days -> due

    assert archived.read_text(encoding="utf-8") == previous_content  # untouched, not clobbered
    rows = _rows(config)
    assert rows[-1]["notes"] == "retention curve empty this run; kept previous archive"


def test_pull_writes_header_only_archive_and_note_when_curve_empty_and_no_previous(tmp_path):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    cli.run_pull(config, FakeReporting(), FakeAnalytics(curve=[]), today=date(2026, 7, 12))
    archived = config.retention_dir / "seller-repairs.csv"
    assert archived.is_file()
    with archived.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows == []  # header only, no data rows - nothing to lose
    assert _rows(config)[0]["notes"] == "retention curve empty this run"


# --- reach_archive call-site wiring ------------------------------------------

def test_run_pull_calls_reach_archive_with_config_dir_and_printer(tmp_path, monkeypatch):
    config = _config(tmp_path)
    _write_bet_card(config, "seller-repairs", "vidREAL01")
    calls = []

    def fake_gather(client, archive_dir, printer=print):
        calls.append((archive_dir, printer))
        return []

    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows", fake_gather)
    out = []
    cli.run_pull(config, FakeReporting(), FakeAnalytics(), today=date(2026, 7, 12), out=out.append)
    assert len(calls) == 1
    archive_dir, printer = calls[0]
    assert archive_dir == config.reach_reports_dir
    assert printer == out.append  # bound methods compare equal even though `is` would not
