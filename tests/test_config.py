"""Tests for metrics_lib.config - the house-handed config, and the refusal to guess.

This tool is shared by more than one YouTube channel ("house"). The single failure this
suite exists to rule out: two houses sharing one saved sign-in because the tool guessed a
folder instead of being told one. So every test either proves a value is handed in and used
correctly, or proves a missing value refuses loudly instead of falling back to a shared
default.
"""

from pathlib import Path

import pytest

from metrics_lib.config import HouseNotConfigured, MissingSetting, load_config

# Sanctioned fake channel id - this tool names no real business or account.
FAKE_CHANNEL_ID = "UCanchorandivydatafake"

REQUIRED_ENV = {
    "METRICS_DATA_DIR": "data",
    "METRICS_SCRIPTS_DIR": "scripts",
    "METRICS_REACH_JOB_NAME": "Anchor and Ivy reach",
    "YT_CHANNEL_ID": FAKE_CHANNEL_ID,
}


def _set_required_env(monkeypatch, **overrides):
    values = dict(REQUIRED_ENV, **overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


# --- the refusal: no house_dir, no fallback ---------------------------------

def test_no_house_dir_and_no_env_var_refuses(monkeypatch):
    monkeypatch.delenv("METRICS_HOUSE", raising=False)
    with pytest.raises(HouseNotConfigured) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "METRICS_HOUSE" in message
    assert "more than one" in message  # explains WHY, in plain English


def test_metrics_house_env_var_is_used_when_no_argument_given(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    house = tmp_path / "a-house"
    house.mkdir()
    monkeypatch.setenv("METRICS_HOUSE", str(house))
    _set_required_env(monkeypatch)
    cfg = load_config()
    assert cfg.house_dir == house


def test_passed_house_dir_takes_precedence_over_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("METRICS_HOUSE", str(tmp_path / "wrong-house"))
    _set_required_env(monkeypatch)
    right_house = tmp_path / "right-house"
    right_house.mkdir()
    cfg = load_config(right_house)
    assert cfg.house_dir == right_house


# --- required settings, each refusing on its own with its own message ------

def test_missing_data_dir_refuses_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_DATA_DIR=None)
    with pytest.raises(MissingSetting) as excinfo:
        load_config(tmp_path)
    assert "METRICS_DATA_DIR" in str(excinfo.value)


def test_missing_scripts_dir_refuses_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_SCRIPTS_DIR=None)
    with pytest.raises(MissingSetting) as excinfo:
        load_config(tmp_path)
    assert "METRICS_SCRIPTS_DIR" in str(excinfo.value)


def test_missing_reach_job_name_refuses_by_name(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_REACH_JOB_NAME=None)
    with pytest.raises(MissingSetting) as excinfo:
        load_config(tmp_path)
    assert "METRICS_REACH_JOB_NAME" in str(excinfo.value)


def test_missing_channel_id_refuses_by_name_and_explains_what_it_is(tmp_path, monkeypatch):
    # This is the important refusal: with no channel id, a wrong sign-in dropped into
    # this house's secrets/ folder could pull another channel's numbers and nothing
    # would catch it. That must stop the run, not just print a note and carry on.
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, YT_CHANNEL_ID=None)
    with pytest.raises(MissingSetting) as excinfo:
        load_config(tmp_path)
    message = str(excinfo.value)
    assert "YT_CHANNEL_ID" in message
    assert "channel" in message.lower()  # explains what a channel id is, in plain English


# --- path resolution: relative vs absolute, and ~ expansion -----------------

def test_relative_data_and_scripts_dirs_resolve_against_house_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.scripts_dir == tmp_path / "scripts"


