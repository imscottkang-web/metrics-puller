"""What the channel says it has published: the daily pull's half of the channel watcher.

**Why this exists (audit-repair 05).** Scott posts a video and nothing goes and finds it. The
video id has to be typed into a script folder by hand before anything can grade it. The fix is
in two halves, in two repositories on purpose: the MATCHING lives beside the script folders,
where it is blind to whose channel it is reading, and THIS half - the wire - is the daily pull
learning what the channel has published and writing it down where the matcher reads it.

**Why a second API and a second credential.** The rest of this tool reads the PRIVATE numbers
through the Analytics API, whose token is scoped to yt-analytics.readonly. That API answers in
video ids and never in titles, and a title is the only thing a folder and a video can be
matched on without guessing. So the list comes from the PUBLIC Data API instead - the same
free, key-based API the radar already uses - reading only what the channel already shows
anyone who visits it. Nothing here can post, delete, or see a private number.

**Missing configuration is a setup state, not a failure.** With no key or no channel id, this
is skipped with one plain line and the pull records every number it always did. A run that
went red for a feature nobody had switched on yet would train Scott to ignore a red run.

Read-only and side-effect-free apart from the one file :func:`write_published_videos` writes.
"""

import json
from pathlib import Path

BASE = "https://www.googleapis.com/youtube/v3"

# How far back the list reaches. The watcher only ever acts on a video that is not already
# logged in a folder, so a long list costs nothing but quota; 50 is one page, one quota unit,
# and comfortably more videos than this channel publishes between two daily runs.
DEFAULT_LIMIT = 50


class UploadsClient:
    """Read-only client for the two public Data API calls this needs."""

    BASE = BASE

    def __init__(self, transport) -> None:
        self._transport = transport

    def uploads_playlist(self, channel_id: str) -> str | None:
        """The id of the channel's uploads playlist, or None if the channel is not found.

        Every channel has one, and it is the only listing that returns EVERY upload in
        publish order. The alternative (search.list) is 100 quota units, is eventually
        consistent, and can silently omit a video posted minutes ago - which is exactly the
        video this whole seam exists to catch.
        """
        data = self._transport.get(
            f"{self.BASE}/channels", {"part": "contentDetails", "id": channel_id})
        items = data.get("items") or []
        if not items:
            return None
        related = (items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}
        return related.get("uploads")

    def published_videos(self, playlist_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """The channel's most recent uploads: id, title, and the day each went out.

        The publish day is read off contentDetails.videoPublishedAt, NOT off the snippet's
        own publishedAt. They differ: the snippet's date is when the video was added to the
        uploads playlist, which for a video published from a scheduled draft is the day it
        was uploaded rather than the day it went live - and the day it went live is what the
        milestone windows count from.

        A row with no video id is skipped rather than returned half-formed; a row missing
        only its publish date is returned with None there, because an id and a title are
        still enough to fill a folder in.
        """
        data = self._transport.get(
            f"{self.BASE}/playlistItems",
            {"part": "snippet,contentDetails", "playlistId": playlist_id,
             "maxResults": min(int(limit), 50)},
        )
        videos = []
        for item in data.get("items") or []:
            details = item.get("contentDetails") or {}
            snippet = item.get("snippet") or {}
            video_id = str(details.get("videoId")
                           or (snippet.get("resourceId") or {}).get("videoId") or "").strip()
            if not video_id:
                continue
            published_at = str(details.get("videoPublishedAt") or "").strip()
            videos.append({
                "video_id": video_id,
                "title": str(snippet.get("title") or "").strip(),
                # The API answers a full timestamp; the day is what a bet card's date row
                # holds and what the checkpoint windows count from.
                "published_on": published_at[:10] or None,
            })
        return videos


def gather_published_videos(client, channel_id: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
    """The channel's published videos, in one call from the caller's point of view."""
    playlist_id = client.uploads_playlist(channel_id)
    if not playlist_id:
        return []
    return client.published_videos(playlist_id, limit=limit)


def write_published_videos(path, videos, *, pulled_on: str) -> Path:
    """Write the list where the matcher reads it, and say when it was pulled.

    The date on the record is load-bearing rather than decoration: this file is committed by
    the daily job and read later, somewhere else, so a list that has stopped being refreshed
    has to be visibly stale instead of quietly trusted as today's.

    Written to a temp file in the same directory and moved into place, so a crash mid-write
    can only ever leave the old list or the complete new one - never a half-written file the
    matcher would refuse and report as a channel with nothing on it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"pulled_on": pulled_on, "videos": list(videos)}
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    tmp_path.replace(path)
    return path
