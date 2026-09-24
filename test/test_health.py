"""The daily self-check.

The failure it exists for: the Monday candidate digest never fired, and
nothing said so for a week. Silence read identically to success.
"""
import importlib

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "FROM_NAME", "Laurenz")
    monkeypatch.setattr(config, "PORTFOLIO_URL", "www.lockedinstudio.com")
    monkeypatch.setattr(config, "STUDIO_ADDRESS", "")
    import db as db_module
    importlib.reload(db_module)
    monkeypatch.setattr(db_module.config, "DB_PATH", str(tmp_path / "test.db"))
    db_module.init()
    import health
    importlib.reload(health)
    return db_module, health, config


# --- job history ------------------------------------------------------------
def test_job_runs_are_recorded(env):
    db, _, _ = env
    db.record_job_run("discover", "ok", "found 12", 4.2)
    history = db.job_history()
    assert history[0]["job"] == "discover"
    assert history[0]["outcome"] == "ok"
    assert history[0]["hours_ago"] < 1


def test_history_keeps_only_the_latest_run_per_job(env):
    db, _, _ = env
    db.record_job_run("discover", "ok", "first", 1)
    db.record_job_run("discover", "failed", "second", 1)
    history = db.job_history()
    assert len(history) == 1
    assert history[0]["outcome"] == "failed"


def test_failures_are_listed_separately(env):
    db, _, _ = env
    db.record_job_run("snapshot", "failed", "quota exceeded", 1)
    assert db.recent_failures()[0]["job"] == "snapshot"


# --- what the check catches -------------------------------------------------
def test_healthy_system_reports_nothing(env):
    _, health, _ = env
    assert health.check() == []


def test_a_failed_job_is_reported(env):
    db, health, _ = env
    db.record_job_run("snapshot", "failed", "YouTube quota exceeded", 2.0)
    assert any("snapshot failed" in p for p in health.check())


def test_a_job_that_stopped_running_is_reported(env):
    # THE bug this was written for: candidate-digest last ran 9 days ago and
    # nobody knew.
    db, health, _ = env
    from datetime import datetime, timedelta, timezone
    stale = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    conn = db.connect()
    conn.execute("INSERT INTO job_runs (job, outcome, detail, duration_s, ran_at) "
                 "VALUES (?,?,?,?,?)", ("candidate-digest", "ok", "sent 9", 1.0, stale))
    conn.commit(); conn.close()

    problems = health.check()
    assert any("candidate-digest has not run" in p for p in problems)


def test_a_recent_run_is_not_reported_as_stale(env):
    db, health, _ = env
    db.record_job_run("candidate-digest", "ok", "sent 9", 1.0)
    assert not any("candidate-digest" in p for p in health.check())


def test_weekly_jobs_are_not_flagged_after_a_normal_week(env):
    # A weekly job at 6 days old is healthy; flagging it would train you to
    # ignore the report.
    db, health, _ = env
    from datetime import datetime, timedelta, timezone
    six_days = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    conn = db.connect()
    conn.execute("INSERT INTO job_runs (job, outcome, detail, duration_s, ran_at) "
                 "VALUES (?,?,?,?,?)", ("discover", "ok", "found 12", 1.0, six_days))
    conn.commit(); conn.close()
    assert not any("discover" in p for p in health.check())


def test_a_dropped_prospect_count_is_reported(env):
    # The volume coming unmounted means everyone gets contacted again.
    db, health, _ = env
    db.upsert_prospect(channel_name="A", email="a@x.com", source="t")
    db.upsert_prospect(channel_name="B", email="b@x.com", source="t")
    health.check()                      # records the baseline

    conn = db.connect(); conn.execute("DELETE FROM prospects WHERE email='b@x.com'")
    conn.commit(); conn.close()

    assert any("PROSPECT COUNT DROPPED" in p for p in health.check())


def test_a_growing_prospect_count_is_fine(env):
    db, health, _ = env
    db.upsert_prospect(channel_name="A", email="a@x.com", source="t")
    health.check()
    db.upsert_prospect(channel_name="B", email="b@x.com", source="t")
    assert not any("DROPPED" in p for p in health.check())


def test_a_stuck_personalization_queue_is_reported(env):
    db, health, _ = env
    for i in range(3):
        db.upsert_prospect(channel_name=f"C{i}", email=f"c{i}@x.com", source="t")
    assert any("none personalized" in p for p in health.check())


def test_a_large_unanswered_digest_queue_is_reported(env):
    db, health, _ = env
    for i in range(30):
        db.upsert_candidate(f"UC{i}", f"Ch{i}", 800_000, fast_track=True)
    assert any("waiting on an email address" in p for p in health.check())


