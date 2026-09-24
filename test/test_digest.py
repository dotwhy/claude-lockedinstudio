"""The weekly digest, the reply parser, and the confirmation that closes the loop.

The parser reads free text you typed on a phone, so it is deliberately
tolerant. What it must never do is drop a line silently — you would believe a
channel was queued when it was never contacted.
"""
import importlib

import pytest


class FakeGmail:
    """Records sends instead of making them, and replays a canned reply."""

    def __init__(self, inbound=None):
        self.sent = []
        self._inbound = inbound

    def address(self):
        return "you@gmail.com"

    def send(self, to, subject, body, thread_id=None, **kwargs):
        self.sent.append({"to": to, "subject": subject, "body": body,
                          "thread_id": thread_id})
        return "gmail-id", thread_id or "thread-1", "<msg-id>"

    def latest_inbound(self, thread_id, after_ms):
        return (self._inbound, "<in-id>") if self._inbound else (None, None)

    def latest_in_thread(self, thread_id, exclude_ids=()):
        return (self._inbound, "<in-id>") if self._inbound else (None, None)

    def flag_thread(self, thread_id):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    import db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module.config, "DB_PATH", str(tmp_path / "test.db"))
    db_module.init()
    import engine
    importlib.reload(engine)
    return db_module, engine, config


def growing(db, channel_id, name, subs, pct=9.0):
    db.upsert_candidate(channel_id, name, subs)
    db.set_candidate_growth(channel_id, "growing", pct)


# --- the digest -------------------------------------------------------------
def test_empty_queue_sends_nothing(env, monkeypatch):
    db, engine, _ = env
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)
    assert engine.send_candidate_digest() == 0
    assert fake.sent == []


def test_the_whole_batch_arrives_in_one_email(env, monkeypatch):
    # One email per batch, never one email per channel.
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 12)
    for i in range(12):
        growing(db, f"UC{i}", f"Channel{i}", 50_000 + i)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)

    assert engine.send_candidate_digest() == 12
    assert len(fake.sent) == 1          # ONE email, not twelve
    body = fake.sent[0]["body"]
    assert "Channel0" in body and "Channel11" in body


def test_digest_explains_why_each_channel_qualified(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "Rising", 80_000, pct=12.5)
    db.upsert_candidate("UC2", "BigActive", 900_000, fast_track=True)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)
    engine.send_candidate_digest()

    body = fake.sent[0]["body"]
    assert "+12.5%" in body
    assert "500k+ and posting" in body


def test_digest_stamps_asked_so_next_week_is_quiet(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "Rising", 80_000)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)

    engine.send_candidate_digest()
    assert engine.send_candidate_digest() == 0   # same week, nothing to re-ask
    assert len(fake.sent) == 1


def test_digest_thread_is_remembered(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "Rising", 80_000)
    monkeypatch.setattr(engine, "gmail_client", FakeGmail())
    engine.send_candidate_digest()
    assert db.latest_digest_thread() == "thread-1"


# --- parsing your reply -----------------------------------------------------
def test_parses_the_documented_format(env):
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply(
        "SammyGames: sammy@business.com\nBloxKing: contact@bloxking.tv"
    )
    assert pairs == [("SammyGames", "sammy@business.com"),
                     ("BloxKing", "contact@bloxking.tv")]
    assert unparsed == []


def test_tolerates_dashes_and_bare_whitespace(env):
    _, engine, _ = env
    pairs, _ = engine._parse_digest_reply(
        "SammyGames - sammy@business.com\nBloxKing   contact@bloxking.tv"
    )
    assert [p[0] for p in pairs] == ["SammyGames", "BloxKing"]


def test_ignores_quoted_original_message(env):
    # Replying on a phone quotes the whole digest underneath.
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply(
        "SammyGames: sammy@business.com\n"
        "> 12 channels worth contacting\n"
        "> • BloxKing (120,000 subs, growing +15.0%)\n"
    )
    assert len(pairs) == 1
    assert unparsed == []


def test_ignores_the_digests_own_bullets(env):
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("• BloxKing (120,000 subs, growing)")
    assert pairs == [] and unparsed == []


def test_prose_is_not_reported_as_unparsed(env):
    # Complaining about "thanks!" would train you to ignore the confirmation.
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("thanks, here you go\nignore the rest")
    assert pairs == [] and unparsed == []


def test_a_typod_address_is_reported(env):
    # This is the whole point: a line that LOOKS like an attempt must come back
    # to you, not vanish.
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("SammyGames: sammy@@business")
    assert pairs == []
    assert unparsed == ["SammyGames: sammy@@business"]


def test_address_with_no_channel_name_is_reported(env):
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("sammy@business.com")
    assert pairs == []
    assert unparsed == ["sammy@business.com"]


