"""Batching, quota discipline, and the discovery split.

The bug these tests exist for: snapshot_all() used to resolve every tracked
channel with search.list (100 units each), which at 174 channels cost 17,400
units against a 10,000/day quota. The job died partway through every week and
growth history came out full of holes.

A FakeYouTube counts calls by type so the tests can assert on quota shape, not
just on results.
"""
import importlib

import pytest


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class FakeYouTube:
    """Minimal stand-in for the googleapiclient resource, with call counters."""

    def __init__(self, channels=None, search_hits=None, fail_batches=()):
        self._channels = channels or {}
        self._search_hits = search_hits or {}
        self._fail_batches = set(fail_batches)
        self.calls = {"search": 0, "channels": 0, "playlistItems": 0, "videos": 0}
        self.batch_sizes = []

    # -- channels.list --
    def channels(self):
        return self

    # -- search.list --
    def search(self):
        return self

    def playlistItems(self):
        return self

    def videos(self):
        return self

    def list(self, **kwargs):
        # channels.list is the only one called with an `id` + snippet,statistics
        part = kwargs.get("part", "")
        if "statistics" in part and "id" in kwargs:
            ids = kwargs["id"].split(",")
            self.calls["channels"] += 1
            self.batch_sizes.append(len(ids))
            if tuple(sorted(ids)) in self._fail_batches:
                raise RuntimeError("simulated API failure")
            items = [self._channels[i] for i in ids if i in self._channels]
            return FakeRequest({"items": items})
        if kwargs.get("type") == "channel":
            self.calls["search"] += 1
            hit = self._search_hits.get(kwargs.get("q"))
            items = [{"snippet": {"channelId": hit}}] if hit else []
            return FakeRequest({"items": items})
        if "playlistId" in kwargs:
            self.calls["playlistItems"] += 1
            return FakeRequest({"items": []})
        self.calls["videos"] += 1
        return FakeRequest({"items": []})


def channel(channel_id, name, subs):
    return {
        "id": channel_id,
        "snippet": {"title": name, "description": ""},
        "statistics": {"subscriberCount": str(subs), "viewCount": "1000",
                       "videoCount": "50"},
        "contentDetails": {"relatedPlaylists": {"uploads": f"UU{channel_id[2:]}"}},
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "YOUTUBE_API_KEY", "fake-key")
    import db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module.config, "DB_PATH", str(tmp_path / "test.db"))
    db_module.init()
    import youtube_sourcing
    importlib.reload(youtube_sourcing)
    return db_module, youtube_sourcing, config


# --- batching ---------------------------------------------------------------
def test_resolves_in_batches_of_fifty(env):
    _, sourcing, _ = env
    channels = {f"UC{i:03d}": channel(f"UC{i:03d}", f"Ch{i}", 50_000) for i in range(174)}
    fake = FakeYouTube(channels=channels)

    resolved = sourcing.channels_by_ids(list(channels), yt=fake)

    assert len(resolved) == 174
    # 174 channels -> ceil(174/50) = 4 calls, NOT 174.
    assert fake.calls["channels"] == 4
    assert fake.calls["search"] == 0
    assert fake.batch_sizes == [50, 50, 50, 24]


def test_quota_stays_under_the_daily_cap(env):
    """The regression test for the 17,400-unit bug."""
    _, sourcing, _ = env
    channels = {f"UC{i:03d}": channel(f"UC{i:03d}", f"Ch{i}", 50_000) for i in range(174)}
    fake = FakeYouTube(channels=channels)

    sourcing.channels_by_ids(list(channels), yt=fake)

    units = fake.calls["channels"] * 1 + fake.calls["search"] * 100
    assert units <= 10, f"expected single-digit units, spent {units}"


def test_exactly_fifty_is_one_call(env):
    _, sourcing, _ = env
    channels = {f"UC{i:03d}": channel(f"UC{i:03d}", f"Ch{i}", 50_000) for i in range(50)}
    fake = FakeYouTube(channels=channels)
    sourcing.channels_by_ids(list(channels), yt=fake)
    assert fake.calls["channels"] == 1


