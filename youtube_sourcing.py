"""Prospect discovery via the YouTube Data API v3.

IMPORTANT — the email gap:
YouTube's API does NOT expose channel contact emails. A creator's business
email sits on their About page behind a captcha ("I'm not a robot") reveal,
and bypassing that is off the table. So this module discovers and ranks
*candidates* (channel, subs, latest video) and stores them with a blank email.
Fill the email in one of two ways:
  1. Export these rows, enrich them in Apollo/Clay, re-import via `run.py import`.
  2. Implement `enrich_email()` below against whatever provider you use.
Rows without an email are simply never contacted.
"""
from googleapiclient.discovery import build

import config
import db


def _client():
    if not config.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY, cache_discovery=False)


def enrich_email(channel):
    """Plug your email-enrichment provider in here (Apollo/Clay/etc.).
    Return an email string or None. Left unimplemented on purpose."""
    return None


def discover(max_per_term=25):
    """Search each configured term, keep channels inside the subscriber band,
    attach their latest video title, and upsert them as 'new' prospects."""
    yt = _client()
    seen = set()
    added = 0

    for term in config.YOUTUBE_SEARCH_TERMS:
        search = yt.search().list(
            q=term, type="channel", part="snippet", maxResults=max_per_term
        ).execute()
        channel_ids = [it["snippet"]["channelId"] for it in search.get("items", [])]
        channel_ids = [c for c in channel_ids if c not in seen]
        seen.update(channel_ids)
        if not channel_ids:
            continue

        details = yt.channels().list(
            id=",".join(channel_ids), part="snippet,statistics,contentDetails"
        ).execute()

        for ch in details.get("items", []):
            subs = int(ch["statistics"].get("subscriberCount", 0))
            if not (config.MIN_SUBSCRIBERS <= subs <= config.MAX_SUBSCRIBERS):
                continue
            name = ch["snippet"]["title"]
            uploads = ch["contentDetails"]["relatedPlaylists"].get("uploads")
            latest = None
            if uploads:
                pl = yt.playlistItems().list(
                    playlistId=uploads, part="snippet", maxResults=1
                ).execute()
                items = pl.get("items", [])
                if items:
                    latest = items[0]["snippet"]["title"]

            email = enrich_email(ch)  # returns None unless you wire a provider
            created = db.upsert_prospect(
                channel_id=ch["id"], channel_name=name, email=email,
                subscriber_count=subs, latest_video=latest, source="youtube",
            )
            if created:
                added += 1
            else:
                # No email yet: stash the candidate so you can enrich + import later.
                _log_candidate(ch["id"], name, subs, latest)

    return added


def _log_candidate(channel_id, name, subs, latest):
    """Write email-less candidates to candidates.csv for enrichment."""
    import csv
    import os

    path = "candidates.csv"
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["channel_id", "channel_name", "subscriber_count", "latest_video", "email"])
        w.writerow([channel_id, name, subs, latest or "", ""])


# --- enrichment + scanner (the "subscribers over time" engine) --------------
def _find_channel(name):
    """Best-effort: search YouTube for a handle/name, return the top channel's
    id + stats + latest video. Returns None if nothing usable."""
    yt = _client()
    res = yt.search().list(q=name, type="channel", part="snippet", maxResults=1).execute()
    items = res.get("items", [])
    if not items:
        return None
    cid = items[0]["snippet"]["channelId"]
    det = yt.channels().list(
        id=cid, part="statistics,contentDetails,snippet"
    ).execute().get("items", [])
    if not det:
        return None
    ch = det[0]
    stats = ch["statistics"]
    latest = None
    uploads = ch["contentDetails"]["relatedPlaylists"].get("uploads")
    if uploads:
        pl = yt.playlistItems().list(playlistId=uploads, part="snippet", maxResults=1).execute()
        its = pl.get("items", [])
        if its:
            latest = its[0]["snippet"]["title"]
    return {
        "channel_id": cid,
        "channel_name": ch["snippet"]["title"],
        "subs": int(stats.get("subscriberCount", 0)),
        "views": int(stats.get("viewCount", 0)),
        "videos": int(stats.get("videoCount", 0)),
        "latest_video": latest,
    }


def enrich_prospects():
    """Fill channel_id / subscriber_count / latest_video for prospects that are
    missing it, by matching their handle/name to a YouTube channel. This is what
    turns a bare email list into something you can personalize from."""
    filled = 0
    for p in db.all_emailable():
        if p["channel_id"] and p["subscriber_count"]:
            continue
        info = _find_channel(p["channel_name"] or p["first_name"] or p["email"].split("@")[0])
        if info:
            db.set_channel_data(p["id"], info["channel_id"], info["subs"], info["latest_video"])
            filled += 1
            print(f"  [enriched] {p['channel_name']}: {info['subs']:,} subs — {info['latest_video']}")
    return filled


def snapshot_all():
    """The scanner: record a fresh subscriber/view/upload snapshot for every
    tracked channel. Run weekly (cron) to build a growth-over-time history."""
    taken = 0
    for row in db.channels_to_track():
        info = _find_channel(row["channel_name"])
        if info:
            db.record_snapshot(info["channel_id"], info["channel_name"], info["subs"],
                               info["views"], info["videos"], info["latest_video"])
            taken += 1
    print(f"Snapshot taken for {taken} channel(s).")
    return taken
