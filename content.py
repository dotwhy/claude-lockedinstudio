"""Daily content engine for an ANONYMOUS LockedIn brand account.

Generates a batch of ready-to-post social content (X posts + TikTok script/
caption ideas) built around Roblox game-dev content pillars, then emails the
batch to you to review. It never posts anywhere — you stay the publish button.

The voice is faceless/brand: no "I", no personal identity, no face. It's about
the WORK — builds, before/afters, timelapses, tips.
"""
import datetime

from anthropic import Anthropic

import config
import gmail_client

_client = None


def client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# Content pillars keep the feed varied instead of same-y.
PILLARS = [
    "build breakdown: explain one system we built and why it matters for retention",
    "before/after: a rough prototype vs the polished result",
    "timelapse hook: caption for a sped-up build clip",
    "how we made X: short walkthrough of a mechanic (combat, tycoon, roleplay map)",
    "myth or mistake: a common Roblox dev mistake creators make, and the fix",
    "engagement: a question or hot take that gets Roblox devs/creators replying",
]

X_PROMPT = """You write posts for an ANONYMOUS Roblox game-development studio brand account on X.
Rules:
- Faceless brand voice. Never use "I" or a personal identity. It's about the work.
- No hashtags spam (0-1 max). No emojis unless it genuinely lands. No hype words.
- Punchy, concrete, useful to Roblox creators and devs. Show, don't sell.
- 1-3 short lines. If it's a thread hook, mark it "(thread)".

Write ONE X post for this pillar: {pillar}
Return only the post text."""

TIKTOK_PROMPT = """You write short-form video ideas for an ANONYMOUS Roblox game-dev studio brand (faceless, no talking head — screen-recording / build footage only).
For this pillar: {pillar}
Return exactly:
HOOK: <first 2 seconds on-screen text, max 8 words>
SHOTS: <one line describing the b-roll/screen capture to show>
CAPTION: <the posted caption, 1 line, 0-1 hashtag>
Nothing else."""


def _gen(prompt, pillar, max_tokens=200):
    msg = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt.format(pillar=pillar)}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def build_batch(x_count=3, tiktok_count=2):
    """Generate today's content: a few X posts + a couple TikTok ideas."""
    day = datetime.date.today()
    x_posts = [_gen(X_PROMPT, PILLARS[i % len(PILLARS)]) for i in range(x_count)]
    tiktoks = [_gen(TIKTOK_PROMPT, PILLARS[(i + 2) % len(PILLARS)]) for i in range(tiktok_count)]

    parts = [f"LockedIn content — {day:%A %d %b} (review, then post)\n"]
    parts.append("=== X / Twitter ===")
    for i, p in enumerate(x_posts, 1):
        parts.append(f"\n[{i}]\n{p}")
    parts.append("\n\n=== TikTok (faceless / screen-record) ===")
    for i, t in enumerate(tiktoks, 1):
        parts.append(f"\n[{i}]\n{t}")
    parts.append("\n\n—\nGenerated for review. Nothing has been posted.")
    return "\n".join(parts)


def run():
    """Build today's batch and email it to yourself for review."""
    body = build_batch()
    subject = f"Content to review — {datetime.date.today():%d %b}"
    gmail_client.send(to=gmail_client.address(), subject=subject, body=body)
    print("Content batch generated and emailed to you for review.")
    if config.DRY_RUN:
        print("\n(DRY_RUN preview:)\n" + body)
    return body
