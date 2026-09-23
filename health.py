"""Daily self-check. Emails you only what you need to know.

Written after a week in which the Monday candidate digest never fired and
nothing said so. An unsupervised system that can fail silently isn't
autonomous, it's just unattended — and silence reads identically to success.

The rule this follows: one line on a good day. An alert you learn to skim is
worse than no alert, so the report is short unless something is genuinely
wrong, and every problem names what to do about it.

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
    "content": 48,
    "digest": 100,
    "digest-replies": 100,
    "discover": 200,
    "enrich": 200,
    "snapshot": 200,
    "promote": 200,
    "candidate-digest": 200,
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
    waiting = len(db.candidates_needing_email(config.CANDIDATE_REASK_DAYS))
    if waiting >= 25:
        problems.append(
            f"{waiting} channels are waiting on an email address from you — "
            "reply to the weekly digest to unblock them")

    return problems


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
    problems = check()
    s = summary()
    verdict = "OK" if not problems else f"{len(problems)} PROBLEM(S)"

    lines = [f"Outreach health: {verdict}", ""]
    if problems:
        for problem in problems:
            lines.append(f"  ✗ {problem}")
        lines.append("")

    lines += [
        f"Sent today          {s['sent_today']} / {s['daily_cap']}"
        + ("   (DRY_RUN is on — nothing is actually sending)" if s["dry_run"] else ""),
        f"In sequence         {s['in_sequence']}",
        f"Replies to handle   {s['awaiting_your_reply']}",
        f"Channels tracked    {s['candidates']}",
        f"Need an address     {s['need_an_address']}",
        f"Unsubscribed        {s['unsubscribed']}",
        "",
        "Jobs (last run):",
    ]
    history = db.job_history()
    if not history:
        lines.append("  nothing has run yet")
    for row in history:
        mark = "ok " if row["outcome"] == "ok" else "FAIL"
        age = f"{row['hours_ago']:.0f}h ago" if row["hours_ago"] is not None else "?"
        lines.append(f"  [{mark}] {row['job']:<18} {age}")

    return verdict, "\n".join(lines)


def run(always_email=False):
    """Check, and email you if something needs attention.

    Emails on problems, and once a week on a good day so the absence of mail
    never becomes ambiguous — a silent monitor and a dead one look the same.
    """
    verdict, body = build_report()
    problems = verdict != "OK"

    weekly_checkin = datetime.now(timezone.utc).weekday() == 0  # Monday
    if not (problems or weekly_checkin or always_email):
        print(f"Health: {verdict}. No email sent.")
        return verdict

    subject = "Outreach health: OK" if not problems else f"Outreach health: {verdict}"
    gmail_client.send(to=gmail_client.address(), subject=subject, body=body)
    print(f"Health: {verdict}. Report emailed.")
    return verdict