def test_blocked_sending_is_reported(env):
    db, health, cfg = env
    monkey = cfg
    monkey.PORTFOLIO_URL = "your-portfolio-link-here"
    assert any("SENDING BLOCKED" in p for p in health.check())


# --- the report itself ------------------------------------------------------
def test_a_good_day_reads_as_ok(env):
    _, health, _ = env
    verdict, body = health.build_report()
    assert verdict == "OK"
    assert body.startswith("LockedIn outreach —")
    assert "No problems" in body


def test_problems_appear_at_the_top(env):
    db, health, _ = env
    db.record_job_run("snapshot", "failed", "quota exceeded", 1.0)
    verdict, body = health.build_report()
    assert "PROBLEM" in verdict
    # The problem must come before the numbers, or it gets skimmed past.
    assert body.index("✗") < body.index("PIPELINE")
    assert "NEEDS ATTENTION" in body


def test_report_shows_which_jobs_ran(env):
    db, health, _ = env
    db.record_job_run("discover", "ok", "found 12", 1.0)
    _, body = health.build_report()
    assert "discover" in body


def test_report_warns_when_dry_run_is_on(env, monkeypatch):
    _, health, cfg = env
    monkeypatch.setattr(cfg, "DRY_RUN", True)
    _, body = health.build_report()
    assert "DRY_RUN is ON" in body
    assert "nothing is actually being sent" in body


def test_reports_every_day_even_when_healthy(env, monkeypatch):
    # A status report you only get when something breaks can't tell you the
    # difference between a quiet day and a dead worker.
    _, health, _ = env
    sent = []
    monkeypatch.setattr(health.gmail_client, "send",
                        lambda **kw: sent.append(kw) or ("i", "t", "m"))
    health.run()
    assert len(sent) == 1
    assert "sent" in sent[0]["subject"]


def test_emails_when_something_is_wrong(env, monkeypatch):
    db, health, _ = env
    db.record_job_run("snapshot", "failed", "quota exceeded", 1.0)
    sent = []
    monkeypatch.setattr(health.gmail_client, "send",
                        lambda **kw: sent.append(kw) or ("i", "t", "m"))
    health.run()
    assert len(sent) == 1
    assert "PROBLEM" in sent[0]["subject"]


def test_the_subject_carries_the_verdict(env, monkeypatch):
    # A problem must be visible from the inbox list without opening anything.
    db, health, _ = env
    db.record_job_run("snapshot", "failed", "quota exceeded", 1.0)
    sent = []
    monkeypatch.setattr(health.gmail_client, "send",
                        lambda **kw: sent.append(kw) or ("i", "t", "m"))
    health.run()
    assert "PROBLEM" in sent[0]["subject"]


def _fixed_weekday(weekday):
    """A datetime stand-in pinned to a given weekday."""
    from datetime import datetime as real_datetime, timedelta, timezone

    class Fixed:
        @staticmethod
        def now(tz=None):
            now = real_datetime.now(timezone.utc)
            return now + timedelta(days=(weekday - now.weekday()))
    return Fixed


# --- what moved, not just what exists ---------------------------------------
def test_report_shows_what_actually_went_out(env):
    # "69 in sequence" reads the same whether twelve people were contacted
    # yesterday or nobody has been contacted in a month.
    db, health, _ = env
    db.upsert_prospect(channel_name="A", email="a@x.com", source="t")
    p = db.prospects_by_status("new")[0]
    db.record_send(p, 1, "Subject", "Body", "gid", "tid", "<mid>")

    _, body = health.build_report()
    assert "LAST 24 HOURS" in body
    assert "1 opener(s) sent" in body


def test_report_shows_a_quiet_day_as_quiet(env):
    _, health, _ = env
    _, body = health.build_report()
    assert "nothing sent, nothing received" in body


def test_report_shows_movement_since_the_last_one(env):
    db, health, _ = env
    db.upsert_prospect(channel_name="A", email="a@x.com", source="t")
    db.set_line(db.prospects_by_status("new")[0]["id"], "a line")
    health.build_report()                      # records the baseline

    db.upsert_prospect(channel_name="B", email="b@x.com", source="t")
    db.set_line(db.prospects_by_status("new")[0]["id"], "a line")
    _, body = health.build_report()
    assert "+1" in body


def test_first_report_has_no_deltas_to_show(env):
    # Nothing to compare against yet — it must not print a misleading +N.
    _, health, _ = env
    _, body = health.build_report()
    assert "+" not in body.split("PIPELINE")[1].split("JOBS")[0]


def test_failed_jobs_show_their_error_in_the_report(env):
    db, health, _ = env
    db.record_job_run("tick", "failed", "TimeoutError: read timed out", 1.0)
    _, body = health.build_report()
    assert "TimeoutError" in body
