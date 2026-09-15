"""Central configuration. Everything tunable lives here or in .env."""
import os
from dotenv import load_dotenv

load_dotenv()


def _bool(name, default="false"):
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- Safety -----------------------------------------------------------------
DRY_RUN = _bool("DRY_RUN", "true")
MAX_SENDS_PER_DAY = _int("MAX_SENDS_PER_DAY", 30)

# Shared secret for the HTTP API. Without it the API refuses to serve — the
# endpoints write into `prospects`, and anything written there gets cold-emailed
# from your personal Gmail, so an open endpoint is a spam relay with your name
# on it. Unset means the API simply doesn't start; the scheduler still runs.
API_KEY = os.getenv("API_KEY", "").strip()
API_PORT = _int("PORT", 8080)  # Railway injects PORT

# --- Discovery + growth -----------------------------------------------------
# Window used to decide "is this channel growing". 30 days is long enough to
# smooth a viral spike and short enough to catch momentum while it's live.
GROWTH_WINDOW_DAYS = _int("GROWTH_WINDOW_DAYS", 30)

# How long before the weekly digest re-asks about a channel you never resolved.
CANDIDATE_REASK_DAYS = _int("CANDIDATE_REASK_DAYS", 30)

# Snapshots resolve channels by id through channels.list (~1 unit per 50 ids).
# Rows with no stored channel_id fall back to search.list, which costs 100 units
# EACH — so the fallback is capped. Without this cap a few hundred id-less rows
# would silently eat the entire 10,000/day quota and the job would die midway.
CHANNEL_BATCH_SIZE = _int("CHANNEL_BATCH_SIZE", 50)
MAX_SEARCH_FALLBACKS = _int("MAX_SEARCH_FALLBACKS", 20)

# --- Anthropic --------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# --- YouTube ----------------------------------------------------------------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_SEARCH_TERMS = [
    t.strip() for t in os.getenv("YOUTUBE_SEARCH_TERMS", "roblox").split(",") if t.strip()
]
MIN_SUBSCRIBERS = _int("MIN_SUBSCRIBERS", 20000)

# Upper bound on discovery. Raised from 500k so that established channels are
# found at all — they used to be filtered out before anything else could look
# at them. The cap still exists because there is no point queueing 20M-sub
# channels that will never answer a cold email; they just burn quota and take
# up slots under the daily send cap.
MAX_SUBSCRIBERS = _int("MAX_SUBSCRIBERS", 5_000_000)

# --- fast track -------------------------------------------------------------
# At or above this size, a channel does not have to prove it is growing. It is
# already established, and waiting two snapshot cycles to email it is two weeks
# of nothing. Growth detection exists to find channels on the way up; a channel
# that is already up doesn't need the test.
#
# "Actively posting" is the other half — a large but dormant channel is worse
# than a small live one, because the audience has already moved on.
FASTTRACK_SUBSCRIBERS = _int("FASTTRACK_SUBSCRIBERS", 500_000)
ACTIVE_UPLOAD_DAYS = _int("ACTIVE_UPLOAD_DAYS", 30)

# --- Gmail ------------------------------------------------------------------
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
FROM_NAME = os.getenv("FROM_NAME", "")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
]

# --- Brand / content --------------------------------------------------------
STUDIO_NAME = os.getenv("STUDIO_NAME", "LockedIn Studio")
STUDIO_ADDRESS = os.getenv("STUDIO_ADDRESS", "")
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL", "")

# What the studio actually does, in the words the opener should be written
# from. This is fed to Claude when generating the personalized line so the line
# can connect what a creator already makes to what we'd build for them —
# without it, the model can only comment on their content and the connection to
# the offer has to be carried by the template, which reads like a form letter.
#
# Keep this short. It is context for one sentence, not a brief.
STUDIO_CONTEXT = os.getenv("STUDIO_CONTEXT", """\
LockedIn Studio builds full Roblox games for creators, end to end: design,
development, and live-ops. The creator owns the game; we handle everything
inside Roblox Studio. Games are built around the creator's existing audience
and content style, so their viewers already want to play it.""").strip()

# --- Storage ----------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "outreach.db")

# --- Sequence cadence -------------------------------------------------------
# Days to wait after the PREVIOUS step before sending the next one.
# [4, 3]  ->  opener, then follow-up 1 four days later, then breakup 3 days after that.
FOLLOWUP_GAP_DAYS = [4, 3]
TOTAL_STEPS = 1 + len(FOLLOWUP_GAP_DAYS)  # opener + follow-ups

