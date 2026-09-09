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

# --- Anthropic --------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# --- YouTube ----------------------------------------------------------------
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_SEARCH_TERMS = [
    t.strip() for t in os.getenv("YOUTUBE_SEARCH_TERMS", "roblox").split(",") if t.strip()
]
MIN_SUBSCRIBERS = _int("MIN_SUBSCRIBERS", 20000)
MAX_SUBSCRIBERS = _int("MAX_SUBSCRIBERS", 500000)

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


# --- Email templates --------------------------------------------------------
# Each returns (subject, body). `line` is the AI-generated personalization.
def opener(first_name, channel_name, line):
    subject = f"A Roblox game built for {channel_name}"
    body = (
        f"Hey {first_name},\n\n"
        f"{line}\n\n"
        "We build full Roblox games end-to-end for creators — design, development "
        "and live-ops — so you can launch something that actually retains and "
        "monetises, without touching Studio yourself.\n\n"
        f"A few things we've shipped: {PORTFOLIO_URL}\n\n"
        "Worth a quick chat?\n\n"
        f"best,\n{FROM_NAME}\n{STUDIO_NAME}\n\n"
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
