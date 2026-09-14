"""Growth signal: every branch of the state machine in growth.py.

`now` is injected everywhere so the window tests pin a fixed clock instead of
depending on when the suite runs.
"""
from datetime import datetime, timedelta, timezone

import growth

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def snap(subs, days_ago, channel_id="UC1"):
    """A snapshot row shaped like what db.record_snapshot writes."""
    return {
        "channel_id": channel_id,
        "subscriber_count": subs,
        "captured_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


# --- insufficient data ------------------------------------------------------
def test_no_snapshots_is_insufficient():
    assert growth.signal([], now=NOW) == growth.INSUFFICIENT


def test_single_snapshot_is_insufficient():
    # One point can't describe a trend, no matter how big the number.
    assert growth.signal([snap(90_000, 1)], now=NOW) == growth.INSUFFICIENT


def test_zero_starting_count_is_insufficient():
    # Percent change from zero is undefined — must not raise or report growth.
    assert growth.signal([snap(0, 30), snap(5_000, 1)], now=NOW) == growth.INSUFFICIENT


def test_insufficient_returns_no_numbers():
    signal, pct, delta = growth.details([snap(100, 1)], now=NOW)
    assert (signal, pct, delta) == (growth.INSUFFICIENT, None, None)


# --- the three live labels --------------------------------------------------
def test_growth_above_threshold_is_growing():
    # 100k -> 110k = +10%, comfortably over the +5% bar.
    assert growth.signal([snap(100_000, 30), snap(110_000, 1)], now=NOW) == growth.GROWING


def test_exactly_at_growing_threshold_counts_as_growing():
    # Boundary: +5.0% must qualify, since the check is >=.
    assert growth.signal([snap(100_000, 30), snap(105_000, 1)], now=NOW) == growth.GROWING


def test_decline_below_threshold_is_declining():
    assert growth.signal([snap(100_000, 30), snap(90_000, 1)], now=NOW) == growth.DECLINING


def test_exactly_at_declining_threshold_counts_as_declining():
    # Boundary: -2.0% must qualify, since the check is <=.
    assert growth.signal([snap(100_000, 30), snap(98_000, 1)], now=NOW) == growth.DECLINING


def test_flat_is_stable():
    assert growth.signal([snap(100_000, 30), snap(100_000, 1)], now=NOW) == growth.STABLE


def test_mild_growth_below_threshold_is_stable():
    # +2% is movement but not momentum — must not promote this channel.
    assert growth.signal([snap(100_000, 30), snap(102_000, 1)], now=NOW) == growth.STABLE


def test_mild_decline_above_threshold_is_stable():
    assert growth.signal([snap(100_000, 30), snap(99_000, 1)], now=NOW) == growth.STABLE


# --- the numbers behind the label -------------------------------------------
def test_details_reports_pct_and_delta():
    signal, pct, delta = growth.details([snap(100_000, 30), snap(108_000, 1)], now=NOW)
    assert signal == growth.GROWING
    assert round(pct, 2) == 8.0
    assert delta == 8_000


def test_details_reports_negative_delta():
    _, pct, delta = growth.details([snap(50_000, 30), snap(45_000, 1)], now=NOW)
    assert round(pct, 1) == -10.0
    assert delta == -5_000


# --- window filtering -------------------------------------------------------
def test_window_excludes_older_snapshots():
    # The 90-day-old point would show +50%, but inside a 30d window the channel
    # has been flat. The window must win.
    snapshots = [snap(60_000, 90), snap(90_000, 20), snap(90_000, 1)]
    assert growth.signal(snapshots, window_days=30, now=NOW) == growth.STABLE


def test_no_window_uses_all_snapshots():
    snapshots = [snap(60_000, 90), snap(90_000, 20), snap(90_000, 1)]
    assert growth.signal(snapshots, window_days=None, now=NOW) == growth.GROWING


def test_window_that_excludes_everything_is_insufficient():
    snapshots = [snap(60_000, 90), snap(90_000, 80)]
    assert growth.signal(snapshots, window_days=30, now=NOW) == growth.INSUFFICIENT


def test_unordered_snapshots_are_sorted_before_comparing():
    # Rows can arrive in any order from SQLite; newest-first must not invert
    # the sign of the result.
    snapshots = [snap(110_000, 1), snap(100_000, 30)]
    assert growth.signal(snapshots, now=NOW) == growth.GROWING


# --- timestamp tolerance ----------------------------------------------------
def test_naive_timestamps_are_treated_as_utc():
    # Rows written before the tz-aware _now(), or hand-edited, lack an offset.
    snapshots = [
        {"channel_id": "UC1", "subscriber_count": 100_000,
         "captured_at": "2026-08-15 12:00:00"},
        {"channel_id": "UC1", "subscriber_count": 120_000,
         "captured_at": "2026-09-13 12:00:00"},
    ]
    assert growth.signal(snapshots, now=NOW) == growth.GROWING


# --- grouping ---------------------------------------------------------------
def test_by_channel_groups_flat_rows():
    rows = [
        snap(100, 30, "UCa"), snap(200, 1, "UCa"),
        snap(300, 30, "UCb"),
    ]
    grouped = growth.by_channel(rows)
    assert set(grouped) == {"UCa", "UCb"}
    assert len(grouped["UCa"]) == 2
    assert len(grouped["UCb"]) == 1


def test_by_channel_on_empty_input():
    assert growth.by_channel([]) == {}
