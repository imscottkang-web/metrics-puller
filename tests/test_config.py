"""Tests for metrics_lib.config - path + secret-location resolution.

The only secret-adjacent value config holds is a PATH to the client-secret
file; the secret contents live in that file, never in config. Resolution
mirrors the radar: YT_OAUTH_CLIENT_SECRET env first, then .env, then default.
"""

from pathlib import Path

from metrics_lib.config import load_config


def test_default_client_secret_and_token_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == tmp_path / "secrets" / "client_secret.json"
    assert cfg.token_path == tmp_path / "secrets" / "token.json"


def test_env_overrides_secret_path(tmp_path, monkeypatch):
    monkeypatch.setenv("YT_OAUTH_CLIENT_SECRET", "/custom/cs.json")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/custom/cs.json")


def test_dotenv_provides_secret_path(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    (tmp_path / ".env").write_text("YT_OAUTH_CLIENT_SECRET=/from/dotenv.json\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/from/dotenv.json")


def test_env_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("YT_OAUTH_CLIENT_SECRET=/from/dotenv.json\n", encoding="utf-8")
    monkeypatch.setenv("YT_OAUTH_CLIENT_SECRET", "/from/env.json")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/from/env.json")


def test_dotenv_value_may_be_quoted(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    (tmp_path / ".env").write_text('YT_OAUTH_CLIENT_SECRET="/quoted/cs.json"\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/quoted/cs.json")


def test_scope_is_readonly_analytics_only(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    cfg = load_config(tmp_path)
    assert cfg.scopes == ["https://www.googleapis.com/auth/yt-analytics.readonly"]
    # Read-only guarantee: every scope is a readonly one; the monetary scope is never requested.
    assert all("readonly" in s for s in cfg.scopes)
    assert not any("monetary" in s for s in cfg.scopes)


def test_data_paths_resolve_relative_to_second_brain(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    root = tmp_path / "second-brain" / "tools" / "metrics"
    root.mkdir(parents=True)
    cfg = load_config(root)
    sb = tmp_path / "second-brain"
    assert cfg.snapshots_csv == sb / "data" / "metrics" / "weekly_snapshots.csv"
    assert cfg.retention_dir == sb / "data" / "metrics" / "retention"
    assert cfg.reach_reports_dir == sb / "data" / "metrics" / "_reach_reports"
    assert cfg.scripts_dir == sb / "scripts"
