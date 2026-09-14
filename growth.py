"""Is this channel growing? That is the whole job of this module.

A channel becomes worth emailing when its subscriber count is climbing — a
creator on the way up has budget and ambition, and hasn't been pitched by
everyone yet. This turns a pile of weekly snapshots into one of four labels.

Ported from the metabase's `src/analyze.ts` (`calculateGrowthSignal`), keeping
the same thresholds so both systems agree on what "growing" means. The
metabase's fit score, upload frequency, and engagement scoring were deliberately
NOT ported — nothing here consumes them. See TODOS.md item 6.

    snapshots in window < 2  ──►  "insufficient_data"
             │                     (need two points to draw a line)
    earliest count == 0      ──►  "insufficient_data"
             │                     (can't compute % change from zero)
    pct >= GROWING_PCT       ──►  "growing"
             │
    pct <= DECLINING_PCT     ──►  "declining"
             │
    else                     ──►  "stable"

The asymmetric thresholds (+5% vs -2%) are intentional: a channel needs real
momentum to qualify, but only mild bleeding to be written off.
"""
from datetime import datetime, timedelta, timezone

# Percent change across the window that flips the label.
GROWING_PCT = 5.0
DECLINING_PCT = -2.0

GROWING = "growing"
STABLE = "stable"
DECLINING = "declining"
INSUFFICIENT = "insufficient_data"


def _parse(stamp):
    """Parse a stored timestamp into an aware datetime.

    Snapshots are written by `db._now()` as UTC ISO strings, but rows imported
    or hand-edited may lack the offset. Anything naive is read as UTC so window
    comparisons never raise on mixed input.
    """
    if isinstance(stamp, datetime):
        parsed = stamp
    else:
        parsed = datetime.fromisoformat(str(stamp).replace(" ", "T"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _in_window(snapshots, window_days, now=None):
    """Snapshots captured within the last `window_days`, oldest first.

    `window_days=None` means use everything (the all-time trend).
    """
    ordered = sorted(snapshots, key=lambda s: _parse(s["captured_at"]))
    if window_days is None:
        return ordered
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=window_days)
    return [s for s in ordered if _parse(s["captured_at"]) >= cutoff]


def details(snapshots, window_days=None, now=None):
    """Growth label plus the raw numbers behind it.

    Returns (signal, pct_change, delta). pct_change and delta are None when
    there isn't enough data to compute them — callers store all three so the
    digest can say "+8.2% over 30d" without re-reading snapshots.

    `now` is injectable so tests can pin the window without sleeping.
    """
    windowed = _in_window(snapshots, window_days, now=now)
    if len(windowed) < 2:
        return INSUFFICIENT, None, None

    first = windowed[0]["subscriber_count"] or 0
    last = windowed[-1]["subscriber_count"] or 0
    if first == 0:
        # A channel that reported zero subs is either brand new or the API
        # returned a hidden count. Either way there's no ratio to take.
        return INSUFFICIENT, None, None

    delta = last - first
    pct = (delta / first) * 100.0

    if pct >= GROWING_PCT:
        return GROWING, pct, delta
    if pct <= DECLINING_PCT:
        return DECLINING, pct, delta
    return STABLE, pct, delta


def signal(snapshots, window_days=None, now=None):
    """Just the label, for callers that don't need the numbers."""
    return details(snapshots, window_days=window_days, now=now)[0]


def by_channel(rows):
    """Group flat snapshot rows into {channel_id: [snapshot, ...]}.

    The snapshot job reads every channel's history in ONE query and groups here,
    rather than issuing a query per channel. At 174 channels that's the
    difference between 1 round trip and 175, and the count only grows as
    discovery adds channels.
    """
    grouped = {}
    for row in rows:
        grouped.setdefault(row["channel_id"], []).append(row)
    return grouped