# The Gmail label applied to threads that need your attention.
REVIEW_LABEL = "Outreach/Review"


def _footer():
    return (
        f"—\n{STUDIO_NAME} · {STUDIO_ADDRESS}\n"
        "Reply 'unsubscribe' and I won't email again."
    )


# --- sending preflight ------------------------------------------------------
# Markers that mean a value was never filled in. Two of these went out in real
# emails: "A few we've shipped: your-portfolio-link-here" and a CAN-SPAM footer
# reading "LockedIn Studio · your real postal address here".
#
# A placeholder in the footer is not cosmetic — the postal address is what makes
# the email legal to send. Refusing to send is strictly better than sending
# something that damages the brand and the compliance position at once.
_PLACEHOLDER_MARKERS = ("your-", "your real", "your street", "your city",
                        "<", ">", "example.com", "example.", "placeholder",
                        "todo", "xxx", "here>", "-here", " here")


def _looks_unset(value):
    if not value or not value.strip():
        return True
    low = value.strip().lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def sending_config_problems():
    """Config that must be real before anything may be emailed.

    Returns a list of human-readable problems; empty means safe to send.
    """
    problems = []
    if _looks_unset(FROM_NAME):
        problems.append("FROM_NAME is empty or a placeholder")
    if _looks_unset(STUDIO_ADDRESS):
        problems.append(
            "STUDIO_ADDRESS is empty or a placeholder — this is the CAN-SPAM "
            "postal address and appears in every email footer")
    if _looks_unset(PORTFOLIO_URL):
        problems.append(
            "PORTFOLIO_URL is empty or a placeholder — it appears in the "
            "opener as 'A few we've shipped: ...'")
    return problems


# --- Email templates --------------------------------------------------------
# Each returns (subject, body). `line` is the AI-generated personalization.
def opener(first_name, channel_name, line):
    """The first email. ~35 words before the footer, on purpose.

    The old version wrapped the personalized line in a 30-word pitch paragraph,
    which made a message that opened with genuine attention read like a
    templated sales email by the third line. The line does the work; the body
    just has to say what we do and get out of the way.
    """
    subject = f"A Roblox game built for {channel_name}"
    body = (
        f"Hey {first_name},\n\n"
        f"{line}\n\n"
        "We build Roblox games for creators, end to end. "
        f"A few we've shipped: {PORTFOLIO_URL}\n\n"
        "Worth a quick chat?\n\n"
        f"{FROM_NAME}, {STUDIO_NAME}\n\n"
        f"{_footer()}"
    )
    return subject, body


def followup_1(first_name, channel_name, line):
    body = (
        f"Hey {first_name},\n\n"
        f"Following up — did the portfolio land? {PORTFOLIO_URL}\n\n"
        f"Happy to sketch a rough game concept that fits {channel_name} "
        "specifically, no cost. Want me to put one together?\n\n"
        f"best,\n{FROM_NAME}\n\n"
        f"{_footer()}"
    )
    return None, body  # None subject => reply in-thread, keeps original subject


def followup_2(first_name, channel_name, line):
    body = (
        f"Hey {first_name},\n\n"
        "Last one from me — if owning your own Roblox game isn't a priority "
        "right now, no worries, I'll close this out.\n\n"
        "If it is, just reply and I'll take it from there.\n\n"
        f"best,\n{FROM_NAME}\n\n"
        f"{_footer()}"
    )
    return None, body


# step number (1-indexed) -> template function
STEP_TEMPLATES = {1: opener, 2: followup_1, 3: followup_2}


# --- long-loop re-touch -----------------------------------------------------
# Sent to prospects who got the first sequence months ago and never replied.
# Deliberately short and more personal than the original opener.
RETOUCH_WAIT_DAYS = 90  # ~3 months before a parked prospect is re-touched

def retouch(first_name, channel_name, line):
    subject = f"{first_name} — still building?"
    personal = f"{line}\n\n" if line else ""
    body = (
        f"Hey {first_name},\n\n"
        f"{personal}"
        "Reached out a while back about building you a Roblox game — circling "
        "back now we've shipped a few more. If owning your own is on your radar, "
        "worth a quick look? No stress if not.\n\n"
        f"{FROM_NAME}, {STUDIO_NAME}\n"
        "Reply 'unsubscribe' to opt out."
    )
    return subject, body
