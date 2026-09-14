"""One place for every rule about what counts as a usable email address.

This used to live in two files. `youtube_sourcing.py` had the regex plus a list
of junk substrings; `import_sheet.py` had the same regex plus the August bounce
list. Neither knew about the other, so a channel whose description contained an
address that hard-bounced in August would sail straight through discovery and
bounce again — and every bounce costs sending reputation.

The permanent rejects (bounces) now live in the `suppression` table instead of a
module constant. `db.upsert_prospect()` already consults that table on every
intake path, so discovery, CSV import, the API, and anything added later all get
bounce protection without having to remember to ask for it.

What stays here is the stateless part: does this text contain an address, and is
that address obviously not a human contact.
"""
import re

# Matches a plain email in free text. Deliberately permissive — `is_junk` does
# the rejecting, so that the two concerns stay separately testable.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Substrings that never indicate a real business contact: platform noise,
# transactional senders, and image filenames that happen to match the regex
# ("thumb@2x.png" is a real thing that appears in video descriptions).
JUNK_MARKERS = (
    "example.com",
    "youtube.com",
    "sentry",
    "noreply",
    "no-reply",
    "donotreply",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)

# Hard-bounced during the August 2026 run. Seeded into `suppression` by
# `db.init()` so every intake path rejects them; kept here as the migration's
# source list, not as a live check. Do not add to this — suppress() is the
# durable home for anything discovered after the migration.
AUGUST_BOUNCES = (
    "mackenzie@ellify.com",
    "foltyn@ellify.com",
    "ella@ellify.com",
    "alaskaviolet@ellify.com",
    "flamingo@ellify.com",
    "itsnico@anc-network.com",
    "musa@lyaison.com",
    "wucutesingle@night.co",
)


def is_junk(email):
    """True if this address is structurally not a business contact.

    Checked against the lowercased address, so "NoReply@Studio.com" is caught.
    """
    if not email:
        return True
    low = email.lower()
    return any(marker in low for marker in JUNK_MARKERS)


def clean(raw):
    """Pull the first usable email out of a messy string.

    Returns a lowercased address, or None if there isn't one worth keeping.
    Handles the common real-world shapes: a bare address, an address buried in
    a sentence ("business enquiries: x@y.com"), and a cell holding several
    addresses (first usable one wins).
    """
    if not raw:
        return None
    for match in EMAIL_RE.findall(raw):
        candidate = match.lower()
        if not is_junk(candidate):
            return candidate
    return None


def first_in(*texts):
    """First usable address across several blobs of text, in priority order.

    Discovery calls this with the channel description first and recent video
    descriptions after, so a creator who lists contact details on their About
    page wins over an address mentioned in passing in a video.
    """
    for text in texts:
        found = clean(text)
        if found:
            return found
    return None
