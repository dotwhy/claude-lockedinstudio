"""API authentication.

These endpoints write into `prospects`, and anything in `prospects` gets
cold-emailed from a personal Gmail account. Before this, the routes were open
on a public Railway URL.
"""
import importlib

import pytest

import api
import config


class FakeHandler:
    def __init__(self, key=None):
        self.headers = {"X-API-Key": key} if key else {}


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "secret-key")
    importlib.reload(api)
    monkeypatch.setattr(api.config, "API_KEY", "secret-key")
    return api


def test_correct_key_is_accepted(keyed):
    assert keyed._authed(FakeHandler("secret-key")) is True


def test_wrong_key_is_rejected(keyed):
    assert keyed._authed(FakeHandler("wrong-key")) is False


def test_missing_header_is_rejected(keyed):
    assert keyed._authed(FakeHandler()) is False


def test_empty_key_header_is_rejected(keyed):
    assert keyed._authed(FakeHandler("")) is False


def test_nothing_is_authed_when_no_key_is_configured(monkeypatch):
    # Fail closed: an unset API_KEY must not mean "let everyone in".
    monkeypatch.setattr(api.config, "API_KEY", "")
    assert api._authed(FakeHandler("anything")) is False
    assert api._authed(FakeHandler()) is False


def test_api_does_not_start_without_a_key(monkeypatch):
    monkeypatch.setattr(api.config, "API_KEY", "")
    assert api.start() is None


def test_missing_key_does_not_raise(monkeypatch):
    """The scheduler calls start() before its blocking loop.

    Raising here would take every outreach job down over a missing env var —
    trading an exposed endpoint for total silence, which is worse.
    """
    monkeypatch.setattr(api.config, "API_KEY", "")
    try:
        api.start()
    except Exception as exc:                      # pragma: no cover
        pytest.fail(f"start() must never raise, got {exc!r}")


def test_api_starts_when_a_key_is_present(monkeypatch):
    monkeypatch.setattr(api.config, "API_KEY", "secret-key")
    monkeypatch.setattr(api.config, "API_PORT", 0)   # ephemeral port
    assert api.start() == 0


# --- the job runner ---------------------------------------------------------
def test_job_whitelist_covers_the_runbook(keyed):
    # Every step the deploy runbook asks you to curl must exist.
    for job in ["discover", "enrich", "snapshot", "promote", "personalize",
                "send", "candidates", "digest-replies", "retouch-now"]:
        assert job in keyed.JOBS, f"runbook references /run/{job} but it isn't registered"


def test_unknown_job_is_rejected(keyed):
    # A job runner that dispatches on arbitrary names is one mistake away from
    # being a remote shell.
    assert "rm -rf" not in keyed.JOBS
    assert keyed.JOBS.get("anything-else") is None


def test_retouch_now_is_a_separate_job_from_the_scheduled_one(keyed):
    # Forcing every parked prospect is one-way, so it must not be reachable by
    # triggering the normal, window-respecting retouch.
    assert "retouch-now" in keyed.JOBS
    assert "retouch" not in keyed.JOBS


def test_failing_job_does_not_escape(keyed):
    # Jobs run on a daemon thread; an exception there must be logged, not
    # propagated into the worker.
    def boom():
        raise RuntimeError("simulated failure")
    keyed._run_safely("boom", boom)   # must not raise
