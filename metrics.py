"""Part C metrics puller - command-line entrypoint.

Two commands:
  setup-reach-job  Create the reach-report job once, early. Impressions and
                   click-through rate (the share of people shown the thumbnail
                   who clicked) only start accruing from job creation and do not
                   backfill, so run this as soon as sign-in is done.
  pull             Read each project video's numbers and append one row per
                   video to data/metrics/weekly_snapshots.csv, and write down
                   what the channel says it has published so the channel
                   watcher can match a new video back to its script folder.

The orchestration functions (run_pull, run_setup_reach_job, dispatch) take
injected clients so they are testable with fakes; main() builds the real
read-only clients. This module never handles the token itself - only the
Transport does - so nothing here can print or log a secret.

Channel-identity guard (the thing this must never let happen): this tool is
shared by more than one YouTube channel ("house"). Each house is handed its
own house_dir (see metrics_lib.config), and that separation - one house, one
folder, one saved sign-in file - is the PRIMARY defence against a house
running with the wrong channel's numbers. Every house must also be told its
own channel id (YT_CHANNEL_ID - see metrics_lib.config.load_config, which
refuses to run without it), which adds a second net for the day the primary
defence is somehow bypassed anyway (e.g. a token file copied by hand into the
wrong house's secrets/ folder): before anything is read for real or written
to the CSV, it checks that the saved sign-in actually belongs to the channel
this house was configured with (Config.channel_id). Two independent checks,
because either alone can miss a mismatch:
  - Net A: every real Analytics API call is addressed to this house's
    specific channel id (see metrics_lib.analytics.AnalyticsClient), never
    "whichever channel this token happens to be signed in as" - so a wrong
    token gets refused by YouTube (403) instead of happily returning
    someone else's numbers.
  - Net B: a preflight (run before run_pull's per-video loop, and before
    setup-reach-job creates anything) makes one cheap probe call and, when
    that is not conclusive (e.g. the API cannot be reached), also
    cross-checks the channel id already recorded in this house's own
    locally archived reach reports. Either check failing raises
    ChannelMismatch, which stops the run before any row is written or any
    job is created.

Exit codes (returned by dispatch(), and therefore by main()):
  0  ok - everything requested completed with no failures or warnings.
  1  unknown command, or an unexpected internal error (reported as one plain
     "Unexpected error: <type>: <message>" line, never a raw traceback).
  2  auth/credential problem (ReauthRequired, CredentialFileError, or
     ChannelMismatch - the saved sign-in is not for this house's channel) - a
     plain-English fix is printed; never a raw traceback.
  3  a top-level API failure - a MetricsApiError that stopped the whole
     command (e.g. setup-reach-job could not reach the API), so nothing could
     be salvaged this run. A reach-gather failure during `pull` is NOT this:
     it degrades to exit 4 (see below) rather than erasing the day.
  4  partial data this run - the `pull` command wrote every snapshot row it
     could, but at least one due video's fetch failed (see the per-video "not
     recorded" summary), and/or reach data (impressions/CTR) was unavailable
     so those cells are blank, and/or the channel's published-video list could
     not be read (the previous list is kept), and/or the registry reported
     warnings about a bet card it had to skip. Re-run to retry what was missed.
     NOT this: no Data API key configured at all, so the published-video
     list is simply not built. That is a setup state rather than a failure,
     and it stays green.
  5  a snapshot schema problem (SnapshotError) - weekly_snapshots.csv is not
     shaped the way the writer expects (e.g. a stale or hand-edited header);
     no row is appended until the file is fixed.
"""

import argparse
import csv
import os
import sys
from datetime import date

from metrics_lib import checkpoints as checkpoints_mod
from metrics_lib import reach_archive as reach_archive_mod
from metrics_lib import registry as registry_mod
from metrics_lib import snapshots as snapshots_mod
from metrics_lib import uploads as uploads_mod
from metrics_lib.analytics import AnalyticsClient
from metrics_lib.auth import CredentialFileError, OAuthTokenProvider, ReauthRequired
from metrics_lib.config import Config, HouseNotConfigured, MissingSetting, load_config
from metrics_lib.http import ApiKeyTransport, MetricsApiError, Transport
from metrics_lib.reporting import ReportingClient

# Analytics backfills from full history, so a wide start date is safe; the real
# window is start_date..today. YouTube clamps to when data actually exists.
DEFAULT_START_DATE = "2005-01-01"


