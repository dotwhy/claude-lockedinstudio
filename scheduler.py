"""Single-worker scheduler for Railway.

Runs every recurring job on its own schedule inside ONE long-lived process, so
you deploy one Railway service instead of juggling many cron services. Each job
is wrapped so one failure never kills the scheduler.

Deliberately NOT scheduled: `drafts` — draft-building stays manual because you
review and send those yourself.

Times are UTC (Railway runs UTC). Adjust the hours to your timezone.
"""
import logging
import traceback

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("scheduler")


def safe(fn, name):
    """Run a job, logging any exception instead of crashing the scheduler."""
    def wrapped():
        log.info(f"[{name}] start (DRY_RUN={config.DRY_RUN})")
        try:
            fn()
            log.info(f"[{name}] done")
        except Exception:
            log.error(f"[{name}] FAILED\n{traceback.format_exc()}")
    return wrapped


def main():
    import db
    import engine
    import personalize
    import content
    import youtube_sourcing
    import api

    db.init()

    port = api.start()
    if port:
        log.info(f"API listening on port {port}")
    else:
        log.warning("API not started (no API_KEY). Scheduled jobs still run.")

    sched = BlockingScheduler(timezone="UTC")

    # Outreach loop: personalize new prospects, send due steps, process replies.
    sched.add_job(safe(lambda: (personalize.run(), engine.send_due(), engine.process_replies()),
                       "tick"),
                  CronTrigger(day_of_week="mon-fri", hour="9,13,17"), name="tick")

    # Faster reply flagging through the day (so hot leads surface quickly).
    sched.add_job(safe(engine.process_replies, "replies"),
                  CronTrigger(day_of_week="mon-fri", hour="10-18"), name="replies")

    # Daily content batch emailed to you for review.
    sched.add_job(safe(content.run, "content"),
                  CronTrigger(hour=8, minute=0), name="content")

    # End-of-day digest of replies needing your attention.
    sched.add_job(safe(engine.send_digest, "digest"),
                  CronTrigger(day_of_week="mon-fri", hour=18, minute=30), name="digest")

    # --- the Monday research block ------------------------------------------
    # Ordered deliberately: discover finds channels, enrich fills gaps in rows
    # that arrived without a channel_id, snapshot captures counts AND scores
    # growth, promote graduates whatever now qualifies, digest asks you about
    # the ones still missing an address. Each step feeds the next, so the gaps
    # are sized to let a slow run finish before the next starts.
    sched.add_job(safe(youtube_sourcing.discover, "discover"),
                  CronTrigger(day_of_week="mon", hour=6, minute=0), name="discover")
    sched.add_job(safe(youtube_sourcing.enrich_prospects, "enrich"),
                  CronTrigger(day_of_week="mon", hour=6, minute=30), name="enrich")
    sched.add_job(safe(youtube_sourcing.snapshot_all, "snapshot"),
                  CronTrigger(day_of_week="mon", hour=7, minute=0), name="snapshot")
    sched.add_job(safe(youtube_sourcing.promote_growing, "promote"),
                  CronTrigger(day_of_week="mon", hour=7, minute=30), name="promote")
    sched.add_job(safe(engine.send_candidate_digest, "candidate-digest"),
                  CronTrigger(day_of_week="mon", hour=8, minute=0), name="candidate-digest")

    # Your reply to the digest lands whenever it lands. Checked a few times a
    # day rather than weekly, so an address you send on Monday afternoon is in
    # the sequence by Monday evening instead of waiting a week.
    sched.add_job(safe(engine.process_digest_replies, "digest-replies"),
                  CronTrigger(day_of_week="mon-fri", hour="9,14,18"), name="digest-replies")

    # Long loop: re-touch parked prospects whose ~90-day wait is up. Runs the
    # normal (non-force) pass; the one-time re-enroll is `run.py retouch --now`.
    sched.add_job(safe(engine.retouch_due, "retouch"),
                  CronTrigger(day_of_week="tue", hour=9, minute=0), name="retouch")

    log.info("Scheduler started. Jobs:")
    for j in sched.get_jobs():
        log.info(f"  {j.name}: {j.trigger}")
    sched.start()


if __name__ == "__main__":
    main()
