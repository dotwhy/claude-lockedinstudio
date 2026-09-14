"""Generates ONE personalized opening sentence per prospect.

This is the only creative step and the one that actually moves reply rates: a
single line that proves you looked at their channel, not "creators like you".

Two things the earlier version was missing.

First, it never told the model what LockedIn Studio does, so the line could
only comment on the creator's content. The connection to the offer had to be
carried entirely by the template, which is what made the email read like a form
letter. With studio context the line can land on the thing about their channel
that a game would actually be built around.

Second, it only had a video title. "I saw your latest video" is what every
other cold email says. Knowing that the video did 3x the channel average lets
the line say something the creator knows is true and assumes only a real viewer
would notice.
"""
from anthropic import Anthropic

import config
import db

_client = None


def client():
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set in .env")
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


PROMPT = """You write the opening line of a cold email from a Roblox game studio to a YouTube creator.

ABOUT THE STUDIO (context only — do NOT pitch it in the line):
{studio}

THE CREATOR:
Channel: {channel}
Subscribers: {subs}
Latest video: {latest}
{performance}

Write ONE sentence, maximum 25 words, that proves a real person looked at THIS channel.

Rules:
- Reference something concrete: the latest video, their format, or how it performed.
- If the performance note says a video beat their average, that is the strongest
  thing to lead with — creators notice when someone noticed.
- Do NOT pitch, do NOT mention games, pricing, or building anything. The email
  body does that. This line only earns the right to be read.
- No exclamation marks. No emojis. No "I hope this finds you well".
- Never invent a fact. Only describe what is stated above.
- Claim no pattern you were not given. You are seeing ONE video, so never say
  "consistently", "always", "every video", or "we noticed a pattern".
- Banned phrasing, it reads as automated: "hit different", "hitting different",
  "is fire", "next level", "speaks volumes".
- Sound like a peer who watches their stuff, not a marketer.

IF THE CHANNEL DATA ABOVE IS MISSING OR UNUSABLE (no real video title, nothing
concrete to point at): reply with exactly the word NONE and nothing else. The
email works fine without this line. Never explain what you're missing and never
write a placeholder — whatever you return goes straight into an email to a real
person.

Return ONLY the sentence, or NONE."""

# Phrases that mean the model narrated its own limits instead of writing a line.
# Any of these reaching an inbox is worse than sending no line at all.
_META_MARKERS = (
    "i don't have", "i do not have", "i can't", "i cannot", "i'm unable",
    "to write this", "i'd need", "i would need", "no access", "not enough",
    "without inventing", "as an ai", "placeholder", "insufficient",
    "unusable", "[", "unknown)",
)

# Claims we cannot support from a single video's numbers.
_OVERCLAIM_MARKERS = ("consistently", "we noticed", "always ", "every video",
                      "each week", "every upload")

# Verbal tics. Fine once; across 40 emails it reads as a template.
_TIC_MARKERS = ("hit different", "hitting different", "is fire", "next level",
                "speaks volumes")


def validate_line(line):
    """Return a usable line, or None if it must not be sent.

    Whatever comes back from the model is pasted verbatim into an email to a
    real creator, so this is the last gate before that happens. Returning None
    is always safe: the templates render cleanly without a personalized line.
    """
    if not line:
        return None
    cleaned = " ".join(line.split()).strip().strip('"').strip()
    if not cleaned or cleaned.upper().startswith("NONE"):
        return None
    low = cleaned.lower()
    if any(marker in low for marker in _META_MARKERS):
        return None
    if any(marker in low for marker in _OVERCLAIM_MARKERS):
        return None
    if any(marker in low for marker in _TIC_MARKERS):
        return None
    # A real opening line is one sentence. Anything long is the model
    # explaining itself in prose.
    if len(cleaned.split()) > 30:
        return None
    return cleaned


def performance_note(latest_views, avg_views):
    """One line describing how the latest upload did against the usual.

    Returns "" when there isn't enough to say something true — an empty note is
    much better than a vague one, because the model will otherwise reach for
    filler like "your recent video is doing great".
    """
    if not latest_views or not avg_views or avg_views <= 0:
        return ""
    ratio = latest_views / avg_views
    if ratio >= 1.5:
        return (f"Performance: that video has {latest_views:,} views against a "
                f"channel average of {avg_views:,} — about {ratio:.1f}x their usual.")
    if ratio <= 0.6:
        # Deliberately not surfaced as a talking point. Telling a creator their
        # video underperformed is a bad opening line, but the model should know
        # not to praise it either.
        return "Performance: that video did below their usual numbers — do not comment on it."
    return f"Performance: that video did about typical numbers for them ({latest_views:,} views)."


def _ask(prompt):
    message = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


def _generate(channel, subs, latest, latest_views, avg_views):
    """One validated line, or None.

    Retries once on a rejected line, because a single bad sample is usually
    just a bad roll. After that it gives up and returns None rather than
    spending tokens to force a line the data can't support.
    """
    prompt = PROMPT.format(
        studio=config.STUDIO_CONTEXT,
        channel=channel or "(unknown)",
        subs=f"{subs:,}" if subs else "unknown",
        latest=latest or "(unknown)",
        performance=performance_note(latest_views, avg_views),
    )
    line = validate_line(_ask(prompt))
    if line:
        return line
    retry = prompt + ("\n\nYour previous attempt was rejected. Return ONE short "
                      "sentence about the video title above, or exactly NONE.")
    return validate_line(_ask(retry))


def revalidate_stored():
    """Re-check every line already in the database against current validation.

    Lines written before a validation rule existed are still sitting there
    waiting to be sent. This clears the ones that would now be rejected and
    resets their attempt counter so the next personalize run rewrites them.

    Returns (cleared, kept).
    """
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, channel_name, personalized_line FROM prospects "
        "WHERE personalized_line IS NOT NULL AND personalized_line != ''"
    ).fetchall()
    cleared = 0
    for row in rows:
        if validate_line(row["personalized_line"]) is not None:
            continue
        conn.execute(
            "UPDATE prospects SET personalized_line=NULL, line_attempts=0 WHERE id=?",
            (row["id"],),
        )
        cleared += 1
        print(f"  [cleared] {row['channel_name']}: {row['personalized_line'][:70]}")
    conn.commit()
    conn.close()
    print(f"Cleared {cleared} unusable line(s); kept {len(rows) - cleared}.")
    return cleared, len(rows) - cleared


def _row(prospect, field):
    """Read a column that may not exist on rows from an older schema."""
    try:
        return prospect[field]
    except (IndexError, KeyError):
        return None


def run(limit=None):
    """Fill personalized lines for every 'new' prospect that has an email."""
    rows = db.prospects_needing_line()
    if limit:
        rows = rows[:limit]
    written = 0
    skipped = 0
    for prospect in rows:
        db.record_line_attempt(prospect["id"])
        line = _generate(
            prospect["channel_name"],
            prospect["subscriber_count"],
            prospect["latest_video"],
            _row(prospect, "latest_video_views"),
            _row(prospect, "avg_views"),
        )
        if not line:
            # No usable line. Deliberately leave it empty: the templates read
            # fine without one, and sending nothing beats sending the model
            # narrating what data it was missing.
            skipped += 1
            print(f"  [no line] {prospect['channel_name']} — sending without one")
            continue
        db.set_line(prospect["id"], line)
        written += 1
        print(f"  [line] {prospect['channel_name']}: {line}")
    print(f"Wrote {written} line(s); {skipped} will send without one.")
    return written
