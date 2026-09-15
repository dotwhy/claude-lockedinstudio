#!/usr/bin/env python3
"""Command-line entrypoint for the LockedIn creator-outreach system.

Usage:
    python run.py init                 # create the database
    python run.py import prospects.csv # load prospects (columns: email, channel_name, [first_name, subscriber_count, latest_video])
    python run.py load sheet.csv       # load your streamer sheet (Account Name, Email, Status, URL, Contact method, language)
    python run.py retouch --now        # one-time: re-enroll ALL parked prospects into the long-loop opener now
    python run.py retouch              # long-loop pass: re-touch only parked prospects whose ~90-day wait is up
    python run.py source               # discover new candidates via YouTube (writes candidates.csv for enrichment)
    python run.py personalize          # generate a custom opening line for each new prospect
    python run.py send                 # send openers + due follow-ups (respects DRY_RUN + daily cap)
    python run.py replies              # read replies, classify, drop/suppress/flag
    python run.py digest               # email yourself the review queue
    python run.py tick                 # personalize -> send -> replies  (the daily autonomous job)
    python run.py stats                # show pipeline counts

Make it autonomous by scheduling `python run.py tick` (see README).
"""
import csv
import sys

import config
import db


def cmd_init():
    db.init()
    print(f"Initialized database at {config.DB_PATH}")


def cmd_import(path):
    db.init()
    added = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if db.upsert_prospect(
                channel_id=row.get("channel_id"),
                channel_name=row.get("channel_name"),
                first_name=row.get("first_name"),
                email=row.get("email"),
                subscriber_count=int(row["subscriber_count"]) if row.get("subscriber_count") else None,
                latest_video=row.get("latest_video"),
                source="import",
            ):
                added += 1
    print(f"Imported {added} new prospect(s) from {path}")


def cmd_source():
    import youtube_sourcing
    db.init()
    added = youtube_sourcing.discover()
    print(f"Sourced {added} new prospect(s) with emails. "
          "Email-less candidates written to candidates.csv for enrichment.")


def cmd_personalize():
    import personalize
    n = personalize.run()
    print(f"Generated {n} personalized line(s).")


def cmd_send():
    import engine
    engine.send_due()


def cmd_replies():
    import engine
    engine.process_replies()


def cmd_digest():
    import engine
    engine.send_digest()


def cmd_content():
    import content
    content.run()


def cmd_enrich():
    import youtube_sourcing
    db.init()
    n = youtube_sourcing.enrich_prospects()
    print(f"Enriched {n} prospect(s) with channel data.")


def cmd_snapshot():
    import youtube_sourcing
    db.init()
    youtube_sourcing.snapshot_all()


def cmd_drafts():
    import drafts
    db.init()
    drafts.build_all()


def cmd_load(path):
    import import_sheet
    db.init()
    counts = import_sheet.load(path)
    print(f"Loaded sheet: {counts}")
    print("  new          = will get the opener")
    print("  parked       = already contacted; waits for long-loop re-touch")
    print("  hold         = manual / in contact / info-gathering; machine won't email")
    print("  no_email     = no usable address, or Discord/donation only")
    print("  suppressed   = previously bounced or unsubscribed; held out on purpose")
    print("  already_known= email already in the database (safe re-import)")


def cmd_audit():
    """Render every template and report anything a recipient shouldn't see."""
    import copy_audit
    raise SystemExit(1 if copy_audit.report() else 0)


def cmd_promote():
    import youtube_sourcing
    db.init()
    youtube_sourcing.promote_growing()


def cmd_candidates():
    """Send the weekly 'these channels need an email' digest."""
    import engine
    db.init()
    engine.send_candidate_digest()


def cmd_digest_replies():
    import engine
    db.init()
    engine.process_digest_replies()


def cmd_retouch(force=False):
    import engine
    db.init()
    engine.retouch_due(force=force)


def cmd_tick():
    import personalize
    import engine
    print("== personalize =="); personalize.run()
    print("== send =="); engine.send_due()
    print("== replies =="); engine.process_replies()


def cmd_stats():
    db.init()
    print(f"DRY_RUN={config.DRY_RUN}  |  daily cap={config.MAX_SENDS_PER_DAY}  |  sent today={db.sends_today()}")
    print("Pipeline:")
    for r in db.counts_by_status():
        print(f"  {r['status']:<15} {r['n']}")
    review = db.unresolved_review()
    print(f"Replies awaiting your review: {len(review)}")


COMMANDS = {
    "init": cmd_init, "import": cmd_import, "source": cmd_source,
    "personalize": cmd_personalize, "send": cmd_send, "replies": cmd_replies,
    "digest": cmd_digest, "tick": cmd_tick, "stats": cmd_stats,
    "content": cmd_content, "enrich": cmd_enrich, "snapshot": cmd_snapshot,
    "drafts": cmd_drafts, "load": cmd_load, "retouch": cmd_retouch,
    "promote": cmd_promote, "audit": cmd_audit, "candidates": cmd_candidates,
    "digest-replies": cmd_digest_replies,
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd in ("import", "load"):
        if len(sys.argv) < 3:
            print(f"Usage: python run.py {cmd} <file.csv>")
            sys.exit(1)
        (cmd_import if cmd == "import" else cmd_load)(sys.argv[2])
    elif cmd == "retouch":
        # `retouch --now` re-enrolls all parked prospects immediately.
        cmd_retouch(force="--now" in sys.argv)
    else:
        COMMANDS[cmd]()


if __name__ == "__main__":
    main()
