"""Discovery, snapshots, and promotion — the front half of the pipeline.

Where creator emails actually come from (NOT Apollo — that's a B2B corporate
database with no creators in it): creators publish a business email in their own
public text — the channel About description and their recent video descriptions
("Business enquiries: name@gmail.com"). The API returns that text, so we regex
the email straight out of it. No third-party enrichment, no captcha.

The one email we do NOT touch is the separate "View email address" button on the
About page — that's captcha-gated on purpose and off limits. We don't need it;
most creators also paste the same address into their descriptions.

QUOTA IS THE DESIGN CONSTRAINT HERE. The free tier is 10,000 units/day:

    search.list        100 units   <- expensive, avoid in loops
    channels.list        1 unit    <- accepts 50 ids per call
    playlistItems.list   1 unit
    videos.list          1 unit

The previous version resolved every tracked channel with search.list, one call
each. At 174 channels that was 17,400 units for a single weekly snapshot — 74%
over the entire daily budget, so the job died partway through every Monday and
growth history came out full of holes. Snapshots now resolve by stored
channel_id through channels.list in batches of 50: ~4 units for the same work.
"""
import logging
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build

import config
import db
import emails
import growth
import netretry

log = logging.getLogger("sourcing")


def _client():
    if not config.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set in .env")
    return build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY, cache_discovery=False)


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _stats(channel):
    """Flatten a channels.list item into the fields we store."""
    stats = channel.get("statistics", {})
    return {
        "channel_id": channel["id"],
        "channel_name": channel["snippet"]["title"],
        "subs": int(stats.get("subscriberCount", 0) or 0),
        "views": int(stats.get("viewCount", 0) or 0),
        "videos": int(stats.get("videoCount", 0) or 0),
        "description": channel["snippet"].get("description", ""),
        "uploads": channel.get("contentDetails", {})
                          .get("relatedPlaylists", {}).get("uploads"),
    }


def channels_by_ids(channel_ids, yt=None):
    """Resolve many channels in as few API calls as possible.

    50 ids per channels.list call, ~1 unit each. This is the function that
    turns a 17,400-unit snapshot into a 4-unit one.
    """
    yt = yt or _client()
    resolved = {}
    for batch in _chunks(list(channel_ids), config.CHANNEL_BATCH_SIZE):
        response = netretry.call(
            yt.channels().list(
                id=",".join(batch), part="snippet,statistics,contentDetails").execute,
            what=f"youtube channels.list({len(batch)} ids)")
        for item in response.get("items", []):
            resolved[item["id"]] = _stats(item)
    return resolved


def _recent_video_ids(yt, uploads_playlist, limit=5):
    if not uploads_playlist:
        return []
    response = netretry.call(
        yt.playlistItems().list(
            playlistId=uploads_playlist, part="snippet", maxResults=limit).execute,
        what="youtube playlistItems.list")
    return [it["snippet"]["resourceId"]["videoId"] for it in response.get("items", [])]


def _recent_videos(yt, uploads_playlist, limit=5):
    """Titles + view counts for recent uploads, used to personalize the opener.

    Two calls (playlistItems + videos) so the opener can say "this one did 3x
    your usual" instead of just naming a title.
    """
    video_ids = _recent_video_ids(yt, uploads_playlist, limit)
    if not video_ids:
        return []
    response = netretry.call(
        yt.videos().list(id=",".join(video_ids), part="snippet,statistics").execute,
        what="youtube videos.list")
    videos = []
    for item in response.get("items", []):
        videos.append({
            "title": item["snippet"]["title"],
            "description": item["snippet"].get("description", ""),
            "published_at": item["snippet"].get("publishedAt"),
            "views": int(item.get("statistics", {}).get("viewCount", 0) or 0),
        })
    return videos


