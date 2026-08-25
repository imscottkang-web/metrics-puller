"""The wire half of the channel watcher: what the channel says it has published.

Fakes only, no network. The matching itself is not tested here and does not live here - it
lives beside the script folders, in the shared script tools, and is blind to whose channel
this is. This file proves only that the daily pull learns the channel's published videos and
writes them down in the shape the matcher agreed to read.
"""

import json
from datetime import date

import pytest

import metrics as cli
from metrics_lib import uploads as uploads_mod
from metrics_lib.config import Config
from metrics_lib.http import ApiKeyTransport, MetricsApiError

CHANNEL_ID = "UCanchorandivydatafake"
PLAYLIST_ID = "UUanchorandivydatafake"


class FakeTransport:
    """Answers the two Data API calls off canned bodies, and remembers what it was asked."""

    def __init__(self, bodies=None, raise_exc=None):
        self.bodies = bodies or {}
        self.raise_exc = raise_exc
        self.calls = []

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if self.raise_exc:
            raise self.raise_exc
        for fragment, body in self.bodies.items():
            if url.endswith(fragment):
                return body
        return {}


def _channels_body(uploads=PLAYLIST_ID):
    return {"items": [{"contentDetails": {"relatedPlaylists": {"uploads": uploads}}}]}


def _playlist_body(rows):
    return {"items": [
        {"snippet": {"title": title, "resourceId": {"videoId": video_id}},
         "contentDetails": {"videoId": video_id, "videoPublishedAt": published_at}}
        for video_id, title, published_at in rows
    ]}


def _config(tmp_path, **extra):
    root = tmp_path / "second-brain" / "tools" / "metrics"
    root.mkdir(parents=True)
    return Config(root=root, client_secret_path=root / "secrets" / "cs.json",
                  token_path=root / "secrets" / "token.json", **extra)


# ---------------------------------------------------------------------------
# Reading the channel
# ---------------------------------------------------------------------------


def test_the_uploads_playlist_is_read_off_the_channel():
    transport = FakeTransport({"/channels": _channels_body()})

    assert uploads_mod.UploadsClient(transport).uploads_playlist(CHANNEL_ID) == PLAYLIST_ID
    url, params = transport.calls[0]
    assert params == {"part": "contentDetails", "id": CHANNEL_ID}


def test_a_channel_that_is_not_there_answers_none_rather_than_crashing():
    transport = FakeTransport({"/channels": {"items": []}})

    assert uploads_mod.UploadsClient(transport).uploads_playlist(CHANNEL_ID) is None


def test_each_published_video_carries_its_id_title_and_the_day_it_went_out():
    transport = FakeTransport({"/playlistItems": _playlist_body([
        ("dQw4w9WgXcQ", "Never hire a listing agent who says these 5 things",
         "2026-08-24T15:00:00Z"),
    ])})

    videos = uploads_mod.UploadsClient(transport).published_videos(PLAYLIST_ID)

    assert videos == [{"video_id": "dQw4w9WgXcQ",
                       "title": "Never hire a listing agent who says these 5 things",
                       "published_on": "2026-08-24"}]


def test_the_day_read_is_when_it_went_live_not_when_it_was_uploaded():
    """A video published from a scheduled draft is ADDED to the uploads playlist on the day it
    was uploaded and goes LIVE days later. The milestone windows count from the day it went
    live, so reading the snippet's own date would date every scheduled video wrongly."""
    body = _playlist_body([("dQw4w9WgXcQ", "A scheduled one", "2026-08-24T15:00:00Z")])
    body["items"][0]["snippet"]["publishedAt"] = "2026-08-20T09:00:00Z"
    transport = FakeTransport({"/playlistItems": body})

    videos = uploads_mod.UploadsClient(transport).published_videos(PLAYLIST_ID)

    assert videos[0]["published_on"] == "2026-08-24"


def test_a_row_with_no_video_id_is_skipped_rather_than_returned_half_formed():
    body = _playlist_body([("dQw4w9WgXcQ", "A real one", "2026-08-24T15:00:00Z")])
    body["items"].append({"snippet": {"title": "A torn row"}, "contentDetails": {}})
    transport = FakeTransport({"/playlistItems": body})

    videos = uploads_mod.UploadsClient(transport).published_videos(PLAYLIST_ID)

    assert [row["video_id"] for row in videos] == ["dQw4w9WgXcQ"]


