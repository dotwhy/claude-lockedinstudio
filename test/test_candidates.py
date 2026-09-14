"""Candidates table: dedup, growth storage, the ask/re-ask gate, and promotion.

Also covers the regression this whole refactor exists for: an address that
hard-bounced in August must never come back through any intake path.

Each test gets a fresh SQLite file via the tmp_db fixture, so nothing leaks
between tests and none of this touches the real /data volume.
"""
import importlib

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A fresh initialised database, with config.DB_PATH pointed at it."""
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    import db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module.config, "DB_PATH", str(tmp_path / "test.db"))
    db_module.init()
    return db_module


# --- dedup ------------------------------------------------------------------
def test_new_candidate_is_created(db):
    assert db.upsert_candidate("UC1", "SammyGames", 82_000) is True
    assert len(db.candidates_needing_email()) == 0  # no growth signal yet


def test_same_channel_twice_creates_one_row(db):
    # A channel matching five search terms must land once, not five times.
    # The old candidates.csv appended blindly and got this wrong.
    db.upsert_candidate("UC1", "SammyGames", 82_000)
    assert db.upsert_candidate("UC1", "SammyGames", 82_000) is False
    db.set_candidate_growth("UC1", "growing", 8.0)
    assert len(db.candidates_needing_email()) == 1


def test_re_upsert_refreshes_volatile_fields(db):
    db.upsert_candidate("UC1", "SammyGames", 82_000)
    db.upsert_candidate("UC1", "Sammy Games HD", 91_000)
    db.set_candidate_growth("UC1", "growing", 8.0)
    row = db.candidates_needing_email()[0]
    assert row["channel_name"] == "Sammy Games HD"
    assert row["subscriber_count"] == 91_000


def test_candidate_without_channel_id_is_rejected(db):
    # channel_id is the dedup key; a row without one can never be matched again.
    assert db.upsert_candidate(None, "Nameless") is False


# --- the digest queue -------------------------------------------------------
def test_only_growing_candidates_are_asked_about(db):
    db.upsert_candidate("UC1", "Growing", 80_000)
    db.upsert_candidate("UC2", "Flat", 80_000)
    db.upsert_candidate("UC3", "Unknown", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_growth("UC2", "stable", 0.4)
    # UC3 deliberately left with no signal computed yet.
    names = [r["channel_name"] for r in db.candidates_needing_email()]
    assert names == ["Growing"]


def test_candidate_with_an_email_is_not_asked_about(db):
    db.upsert_candidate("UC1", "HasEmail", 80_000, email="x@y.com")
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert db.candidates_needing_email() == []


def test_digest_queue_is_sorted_by_size(db):
    db.upsert_candidate("UC1", "Small", 25_000)
    db.upsert_candidate("UC2", "Big", 300_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_growth("UC2", "growing", 9.0)
    names = [r["channel_name"] for r in db.candidates_needing_email()]
    assert names == ["Big", "Small"]


def test_asking_stops_the_same_channel_recurring_next_week(db):
    # The failure this prevents: the same 12 channels in every Monday digest,
    # forever, until you hand-edit the database.
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert len(db.candidates_needing_email()) == 1

    db.mark_candidates_asked(["UC1"])
    assert db.candidates_needing_email() == []


def test_channel_returns_after_the_reask_window(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.mark_candidates_asked(["UC1"])
    # A zero-day window means "everything asked before now is fair game".
    assert len(db.candidates_needing_email(reask_after_days=0)) == 1


def test_marking_nothing_is_harmless(db):
    assert db.mark_candidates_asked([]) == 0


# --- promotion --------------------------------------------------------------
def test_growing_candidate_with_email_is_promotable(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_email("UC1", "sammy@biz.com")
    assert len(db.candidates_to_promote()) == 1


def test_growing_candidate_without_email_is_not_promotable(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert db.candidates_to_promote() == []


def test_stable_candidate_with_email_is_not_promotable(db):
    # Having an address isn't the bar — the channel has to be rising.
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "stable", 0.2)
    db.set_candidate_email("UC1", "sammy@biz.com")
    assert db.candidates_to_promote() == []


def test_promotion_creates_a_prospect(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000, latest_video="Brainrot 3")
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_email("UC1", "sammy@biz.com")

    candidate = db.candidates_to_promote()[0]
    assert db.promote_candidate(candidate) is True

    prospects = db.prospects_by_status("new")
    assert [p["email"] for p in prospects] == ["sammy@biz.com"]
    assert prospects[0]["source"] == "discovery"


def test_promotion_is_not_repeated(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_email("UC1", "sammy@biz.com")
    db.promote_candidate(db.candidates_to_promote()[0])
    assert db.candidates_to_promote() == []


def test_email_is_lowercased_on_the_way_in(db):
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    db.set_candidate_email("UC1", "Sammy@Biz.COM")
    db.set_candidate_growth("UC1", "growing", 9.0)
    assert db.candidates_to_promote()[0]["email"] == "sammy@biz.com"


# --- the bounce regression --------------------------------------------------
def test_august_bounces_are_suppressed_on_init(db):
    assert db.is_suppressed("flamingo@ellify.com")
    assert db.is_suppressed("musa@lyaison.com")


def test_discovery_cannot_re_add_a_bounced_address(db):
    # THE regression. Before the refactor the bounce list lived only in
    # import_sheet.py, so a channel whose description contained a bounced
    # address would be discovered, promoted, emailed, and bounce again.
    db.upsert_candidate("UC1", "Ellify", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_email("UC1", "flamingo@ellify.com")

    candidate = db.candidates_to_promote()[0]
    assert db.promote_candidate(candidate) is False
    assert db.prospects_by_status("new") == []


def test_bounced_candidate_is_not_retried_forever(db):
    # Promotion refused, but promoted_at must still be stamped or the job
    # picks this row up again every single week.
    db.upsert_candidate("UC1", "Ellify", 80_000)
    db.set_candidate_growth("UC1", "growing", 9.0)
    db.set_candidate_email("UC1", "flamingo@ellify.com")
    db.promote_candidate(db.candidates_to_promote()[0])
    assert db.candidates_to_promote() == []


def test_init_is_idempotent(db):
    # init() runs on every worker boot; re-seeding bounces must not raise.
    db.init()
    db.init()
    assert db.is_suppressed("flamingo@ellify.com")


# --- snapshot plumbing ------------------------------------------------------
def test_candidates_are_tracked_for_snapshots(db):
    # If candidates weren't tracked, growth could never be computed for them,
    # and the digest could never say which email-less channels are rising.
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    tracked = [r["channel_id"] for r in db.channels_to_track()]
    assert "UC1" in tracked


def test_all_snapshots_returns_every_row_in_one_query(db):
    db.record_snapshot("UCa", "A", 100, 1, 1, "v")
    db.record_snapshot("UCa", "A", 110, 1, 1, "v")
    db.record_snapshot("UCb", "B", 200, 1, 1, "v")
    rows = db.all_snapshots()
    assert len(rows) == 3
    import growth
    assert set(growth.by_channel(rows)) == {"UCa", "UCb"}


# --- personalization covers parked sheet rows, without re-queueing them ------
def test_parked_prospect_without_a_line_gets_personalized(db):
    # Sheet-imported rows arrive parked with no line. Without this they'd be
    # re-contacted with generic copy.
    db.upsert_prospect(channel_name="Sammy", email="s@x.com",
                       status="parked", source="sheet")
    assert [p["email"] for p in db.prospects_needing_line()] == ["s@x.com"]


def test_personalizing_a_parked_prospect_keeps_it_parked(db):
    # THE trap: set_line() used to force status='queued'. That would put
    # someone contacted in August into the first-contact sequence and greet
    # them as a stranger.
    db.upsert_prospect(channel_name="Sammy", email="s@x.com",
                       status="parked", source="sheet")
    prospect = db.prospects_by_status("parked")[0]
    db.set_line(prospect["id"], "a line")

    still_parked = db.prospects_by_status("parked")
    assert len(still_parked) == 1
    assert still_parked[0]["personalized_line"] == "a line"
    assert db.prospects_by_status("queued") == []


def test_personalizing_a_new_prospect_still_queues_it(db):
    db.upsert_prospect(channel_name="Sammy", email="s@x.com", source="discovery")
    prospect = db.prospects_by_status("new")[0]
    db.set_line(prospect["id"], "a line")
    assert len(db.prospects_by_status("queued")) == 1


def test_parked_prospect_with_a_line_is_not_redone(db):
    db.upsert_prospect(channel_name="Sammy", email="s@x.com",
                       status="parked", source="sheet")
    prospect = db.prospects_by_status("parked")[0]
    db.set_line(prospect["id"], "already written")
    assert db.prospects_needing_line() == []


def test_hold_prospects_are_never_prepared_for_sending(db):
    # These are mid-conversation with you by hand. The machine must not queue
    # anything for them.
    db.upsert_prospect(channel_name="InTalks", email="t@x.com",
                       status="hold", source="sheet")
    assert db.prospects_needing_line() == []
