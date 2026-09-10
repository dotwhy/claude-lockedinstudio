"""Prospect discovery + email discovery via the YouTube Data API v3.

Where creator emails actually come from (NOT Apollo — that's a B2B corporate
database with no creators in it): creators publish a business email in their own
public text — the channel About description and their recent video descriptions
("Business enquiries: name@gmail.com"). The API returns that text, so we regex
the email straight out of it. No third-party enrichment, no captcha.

The one email we do NOT touch is the separate "View email address" button on the
About page — that's captcha-gated on purpose and off limits. We don't need it;
most creators also paste the same address into their descriptions.
"""
import re

from googleapiclient.discovery import build

import config
import db

# Matches a plain email in free text; skips obvious noise later.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_SKIP = ("example.com", "youtube.com", "sentry", "noreply", ".png", ".jpg")


def _client():
    if not config.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY, cache_discovery=False)


def _pick_email(*texts):
    """Return the first plausible business email found across the given texts."""
    for t in texts:
        if not t:
            continue
        for m in _EMAIL_RE.findall(t):
            low = m.lower()
            if not any(s in low for s in _SKIP):
                return low
    return None


def extract_email(channel_id, channel_description="", latest_video_ids=None):
    """Pull a business email from the channel description + recent video
    descriptions. Returns an email or None."""
    yt = _client()
    texts = [channel_description]
    ids = latest_video_ids or []
    if ids:
        vids = yt.videos().list(id=",".join(ids[:5]), part="snippet").execute()
        texts += [v["snippet"].get("description", "") for v in vids.get("items", [])]
    return _pick_email(*texts)


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
            video_ids = []
            if uploads:
                pl = yt.playlistItems().list(
                    playlistId=uploads, part="snippet", maxResults=5
                ).execute()
                items = pl.get("items", [])
                if items:
                    latest = items[0]["snippet"]["title"]
                    video_ids = [it["snippet"]["resourceId"]["videoId"] for it in items]

            # Email from the channel's own public text — no Apollo, no captcha.
            email = extract_email(ch["id"], ch["snippet"].get("description", ""), video_ids)
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
