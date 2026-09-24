"""Retry for flaky network calls to Google.

Every few hours a job dies on `ssl.SSLError: record layer failure` or a read
timeout talking to Gmail or the YouTube API. Neither is a fault in this code —
a TLS connection gets reset, or a socket goes stale — but with no retry the
whole job is lost until its next scheduled slot, which for the weekly ones
means seven days.

READS ONLY. This deliberately does not wrap anything that sends email.
Retrying a failed send is how people get the same cold email twice: the
request may well have reached Gmail and succeeded, with only the response
lost. A lost read costs a few seconds; a duplicated send costs a creator's
goodwill and your sending reputation.
"""
import logging
import socket
import ssl
import time

log = logging.getLogger("netretry")

# Failures worth retrying: the connection broke, not the request. An auth
# error or a malformed request will fail identically every time, so retrying
# those just delays the real error.
TRANSIENT = (
    ssl.SSLError,
    socket.timeout,
    TimeoutError,
    ConnectionError,       # covers ConnectionReset/Aborted/Refused
    BrokenPipeError,
    OSError,               # httplib2 surfaces some socket errors as bare OSError
)


def is_transient(exc):
    """True if this looks like a broken connection rather than a bad request."""
    if isinstance(exc, TRANSIENT):
        return True
    # googleapiclient wraps server-side faults in HttpError; 5xx and 429 are
    # worth another go, 4xx are not.
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in (429, 500, 502, 503, 504)


def call(fn, *args, attempts=3, base_delay=2.0, what="request", **kwargs):
    """Run a READ, retrying transient network failures with backoff.

    Delays are 2s then 4s by default — long enough for a reset connection to
    be replaced, short enough that a job still finishes inside its slot.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                      # noqa: BLE001
            if not is_transient(exc) or attempt == attempts:
                raise
            last = exc
            delay = base_delay * (2 ** (attempt - 1))
            log.warning(
                f"{what} failed ({type(exc).__name__}: {exc}); "
                f"retrying in {delay:.0f}s [{attempt}/{attempts - 1}]"
            )
            time.sleep(delay)
    raise last                                        # pragma: no cover