class ChannelMismatch(Exception):
    """The saved sign-in is not for the channel this house was configured with.

    See the module docstring's "Channel-identity guard" section for the two checks
    that raise this. str() names Config.channel_id, Config.house_dir, and
    Config.token_path (the PATH only, never file contents) and the fix: delete the
    saved sign-in and sign in again as the right account.
    """


def _channel_mismatch_message(config: Config) -> str:
    return (
        "This saved sign-in was refused for the YouTube channel this house was "
        f"configured with (channel id {config.channel_id}). Two things can cause that: "
        "the sign-in belongs to a different channel, or this channel's own sign-in has "
        "lost access (a revoked permission, or the API turned off for this project) - "
        "both look the same from here. Refusing to pull any numbers with it - "
        "continuing could record another channel's numbers under this house's name, or "
        "mean two houses are sharing one sign-in without anyone noticing. Nothing was "
        "written this run. "
        f"Fix: delete the saved sign-in file at {config.token_path} and run this tool "
        f"locally for the house at {config.house_dir} to sign in again, making sure to "
        "pick the right channel's account when the browser asks; if that does not help, "
        "check the channel's API access and permissions in the Google Cloud project."
    )


def _check_channel_identity(config: Config, analytics_client, *, today, out=print) -> None:
    """Net A is the shape of every real Analytics call (see AnalyticsClient); this is
    Net B - a preflight, run before anything else in run_pull (and before
    run_setup_reach_job creates anything), so a mismatch is caught before any row is
    written or any job is created, rather than partway through the per-video loop.

    Two independent checks, because either one alone can miss a mismatch:
      - a live probe call, targeted at config.channel_id; a 403 means the token is for
        a different channel (or has lost access to this one - see
        _channel_mismatch_message);
      - the channel id already recorded in this house's own locally archived reach
        reports (metrics_lib.reach_archive.read_archived_reach_rows) - this one keeps
        working even when the API cannot be reached at all, so a probe call that fails
        for any OTHER reason (not a 403) does not abort the run: it is reported in one
        plain line and this second check still runs. A genuine total API outage still
        fails later, in the per-video loop, exactly as it did before this preflight
        existed.
    config.channel_id is always set (metrics_lib.config.load_config refuses to build a
    Config without one), so both checks always run.
    """
    try:
        analytics_client.probe_channel(today.isoformat())
    except MetricsApiError as exc:
        if exc.status == 403:
            raise ChannelMismatch(_channel_mismatch_message(config)) from None
        out(
            f"Could not reach YouTube to check the sign-in this run ({exc.status} "
            f"{exc.reason}). Falling back to the local archive cross-check."
        )

    archived_rows = reach_archive_mod.read_archived_reach_rows(config.reach_reports_dir)
    seen_channels = {row["channel_id"] for row in archived_rows if row.get("channel_id")}
    if archived_rows and not seen_channels:
        out(
            "Could not cross-check the sign-in against the local reach-report archive: "
            "none of the archived reports include a channel id column, so this half of "
            "the check could not be made. Every number below is unaffected."
        )
    elif seen_channels and config.channel_id not in seen_channels:
        raise ChannelMismatch(_channel_mismatch_message(config))


def run_setup_reach_job(reporting_client, name, *, printer=print):
    """Create (once, idempotently) the reach-report job; return its id.

    name is required (no default): this tool is shared by more than one channel, and
    a shared default job name is exactly the kind of collision this build removes.
    Callers pass the house's own Config.reach_job_name.
    """
    return reporting_client.ensure_reach_job(name, printer=printer)


def _archive_curve(config, slug, curve):
    """Write the full retention curve to data/metrics/retention/<slug>.csv.

    Always archived so no curve data is ever lost, even when the snapshot's
    retention_at_hook_end cell stays blank for want of a hook-end timestamp.

    An EMPTY curve (an API blip, or a privacy-thresholded video) never
    overwrites a previously archived NON-empty curve: the existing file is
    left untouched and a note is returned for the caller to attach to that
    video's snapshot row, instead of silently replacing good history with a
    header-only file. When there is no existing archive yet, an empty curve
    still produces the header-only file (there is nothing to lose), with its
    own note. Every write goes to a temp file in the same directory, then
    os.replace()'s it into place, so a crash mid-write can never leave a
    truncated or corrupt archive - only the old file or the complete new one.

    Returns the note to attach to the row's "notes" column - "" when the
    curve was non-empty (nothing noteworthy happened).
    """
    config.retention_dir.mkdir(parents=True, exist_ok=True)
    path = config.retention_dir / f"{slug}.csv"

    if not curve and path.is_file() and path.stat().st_size > 0:
        return "retention curve empty this run; kept previous archive"

    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["elapsed_ratio", "audience_watch_ratio", "relative_retention"])
        for point in curve:
            writer.writerow(
                [point.get("elapsed_ratio"), point.get("audience_watch_ratio"), point.get("relative_retention")]
            )
    os.replace(tmp_path, path)
    return "retention curve empty this run" if not curve else ""


