"""Generates ONE personalized opening sentence per prospect.

This is the only creative step and the one that actually moves reply rates:
a single line that proves you looked at their channel, not "creators like you".
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

Creator channel: {channel}
Subscriber count: {subs}
Their latest video title: {latest}

Write ONE sentence (max 25 words) that shows genuine, specific attention to THIS creator.
Rules:
- Reference their content/style/latest video concretely. No generic flattery.
- Do NOT pitch, do NOT mention pricing, do NOT use exclamation marks.
- Sound like a peer who watches their stuff, not a marketer.
- Plain, warm, direct. No emojis.
Return ONLY the sentence, nothing else."""


def _generate(channel, subs, latest):
    msg = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": PROMPT.format(
                channel=channel or "(unknown)",
                subs=subs or "unknown",
                latest=latest or "(unknown)",
            ),
        }],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def run(limit=None):
    """Fill personalized lines for every 'new' prospect that has an email."""
    rows = db.prospects_needing_line()
    if limit:
        rows = rows[:limit]
    done = 0
    for p in rows:
        line = _generate(p["channel_name"], p["subscriber_count"], p["latest_video"])
        db.set_line(p["id"], line)
        done += 1
        print(f"  [line] {p['channel_name']}: {line}")
    return done
