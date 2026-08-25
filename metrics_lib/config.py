"""Load the metrics puller's config from environment + .env into a frozen Config.

This tool is shared by more than one YouTube channel ("house" - a channel's own
folder of numbers and secrets). It must never guess which house it is serving:
a shared default location is exactly the collision this module exists to
prevent (two houses could end up sharing one saved sign-in file, so one
house's sign-in silently overwrites the other's). Every path a house needs
comes from that house's own folder, handed in as house_dir - see
HouseNotConfigured for what happens when nothing is handed in.

The only secret-adjacent value here is a PATH to the OAuth client-secret file
and the saved token; the secret contents live in those files, never in this
module. Resolution for every setting below (mirrors the radar's api-key
resolution): the environment variable first, then a .env file at house_dir,
then - for paths only - a fixed default filename under house_dir/secrets/.
There is no fallback for the settings that name a house's own folders; those
must be set explicitly, on purpose, for every house.
"""

from dataclasses import dataclass
from pathlib import Path
import os

# Read-only analytics scope. It authorizes BOTH the Reporting API reach reports
# and the Analytics API queries; the monetary scope is deliberately never added.
SCOPES = ("https://www.googleapis.com/auth/yt-analytics.readonly",)


class ConfigError(Exception):
    """Base class for a metrics-config problem. str() is a complete plain-English
    explanation of what is missing and how to fix it - never a file's contents."""


class HouseNotConfigured(ConfigError):
    """No house_dir was given, and METRICS_HOUSE is not set either.

    This tool serves more than one YouTube channel, so it can never guess
    which one it should be running as - it must be told, every time.
    """


class MissingSetting(ConfigError):
    """A setting this house must supply is missing from both the environment
    and this house's .env file."""


@dataclass(frozen=True)
class Config:
    house_dir: Path  # this house's own metrics folder: holds its .env and its secrets/
    client_secret_path: Path
    token_path: Path
    data_dir: Path  # where this house's snapshots, retention curves, reach archive, and
    # published-video list are written
    scripts_dir: Path  # this house's video script folders (each with a bet_card.md)
    reach_job_name: str  # this house's YouTube Reporting API reach-report job name
    timeout: int = 20
    # The PUBLIC Data API key and the channel it reads, for the published-video list the
    # channel watcher matches against. Both None where they were never configured, which is
    # a setup state rather than a failure: the pull still records every number it always did.
    api_key: str | None = None
    channel_id: str | None = None

    @property
    def scopes(self) -> list[str]:
        return list(SCOPES)

    @property
    def snapshots_csv(self) -> Path:
        return self.data_dir / "weekly_snapshots.csv"

    @property
    def retention_dir(self) -> Path:
        return self.data_dir / "retention"

    @property
    def reach_reports_dir(self) -> Path:
        return self.data_dir / "_reach_reports"

    @property
    def published_videos_json(self) -> Path:
        """Where the daily pull writes down what the channel says it has published.

        Inside data_dir deliberately: that is the one folder each house's daily job commits,
        so the list is committed with the numbers rather than needing its own rule.
        """
        return self.data_dir / "published_videos.json"


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines from a .env file.

    Blank lines and '#' comments are ignored. Values may be unquoted or wrapped
    in matching single or double quotes. (Same shape as the radar's parser.)
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _resolve(house_dir: Path, name: str) -> str | None:
    """An environment value, else the same name out of house_dir's .env, else None.

    The environment is checked first (which is how a cloud job passes a repository secret
    in), then a local .env file for a Mac running this by hand.
    """
    value = os.environ.get(name) or _parse_dotenv(house_dir / ".env").get(name)
    return value.strip() or None if value else None


def _require(house_dir: Path, name: str, purpose: str) -> str:
    """The same resolution as _resolve, but refuses when nothing was set.

    Every required setting gets its own message naming the setting and what it is for -
    a human reading a failed daily job should never have to go read this module's source
    to find out what to set.
    """
    value = _resolve(house_dir, name)
    if not value:
        raise MissingSetting(
            f"{name} is not set for this house. {purpose} "
            f"Set it in the environment, or add a line '{name}=...' to {house_dir / '.env'}."
        )
    return value


def _resolve_dir(house_dir: Path, value: str) -> Path:
    """A relative setting is resolved against house_dir; an absolute one is used as-is."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return house_dir / path


def _resolve_secret_path(house_dir: Path) -> Path:
    env_value = os.environ.get("YT_OAUTH_CLIENT_SECRET")
    if env_value:
        return Path(env_value).expanduser()
    dotenv_value = _parse_dotenv(house_dir / ".env").get("YT_OAUTH_CLIENT_SECRET")
    if dotenv_value:
        return Path(dotenv_value).expanduser()
    return house_dir / "secrets" / "client_secret.json"


def load_config(house_dir: Path | None = None) -> Config:
    """Build this run's Config for exactly one house.

    house_dir is the one required value that says which channel this run serves: pass it
    directly, or set the METRICS_HOUSE environment variable to the full path of that
    channel's own metrics folder (the folder holding its .env and its secrets/ folder).
    There is no shared default location - that would be the exact mix-up (two houses
    sharing one folder, and so one saved sign-in) this module exists to rule out.
    """
    if not house_dir:
        house_dir = os.environ.get("METRICS_HOUSE")
    if not house_dir:
        raise HouseNotConfigured(
            "This tool is shared by more than one YouTube channel, so it has to be told "
            "which channel's own folder ('house') to use before it can run - there is no "
            "default. Fix: set the METRICS_HOUSE environment variable to the full path of "
            "that channel's metrics folder (the folder that holds its .env file and its "
            "secrets/ folder), or pass that path in directly if you are calling this from "
            "code."
        )
    house_dir = Path(house_dir).expanduser()

    data_dir_value = _require(
        house_dir, "METRICS_DATA_DIR",
        "This is the folder this house's weekly snapshot CSV, retention curves, "
        "reach-report archive, and published-video list get written to.",
    )
    scripts_dir_value = _require(
        house_dir, "METRICS_SCRIPTS_DIR",
        "This is the folder holding this house's video script folders (each one with a "
        "bet_card.md), which is how the puller knows which videos to record numbers for.",
    )
    reach_job_name = _require(
        house_dir, "METRICS_REACH_JOB_NAME",
        "This name is given to this house's reach-report job in the YouTube Reporting "
        "API, so this house's job can be told apart from any other house's job in the "
        "Google API console.",
    )

    return Config(
        house_dir=house_dir,
        client_secret_path=_resolve_secret_path(house_dir),
        token_path=house_dir / "secrets" / "token.json",
        data_dir=_resolve_dir(house_dir, data_dir_value),
        scripts_dir=_resolve_dir(house_dir, scripts_dir_value),
        reach_job_name=reach_job_name,
        api_key=_resolve(house_dir, "YOUTUBE_API_KEY"),
        channel_id=_resolve(house_dir, "YT_CHANNEL_ID"),
    )