def _duration_seconds(delivered_diff_path):
    """Read the video's total length (MM:SS or H:MM:SS) from a delivered_diff.md, if noted.

    Returns whole seconds, or None. The hook-end timestamp is a ratio along the
    video, so converting it to retention needs the total length; when the length
    is not recorded, retention_at_hook_end stays blank (the curve is still
    archived either way).
    """
    if not delivered_diff_path.is_file():
        return None
    for line in delivered_diff_path.read_text(encoding="utf-8").splitlines():
        low = line.lower()
        if "length" in low or "duration" in low:
            seconds = snapshots_mod.parse_clock_seconds(line)
            if seconds is not None:
                return seconds
    return None


def _retention_pct(config, entry, curve):
    """% remaining at the real hook end, or None when it cannot be computed yet."""
    delivered_diff = config.scripts_dir / entry["slug"] / "delivered_diff.md"
    hook_end = snapshots_mod.parse_hook_end_seconds(delivered_diff)
    duration = _duration_seconds(delivered_diff)
    if hook_end is None or duration is None:
        return None
    return snapshots_mod.retention_at_hook_end(curve, hook_end, duration)


def _failure_reason(exc):
    """One readable line for a per-video fetch failure - never a bare repr.

    A KeyError's str() is just the missing key in quotes (e.g. "'views'"),
    which reads like noise in a warning line, so it is spelled out as the
    response-shape problem it actually is.
    """
    if isinstance(exc, KeyError):
        key = exc.args[0] if exc.args else "?"
        return f"response missing expected field {key}"
    return str(exc)


def _videos_due(entries, history, today):
    """Pick the videos to record today, each paired with its checkpoint label.

    Runs daily but records sparingly: a milestone (24h/7d/30d/90d/180d) once,
    plus a weekly-continuity row between milestones. See checkpoints.should_record.
    """
    due = []
    for entry in entries:
        checkpoint = (
            checkpoints_mod.checkpoint_for(entry["publish_date"], today)
            if entry["publish_date"]
            else "weekly"
        )
        past = history.get(entry["video_id"], {})
        if checkpoints_mod.should_record(
            checkpoint, past.get("checkpoints", set()), past.get("last_date"), today
        ):
            due.append((entry, checkpoint))
    return due


def write_published_video_list(config, uploads_client, *, today, out=print):
    """Write down what the channel says it has published, for the channel watcher to match.

    This is the wire half of audit-repair 05. The thing that already checks the channel every
    day is this pull, so this is where the channel's own list of published videos gets
    written down; the matching itself lives beside the script folders, in the shared script
    tools, and is blind to whose channel this is.

    Three ways it can end, and only one of them is a problem:

    * no client at all - no public Data API key is configured (the channel id always is;
      see metrics_lib.config.load_config), so there is nothing to ask with. One plain
      line, no row lost, the run stays green. A feature nobody has switched on yet must
      never turn a daily job red.
    * the call fails - degraded exactly like the reach step: say so, keep the previous list
      (the file is not touched), and let the caller count it toward a partial run.
    * it worked - the list is rewritten with today's date on it.

    Returns the failure reason, or None.
    """
    if uploads_client is None:
        out("No published-video list this run: no public Data API key is configured, so "
            "nothing asked the channel what it has posted. Every number above is unaffected.")
        return None
    try:
        videos = uploads_mod.gather_published_videos(uploads_client, config.channel_id)
    except (MetricsApiError, KeyError, ValueError, TypeError) as exc:
        # Never erase the day's list on a blip: the previous file is left exactly as it was,
        # so the watcher goes on matching against the last good list rather than against
        # nothing, which would read as a channel that had suddenly published nothing at all.
        reason = str(exc)
        out(f"WARNING: could not read the channel's published videos this run: {reason}. "
            "The previous list is untouched.")
        return reason
    uploads_mod.write_published_videos(
        config.published_videos_json, videos, pulled_on=today.isoformat())
    out(f"Wrote {len(videos)} published video(s) to {config.published_videos_json.name} "
        "for the channel watcher to match against.")
    return None


