"""Shared pytest fixtures for the metrics puller tests.

No test opens a socket. Higher-level methods are tested by monkeypatching each
client's request seam with fixture data; the seam's own HTTP handling is tested
by monkeypatching urllib.request.urlopen. Mirrors the radar's tests/conftest.py.

The Config / fake-token fixtures are added in Task 2, once Config exists.
"""

import json
import socket
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Block any real network connection attempt for the whole suite.

    "No network in tests" is a stated success criterion, but until now
    nothing enforced it mechanically - a test could accidentally skip its
    fixture/seam and hit the real network without failing loudly. Every
    legitimate test here works by injecting a fake transport/request seam
    (see above), never by opening a real socket, so a real connect() attempt
    is always a bug in the test, not a real dependency to accommodate.

    monkeypatch (function-scoped) restores socket.socket.connect after each
    test, so this cannot leak into anything outside the suite.
    """

    def _blocked_connect(self, *args, **kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)


@pytest.fixture
def load_fixture():
    """Load a fixture file. `.json` is parsed to a dict/list; anything else
    (e.g. a report `.csv`) is returned as text."""

    def _load(name: str):
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8")
        return json.loads(text) if name.endswith(".json") else text

    return _load


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A Config rooted at a fresh tmp_path (as house_dir), with no client-secret env
    override. The three now-required house settings are set here via the environment
    so tests that only care about auth/paths do not each have to supply them.

    Imported lazily so conftest still collects before metrics_lib.config exists.
    """
    monkeypatch.delenv("YT_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("METRICS_DATA_DIR", "data")
    monkeypatch.setenv("METRICS_SCRIPTS_DIR", "scripts")
    monkeypatch.setenv("METRICS_REACH_JOB_NAME", "Anchor and Ivy reach")
    from metrics_lib.config import load_config

    return load_config(tmp_path)
