"""Load the streamer-outreach sheet, respecting the Status column already in it.

This is signal-driven: a row's existing Status decides what the machine does,
so importing never re-emails someone who was already contacted. Columns expected:
  Account Name, Email, Status, URL, Contact method, language
"""
import csv
import re

import db

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Addresses that hard-bounced in the August run — don't re-contact.
KNOWN_BOUNCED = {
    "mackenzie@ellify.com", "foltyn@ellify.com", "ella@ellify.com",
    "alaskaviolet@ellify.com", "flamingo@ellify.com", "itsnico@anc-network.com",
    "musa@lyaison.com", "wucutesingle@night.co",
}

# His Status text -> machine status.
STATUS_MAP = {
    "not reached out": "new",                      # fresh -> gets the opener
    "first outreach - no answer": "parked",        # already opened -> long-loop re-touch
    "first contact": "hold",                       # already in contact -> manual
    "information retrieval": "hold",               # still gathering info
}


def _clean_email(raw):
    """Pull the first valid email out of a messy cell; None if there isn't one."""
    m = _EMAIL_RE.search(raw or "")
    if not m:
        return None
    e = m.group(0).lower()
    return None if e in KNOWN_BOUNCED else e


def _platform(url):
    u = (url or "").lower()
    if "twitch" in u:
        return "twitch"
    if "youtu" in u:
        return "youtube"
    return "other"


def load(path):
    counts = {"new": 0, "parked": 0, "hold": 0, "skipped": 0}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            email = _clean_email(row.get("Email"))
            method = (row.get("Contact method") or "").strip().lower()
            status_text = (row.get("Status") or "").strip().lower()

            # No valid/live email, or reachable only via a non-email channel.
            if not email or method in ("discord", "donation"):
                counts["skipped"] += 1
                continue

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
            counts[machine_status if created else "skipped"] += 1
    return counts