def run_pull(config, reporting_client, analytics_client, *, today, start_date=DEFAULT_START_DATE, out=print, uploads_client=None):
    """Append snapshot rows for the videos DUE today. Returns a summary dict.

    The cron runs every day so no milestone is missed, but only the videos due
    today are recorded (a milestone once, plus a roughly weekly row in between).
    Only videos with a real bet card are read (the allowlist); a channel video
    with no bet card is never touched. When a video is recorded, its retention
    curve is archived; retention_at_hook_end is filled only when the hook-end
    timestamp and the video length are both known. On a day when nothing is due,
    no API data call is made and no row is written.

    Per-video isolation: one video's fetch failing does not lose the other due
    videos' rows. Because a milestone's window is narrow (e.g. the 24h
    checkpoint is age 0-1 days), a run lost to one flaky call could
    permanently miss that milestone, so the failing video's (slug, reason) is
    recorded in the returned "failures" list and the loop moves on; every
    video that DID succeed is still written via append_snapshot before this
    function returns. The catch covers MetricsApiError AND the shape errors an
    HTTP-200-but-malformed response raises inside the analytics client's
    row["..."] / int(...) / float(...) reads (KeyError, ValueError, TypeError)
    - those must not defeat the isolation either.

    Reach degradation: the once-per-run reach-report gathering step failing
    (a MetricsApiError from reach_archive.gather_reach_rows) does NOT abort
    the pull - views/retention/traffic are still recorded for every due
    video, with impressions and ctr left blank (the blank-until-known
    convention, never 0) and the note "reach data unavailable this run" on
    each row written. The reason is returned as "reach_failure" (else None)
    and counts toward dispatch's exit code 4.

    The registry's plain-English "notes" (informational - e.g. a still-drafted
    bet card) and "warnings" (actionable - e.g. a bad date, a duplicate video
    id) are passed through unchanged as "registry_notes" / "registry_warnings".

    Returns {"appended", "videos" (slugs actually written), "registry_notes",
    "registry_warnings", "failures" (list of (slug, reason)),
    "reach_failure" (str or None), "uploads_failure" (str or None - the
    channel's published-video list could not be read this run)}.
    """
    _check_channel_identity(config, analytics_client, today=today, out=out)

    registry_result = registry_mod.read_registry(config.scripts_dir)
    entries = registry_result.entries
    registry_notes = list(registry_result.notes)
    registry_warnings = list(registry_result.warnings)

    history = snapshots_mod.read_video_history(config.snapshots_csv)
    due = _videos_due(entries, history, today)
    # Written BEFORE the due check, not after. The day a video is first published is exactly
    # a day when nothing is due for it - it has no bet card yet, so the registry has never
    # heard of it - and that is the one day this list has to be right.
    uploads_failure = write_published_video_list(config, uploads_client, today=today, out=out)
    if not due:
        return {
            "appended": 0,
            "videos": [],
            "registry_notes": registry_notes,
            "registry_warnings": registry_warnings,
            "failures": [],
            "reach_failure": None,
            "uploads_failure": uploads_failure,
        }

    end_date = today.isoformat()
    reach_rows = []
    reach_failure = None
    try:
        reach_rows = reach_archive_mod.gather_reach_rows(
            reporting_client, config.reach_reports_dir, config.reach_job_name, printer=out)
    except MetricsApiError as exc:
        # Degrade, do not erase the day: views/retention/traffic can still be
        # recorded for every due video, and a missed 24h milestone window
        # would be permanent. Impressions/ctr stay blank (never 0) below.
        reach_failure = str(exc)
        out(
            f"WARNING: reach data (impressions/CTR) unavailable this run: {reach_failure}. "
            "Still recording views/retention/traffic; impressions and ctr left blank."
        )

    out_rows = []
    failures = []
    for entry, checkpoint in due:
        video_id = entry["video_id"]
        try:
            if reach_failure is None:
                reach = snapshots_mod.aggregate_reach(reach_rows, video_id)
            else:
                # Blank-until-known, not zero: 0 impressions would read as a
                # real measurement ("nobody was shown it") instead of a gap.
                reach = {"impressions": None, "ctr": None}
            core = analytics_client.core_metrics(video_id, start_date, end_date)
            traffic = analytics_client.traffic_mix(video_id, start_date, end_date)
            curve = analytics_client.retention_curve(video_id, start_date, end_date)
            note_parts = []
            archive_note = _archive_curve(config, entry["slug"], curve)
            if archive_note:
                note_parts.append(archive_note)
            if reach_failure is not None:
                note_parts.append("reach data unavailable this run")
            out_rows.append(
                snapshots_mod.assemble_row(
                    snapshot_date=end_date,
                    checkpoint=checkpoint,
                    entry=entry,
                    reach=reach,
                    core=core,
                    traffic=traffic,
                    retention_pct=_retention_pct(config, entry, curve),
                    notes="; ".join(note_parts),
                )
            )
        except (MetricsApiError, KeyError, ValueError, TypeError) as exc:
            failures.append((entry["slug"], _failure_reason(exc)))

    config.snapshots_csv.parent.mkdir(parents=True, exist_ok=True)
    appended = snapshots_mod.append_snapshot(config.snapshots_csv, out_rows, printer=out)
    return {
        "appended": appended,
        "videos": [row["slug"] for row in out_rows],
        "registry_notes": registry_notes,
        "registry_warnings": registry_warnings,
        "failures": failures,
        "reach_failure": reach_failure,
        "uploads_failure": uploads_failure,
    }


