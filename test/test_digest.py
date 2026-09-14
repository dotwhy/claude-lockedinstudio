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


def test_digest_lists_every_channel_in_one_email(env, monkeypatch):
    db, engine, _ = env
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
