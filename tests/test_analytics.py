"""Tests for metrics_lib.analytics - the YouTube Analytics API v2 client.

No network: every test injects a FakeTransport that records (url, params) and
returns a canned response, either one of the fixtures/*.json files or a small
dict built inline for the two edge cases that have no authored fixture file.
Every value the client reads must go by columnHeaders NAME, never by fixed
row position - the scrambled-header and missing-column tests below exist
specifically to prove that.
"""

import pytest

from metrics_lib.analytics import AnalyticsClient
from metrics_lib.http import MetricsApiError

BASE = "https://youtubeanalytics.googleapis.com/v2"


class FakeTransport:
    """Records every .get call and returns one canned response.

    Small hand-written stand-in for the real Transport (see
    metrics_lib/http.py) - just a .get(url, params) seam, per the house
    dependency-injection pattern. No network, no auth, nothing secret.
    """

    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        return self.response


class RaisingTransport:
    """A transport whose .get always raises, to prove errors propagate."""

    def __init__(self, error):
        self._error = error

    def get(self, url, params=None):
        raise self._error


# ---- core_metrics -----------------------------------------------------------


def test_core_metrics_maps_views_and_duration_by_header_name_even_when_scrambled(load_fixture):
    transport = FakeTransport(load_fixture("analytics_core.json"))
    client = AnalyticsClient(transport)

    result = client.core_metrics("vid123", "2026-01-01", "2026-07-01")

    # The fixture's columnHeaders are averageViewDuration, views - the
    # reverse of the requested metrics= order - so this only passes if the
    # client reads by header name and not by row position.
    assert result == {"views": 2044, "avg_view_duration_sec": 154}


def test_core_metrics_with_empty_rows_returns_zeros(load_fixture):
    fixture = dict(load_fixture("analytics_core.json"), rows=[])
    transport = FakeTransport(fixture)
    client = AnalyticsClient(transport)

    result = client.core_metrics("vid123", "2026-01-01", "2026-07-01")

    assert result == {"views": 0, "avg_view_duration_sec": 0}


def test_core_metrics_sends_expected_params(load_fixture):
    transport = FakeTransport(load_fixture("analytics_core.json"))
    client = AnalyticsClient(transport)

    client.core_metrics("vid123", "2026-01-01", "2026-07-01")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == f"{BASE}/reports"
    params = call["params"]
    assert params["ids"] == "channel==MINE"
    assert params["startDate"] == "2026-01-01"
    assert params["endDate"] == "2026-07-01"
    assert params["metrics"] == "views,averageViewDuration"
    assert params["filters"] == "video==vid123"


def test_core_metrics_errors_propagate_unchanged():
    error = MetricsApiError(403, "insufficient permission")
    client = AnalyticsClient(RaisingTransport(error))

    with pytest.raises(MetricsApiError) as ei:
        client.core_metrics("vid123", "2026-01-01", "2026-07-01")
    assert ei.value is error


# ---- retention_curve ---------------------------------------------------------


def test_retention_curve_returns_points_with_relative_retention(load_fixture):
    transport = FakeTransport(load_fixture("analytics_retention.json"))
    client = AnalyticsClient(transport)

    points = client.retention_curve("vid123", "2026-01-01", "2026-07-01")

    assert points == [
        {"elapsed_ratio": 0.0, "audience_watch_ratio": 1.0, "relative_retention": 0.12},
        {"elapsed_ratio": 0.25, "audience_watch_ratio": 0.82, "relative_retention": 0.05},
        {"elapsed_ratio": 0.5, "audience_watch_ratio": 0.61, "relative_retention": -0.03},
        {"elapsed_ratio": 1.0, "audience_watch_ratio": 0.34, "relative_retention": -0.1},
    ]


