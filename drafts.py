"""Build a personalized Gmail DRAFT for every prospect — you review and send.

Personalization pulls from three real sources:
  1. Their enriched channel data (subs, latest video) from the scanner.
  2. Any prior email thread with them (did they reply? what did they say?).
  3. Whether they're a creator or a management company (different angle).

Nothing is sent. Drafts land in your Gmail Drafts folder.
"""
from anthropic import Anthropic

import config
import db
import gmail_client

_client = None
# Domains that signal a management company / agency rather than a solo creator.
_AGENCY_HINTS = ("mgmt", "management", "media", "network", "agency", "team",
                 ".gg", ".tv", ".co", "manag3r", "1up", "apollo", "click",
                 "futurelegends", "playbook", "amplified")


def client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def is_agency(email):
    dom = email.split("@")[-1].lower()
    if dom.endswith("gmail.com"):
        # gmail can still be a manager (e.g. heavensmgmt@gmail.com) — check local part
        local = email.split("@")[0].lower()
        return any(h in local for h in ("mgmt", "management", "team", "collab", "branding"))
    return any(h in dom for h in _AGENCY_HINTS)


CREATOR_PROMPT = """Write a short cold email from Laurenz at LockedIn Studio, a Roblox game studio, to a YouTube creator.

Creator: {name}
Subscribers: {subs}
Their latest video: {latest}
Prior contact with us: {context}

Rules:
- Open with ONE specific line proving we looked at their channel (use the latest video / niche).
- One tight paragraph: we build full Roblox games for creators end-to-end (design, dev, live-ops).
- Include this proof link naturally: {portfolio}
- If prior contact shows they replied before, acknowledge it lightly; if not, don't mention it.
- Plain, warm, direct. No hype, no exclamation marks, no emojis. Sign "Laurenz, LockedIn Studio".
- End with the unsubscribe line: "Reply 'unsubscribe' and I won't email again."
Return only the email body."""

AGENCY_PROMPT = """Write a short cold email from Laurenz at LockedIn Studio, a Roblox game studio, to a talent manager / agency.

Contact/agency: {name}
Prior contact with us: {context}

Angle — sell the ROSTER, not one creator:
- We can be their on-call Roblox dev team for ANY creator on their roster who wants their own game; they keep a single point of contact.
- Mention we just shipped Ride-a-Brainrot-to-Escape and are mid-build on a roleplay town for a ~175k creator.
- Include proof link: {portfolio}
- Ask for 15 minutes to see if a couple of their creators fit.
- Plain, direct, business tone. No hype/emojis. Sign "Laurenz, LockedIn Studio".
- End with: "Reply 'unsubscribe' and I won't email again."
Return only the email body."""


def _generate(prospect, agency, context):
    prompt = AGENCY_PROMPT if agency else CREATOR_PROMPT
    filled = prompt.format(
        name=prospect["channel_name"] or prospect["first_name"],
        subs=f"{prospect['subscriber_count']:,}" if prospect["subscriber_count"] else "unknown",
        latest=prospect["latest_video"] or "(unknown)",
        context=(context[:400] if context else "none"),
        portfolio=config.PORTFOLIO_URL,
    )
    msg = client().messages.create(
        model=config.ANTHROPIC_MODEL, max_tokens=400,
        messages=[{"role": "user", "content": filled}],
    )
    return "".join(b.text for b in msg.content if b.type == "text").strip()


def build_all():
    """Create a reviewable draft for every emailable prospect."""
    made = 0
    for p in db.all_emailable():
        if p["status"] == "drafted":
            continue
        agency = is_agency(p["email"])
        context = gmail_client.find_thread_context(p["email"])
        body = _generate(p, agency, context)
        subject = "Roblox builds for your roster" if agency \
            else f"A Roblox game built for {p['channel_name']}"
        gmail_client.create_draft(p["email"], subject, body)
        db.set_status(p["id"], "drafted")
        made += 1
        print(f"  [draft{' AGENCY' if agency else ''}] {p['channel_name']} <{p['email']}>")
    print(f"Created {made} draft(s). Review them in Gmail and send.")
    return made
