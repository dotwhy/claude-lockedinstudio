"""Load the streamer-outreach sheet, respecting the Status column already in it.

This is signal-driven: a row's existing Status decides what the machine does,
so importing never re-emails someone who was already contacted. Columns expected:
  Account Name, Email, Status, URL, Contact method, language

Safe to re-run. `upsert_prospect` refuses any email it already knows, so a
second import of the same sheet adds nothing and changes nothing — re-upload
whenever the sheet gains rows rather than trying to import only the new ones.

The hard-bounced addresses that used to live here as a constant now live in the
`suppression` table (seeded by `db.init()`). That means discovery and the API
reject them too, instead of only this one path.
"""
import csv

import db
import emails

# His Status text -> machine status.
STATUS_MAP = {
    "not reached out": "new",                      # fresh -> gets the opener
    "first outreach - no answer": "parked",        # already opened -> long-loop re-touch
    "first contact": "hold",                       # already in contact -> manual
    "information retrieval": "hold",               # still gathering info
}

# Rows reachable only through a channel we don't send on.
UNSENDABLE_METHODS = ("discord", "donation")


def _platform(url):
    u = (url or "").lower()
    if "twitch" in u:
        return "twitch"
    if "youtu" in u:
        return "youtube"
    return "other"


def load(path):
    """Import a sheet. Returns counts by outcome.

    new/parked/hold  — rows that became prospects, by mapped status
    no_email         — no usable address, or reachable only via Discord/donation
    suppressed       — address previously bounced or unsubscribed
    already_known    — email already in the database (a re-import, or a dupe row)
    """
    counts = {"new": 0, "parked": 0, "hold": 0,
              "no_email": 0, "suppressed": 0, "already_known": 0}

    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            email = emails.clean(row.get("Email"))
            method = (row.get("Contact method") or "").strip().lower()

            if not email or method in UNSENDABLE_METHODS:
                counts["no_email"] += 1
                continue

            # Counted separately from a plain duplicate so a re-import can show
            # at a glance that bounces are being held out, not silently dropped.
            if db.is_suppressed(email):
                counts["suppressed"] += 1
                continue

            status_text = (row.get("Status") or "").strip().lower()
            machine_status = STATUS_MAP.get(status_text, "hold")

            created = db.upsert_prospect(
                channel_name=(row.get("Account Name") or "").strip(),
                email=email,
                status=machine_status,
                profile_url=(row.get("URL") or "").strip(),
                platform=_platform(row.get("URL")),
                language=(row.get("language") or "").strip() or None,
                contact_method=method or None,
                source="sheet",
            )
            counts[machine_status if created else "already_known"] += 1

    return counts