def posts_actively(videos, now=None):
    """Has this channel uploaded inside the activity window?

    A big dormant channel is worse than a small live one — the audience has
    already moved on, and a game built for them launches to nobody. So size
    alone never fast-tracks; size AND a recent upload does.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=config.ACTIVE_UPLOAD_DAYS)
    for video in videos or []:
        stamp = video.get("published_at")
        if not stamp:
            continue
        try:
            published = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        except ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published >= cutoff:
            return True
    return False


def is_fast_track(subs, videos, now=None):
    """Big enough and live enough to skip the growth gate."""
    return subs >= config.FASTTRACK_SUBSCRIBERS and posts_actively(videos, now=now)


def extract_email(channel_description="", videos=None):
    """Business email from the channel description, then video descriptions."""
    texts = [channel_description] + [v.get("description", "") for v in (videos or [])]
    return emails.first_in(*texts)


# --- discovery --------------------------------------------------------------
def discover(max_per_term=25):
    """Search each configured term and record everything inside the sub band.

    Channels WITH a findable email become prospects immediately. Channels
    without one become candidates — tracked, snapshotted, and surfaced in the
    weekly digest so you can supply the address by replying to it.
    """
    yt = _client()
    seen = set()
    added_prospects = 0
    added_candidates = 0
    fast_tracked = 0

    for term in config.YOUTUBE_SEARCH_TERMS:
        search = netretry.call(
            yt.search().list(
                q=term, type="channel", part="snippet", maxResults=max_per_term).execute,
            what=f"youtube search.list({term!r})")
        ids = [it["snippet"]["channelId"] for it in search.get("items", [])]
        ids = [c for c in ids if c not in seen]
        seen.update(ids)
        if not ids:
            continue

        for channel_id, info in channels_by_ids(ids, yt=yt).items():
            if not (config.MIN_SUBSCRIBERS <= info["subs"] <= config.MAX_SUBSCRIBERS):
                continue

            videos = _recent_videos(yt, info["uploads"])
            latest = videos[0]["title"] if videos else None
            email = extract_email(info["description"], videos)
            profile_url = f"https://www.youtube.com/channel/{channel_id}"
            fast_track = is_fast_track(info["subs"], videos)

            if email:
                # Reachable right now. Channels with an email never wait on a
                # growth signal regardless of size — the signal only ever
                # decides whether it's worth chasing an address we don't have.
                created = db.upsert_prospect(
                    channel_id=channel_id, channel_name=info["channel_name"],
                    email=email, subscriber_count=info["subs"],
                    latest_video=latest, profile_url=profile_url, source="youtube",
                )
                if created:
                    added_prospects += 1
                    if fast_track:
                        fast_tracked += 1
                    # Numbers the opener line reasons about. Captured now,
                    # while we already have the video data in hand — fetching
                    # them again at personalize time would cost quota per
                    # prospect for data we've already paid for.
                    avg = int(info["views"] / info["videos"]) if info["videos"] else None
                    db.set_video_stats(
                        channel_id,
                        videos[0]["views"] if videos else None,
                        avg,
                    )
                # upsert refusing (already a prospect, or suppressed/bounced)
                # must NOT fall through to upsert_candidate — the old version
                # did exactly that and re-logged every duplicate on every run.
                continue

            if db.upsert_candidate(
                channel_id=channel_id, channel_name=info["channel_name"],
                subscriber_count=info["subs"], latest_video=latest,
                profile_url=profile_url, fast_track=fast_track,
            ):
                added_candidates += 1
                if fast_track:
                    fast_tracked += 1

    log.info(f"Discovery: {added_prospects} new prospect(s), "
             f"{added_candidates} new candidate(s) needing an email, "
             f"{fast_tracked} fast-tracked (>={config.FASTTRACK_SUBSCRIBERS:,} and active).")
    return added_prospects, added_candidates


# --- the scanner ------------------------------------------------------------
def snapshot_all():
    """Capture subscriber counts for every tracked channel, then score growth.

    Two phases, both batched:
      1. channels.list in chunks of 50 -> one snapshot row per channel
      2. ONE query for all snapshot history -> growth label per candidate

    Neither phase issues a per-channel query or a per-channel API call.
    """
    tracked = db.channels_to_track()
    if not tracked:
        log.info("Nothing tracked yet — no snapshot taken.")
        return 0

    ids = [row["channel_id"] for row in tracked]
    yt = _client()
    taken = 0
    for batch in _chunks(ids, config.CHANNEL_BATCH_SIZE):
        try:
            resolved = channels_by_ids(batch, yt=yt)
        except Exception:
            # One bad batch must not cost the other 170 channels their
            # snapshot — a hole in the history breaks growth detection for
            # everyone, not just the failed batch.
            log.exception(f"Snapshot batch of {len(batch)} failed, continuing")
            continue
        for channel_id, info in resolved.items():
            db.record_snapshot(channel_id, info["channel_name"], info["subs"],
                               info["views"], info["videos"], None)
            taken += 1

    scored = score_growth()
    log.info(f"Snapshot: {taken} channel(s) captured, {scored} growth label(s) updated.")
    return taken


def score_growth():
    """Recompute growth for every tracked channel from stored snapshots.

    Denormalized onto the rows so promotion and the digest each read one
    indexed column instead of re-deriving the trend from history.

    One query for all snapshots, one bulk write per table. The earlier version
    looped over tracked channels issuing an UPDATE each, most of which matched
    no row at all, and reported the loop count as if every one had landed.
    """
    grouped = growth.by_channel(db.all_snapshots())
    labels = {}
    for channel_id, snapshots in grouped.items():
        signal, pct, _ = growth.details(snapshots, window_days=config.GROWTH_WINDOW_DAYS)
        labels[channel_id] = (signal, pct)

    candidates, prospects = db.apply_growth(labels)
    log.info(f"Growth scored for {len(labels)} channel(s): "
             f"{candidates} candidate row(s), {prospects} prospect row(s) updated.")
    return candidates + prospects


def promote_growing():
    """Move actionable candidates that have an email into the outreach sequence.

    This is the handoff: everything before it is research, everything after it
    sends email. A candidate crosses when it is reachable AND either rising or
    fast-tracked (already big and still posting).
    """
    promoted = 0
    for candidate in db.candidates_to_promote():
        if not db.promote_candidate(candidate):
            continue
        promoted += 1
        # Fast-tracked rows can have no growth_pct at all — they were promoted
        # on size and activity, without ever waiting for a second snapshot.
        pct = candidate["growth_pct"]
        why = f"{pct:+.1f}%" if pct is not None else "fast-track"
        log.info(f"  [promoted] {candidate['channel_name']} "
                 f"({candidate['subscriber_count']:,} subs, {why})")
    log.info(f"Promoted {promoted} candidate(s) into the sequence.")
    return promoted


# --- enrichment for rows that arrived without a channel_id ------------------
def _find_channel(name, yt=None):
    """Resolve a channel by name via search. Costs 100 units — used sparingly.

    Only needed for prospects imported from a CSV, which have an email and a
    name but no channel_id. Discovery never needs this.
    """
    yt = yt or _client()
    res = netretry.call(
        yt.search().list(q=name, type="channel", part="snippet", maxResults=1).execute,
        what=f"youtube search.list({name!r})")
    items = res.get("items", [])
    if not items:
        return None
    channel_id = items[0]["snippet"]["channelId"]
    resolved = channels_by_ids([channel_id], yt=yt)
    return resolved.get(channel_id)


def enrich_prospects():
    """Fill channel_id and stats for prospects that arrived without them.

    Capped at MAX_SEARCH_FALLBACKS per run. Each miss costs 100 units, so a
    bulk CSV import of id-less rows could otherwise drain the day's quota in
    one pass and take the snapshot job down with it.
    """
    yt = _client()
    filled = 0
    searches = 0

    for prospect in db.all_emailable():
        if prospect["channel_id"] and prospect["subscriber_count"]:
            continue
        if searches >= config.MAX_SEARCH_FALLBACKS:
            log.warning(
                f"Hit the search fallback cap ({config.MAX_SEARCH_FALLBACKS}). "
                "Remaining prospects will be enriched next run."
            )
            break

        name = prospect["channel_name"] or prospect["first_name"] or prospect["email"].split("@")[0]
        searches += 1
        try:
            info = _find_channel(name, yt=yt)
        except Exception:
            log.exception(f"Enrich failed for {name}")
            continue
        if not info:
            continue

        db.set_channel_data(prospect["id"], info["channel_id"], info["subs"], None)
        filled += 1
        log.info(f"  [enriched] {info['channel_name']}: {info['subs']:,} subs")

    log.info(f"Enriched {filled} prospect(s) using {searches} search call(s).")
    return filled
