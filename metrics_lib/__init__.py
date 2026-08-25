"""Part C metrics puller - read-only YouTube metrics into weekly_snapshots.csv.

Impressions + CTR come from the YouTube Reporting API reach reports; views and
retention come from the YouTube Analytics API. Both are read-only under one
sign-in scope (yt-analytics.readonly). See ../README.md and the design spec at
second-brain/docs/specs/2026-07-12-part-c-metrics-puller-design.md.
"""
