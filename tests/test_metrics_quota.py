"""The public metrics pull records its Data API costs with fake provider responses."""

import json
import sys
from pathlib import Path
from datetime import date

import metrics as cli
from metrics_lib.config import Config

from metrics_lib.http import ApiKeyTransport
from metrics_lib.uploads import UploadsClient, gather_published_videos

sys.path.append(str(Path(__file__).resolve().parents[2] / "youtube-radar"))
from radar_lib.quota import Ledger


def test_metrics_pull_records_the_channels_and_playlist_units(tmp_path, monkeypatch):
    ledger = Ledger(tmp_path / "quota.json", 6000)

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.body).encode()

    def request(req, **kwargs):
        if "/channels?" in req.full_url:
            return Response({"items": [{"contentDetails": {
                "relatedPlaylists": {"uploads": "UUanchorandivyfake"},
            }}]})
        return Response({"items": []})

    monkeypatch.setattr("urllib.request.urlopen", request)
    client = UploadsClient(ApiKeyTransport("fake-key", ledger=ledger))
    assert gather_published_videos(client, "UCanchorandivyfake") == []
    assert ledger.spent_today() == 2
    events = json.loads(ledger.path.read_text())[ledger.today()]["events"]
    assert [(row["label"], row["units"]) for row in events] == [
        ("metrics:channels", 1), ("metrics:playlistItems", 1),
    ]


def test_real_metrics_pull_refuses_over_budget_request_and_keeps_partial_exit(
    tmp_path, monkeypatch,
):
    radar = tmp_path / "radar"
    radar.mkdir()
    (radar / "config.toml").write_text("daily_quota_budget = 1\n")
    monkeypatch.setattr("metrics_lib.http._RADAR_TOOL", radar)
    config = Config(
        house_dir=tmp_path, client_secret_path=tmp_path / "unused-client.json",
        token_path=tmp_path / "unused-token.json", data_dir=tmp_path / "data",
        scripts_dir=tmp_path / "scripts", reach_job_name="Anchor and Ivy reach",
        channel_id="UCanchorandivyfake", api_key="fake-key",
    )
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"items":[{"contentDetails":{"relatedPlaylists":{"uploads":"UUanchorandivyfake"}}}]}'

    def request(req, **kwargs):
        calls.append("called")
        return Response()

    class Analytics:
        def probe_channel(self, on_date):
            pass

    monkeypatch.setattr("urllib.request.urlopen", request)
    # Building clients is credential-free; only the fake uploads HTTP is exercised.
    uploads = cli.build_clients(config)[2]
    lines = []
    code = cli.dispatch(
        "pull", config=config, reporting_client=None, analytics_client=Analytics(),
        uploads_client=uploads, today=date(2026, 9, 10), out=lines.append,
    )
    assert code == 4
    assert len(calls) == 1
    assert any("only 0" in line for line in lines)
    assert Ledger(radar / "data" / "quota_ledger.json", 1).spent_today() == 1
    assert not config.published_videos_json.exists()
