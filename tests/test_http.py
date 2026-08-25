"""Tests for metrics_lib.http - the shared, leak-safe request seam.

Higher-level clients (reporting, analytics) are tested by injecting a fake
transport. Transport's OWN handling is tested here by monkeypatching
urllib.request.urlopen, so no socket is ever opened (mirrors the radar's
yt_api _request tests). The token and URL must never appear in an error.
"""

import io
import json
import socket
import urllib.error

import pytest

import metrics_lib.http as http_module
from metrics_lib.http import MetricsApiError, Transport


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeToken:
    def __init__(self, token="FAKE-TOKEN"):
        self._t = token

    def bearer(self):
        return self._t


def _transport(token="FAKE-TOKEN"):
    return Transport(FakeToken(token))


def test_get_attaches_bearer_and_parses_json(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["method"] = req.get_method()
        return _FakeResp(json.dumps({"ok": True}).encode())

    monkeypatch.setattr(http_module.urllib.request, "urlopen", fake_urlopen)
    data = _transport("TOK123").get(
        "https://youtubeanalytics.googleapis.com/v2/reports", {"ids": "channel==MINE"}
    )
    assert data == {"ok": True}
    assert seen["auth"] == "Bearer TOK123"
    assert seen["method"] == "GET"
    assert "ids=channel%3D%3DMINE" in seen["url"]


def test_post_sends_json_body_and_method(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["method"] = req.get_method()
        seen["body"] = req.data
        seen["ctype"] = req.get_header("Content-type")
        return _FakeResp(json.dumps({"id": "job1"}).encode())

    monkeypatch.setattr(http_module.urllib.request, "urlopen", fake_urlopen)
    data = _transport().post(
        "https://youtubereporting.googleapis.com/v1/jobs",
        {"reportTypeId": "channel_reach_basic_a1"},
    )
    assert data == {"id": "job1"}
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == {"reportTypeId": "channel_reach_basic_a1"}
    assert seen["ctype"] == "application/json"


def test_http_error_becomes_metrics_error_without_leaking_token_or_url(monkeypatch):
    err_body = json.dumps({"error": {"code": 403, "message": "Insufficient permission"}}).encode()

    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=io.BytesIO(err_body))

    monkeypatch.setattr(http_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MetricsApiError) as ei:
        _transport("SECRET-TOKEN").get(
            "https://youtubeanalytics.googleapis.com/v2/reports", {"ids": "channel==MINE"}
        )
    msg = str(ei.value)
    assert ei.value.status == 403
    assert "Insufficient permission" in msg
    assert "SECRET-TOKEN" not in msg
    assert "Bearer" not in msg
    assert "googleapis.com" not in msg


def test_urlerror_becomes_metrics_error(monkeypatch):
    def fake_urlopen(req, timeout=None, context=None):
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(http_module.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(MetricsApiError) as ei:
        _transport("SECRET-TOKEN").get(
            "https://youtubeanalytics.googleapis.com/v2/reports", {"ids": "channel==MINE"}
        )
    msg = str(ei.value)
    assert ei.value.status == 0
    assert "SECRET-TOKEN" not in msg
    assert "Bearer" not in msg
    assert "googleapis.com" not in msg


def test_timeout_during_read_becomes_metrics_error_without_leaking(monkeypatch):
    # do_open only wraps the SEND phase in URLError; a timeout while reading
    # the response body surfaces as a raw TimeoutError (verified against
    # CPython 3.13's urllib.request.AbstractHTTPHandler.do_open).
    class _TimeoutResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a, **k):
            raise TimeoutError("timed out")

    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _TimeoutResp(b""))
    with pytest.raises(MetricsApiError) as ei:
        _transport("SECRET-TOKEN").get("https://youtubeanalytics.googleapis.com/v2/reports")
    msg = str(ei.value)
    assert ei.value.status == 0
    assert "request timed out or connection failed" in msg
    assert "SECRET-TOKEN" not in msg
    assert "googleapis.com" not in msg


def test_socket_timeout_during_read_becomes_metrics_error_without_leaking(monkeypatch):
    # socket.timeout is TimeoutError as of Python 3.10, but both spellings are
    # covered explicitly in case a future runtime re-splits them.
    class _SocketTimeoutResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *a, **k):
            raise socket.timeout("timed out")

    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _SocketTimeoutResp(b""))
    with pytest.raises(MetricsApiError) as ei:
        _transport("SECRET-TOKEN").get("https://youtubeanalytics.googleapis.com/v2/reports")
    msg = str(ei.value)
    assert ei.value.status == 0
    assert "request timed out or connection failed" in msg
    assert "SECRET-TOKEN" not in msg
    assert "googleapis.com" not in msg


def test_malformed_json_response_becomes_metrics_error_without_leaking_body(monkeypatch):
    # A Google outage page (HTML, status 200) must not leak into the message.
    html_body = b"<html><body>Google is currently unable to handle this request.</body></html>"
    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _FakeResp(html_body))
    with pytest.raises(MetricsApiError) as ei:
        _transport().get("https://youtubeanalytics.googleapis.com/v2/reports")
    msg = str(ei.value)
    assert ei.value.status == 0
    assert "could not parse response body" in msg
    assert "Google is currently unable to handle this request" not in msg
    assert "<html>" not in msg


def test_empty_body_returns_empty_dict(monkeypatch):
    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _FakeResp(b""))
    assert _transport().get("https://x") == {}


def test_get_bytes_returns_raw_for_report_download(monkeypatch):
    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"a,b\n1,2\n"))
    assert _transport().get_bytes("https://download/report.csv") == b"a,b\n1,2\n"


def test_bearer_fetched_per_request(monkeypatch):
    monkeypatch.setattr(http_module.urllib.request, "urlopen", lambda *a, **k: _FakeResp(b"{}"))
    calls = []

    class CountingToken:
        def bearer(self):
            calls.append(1)
            return "T"

    t = Transport(CountingToken())
    t.get("https://x")
    t.get("https://y")
    assert len(calls) == 2