def test_fifty_one_is_two_calls(env):
    _, sourcing, _ = env
    channels = {f"UC{i:03d}": channel(f"UC{i:03d}", f"Ch{i}", 50_000) for i in range(51)}
    fake = FakeYouTube(channels=channels)
    sourcing.channels_by_ids(list(channels), yt=fake)
    assert fake.calls["channels"] == 2
    assert fake.batch_sizes == [50, 1]


def test_empty_input_makes_no_calls(env):
    _, sourcing, _ = env
    fake = FakeYouTube()
    assert sourcing.channels_by_ids([], yt=fake) == {}
    assert fake.calls["channels"] == 0


# --- snapshot resilience ----------------------------------------------------
def test_one_failed_batch_does_not_lose_the_others(env, monkeypatch):
    db, sourcing, _ = env
    for i in range(60):
        db.upsert_candidate(f"UC{i:03d}", f"Ch{i}", 50_000)

    channels = {f"UC{i:03d}": channel(f"UC{i:03d}", f"Ch{i}", 50_000) for i in range(60)}
    first_batch = tuple(sorted(f"UC{i:03d}" for i in range(50)))
    fake = FakeYouTube(channels=channels, fail_batches=[first_batch])
    monkeypatch.setattr(sourcing, "_client", lambda: fake)

    taken = sourcing.snapshot_all()

    # The failing batch is lost, but the surviving 10 are still captured —
    # a hole in one batch must not cost every other channel its history.
    assert taken == 10


def test_snapshot_with_nothing_tracked_is_a_noop(env, monkeypatch):
    _, sourcing, _ = env
    fake = FakeYouTube()
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    assert sourcing.snapshot_all() == 0
    assert fake.calls["channels"] == 0


# --- growth scoring + promotion --------------------------------------------
def test_growth_is_denormalized_onto_candidates(env, monkeypatch):
    db, sourcing, cfg = env
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    # Two snapshots 10 days apart showing +12%.
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    conn = db.connect()
    for subs, days_ago in [(80_000, 10), (89_600, 0)]:
        conn.execute(
            "INSERT INTO channel_snapshots (channel_id, channel_name, "
            "subscriber_count, view_count, video_count, captured_at) "
            "VALUES (?,?,?,?,?,?)",
            ("UC1", "SammyGames", subs, 1, 1, (now - timedelta(days=days_ago)).isoformat()),
        )
    conn.commit()
    conn.close()

    sourcing.score_growth()

    row = db.candidates_needing_email()[0]
    assert row["growth_signal"] == "growing"
    assert round(row["growth_pct"], 1) == 12.0


