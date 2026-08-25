# Part C - Metrics Puller

Read-only tool that fills `../../data/metrics/weekly_snapshots.csv` from the channel's own private YouTube numbers.

Decided 2026-07-23 (walls ticket 11): this puller is private-to-house automation on purpose - it is welded to this channel's own OAuth grant and this repo's private daily GitHub Action, so the tool stays in the house and only the generic setup runbook lives on the neutral `shared-brain/` shelf.

- Impressions + click-through rate (of the people shown the thumbnail, the share who clicked) come from the YouTube Reporting API reach report `channel_reach_basic_a1`, per video.
- Views, average view duration, retention (share still watching at a point in the video), and traffic mix come from the YouTube Analytics API.
- One read-only sign-in scope covers both: `yt-analytics.readonly`. No write access, no revenue data.

## Layout
- `metrics_lib/` - the package (config, auth, API clients, registry, snapshot writer).
- `metrics.py` - the command-line entrypoint (`setup-reach-job`, `pull`).
- `tests/` - the test-first suite; no network, fake responses only.
- `secrets/` - gitignored; holds the downloaded `client_secret.json` and the cached `token.json`. Never committed.

## Credentials
The OAuth "Desktop app" client secret (downloaded from Google Cloud per the workspace-level runbook `../../../../../shared-brain/runbooks/metrics-api-setup.md`) lives at `secrets/client_secret.json` by default, or point `YT_OAUTH_CLIENT_SECRET` at it in a local `.env`.
The one-time browser consent happens on first real run. Claude never handles the Google password or the secret file contents.

## Run
- `python3 metrics.py setup-reach-job` - create the reach-report job once, early. Impressions/CTR only accrue from job creation; they do not backfill.
- `python3 metrics.py pull` - append one snapshot row per video to the CSV.

Status: under construction (test-first build, started 2026-07-12). Not yet runnable end to end.
