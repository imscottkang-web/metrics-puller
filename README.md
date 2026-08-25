# Metrics puller

A read-only tool that fills in a channel's own weekly numbers CSV from that channel's private YouTube data.

This tool is shared: it can serve any number of YouTube channels, and it is never told to favor one over another.
It does not know or store which channel it "belongs to" - every channel that uses it (we call each one a "house") hands it that house's own folder of settings and secrets, every time it runs.
That is on purpose: if two houses ever shared one saved sign-in file, one house could quietly start pulling the wrong channel's numbers, or silently go dead, with nothing on screen saying so.
Keeping each house in its own folder, with its own sign-in, is what rules that out.

- Impressions and click-through rate (the share of people shown the thumbnail who clicked) come from the YouTube Reporting API's reach report, per video.
- Views, average view duration, retention (the share of viewers still watching at a point in the video), and traffic mix come from the YouTube Analytics API.
- One read-only sign-in scope covers both: `yt-analytics.readonly`. No write access, no revenue data.

## Layout

- `metrics_lib/` - the package: settings, sign-in, the two API clients, the local video registry, the snapshot writer.
- `metrics.py` - the command-line entry point (`setup-reach-job`, `pull`).
- `tests/` - the test-first suite; no network, fake responses only.

Nothing under this folder is specific to any one channel. Everything channel-specific - secrets, saved sign-in, settings, the data files this tool writes - lives outside this folder, in the house's own folder (see below).

## What a house is, and what it must set up

A "house" is one channel's own folder, holding:

- `.env` - that channel's settings (see the table below).
- `secrets/` - that channel's downloaded OAuth client-secret file and its saved sign-in. Never committed; keep this folder out of version control.

Every setting below is read from that `.env` file first, then from the environment, so a local run can just keep a `.env` file in the house folder while a cloud job (which has no `.env` file) still passes settings in as repository secrets.

| Setting | Required? | What it is for |
|---|---|---|
| `METRICS_HOUSE` | Yes, unless the code calling this tool passes the house folder in directly | The full path to this house's own folder (the one holding `.env` and `secrets/`). There is no default - the tool refuses to run without it. |
| `METRICS_DATA_DIR` | Yes | Where this house's weekly snapshot CSV, retention curves, reach-report archive, and published-video list get written. A relative path is resolved against the house folder; an absolute path is used as-is. |
| `METRICS_SCRIPTS_DIR` | Yes | This house's folder of video script folders (each one holding a `bet_card.md`), which is how the tool knows which videos to record numbers for. Same relative/absolute rule as above. |
| `METRICS_REACH_JOB_NAME` | Yes | The name given to this house's reach-report job inside the YouTube Reporting API, so this house's job can be told apart from any other house's job in the Google API console. |
| `YT_CHANNEL_ID` | Yes | This house's own YouTube channel id (starts with `UC`, found on the channel's "About" page or in YouTube Studio under Settings > Channel > Advanced settings). This is how the tool knows the saved sign-in in `secrets/` really belongs to this house's channel and not some other channel's - see "Safety checks" below. Also turns on the published-video list when `YOUTUBE_API_KEY` is set too. |
| `YT_OAUTH_CLIENT_SECRET` | No | Full path to the downloaded OAuth "Desktop app" client-secret file. If unset, the tool looks for `secrets/client_secret.json` inside the house folder. |
| `YOUTUBE_API_KEY` | No | A public YouTube Data API key. Combined with `YT_CHANNEL_ID`, this lets the tool ask the channel what it has publicly posted, and write that list down for other tools to match new videos against their script folders. |

If `METRICS_HOUSE` (or an equivalent path passed in directly) is missing, the tool refuses to run at all and prints a plain-English explanation of what to set.
If any of the four required settings above is missing, the tool refuses the same way, naming exactly which setting is missing and what it is for.
There is deliberately no shared fallback location for any of these - a shared default is exactly the mix-up this tool is built to avoid.

For every setting above, this house's own `.env` file is checked before the environment - so a value left exported in a shell from testing one house cannot leak into a different house's run. The one exception is a cloud job with no `.env` file at all, which simply gets every value from the environment (its repository secrets), exactly as before.

## Wiring up a house

1. Create the house's folder anywhere you like (it does not need to live near this tool).
2. Add a `.env` file there with `METRICS_DATA_DIR`, `METRICS_SCRIPTS_DIR`, `METRICS_REACH_JOB_NAME`, and `YT_CHANNEL_ID` set (and `YOUTUBE_API_KEY` too if you want the published-video list).
3. Download the OAuth "Desktop app" client secret from Google Cloud and save it at `secrets/client_secret.json` inside the house folder (or point `YT_OAUTH_CLIENT_SECRET` at wherever you saved it).
4. Point this tool at the house folder - either set `METRICS_HOUSE` to its full path before running, or pass the path in directly if you are calling this from code.
5. Run `python3 metrics.py setup-reach-job` once, early - impressions and click-through rate only start accruing from the moment this job is created; they never backfill.
6. Run `python3 metrics.py pull` on whatever schedule you like (daily is normal). The first run opens a one-time browser consent screen; the saved sign-in is then reused (and refreshed automatically) after that.

## Safety checks

Because this tool is shared, every house must set `YT_CHANNEL_ID`, and the tool uses it to check that the saved sign-in it is about to use is really for that channel:

- Every real API call is addressed to this house's own channel id, never just "whichever account this sign-in happens to be for". A sign-in for the wrong channel gets refused by YouTube rather than quietly handing back someone else's numbers.
- Before writing anything at all (or, for `setup-reach-job`, before creating anything), the tool also makes one small check call and cross-checks the channel id already recorded in this house's own saved reach reports. If either check finds the sign-in does not match this house's channel id, the run stops immediately, nothing is written or created, and a plain-English message explains what to fix (delete the saved sign-in and sign in again as the right account - or, if the sign-in really is right, check the channel's API access and permissions).
- If the small check call cannot reach YouTube at all (for any reason other than a flat refusal), that is reported in one line and the tool falls back to the saved-reach-report cross-check above rather than failing the whole run over a blip.

A house that has not set `YT_CHANNEL_ID` cannot run this tool at all - see the settings table above.

## Credentials

Claude never handles the Google password or the contents of any secret file - only file paths are ever named in this tool's output, never file contents.

## Run

- `python3 metrics.py setup-reach-job` - create the reach-report job once, early.
- `python3 metrics.py pull` - append one snapshot row per due video to the weekly CSV, and write down what the channel says it has published.

Status: test-first build, shared across houses since 2026-08-25.