def test_absolute_data_dir_is_used_as_is(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    absolute = tmp_path / "elsewhere" / "data"
    _set_required_env(monkeypatch, METRICS_DATA_DIR=str(absolute))
    cfg = load_config(tmp_path)
    assert cfg.data_dir == absolute


def test_data_dir_expands_tilde(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_DATA_DIR="~/some-metrics-data")
    cfg = load_config(tmp_path)
    assert cfg.data_dir == Path("~/some-metrics-data").expanduser()


def test_house_settings_may_come_from_dotenv_instead_of_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "METRICS_DATA_DIR=data\nMETRICS_SCRIPTS_DIR=scripts\n"
        "METRICS_REACH_JOB_NAME=Anchor and Ivy reach\n"
        f"YT_CHANNEL_ID={FAKE_CHANNEL_ID}\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path / "data"
    assert cfg.reach_job_name == "Anchor and Ivy reach"
    assert cfg.channel_id == FAKE_CHANNEL_ID


# --- everything downstream hangs off data_dir, with the old filenames kept -

def test_data_paths_hang_off_data_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    cfg = load_config(tmp_path)
    assert cfg.snapshots_csv == tmp_path / "data" / "weekly_snapshots.csv"
    assert cfg.retention_dir == tmp_path / "data" / "retention"
    assert cfg.reach_reports_dir == tmp_path / "data" / "_reach_reports"
    assert cfg.published_videos_json == tmp_path / "data" / "published_videos.json"


# --- secrets live under house_dir/secrets ------------------------------------

def test_default_client_secret_and_token_paths(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == tmp_path / "secrets" / "client_secret.json"
    assert cfg.token_path == tmp_path / "secrets" / "token.json"


def test_env_overrides_secret_path(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.setenv("YT_OAUTH_CLIENT_SECRET", "/custom/cs.json")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/custom/cs.json")


def test_dotenv_provides_secret_path(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    (tmp_path / ".env").write_text("YT_OAUTH_CLIENT_SECRET=/from/dotenv.json\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/from/dotenv.json")


def test_env_takes_precedence_over_dotenv(tmp_path, monkeypatch):
    _set_required_env(monkeypatch)
    (tmp_path / ".env").write_text("YT_OAUTH_CLIENT_SECRET=/from/dotenv.json\n", encoding="utf-8")
    monkeypatch.setenv("YT_OAUTH_CLIENT_SECRET", "/from/env.json")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/from/env.json")


def test_dotenv_value_may_be_quoted(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    (tmp_path / ".env").write_text('YT_OAUTH_CLIENT_SECRET="/quoted/cs.json"\n', encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.client_secret_path == Path("/quoted/cs.json")


def test_scope_is_readonly_analytics_only(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch)
    cfg = load_config(tmp_path)
    assert cfg.scopes == ["https://www.googleapis.com/auth/yt-analytics.readonly"]
    # Read-only guarantee: every scope is a readonly one; the monetary scope is never requested.
    assert all("readonly" in s for s in cfg.scopes)
    assert not any("monetary" in s for s in cfg.scopes)


# --- channel_id is required, api_key stays optional -------------------------

def test_channel_id_is_read_and_api_key_stays_optional(tmp_path, monkeypatch):
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    _set_required_env(monkeypatch)
    cfg = load_config(tmp_path)
    assert cfg.channel_id == FAKE_CHANNEL_ID
    assert cfg.api_key is None


# --- fix 5: a house's own .env beats a leftover value in the environment ----

def test_house_dotenv_beats_the_ambient_environment(tmp_path, monkeypatch):
    # A leftover `export METRICS_DATA_DIR=...` from testing one house in this shell
    # must never leak into a different house's run - the house's own .env file wins.
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_DATA_DIR="from-environment")
    (tmp_path / ".env").write_text("METRICS_DATA_DIR=from-dotenv\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path / "from-dotenv"


def test_environment_still_used_when_house_has_no_dotenv_value(tmp_path, monkeypatch):
    # The cloud job has no .env file at all, so it must still get every value from the
    # environment (its repository secrets) exactly as before.
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    _set_required_env(monkeypatch, METRICS_DATA_DIR="from-environment")
    cfg = load_config(tmp_path)
    assert cfg.data_dir == tmp_path / "from-environment"