def test_gathering_asks_the_channel_then_the_playlist():
    transport = FakeTransport({
        "/channels": _channels_body(),
        "/playlistItems": _playlist_body([("dQw4w9WgXcQ", "One", "2026-08-24T15:00:00Z")]),
    })

    videos = uploads_mod.gather_published_videos(
        uploads_mod.UploadsClient(transport), CHANNEL_ID)

    assert [row["video_id"] for row in videos] == ["dQw4w9WgXcQ"]
    assert transport.calls[1][1]["playlistId"] == PLAYLIST_ID


def test_a_channel_with_no_uploads_playlist_gathers_nothing_without_a_second_call():
    transport = FakeTransport({"/channels": {"items": []}})

    assert uploads_mod.gather_published_videos(
        uploads_mod.UploadsClient(transport), CHANNEL_ID) == []
    assert len(transport.calls) == 1


# ---------------------------------------------------------------------------
# The key goes in the query string, and never anywhere it could leak
# ---------------------------------------------------------------------------


def test_the_public_key_travels_in_the_query_string_and_not_as_a_bearer_token(monkeypatch):
    """The public API takes a key, not a token. Proved through the real transport rather than
    a fake, because the thing being checked is exactly what the real one puts on the wire."""
    seen = {}

    class FakeResponse:
        def read(self):
            return b'{"items": []}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.headers)
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    ApiKeyTransport("a-fake-key").get(f"{uploads_mod.BASE}/channels", {"id": CHANNEL_ID})

    assert "key=a-fake-key" in seen["url"]
    assert not any(name.lower() == "authorization" for name in seen["headers"])


def test_a_failed_public_call_never_names_the_url_or_the_key(monkeypatch):
    """The whole reason the public transport reuses the private one's error handling."""
    import urllib.error

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(MetricsApiError) as raised:
        ApiKeyTransport("a-fake-key").get(f"{uploads_mod.BASE}/channels", {"id": CHANNEL_ID})

    assert "a-fake-key" not in str(raised.value)
    assert "googleapis.com" not in str(raised.value)


# ---------------------------------------------------------------------------
# Writing the list down
# ---------------------------------------------------------------------------


def test_the_list_is_written_with_the_day_it_was_pulled_on_it(tmp_path):
    """A list committed today and read somewhere else next week has to be visibly stale."""
    path = tmp_path / "data" / "metrics" / "published_videos.json"

    uploads_mod.write_published_videos(
        path, [{"video_id": "dQw4w9WgXcQ", "title": "One", "published_on": "2026-08-24"}],
        pulled_on="2026-08-25")

    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["pulled_on"] == "2026-08-25"
    assert record["videos"][0]["video_id"] == "dQw4w9WgXcQ"


