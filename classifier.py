"""Classifies an inbound reply into a single intent bucket.

A keyword fast-path catches explicit opt-outs so we never spend an API call
(or risk a misread) on a clear 'unsubscribe'. Everything else goes to the model.
"""
import json

from anthropic import Anthropic

import config

_client = None

LABELS = ("interested", "question", "not_interested", "unsubscribe", "auto_reply", "other")

_OPTOUT_WORDS = ("unsubscribe", "stop emailing", "remove me", "take me off", "opt out", "do not contact")


def client():
    global _client
    if _client is None:
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


PROMPT = """Classify this reply to a cold outreach email from a Roblox game studio.

Reply text:
\"\"\"{text}\"\"\"

Choose exactly one label:
- interested: wants to talk, book a call, learn more, sounds positive
- question: asks something (price, examples, timeline) but hasn't committed
- not_interested: a clear no / not now / already have someone
- unsubscribe: asks to stop being contacted / opt out
- auto_reply: out-of-office or automated bounce/auto-responder
- other: anything else

Respond with ONLY a JSON object: {{"label": "<one label>", "reason": "<5 words>"}}"""


def classify(text):
    low = (text or "").lower()
    for w in _OPTOUT_WORDS:
        if w in low:
            return {"label": "unsubscribe", "reason": "explicit opt-out keyword"}

    msg = client().messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=80,
        messages=[{"role": "user", "content": PROMPT.format(text=text[:2000])}],
    )
    raw = "".join(b.text for b in msg.content if b.type == "text").strip()
    raw = raw.replace("```json", "").replace("```", "").strip()
    try:
        data = json.loads(raw)
        if data.get("label") in LABELS:
            return data
    except json.JSONDecodeError:
        pass
    return {"label": "other", "reason": "unparseable classification"}
