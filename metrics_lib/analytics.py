"""YouTube Analytics API v2 client - views, duration, retention, traffic mix.

A thin GET-only wrapper over the shared Transport seam (metrics_lib.http):
every method below builds params, calls transport.get(), and shapes the
result into a small plain dict or list of dicts. Every value is read off the
response's columnHeaders by NAME, never by fixed column position - the API
is free to reorder columns between calls, so a position-based read would
silently pair the wrong header with the wrong value.

Transport failures raise MetricsApiError; nothing here catches it, so it
propagates to the caller unchanged. This module never touches the bearer
token or any other secret - only the injected transport does - so there is
nothing here that could print or log one.
"""

BASE = "https://youtubeanalytics.googleapis.com/v2"

_TRAFFIC_SOURCES = {
    "browse": "BROWSE",
    "search": "YT_SEARCH",
    "suggested": "SUGGESTED_VIDEO",
}


def _rows_to_dicts(column_headers: list[dict], rows: list[list]) -> list[dict]:
    """Zip columnHeaders NAMES onto each row, positionally, one dict per row.

    The API pairs row[i] with columnHeaders[i]['name'] for every row in a
    response - that positional zip happens here, exactly once; every method
    below only ever reads the resulting dicts by name.
    """
    names = [header["name"] for header in column_headers]
    return [dict(zip(names, row)) for row in rows]


class AnalyticsClient:
    """Read-only client for the YouTube Analytics API v2 `/reports` endpoint.

    channel_id, when given, is sent as "channel==<id>" on every call instead of the
    default "channel==MINE" - that is Net A of the two-net guard against a house
    running with a sign-in that belongs to a different channel (see metrics.py's module
    docstring for the full picture): with a wrong token, YouTube then answers 403
    ("insufficient permission") rather than happily returning some other channel's
    numbers. When channel_id is None (a house that has not configured one yet),
    "channel==MINE" is used exactly as before, so a house with no channel id stays on
    its existing, already-green behaviour.
    """

    BASE = BASE

    def __init__(self, transport, channel_id: str | None = None) -> None:
        self._transport = transport
        self._channel_id = channel_id

    @property
    def _ids(self) -> str:
        return f"channel=={self._channel_id}" if self._channel_id else "channel==MINE"

    def probe_channel(self, on_date: str) -> None:
        """The cheapest possible call against this client's channel: total views for
        one day, no video filter needed. Used only as a preflight identity check (Net
        B in metrics.py's run_pull) - the return value carries no useful data and is
        discarded; only whether this raises matters. With a mismatched channel id,
        YouTube answers 403 and MetricsApiError is raised same as any other call.
        """
        self._transport.get(
            f"{self.BASE}/reports",
            {
                "ids": self._ids,
                "startDate": on_date,
                "endDate": on_date,
                "metrics": "views",
            },
        )

    def core_metrics(self, video_id: str, start_date: str, end_date: str) -> dict:
        """Total views and average view duration (seconds) for one video.

        Reads the single row the API returns for an undimensioned query. If
        there are no rows at all (e.g. no data yet for the range), returns
        zeros rather than raising.
        """
        data = self._transport.get(
            f"{self.BASE}/reports",
            {
                "ids": self._ids,
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "views,averageViewDuration",
                "filters": f"video=={video_id}",
            },
        )
        rows = _rows_to_dicts(data.get("columnHeaders") or [], data.get("rows") or [])
        if not rows:
            return {"views": 0, "avg_view_duration_sec": 0}
        row = rows[0]
        return {
            "views": int(row["views"]),
            "avg_view_duration_sec": int(row["averageViewDuration"]),
        }

    def retention_curve(self, video_id: str, start_date: str, end_date: str) -> list[dict]:
        """The full retention curve: one point per elapsedVideoTimeRatio sample.

        relativeRetentionPerformance can be entirely absent from the response
        (a channel without enough history for YouTube to compute it) rather
        than present-but-null, so its presence is read per row rather than
        assumed - when absent, relative_retention is None, not a crash.
        """
        data = self._transport.get(
            f"{self.BASE}/reports",
            {
                "ids": self._ids,
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={video_id}",
            },
        )
        rows = _rows_to_dicts(data.get("columnHeaders") or [], data.get("rows") or [])
        points = []
        for row in rows:
            relative = row.get("relativeRetentionPerformance")
            points.append(
                {
                    "elapsed_ratio": float(row["elapsedVideoTimeRatio"]),
                    "audience_watch_ratio": float(row["audienceWatchRatio"]),
                    "relative_retention": float(relative) if relative is not None else None,
                }
            )
        return points

    def traffic_mix(self, video_id: str, start_date: str, end_date: str) -> dict:
        """Share of total views from BROWSE / YT_SEARCH / SUGGESTED_VIDEO.

        Each share is a percent of the video's TOTAL views across every
        traffic-source row the API returns, including sources not named
        here (e.g. EXTERNAL, NOTIFICATION) - so the three numbers returned
        need not sum to 100; that is expected, not a bug. A named source
        with no row at all counts as zero. If total views is 0, every share
        is 0.0 rather than a division error.
        """
        data = self._transport.get(
            f"{self.BASE}/reports",
            {
                "ids": self._ids,
                "startDate": start_date,
                "endDate": end_date,
                "metrics": "views",
                "dimensions": "insightTrafficSourceType",
                "filters": f"video=={video_id}",
            },
        )
        rows = _rows_to_dicts(data.get("columnHeaders") or [], data.get("rows") or [])
        by_source: dict[str, int] = {}
        for row in rows:
            source = row["insightTrafficSourceType"]
            by_source[source] = by_source.get(source, 0) + int(row["views"])
        total = sum(by_source.values())
        if total == 0:
            return {key: 0.0 for key in _TRAFFIC_SOURCES}
        return {
            key: round(100 * by_source.get(source_name, 0) / total, 2)
            for key, source_name in _TRAFFIC_SOURCES.items()
        }