def test_nothing_half_written_is_ever_left_behind(tmp_path):
    """Written to a temp file and moved into place. A half-written list would be refused by
    the matcher and reported as a channel that had suddenly published nothing."""
    path = tmp_path / "published_videos.json"

    uploads_mod.write_published_videos(path, [], pulled_on="2026-08-25")

    assert not path.with_name(path.name + ".tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["videos"] == []


# ---------------------------------------------------------------------------
# The daily pull, which is where this runs
# ---------------------------------------------------------------------------


class FakeUploads:
    def __init__(self, videos=None, raise_exc=None):
        self.videos = videos if videos is not None else [
            {"video_id": "dQw4w9WgXcQ", "title": "One", "published_on": "2026-08-24"}]
        self.raise_exc = raise_exc

    def uploads_playlist(self, channel_id):
        if self.raise_exc:
            raise self.raise_exc
        return PLAYLIST_ID

    def published_videos(self, playlist_id, limit=50):
        return list(self.videos)


def test_the_daily_pull_writes_the_list_even_on_a_day_no_video_is_due(tmp_path):
    """The day a video is FIRST published is exactly a day when nothing is due for it - it has
    no bet card yet, so the registry has never heard of it - and that is the one day this list
    has to be right. Written before the due check, so an early return cannot skip it."""
    config = _config(tmp_path, api_key="k", channel_id=CHANNEL_ID)

    summary = cli.run_pull(config, None, None, today=date(2026, 8, 25),
                           uploads_client=FakeUploads(), out=lambda *a: None)

    assert summary["appended"] == 0
    record = json.loads(config.published_videos_json.read_text(encoding="utf-8"))
    assert record["pulled_on"] == "2026-08-25"
    assert [row["video_id"] for row in record["videos"]] == ["dQw4w9WgXcQ"]


def test_no_key_configured_says_so_plainly_and_keeps_the_run_green(tmp_path):
    """A feature nobody has switched on yet must never turn a daily job red - a job that goes
    red for a non-problem is a job Scott stops reading."""
    config = _config(tmp_path)
    said = []

    failure = cli.write_published_video_list(config, None, today=date(2026, 8, 25),
                                             out=said.append)

    assert failure is None
    assert not config.published_videos_json.exists()
    assert "no Data API key or channel id is configured" in " ".join(said)


def test_a_failed_call_keeps_the_previous_list_untouched(tmp_path):
    """Never erase the day's list on a blip. An empty list would read as a channel that had
    suddenly published nothing at all, and every folder would look unmatched."""
    config = _config(tmp_path, api_key="k", channel_id=CHANNEL_ID)
    uploads_mod.write_published_videos(
        config.published_videos_json,
        [{"video_id": "dQw4w9WgXcQ", "title": "One", "published_on": "2026-08-24"}],
        pulled_on="2026-08-24")
    said = []

    failure = cli.write_published_video_list(
        config, FakeUploads(raise_exc=MetricsApiError(500, "backend blip")),
        today=date(2026, 8, 25), out=said.append)

    assert failure == "YouTube API error (status 500): backend blip"
    record = json.loads(config.published_videos_json.read_text(encoding="utf-8"))
    assert record["pulled_on"] == "2026-08-24"
    assert "previous list is untouched" in " ".join(said)


def test_a_failed_list_makes_the_run_partial_rather_than_silent(tmp_path, monkeypatch):
    """Exit 4: the numbers still landed, but something did not, and the run says so."""
    config = _config(tmp_path, api_key="k", channel_id=CHANNEL_ID)
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows",
                        lambda client, archive_dir, printer=print: [])

    code = cli.dispatch("pull", config=config, reporting_client=None, analytics_client=None,
                        uploads_client=FakeUploads(raise_exc=MetricsApiError(500, "blip")),
                        today=date(2026, 8, 25), out=lambda *a: None)

    assert code == 4


def test_an_unconfigured_list_leaves_the_run_green(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(cli.reach_archive_mod, "gather_reach_rows",
                        lambda client, archive_dir, printer=print: [])

    code = cli.dispatch("pull", config=config, reporting_client=None, analytics_client=None,
                        uploads_client=None, today=date(2026, 8, 25), out=lambda *a: None)

    assert code == 0


def test_the_list_lands_where_the_daily_job_already_commits(tmp_path):
    """Inside data/metrics/, which is the one path the workflow's commit step stages. Anywhere
    else and the list would be written daily and never committed, so the matcher would read a
    list from whenever the folder was last touched by hand."""
    config = _config(tmp_path)

    assert config.published_videos_json.parent == config.snapshots_csv.parent


def test_the_uploads_client_is_only_built_when_both_halves_are_configured(tmp_path,
                                                                         monkeypatch):
    """A key with no channel, or a channel with no key, cannot ask anything. Not built rather
    than built to fail on every daily run."""
    monkeypatch.setattr(cli, "Transport", lambda *a, **k: None)
    monkeypatch.setattr(cli, "OAuthTokenProvider", lambda *a, **k: None)
    monkeypatch.setattr(cli, "ReportingClient", lambda *a, **k: None)
    monkeypatch.setattr(cli, "AnalyticsClient", lambda *a, **k: None)

    assert cli.build_clients(_config(tmp_path))[2] is None
    assert cli.build_clients(_config(tmp_path / "b", api_key="k"))[2] is None
    assert cli.build_clients(_config(tmp_path / "c", channel_id=CHANNEL_ID))[2] is None
    both = cli.build_clients(_config(tmp_path / "d", api_key="k", channel_id=CHANNEL_ID))[2]
    assert isinstance(both, uploads_mod.UploadsClient)
