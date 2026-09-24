"""Daily self-check. Emails you only what you need to know.

Written after a week in which the Monday candidate digest never fired and
nothing said so. An unsupervised system that can fail silently isn't
autonomous, it's just unattended — and silence reads identically to success.

Sent every day, not only when something breaks: an exception-only alert can't
tell you the difference between a quiet day and a dead worker. Problems go at
the top and into the subject line, so a bad day is visible from the inbox list
without opening anything.

Note the blind spot this cannot cover: a monitor inside the worker cannot
report its own death. If the container stops, this stops with it. The external
half is an uptime service pinging /health — see TODOS.md item 0.
"""
from datetime import datetime, timezone

import config
import copy_audit
import db
import gmail_client

# How long each job may go without running before something is wrong. Weekly
# jobs get eight days so a normal week never trips them; the weekday jobs get
# enough slack to sit through a weekend.
EXPECTED_MAX_AGE_HOURS = {
    "tick": 80,
    "replies": 80,
    "digest": 100,
    "digest-replies": 100,
    "discover": 200,
    "enrich": 200,
    "snapshot": 200,
    "promote": 200,
    "candidate-digest": 48,   # daily now, not weekly
    "retouch": 200,
    "health": 48,
}


def check():
    """Everything currently wrong. Empty list means healthy."""
    problems = []

    # 1. Anything that would stop email going out at all.
    for problem in copy_audit.audit():
        problems.append(f"SENDING BLOCKED: {problem}")

    # 2. Jobs that failed last time they ran.
    history = {row["job"]: row for row in db.job_history()}
    for job, row in history.items():
        if row["outcome"] == "failed":
            problems.append(f"{job} failed on its last run: {row['detail']}")

    # 3. Jobs that have gone quiet. This is the one that would have caught the
    #    missed Monday digest on Tuesday instead of the following Wednesday.
    for job, max_age in EXPECTED_MAX_AGE_HOURS.items():
        row = history.get(job)
        if row is None:
            # Never run at all. Only worth reporting once the worker has been
            # up long enough that it should have.
            continue
        if row["hours_ago"] is not None and row["hours_ago"] > max_age:
            days = row["hours_ago"] / 24
            problems.append(
                f"{job} has not run for {days:.1f} days "
                f"(expected at least every {max_age / 24:.1f})")

    # 4. The database losing rows means the volume came unmounted and the
    #    system is about to re-contact everyone from scratch.
    counts = {row["status"]: row["n"] for row in db.counts_by_status()}
    total = sum(counts.values())
    previous = db.get_meta("health_prospect_total")
    if previous is not None and total < int(previous):
        problems.append(
            f"PROSPECT COUNT DROPPED {previous} -> {total}. The /data volume may "
            "have come unmounted; everyone risks being contacted again")
    db.set_meta("health_prospect_total", str(total))

    # 5. Work that is queued but not moving.
    prep = db.prep_progress()
    if counts.get("new", 0) > 0 and prep["personalized"] == 0:
        problems.append(
            f"{counts['new']} prospect(s) waiting but none personalized — "
            "check ANTHROPIC_API_KEY")

    # 6. The fast-track queue can only drain if you answer the digest.
    waiting = db.candidates_waiting_count(config.CANDIDATE_REASK_DAYS)
    if waiting >= 25:
        problems.append(
            f"{waiting} channels are waiting on an email address from you — "
            "answer the daily list to unblock them")

    return problems


def _deltas():
    """Today's numbers against the last report, so movement is visible.

    Status counts alone don't tell you what happened — "69 in sequence" reads
    the same whether twelve people were contacted yesterday or nobody has been
    contacted in a month. The delta is the story.
    """
    import json
    current = db.status_snapshot()
    previous_raw = db.get_meta("status_snapshot")
    previous = {}
    if previous_raw:
        try:
            previous = json.loads(previous_raw)
        except ValueError:
            previous = {}
    db.set_meta("status_snapshot", json.dumps(current))
    return current, previous


def _delta_line(label, value, previous, width=22):
    if previous is None:
        return f"  {label:<{width}} {value:>5}"
    change = value - previous
    if change == 0:
        return f"  {label:<{width}} {value:>5}"
    return f"  {label:<{width}} {value:>5}   {change:+d}"


