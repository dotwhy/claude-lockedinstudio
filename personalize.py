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
- Never invent a fact. If you only have a title, respond to the title.
- Sound like a peer who watches their stuff, not a marketer.

Return ONLY the sentence, nothing else."""


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


def _generate(channel, subs, latest, latest_views, avg_views):
    message = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                studio=config.STUDIO_CONTEXT,
                channel=channel or "(unknown)",
                subs=f"{subs:,}" if subs else "unknown",
                latest=latest or "(unknown)",
                performance=performance_note(latest_views, avg_views),
            ),
        }],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


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
    done = 0
    for prospect in rows:
        line = _generate(
            prospect["channel_name"],
            prospect["subscriber_count"],
            prospect["latest_video"],
            _row(prospect, "latest_video_views"),
            _row(prospect, "avg_views"),
        )
        db.set_line(prospect["id"], line)
        done += 1
        print(f"  [line] {prospect['channel_name']}: {line}")
    return done