def dispatch(command, *, config, reporting_client, analytics_client, today, out=print,
             uploads_client=None):
    """Run one command with injected clients. Returns a process exit code.

    See the module docstring for the full exit-code table.
    """
    try:
        if command == "setup-reach-job":
            # This command CREATES a report job on YouTube - a write, not a read - so the
            # same channel-identity check run_pull does before writing anything runs here
            # too, before the job is created, not after.
            _check_channel_identity(config, analytics_client, today=today, out=out)
            job_id = run_setup_reach_job(reporting_client, config.reach_job_name, printer=out)
            out(f"Reach-report job ready (id: {job_id}).")
            out("Impressions and click-through rate start accruing from now; they do not backfill.")
            return 0
        if command == "pull":
            summary = run_pull(config, reporting_client, analytics_client, today=today, out=out,
                               uploads_client=uploads_client)
            out(f"Appended {summary['appended']} snapshot row(s) for: {', '.join(summary['videos']) or '(none)'}.")
            for line in summary["registry_notes"]:
                out(line)
            for line in summary["registry_warnings"]:
                out(line)
            if summary["failures"]:
                detail = ", ".join(f"{slug} ({reason})" for slug, reason in summary["failures"])
                out(f"WARNING: {len(summary['failures'])} video(s) not recorded this run: {detail}.")
            if (summary["failures"] or summary["registry_warnings"]
                    or summary["reach_failure"] or summary.get("uploads_failure")):
                return 4
            return 0
        out(f"Unknown command: {command}")
        return 1
    except (ReauthRequired, CredentialFileError, ChannelMismatch) as exc:
        out(str(exc))
        return 2
    except MetricsApiError as exc:
        out(f"API error: {exc.status} {exc.reason}")
        return 3
    except snapshots_mod.SnapshotError as exc:
        out(str(exc))
        return 5
    except Exception as exc:
        # The last line of defense at the process boundary: a bug or an
        # unanticipated failure still comes out as one plain line the cron
        # log can show a human, never a raw traceback.
        out(f"Unexpected error: {type(exc).__name__}: {exc}")
        return 1


def build_clients(config):
    """Build the real read-only clients over an OAuth transport (needs Google libs).

    The uploads client is the odd one out and is None unless a public Data API key is
    configured - it reads the PUBLIC listing with a key rather than the private numbers
    with the OAuth token. (The channel id it also needs is always present - see
    metrics_lib.config.load_config - so the key is the only thing that gates it now.)
    """
    transport = Transport(OAuthTokenProvider(config), timeout=config.timeout)
    uploads_client = None
    if config.api_key:
        uploads_client = uploads_mod.UploadsClient(
            ApiKeyTransport(config.api_key, timeout=config.timeout))
    return ReportingClient(transport), AnalyticsClient(transport, config.channel_id), uploads_client


def main(argv=None):
    parser = argparse.ArgumentParser(prog="metrics", description="Read-only YouTube metrics puller (Part C).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup-reach-job", help="Create the reach-report job once, early (do this first).")
    sub.add_parser("pull", help="Append one snapshot row per project video to weekly_snapshots.csv.")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except (HouseNotConfigured, MissingSetting) as exc:
        # Same family as the auth/credential refusals dispatch() handles below: this
        # tool cannot even start without knowing which house it is running as, so it
        # is caught here (before dispatch(), which needs a Config to run at all) and
        # printed the same plain-English way - never a raw traceback.
        print(str(exc))
        return 2
    reporting_client, analytics_client, uploads_client = build_clients(config)
    return dispatch(
        args.command,
        config=config,
        reporting_client=reporting_client,
        analytics_client=analytics_client,
        uploads_client=uploads_client,
        today=date.today(),
    )


if __name__ == "__main__":
    sys.exit(main())