def test_promotion_only_takes_growing_and_reachable(env):
    db, sourcing, _ = env
    db.upsert_candidate("UC1", "Growing+Email", 80_000)
    db.upsert_candidate("UC2", "Growing+NoEmail", 80_000)
    db.upsert_candidate("UC3", "Stable+Email", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_growth("UC2", "growing", 9.0)
    db.set_candidate_growth("UC3", "stable", 0.3)
    db.set_candidate_email("UC1", "a@b.com")
    db.set_candidate_email("UC3", "c@d.com")

    assert sourcing.promote_growing() == 1
    assert [p["email"] for p in db.prospects_by_status("new")] == ["a@b.com"]


# --- discovery split --------------------------------------------------------
def test_channel_with_email_becomes_a_prospect(env, monkeypatch):
    db, sourcing, cfg = env
    ch = channel("UC1", "SammyGames", 80_000)
    ch["snippet"]["description"] = "business: sammy@biz.com"
    fake = FakeYouTube(channels={"UC1": ch}, search_hits={"roblox": "UC1"})
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    monkeypatch.setattr(cfg, "YOUTUBE_SEARCH_TERMS", ["roblox"])

    prospects, candidates = sourcing.discover()

    assert (prospects, candidates) == (1, 0)
    assert db.prospects_by_status("new")[0]["email"] == "sammy@biz.com"


def test_channel_without_email_becomes_a_candidate(env, monkeypatch):
    db, sourcing, cfg = env
    ch = channel("UC1", "SammyGames", 80_000)
    ch["snippet"]["description"] = "subscribe for more"
    fake = FakeYouTube(channels={"UC1": ch}, search_hits={"roblox": "UC1"})
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    monkeypatch.setattr(cfg, "YOUTUBE_SEARCH_TERMS", ["roblox"])

    prospects, candidates = sourcing.discover()

    assert (prospects, candidates) == (0, 1)
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert db.candidates_needing_email()[0]["channel_name"] == "SammyGames"


def test_channel_above_the_ceiling_is_ignored(env, monkeypatch):
    # The ceiling exists so discovery doesn't queue 20M-sub channels that will
    # never answer a cold email but still consume send-cap slots.
    db, sourcing, cfg = env
    ch = channel("UC1", "Enormous", 20_000_000)
    fake = FakeYouTube(channels={"UC1": ch}, search_hits={"roblox": "UC1"})
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    monkeypatch.setattr(cfg, "YOUTUBE_SEARCH_TERMS", ["roblox"])

    assert sourcing.discover() == (0, 0)


def test_channel_below_the_floor_is_ignored(env, monkeypatch):
    db, sourcing, cfg = env
    ch = channel("UC1", "Tiny", 500)
    fake = FakeYouTube(channels={"UC1": ch}, search_hits={"roblox": "UC1"})
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    monkeypatch.setattr(cfg, "YOUTUBE_SEARCH_TERMS", ["roblox"])

    assert sourcing.discover() == (0, 0)


# --- fast track: big + active skips the growth gate -------------------------
def recent_video(days_ago):
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return [{"title": "New upload", "description": "", "published_at": stamp, "views": 1}]


def test_big_and_active_is_fast_track(env):
    _, sourcing, _ = env
    assert sourcing.is_fast_track(800_000, recent_video(3)) is True


def test_big_but_dormant_is_not_fast_track(env):
    # A large channel that stopped uploading has an audience that already
    # moved on — worse to build for than a small live one.
    _, sourcing, _ = env
    assert sourcing.is_fast_track(800_000, recent_video(200)) is False


def test_small_but_active_is_not_fast_track(env):
    # Small channels still have to prove they're growing.
    _, sourcing, _ = env
    assert sourcing.is_fast_track(60_000, recent_video(2)) is False


def test_exactly_at_the_fasttrack_threshold_qualifies(env):
    _, sourcing, cfg = env
    assert sourcing.is_fast_track(cfg.FASTTRACK_SUBSCRIBERS, recent_video(1)) is True


def test_no_videos_is_not_active(env):
    _, sourcing, _ = env
    assert sourcing.is_fast_track(800_000, []) is False
    assert sourcing.is_fast_track(800_000, None) is False


def test_unparseable_upload_date_is_not_active(env):
    _, sourcing, _ = env
    videos = [{"title": "x", "description": "", "published_at": "not-a-date", "views": 1}]
    assert sourcing.is_fast_track(800_000, videos) is False


def test_fast_track_candidate_promotes_without_a_growth_signal(env):
    # THE point of fast-track: no waiting two snapshot cycles to contact a
    # channel that is already established.
    db, sourcing, _ = env
    db.upsert_candidate("UC1", "BigActive", 900_000, fast_track=True)
    db.set_candidate_email("UC1", "big@channel.tv")

    assert db.candidates_to_promote()[0]["channel_name"] == "BigActive"
    assert sourcing.promote_growing() == 1


def test_non_fast_track_candidate_still_waits_for_growth(env):
    db, sourcing, _ = env
    db.upsert_candidate("UC1", "SmallUnknown", 60_000, fast_track=False)
    db.set_candidate_email("UC1", "small@channel.tv")

    assert db.candidates_to_promote() == []
    assert sourcing.promote_growing() == 0


def test_fast_track_candidates_sort_first_in_the_digest(env):
    db, _, _ = env
    db.upsert_candidate("UC1", "HugeActive", 900_000, fast_track=True)
    db.upsert_candidate("UC2", "BiggerButGrowing", 2_000_000, fast_track=False)
    db.set_candidate_growth("UC2", "growing", 9.0)

    names = [r["channel_name"] for r in db.candidates_needing_email()]
    assert names[0] == "HugeActive"


def test_fast_track_is_re_evaluated_on_rediscovery(env):
    # A channel that goes quiet should lose fast-track, not keep it forever
    # because of how it looked the first time we saw it.
    db, _, _ = env
    db.upsert_candidate("UC1", "WasActive", 900_000, fast_track=True)
    db.upsert_candidate("UC1", "WasActive", 900_000, fast_track=False)
    assert db.candidates_to_promote() == []


# --- growth is stored on prospects too --------------------------------------
def test_growth_lands_on_prospects_not_just_candidates(env):
    db, sourcing, _ = env
    db.upsert_prospect(channel_id="UC1", channel_name="Sammy",
                       email="s@x.com", source="test")
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    conn = db.connect()
    for subs, days_ago in [(100_000, 10), (115_000, 0)]:
        conn.execute(
            "INSERT INTO channel_snapshots (channel_id, channel_name, "
            "subscriber_count, view_count, video_count, captured_at) VALUES (?,?,?,?,?,?)",
            ("UC1", "Sammy", subs, 1, 1, (now - timedelta(days=days_ago)).isoformat()),
        )
    conn.commit()
    conn.close()

    sourcing.score_growth()

    prospect = db.prospects_by_status("new")[0]
    assert prospect["growth_signal"] == "growing"
    assert round(prospect["growth_pct"], 1) == 15.0


def test_apply_growth_reports_real_row_counts(env):
    # The old loop counted iterations, not rows written, so the log claimed
    # every tracked channel had been updated even when none matched.
    db, _, _ = env
    db.upsert_candidate("UC1", "Known", 80_000)
    candidates, prospects = db.apply_growth({
        "UC1": ("growing", 9.0),
        "UC-does-not-exist": ("growing", 9.0),
    })
    assert candidates == 1
    assert prospects == 0


def test_apply_growth_with_nothing_to_do(env):
    db, _, _ = env
    assert db.apply_growth({}) == (0, 0)


def test_duplicate_channel_is_not_logged_as_a_candidate(env, monkeypatch):
    # The old code called _log_candidate() whenever upsert_prospect returned
    # False — which includes "already a prospect" — so every rediscovery
    # re-logged the same channel as needing an email.
    db, sourcing, cfg = env
    ch = channel("UC1", "SammyGames", 80_000)
    ch["snippet"]["description"] = "business: sammy@biz.com"
    fake = FakeYouTube(channels={"UC1": ch}, search_hits={"roblox": "UC1"})
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    monkeypatch.setattr(cfg, "YOUTUBE_SEARCH_TERMS", ["roblox"])

    sourcing.discover()
    prospects, candidates = sourcing.discover()

    assert (prospects, candidates) == (0, 0)
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert db.candidates_needing_email() == []


# --- the search fallback cap ------------------------------------------------
def test_enrich_respects_the_fallback_cap(env, monkeypatch):
    db, sourcing, cfg = env
    monkeypatch.setattr(cfg, "MAX_SEARCH_FALLBACKS", 3)
    for i in range(10):
        db.upsert_prospect(channel_name=f"Ch{i}", email=f"c{i}@x.com", source="sheet")

    fake = FakeYouTube()
    monkeypatch.setattr(sourcing, "_client", lambda: fake)
    sourcing.enrich_prospects()

    # 10 id-less prospects would be 1,000 units unbounded. Capped at 3.
    assert fake.calls["search"] == 3