# --- the full reply loop ----------------------------------------------------
def test_reply_attaches_email_and_promotes(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    assert engine.process_digest_replies() == 1
    assert [p["email"] for p in db.prospects_by_status("new")] == ["sammy@business.com"]


def test_reply_matching_is_case_insensitive(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    monkeypatch.setattr(engine, "gmail_client",
                        FakeGmail(inbound="sammygames: sammy@business.com"))
    db.record_digest_thread("thread-1")
    assert engine.process_digest_replies() == 1


def test_confirmation_lists_what_landed(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    confirmation = fake.sent[-1]["body"]
    assert "sammy@business.com" in confirmation
    assert fake.sent[-1]["thread_id"] == "thread-1"   # replies in-thread


def test_confirmation_reports_an_unknown_channel_name(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SamyGames: sammy@business.com")   # typo'd name
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    confirmation = fake.sent[-1]["body"]
    assert "couldn't find a channel" in confirmation
    assert "SamyGames" in confirmation


def test_confirmation_reports_an_unreadable_line(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SammyGames: sammy@@business")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    assert "couldn't read these lines" in fake.sent[-1]["body"]


def test_reply_with_no_addresses_still_gets_an_answer(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="not right now, busy week")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    assert "couldn't find any addresses" in fake.sent[-1]["body"]


def test_no_digest_thread_is_a_noop(env, monkeypatch):
    db, engine, _ = env
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    assert engine.process_digest_replies() == 0
    assert fake.sent == []


def test_no_reply_yet_is_a_noop(env, monkeypatch):
    db, engine, _ = env
    fake = FakeGmail(inbound=None)
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")
    assert engine.process_digest_replies() == 0
    assert fake.sent == []


def test_suppressed_address_from_a_reply_is_not_promoted(env, monkeypatch):
    # Even hand-entered addresses go through suppression.
    db, engine, _ = env
    growing(db, "UC1", "Ellify", 80_000)
    fake = FakeGmail(inbound="Ellify: flamingo@ellify.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    assert db.prospects_by_status("new") == []


# --- the self-thread bug ----------------------------------------------------
def test_digest_records_which_message_was_ours(env, monkeypatch):
    # The digest goes from you to yourself, so sender alone can't distinguish
    # our message from your reply. The id can.
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    monkeypatch.setattr(engine, "gmail_client", FakeGmail())
    engine.send_candidate_digest()
    assert db.digest_message_ids() == ["gmail-id"]


def test_our_own_digest_is_excluded_when_reading_replies(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    engine.send_candidate_digest()

    seen = {}
    original = fake.latest_in_thread

    def spy(thread_id, exclude_ids=()):
        seen["excluded"] = list(exclude_ids)
        return original(thread_id, exclude_ids)

    fake.latest_in_thread = spy
    engine.process_digest_replies()
    assert "gmail-id" in seen["excluded"]


def test_digest_message_ids_do_not_grow_without_bound(env, monkeypatch):
    db, engine, _ = env
    for i in range(30):
        db.record_digest_thread("thread-1", f"msg-{i}")
    assert len(db.digest_message_ids()) <= 20


# --- not re-sending the same confirmation three times a day ------------------
def test_a_reply_is_only_handled_once(env, monkeypatch):
    # digest-replies runs 3x per weekday. Without this, one unmatched line
    # became three confirmation emails a day, indefinitely.
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    assert engine.process_digest_replies() == 1
    before = len(fake.sent)
    assert engine.process_digest_replies() == 0      # second run: nothing
    assert engine.process_digest_replies() == 0      # third run: nothing
    assert len(fake.sent) == before                  # and no extra emails


def test_a_new_reply_is_still_handled(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    growing(db, "UC2", "BloxKing", 90_000)
    fake = FakeGmail(inbound="SammyGames: sammy@business.com")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")
    engine.process_digest_replies()

    # A genuinely new reply arrives (different message id).
    fake._inbound = "BloxKing: contact@bloxking.tv"
    fake.latest_in_thread = lambda t, x=(): (fake._inbound, "<in-id-2>")
    assert engine.process_digest_replies() == 1


# --- channel links get a useful answer, not "couldn't read this" -------------
def test_a_channel_link_is_reported_as_a_link(env, monkeypatch):
    db, engine, _ = env
    growing(db, "UC1", "SammyGames", 80_000)
    fake = FakeGmail(inbound="https://www.youtube.com/@SammyGames")
    monkeypatch.setattr(engine, "gmail_client", fake)
    db.record_digest_thread("thread-1")

    engine.process_digest_replies()
    body = fake.sent[-1]["body"]
    assert "channel links" in body
    assert "isn't public" in body


def test_a_link_is_not_silently_dropped(env):
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("https://youtube.com/@SammyGames")
    assert pairs == []
    assert unparsed == ["https://youtube.com/@SammyGames"]


def test_prose_is_still_not_reported(env):
    _, engine, _ = env
    pairs, unparsed = engine._parse_digest_reply("here you go, thanks")
    assert pairs == [] and unparsed == []


# --- daily batch instead of one weekly dump ---------------------------------
def test_digest_asks_for_a_batch_not_everything(env, monkeypatch):
    # 45 channels at once is functionally zero: you do a handful and the rest
    # scroll away.
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 8)
    for i in range(45):
        growing(db, f"UC{i}", f"Ch{i}", 50_000 + i)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)

    assert engine.send_candidate_digest() == 8


def test_digest_says_how_many_are_left(env, monkeypatch):
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 8)
    for i in range(45):
        growing(db, f"UC{i}", f"Ch{i}", 50_000 + i)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)
    engine.send_candidate_digest()

    body = fake.sent[0]["body"]
    assert "37 more are queued" in body
    assert "tomorrow" in body


def test_tomorrows_batch_is_a_different_set(env, monkeypatch):
    # The whole point of "nudge me again tomorrow": the next batch must be the
    # ones we haven't asked about yet.
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 5)
    for i in range(20):
        growing(db, f"UC{i}", f"Ch{i}", 50_000 + i)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)

    engine.send_candidate_digest()
    first = fake.sent[0]["body"]
    engine.send_candidate_digest()
    second = fake.sent[1]["body"]

    asked_first = {f"Ch{i}" for i in range(20) if f"Ch{i}\n" in first or f"Ch{i} " in first}
    asked_second = {f"Ch{i}" for i in range(20) if f"Ch{i}\n" in second or f"Ch{i} " in second}
    assert asked_first and asked_second
    assert not (asked_first & asked_second), "the same channels were asked twice"


def test_no_remaining_note_when_the_queue_fits(env, monkeypatch):
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 8)
    growing(db, "UC1", "OnlyOne", 80_000)
    fake = FakeGmail()
    monkeypatch.setattr(engine, "gmail_client", fake)
    engine.send_candidate_digest()
    assert "more are queued" not in fake.sent[0]["body"]


# --- forgiving name matching ------------------------------------------------
def test_matching_tolerates_a_partial_name(env):
    # Channel names are full of emoji and spacing nobody retypes exactly, and a
    # near-miss silently dropping a hand-sourced address is the worst outcome.
    db, _, _ = env
    db.upsert_candidate("UC1", "SammyGames HD ✨", 80_000)
    assert db.candidate_by_name("SammyGames")["channel_id"] == "UC1"


def test_matching_tolerates_extra_words(env):
    db, _, _ = env
    db.upsert_candidate("UC1", "SammyGames", 80_000)
    assert db.candidate_by_name("SammyGames HD")["channel_id"] == "UC1"


def test_exact_match_wins_over_partial(env):
    db, _, _ = env
    db.upsert_candidate("UC1", "Blox", 80_000)
    db.upsert_candidate("UC2", "BloxKing", 90_000)
    assert db.candidate_by_name("Blox")["channel_id"] == "UC1"


def test_ambiguous_partial_match_is_refused(env):
    # Guessing between two channels would attach your address to the wrong one.
    db, _, _ = env
    db.upsert_candidate("UC1", "BloxKingOne", 80_000)
    db.upsert_candidate("UC2", "BloxKingTwo", 90_000)
    assert db.candidate_by_name("BloxKing") is None


def test_already_promoted_channels_are_not_matched(env):
    db, _, _ = env
    db.upsert_candidate("UC1", "SammyGames", 80_000, fast_track=True)
    db.set_candidate_email("UC1", "a@b.com")
    db.promote_candidate(db.candidates_to_promote()[0])
    assert db.candidate_by_name("SammyGames") is None


# --- recovering from a bulk ask ---------------------------------------------
def test_reopening_the_queue_puts_unresolved_channels_back(env, monkeypatch):
    # A single bulk ask stamped all 45 at once, so every one sat inside its
    # re-ask window and the daily batch had nothing to send for a month.
    db, engine, cfg = env
    monkeypatch.setattr(cfg, "CANDIDATE_BATCH_SIZE", 8)
    for i in range(20):
        growing(db, f"UC{i}", f"Ch{i}", 80_000)
    db.mark_candidates_asked([f"UC{i}" for i in range(20)])
    assert db.candidates_needing_email(14, limit=8) == []

    assert db.reopen_candidate_queue() == 20
    assert len(db.candidates_needing_email(14, limit=8)) == 8


def test_reopening_does_not_disturb_resolved_channels(env):
    db, _, _ = env
    growing(db, "UC1", "Resolved", 80_000)
    db.set_candidate_email("UC1", "a@b.com")
    db.mark_candidates_asked(["UC1"])
    assert db.reopen_candidate_queue() == 0


def test_reopening_skips_promoted_channels(env):
    db, _, _ = env
    db.upsert_candidate("UC1", "Promoted", 80_000, fast_track=True)
    db.set_candidate_email("UC1", "a@b.com")
    db.promote_candidate(db.candidates_to_promote()[0])
    db.mark_candidates_asked(["UC1"])
    assert db.reopen_candidate_queue() == 0