def summary():
    """The numbers worth seeing even on a good day."""
    counts = {row["status"]: row["n"] for row in db.counts_by_status()}
    discovery = db.discovery_progress()
    return {
        "sent_today": db.sends_today(),
        "daily_cap": config.MAX_SENDS_PER_DAY,
        "dry_run": config.DRY_RUN,
        "in_sequence": counts.get("active", 0) + counts.get("queued", 0),
        "awaiting_your_reply": len(db.unresolved_review()),
        "candidates": discovery["candidates"],
        "need_an_address": len(db.candidates_needing_email(config.CANDIDATE_REASK_DAYS)),
        "unsubscribed": counts.get("unsubscribed", 0),
    }


def build_report():
    """The daily status report: problems first, then what moved, then detail."""
    problems = check()
    verdict = "OK" if not problems else f"{len(problems)} PROBLEM(S)"
    today = datetime.now(timezone.utc)
    activity = db.activity_since(24)
    current, previous = _deltas()

    lines = [f"LockedIn outreach — {today:%A %d %b}", ""]

    # Anything wrong goes at the very top. A report you have to read to the
    # bottom to find a failure is a report you will eventually stop reading.
    if problems:
        lines.append(f"NEEDS ATTENTION ({len(problems)})")
        for problem in problems:
            lines.append(f"  ✗ {problem}")
        lines.append("")
    else:
        lines.append("No problems. Everything below is just so you can see it moving.")
        lines.append("")

    if config.DRY_RUN:
        lines += ["  ⚠ DRY_RUN is ON — nothing is actually being sent.", ""]

    # What happened, rather than what merely exists.
    lines.append("LAST 24 HOURS")
    if activity["total_sent"] == 0 and activity["replies_in"] == 0:
        lines.append("  nothing sent, nothing received")
    else:
        if activity["openers"]:
            lines.append(f"  {activity['openers']} opener(s) sent")
        if activity["followups"]:
            lines.append(f"  {activity['followups']} follow-up(s) sent")
        if activity["retouches"]:
            lines.append(f"  {activity['retouches']} re-touch(es) sent")
        if activity["replies_in"]:
            lines.append(f"  {activity['replies_in']} reply/replies received")
    if activity["channels_found"]:
        lines.append(f"  {activity['channels_found']} new channel(s) discovered")
    if activity["promoted"]:
        lines.append(f"  {activity['promoted']} channel(s) promoted into the sequence")
    lines.append("")

    # The pipeline, with movement since the last report.
    lines.append("PIPELINE                       change")
    for label, key in [("In sequence", "in_sequence"),
                       ("Waiting on your reply", "awaiting_reply"),
                       ("Need an address", "needing_address"),
                       ("Parked", "parked"),
                       ("Channels tracked", "channels_tracked"),
                       ("Unsubscribed / bounced", "unsubscribed")]:
        lines.append(_delta_line(label, current[key], previous.get(key)))
    lines.append("")

    lines.append("JOBS")
    history = db.job_history()
    if not history:
        lines.append("  nothing has run yet")
    for row in history:
        mark = "ok  " if row["outcome"] == "ok" else "FAIL"
        age = f"{row['hours_ago']:.0f}h ago" if row["hours_ago"] is not None else "?"
        detail = f" — {row['detail']}" if row["outcome"] != "ok" else ""
        lines.append(f"  [{mark}] {row['job']:<18} {age:>9}{detail}")

    return verdict, "\n".join(lines)


def run(always_email=True):
    """Email the daily status report.

    Sent every day, not only on problems: the point is that you can see what
    went out and what moved without going to look. An exception-only alert
    can't tell you the difference between a quiet day and a dead worker.

    The subject line carries the verdict, so a problem is visible from the
    inbox list without opening anything.
    """
    verdict, body = build_report()
    problems = verdict != "OK"
    today = datetime.now(timezone.utc)

    if problems:
        subject = f"LockedIn: {verdict} — {today:%a %d %b}"
    else:
        activity = db.activity_since(24)
        subject = (f"LockedIn: {activity['total_sent']} sent, "
                   f"{len(db.unresolved_review())} to answer — {today:%a %d %b}")

    gmail_client.send(to=gmail_client.address(), subject=subject, body=body)
    print(f"Status: {verdict}. Report emailed.")
    return verdict