def test_retention_curve_without_relative_column_is_none():
    # New-channel case: YouTube omits relativeRetentionPerformance entirely
    # (not enough channel history to compute it) rather than sending it as
    # null - the column itself is missing from columnHeaders, not just the
    # value. This fixture is built inline since it isn't one of the three
    # authored fixture files.
    fixture = {
        "columnHeaders": [
            {"name": "elapsedVideoTimeRatio", "columnType": "DIMENSION", "dataType": "FLOAT"},
            {"name": "audienceWatchRatio", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [
            [0.0, 1.0],
            [0.5, 0.55],
        ],
    }
    transport = FakeTransport(fixture)
    client = AnalyticsClient(transport)

    points = client.retention_curve("vid123", "2026-01-01", "2026-07-01")

    assert points == [
        {"elapsed_ratio": 0.0, "audience_watch_ratio": 1.0, "relative_retention": None},
        {"elapsed_ratio": 0.5, "audience_watch_ratio": 0.55, "relative_retention": None},
    ]


def test_retention_curve_sends_expected_params(load_fixture):
    transport = FakeTransport(load_fixture("analytics_retention.json"))
    client = AnalyticsClient(transport)

    client.retention_curve("vid123", "2026-01-01", "2026-07-01")

    params = transport.calls[0]["params"]
    assert params["ids"] == "channel==MINE"
    assert params["startDate"] == "2026-01-01"
    assert params["endDate"] == "2026-07-01"
    assert params["dimensions"] == "elapsedVideoTimeRatio"
    assert params["metrics"] == "audienceWatchRatio,relativeRetentionPerformance"
    assert params["filters"] == "video==vid123"


# ---- traffic_mix --------------------------------------------------------------


def test_traffic_mix_computes_percent_of_total_including_other_sources(load_fixture):
    transport = FakeTransport(load_fixture("analytics_traffic.json"))
    client = AnalyticsClient(transport)

    result = client.traffic_mix("vid123", "2026-01-01", "2026-07-01")

    # Fixture: BROWSE=1200, YT_SEARCH=800, SUGGESTED_VIDEO=500, EXTERNAL=500,
    # total=3000. EXTERNAL is in the denominator but not in the result, so
    # the three named shares deliberately do not sum to 100.
    assert result == {"browse": 40.0, "search": 26.67, "suggested": 16.67}
    assert round(result["browse"] + result["search"] + result["suggested"], 2) != 100.0


def test_traffic_mix_zero_total_views_returns_zeros():
    fixture = {
        "columnHeaders": [
            {"name": "insightTrafficSourceType", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
        ],
        "rows": [],
    }
    transport = FakeTransport(fixture)
    client = AnalyticsClient(transport)

    result = client.traffic_mix("novid", "2026-01-01", "2026-07-01")

    assert result == {"browse": 0.0, "search": 0.0, "suggested": 0.0}


def test_traffic_mix_source_absent_from_rows_counts_as_zero():
    # Only BROWSE and EXTERNAL appear; YT_SEARCH and SUGGESTED_VIDEO are
    # absent from the rows entirely (no zero-view row for them at all).
    fixture = {
        "columnHeaders": [
            {"name": "insightTrafficSourceType", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "views", "columnType": "METRIC", "dataType": "INTEGER"},
        ],
        "rows": [
            ["BROWSE", 60],
            ["EXTERNAL", 40],
        ],
    }
    transport = FakeTransport(fixture)
    client = AnalyticsClient(transport)

    result = client.traffic_mix("vid123", "2026-01-01", "2026-07-01")

    assert result == {"browse": 60.0, "search": 0.0, "suggested": 0.0}


def test_traffic_mix_sends_expected_params(load_fixture):
    transport = FakeTransport(load_fixture("analytics_traffic.json"))
    client = AnalyticsClient(transport)

    client.traffic_mix("vid123", "2026-01-01", "2026-07-01")

    params = transport.calls[0]["params"]
    assert params["ids"] == "channel==MINE"
    assert params["startDate"] == "2026-01-01"
    assert params["endDate"] == "2026-07-01"
    assert params["dimensions"] == "insightTrafficSourceType"
    assert params["metrics"] == "views"
    assert params["filters"] == "video==vid123"


def test_client_base_url_constant():
    assert AnalyticsClient.BASE == BASE


# ---- channel-identity guard, Net A: channel_id targets every call -----------
# (see metrics.py's module docstring for the full two-net picture)

CHANNEL_ID = "UCanchorandivydatafake"


def test_no_channel_id_sends_channel_equals_mine(load_fixture):
    # A house that has not configured a channel id yet keeps today's exact
    # behaviour - this must never turn an already-green daily job red.
    transport = FakeTransport(load_fixture("analytics_core.json"))
    client = AnalyticsClient(transport)

    client.core_metrics("vid123", "2026-01-01", "2026-07-01")

    assert transport.calls[0]["params"]["ids"] == "channel==MINE"


def test_channel_id_targets_core_metrics(load_fixture):
    transport = FakeTransport(load_fixture("analytics_core.json"))
    client = AnalyticsClient(transport, channel_id=CHANNEL_ID)

    client.core_metrics("vid123", "2026-01-01", "2026-07-01")

    assert transport.calls[0]["params"]["ids"] == f"channel=={CHANNEL_ID}"


def test_channel_id_targets_retention_curve(load_fixture):
    transport = FakeTransport(load_fixture("analytics_retention.json"))
    client = AnalyticsClient(transport, channel_id=CHANNEL_ID)

    client.retention_curve("vid123", "2026-01-01", "2026-07-01")

    assert transport.calls[0]["params"]["ids"] == f"channel=={CHANNEL_ID}"


def test_channel_id_targets_traffic_mix(load_fixture):
    transport = FakeTransport(load_fixture("analytics_traffic.json"))
    client = AnalyticsClient(transport, channel_id=CHANNEL_ID)

    client.traffic_mix("vid123", "2026-01-01", "2026-07-01")

    assert transport.calls[0]["params"]["ids"] == f"channel=={CHANNEL_ID}"


def test_wrong_channel_id_gets_refused_with_403(load_fixture):
    # This is exactly the shape a mismatched saved sign-in produces: YouTube
    # refuses rather than happily returning some other channel's numbers.
    client = AnalyticsClient(RaisingTransport(MetricsApiError(403, "insufficient permission")),
                              channel_id=CHANNEL_ID)

    with pytest.raises(MetricsApiError) as excinfo:
        client.core_metrics("vid123", "2026-01-01", "2026-07-01")
    assert excinfo.value.status == 403


# ---- probe_channel: the cheap preflight call (Net B's live half) ------------


def test_probe_channel_sends_a_minimal_one_day_channel_query():
    transport = FakeTransport({"columnHeaders": [], "rows": []})
    client = AnalyticsClient(transport, channel_id=CHANNEL_ID)

    client.probe_channel("2026-08-25")

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == f"{BASE}/reports"
    params = call["params"]
    assert params["ids"] == f"channel=={CHANNEL_ID}"
    assert params["startDate"] == "2026-08-25"
    assert params["endDate"] == "2026-08-25"
    assert params["metrics"] == "views"
    assert "filters" not in params  # no video needed - this never depends on one existing


def test_probe_channel_with_no_channel_id_uses_channel_equals_mine():
    transport = FakeTransport({"columnHeaders": [], "rows": []})
    client = AnalyticsClient(transport)

    client.probe_channel("2026-08-25")

    assert transport.calls[0]["params"]["ids"] == "channel==MINE"


def test_probe_channel_propagates_a_403_unchanged():
    error = MetricsApiError(403, "insufficient permission")
    client = AnalyticsClient(RaisingTransport(error), channel_id=CHANNEL_ID)

    with pytest.raises(MetricsApiError) as excinfo:
        client.probe_channel("2026-08-25")
    assert excinfo.value is error
