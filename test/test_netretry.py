"""Retry for flaky Google calls.

Written after `replies` and `digest-replies` both died twice in a row on
`ssl.SSLError: record layer failure`, and `tick` on a read timeout. None were
code faults; all were lost runs.
"""
import socket
import ssl

import pytest

import netretry


class Flaky:
    """Fails `failures` times with `exc`, then returns `value`."""

    def __init__(self, failures, exc, value="ok"):
        self.remaining = failures
        self.exc = exc
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise self.exc
        return self.value


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Backoff is real in production and pointless in tests."""
    monkeypatch.setattr(netretry.time, "sleep", lambda _s: None)


# --- the errors actually seen in production ---------------------------------
def test_recovers_from_the_ssl_error_that_killed_replies():
    fn = Flaky(1, ssl.SSLError("[SSL] record layer failure (_ssl.c:2580)"))
    assert netretry.call(fn) == "ok"
    assert fn.calls == 2


def test_recovers_from_the_timeout_that_killed_tick():
    fn = Flaky(1, TimeoutError("The read operation timed out"))
    assert netretry.call(fn) == "ok"
    assert fn.calls == 2


def test_recovers_from_two_consecutive_failures():
    # replies failed twice in a row, so one retry would not have been enough.
    fn = Flaky(2, ConnectionResetError("connection reset by peer"))
    assert netretry.call(fn) == "ok"
    assert fn.calls == 3


def test_gives_up_after_the_attempt_limit():
    fn = Flaky(5, socket.timeout("timed out"))
    with pytest.raises(socket.timeout):
        netretry.call(fn, attempts=3)
    assert fn.calls == 3


def test_a_healthy_call_is_not_retried():
    fn = Flaky(0, ssl.SSLError("never raised"))
    assert netretry.call(fn) == "ok"
    assert fn.calls == 1


# --- what must NOT be retried -----------------------------------------------
def test_a_bad_request_fails_immediately():
    # Retrying an auth or malformed-request error just delays the real error.
    class Boom(Exception):
        pass

    fn = Flaky(1, Boom("invalid credentials"))
    with pytest.raises(Boom):
        netretry.call(fn)
    assert fn.calls == 1


def test_a_4xx_is_not_retried():
    class Resp:
        status = 403

    class HttpError(Exception):
        resp = Resp()

    fn = Flaky(1, HttpError("forbidden"))
    with pytest.raises(HttpError):
        netretry.call(fn)
    assert fn.calls == 1


def test_a_5xx_is_retried():
    class Resp:
        status = 503

    class HttpError(Exception):
        resp = Resp()

    fn = Flaky(1, HttpError("backend error"))
    assert netretry.call(fn) == "ok"
    assert fn.calls == 2


def test_rate_limiting_is_retried():
    class Resp:
        status = 429

    class HttpError(Exception):
        resp = Resp()

    fn = Flaky(1, HttpError("rate limit"))
    assert netretry.call(fn) == "ok"


# --- sending is deliberately never wrapped ----------------------------------
def test_sending_is_not_retried_anywhere():
    """A retried send is how someone receives the same cold email twice.

    The request may well have reached Gmail and succeeded, with only the
    response lost. A lost read costs seconds; a duplicate send costs a
    creator's goodwill.
    """
    import gmail_client
    import inspect
    source = inspect.getsource(gmail_client.send)
    assert "netretry" not in source, "send() must never be retried"


def test_reads_are_wrapped():
    import gmail_client
    import inspect
    for fn in (gmail_client.latest_inbound, gmail_client.latest_in_thread):
        assert "netretry" in inspect.getsource(fn), f"{fn.__name__} should retry"
