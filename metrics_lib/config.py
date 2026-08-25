"""Load the metrics puller's config from environment + .env into a frozen Config.

The only secret-adjacent value here is a PATH to the OAuth client-secret file;
the secret contents live in that file, never in this module. Resolution for the
client-secret path (mirrors the radar's api-key resolution): the
YT_OAUTH_CLIENT_SECRET environment variable first, then a .env file at the tool
root, then the default secrets/client_secret.json next to metrics.py.
"""

from dataclasses import dataclass
from pathlib import Path
import os

# Read-only analytics scope. It authorizes BOTH the Reporting API reach reports
# and the Analytics API queries; the monetary scope is deliberately never added.
SCOPES = ("https://www.googleapis.com/auth/yt-analytics.readonly",)


@dataclass(frozen=True)
class Config:
    root: Path  # the tool root: <second-brain>/tools/metrics
    client_secret_path: Path
    token_path: Path
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
    def second_brain_root(self) -> Path:
        # root == <second-brain>/tools/metrics -> go up two to <second-brain>.
        return self.root.parent.parent

    @property
    def snapshots_csv(self) -> Path:
        return self.second_brain_root / "data" / "metrics" / "weekly_snapshots.csv"

    @property
    def retention_dir(self) -> Path:
        return self.second_brain_root / "data" / "metrics" / "retention"

    @property
    def reach_reports_dir(self) -> Path:
        return self.second_brain_root / "data" / "metrics" / "_reach_reports"

    @property
    def scripts_dir(self) -> Path:
        return self.second_brain_root / "scripts"

    @property
    def published_videos_json(self) -> Path:
        """Where the daily pull writes down what the channel says it has published.

        Inside data/metrics/ deliberately: that is the one path the daily job's commit step
        stages, so the list is committed with the numbers rather than needing its own rule.
        """
        return self.second_brain_root / "data" / "metrics" / "published_videos.json"


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


def _resolve_secret_path(root: Path) -> Path:
    env_value = os.environ.get("YT_OAUTH_CLIENT_SECRET")
    if env_value:
        return Path(env_value).expanduser()
    dotenv_value = _parse_dotenv(root / ".env").get("YT_OAUTH_CLIENT_SECRET")
    if dotenv_value:
        return Path(dotenv_value).expanduser()
    return root / "secrets" / "client_secret.json"


def _resolve(root: Path, name: str) -> str | None:
    """An environment value, else the same name out of the tool root's .env, else None.

    Same precedence as the client-secret path above, and the same as the radar's key: the
    environment first (which is how the cloud job passes a repository secret in), then a
    local .env for Scott's Mac.
    """
    value = os.environ.get(name) or _parse_dotenv(root / ".env").get(name)
    return value.strip() or None if value else None


def load_config(root: Path | None = None) -> Config:
    if root is None:
        # config.py lives at <root>/metrics_lib/config.py
        root = Path(__file__).resolve().parent.parent
    root = Path(root)
    return Config(
        root=root,
        client_secret_path=_resolve_secret_path(root),
        token_path=root / "secrets" / "token.json",
        api_key=_resolve(root, "YOUTUBE_API_KEY"),
        channel_id=_resolve(root, "YT_CHANNEL_ID"),
    )
