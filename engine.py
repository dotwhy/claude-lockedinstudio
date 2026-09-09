"""The autonomous loop. Three jobs:
  send_due()   -> send openers + follow-ups that are due, up to the daily cap
  process_replies() -> read replies, classify, act (drop / suppress / flag)
  send_digest()-> email YOU a summary of replies that need attention
"""
from datetime import datetime, timezone, timedelta

import config
import db
import gmail_client
import classifier


def _due(prospect):
    """Is this prospect due for its next step right now?"""
    step = prospect["step"]
    if step >= config.TOTAL_STEPS:
        return False
    if step == 0:
        return True  # opener, send immediately
    gap = config.FOLLOWUP_GAP_DAYS[step - 1]
    last = datetime.fromisoformat(prospect["last_sent_at"])
    return datetime.now(timezone.utc) >= last + timedelta(days=gap)


def send_due():
    """Walk queued + active prospects, send whatever step is due, respecting
    the per-day cap. Openers come from 'queued', follow-ups from 'active'."""
    budget = config.MAX_SENDS_PER_DAY - db.sends_today()
    if budget <= 0:
        print(f"Daily cap reached ({config.MAX_SENDS_PER_DAY}). Nothing sent.")
        return 0

    pipeline = db.prospects_by_status("queued") + db.prospects_by_status("active")
    sent = 0
    for p in pipeline:
        if sent >= budget:
            print(f"Hit daily cap ({config.MAX_SENDS_PER_DAY}). Stopping for today.")
            break
        if db.is_suppressed(p["email"]):
            db.set_status(p["id"], "unsubscribed")
            continue
        if not _due(p):
            continue

        next_step = p["step"] + 1
        template = config.STEP_TEMPLATES[next_step]
        subject, body = template(p["first_name"], p["channel_name"], p["personalized_line"] or "")

        in_reply_to = p["last_message_id"] if next_step > 1 else None
        references = p["references_chain"] if next_step > 1 else None
        thread_id = p["gmail_thread_id"] if next_step > 1 else None

        gmail_id, thread_id, msg_id = gmail_client.send(
            to=p["email"], subject=subject, body=body,
            thread_id=thread_id, in_reply_to=in_reply_to, references=references,
        )
        db.record_send(p, next_step, subject or "(reply)", body, gmail_id, thread_id, msg_id)
        sent += 1
        print(f"  [sent step {next_step}] {p['channel_name']} <{p['email']}>")

    print(f"Sent {sent} message(s). {db.sends_today()}/{config.MAX_SENDS_PER_DAY} used today.")
    return sent


def _act_on(prospect, result, reply_text, msg_id):
    """Apply the consequence of a classification."""
    label = result["label"]
    db.record_inbound(prospect["id"], reply_text, msg_id)
    snippet = " ".join(reply_text.split())[:280]

    if label == "unsubscribe":
        db.suppress(prospect["email"], "requested unsubscribe")
        db.set_status(prospect["id"], "unsubscribed")
        print(f"  [unsubscribed] {prospect['channel_name']} — dropped + suppressed")

    elif label == "not_interested":
        db.set_status(prospect["id"], "not_interested")
        print(f"  [not interested] {prospect['channel_name']} — dropped")

    elif label in ("interested", "question", "other"):
        # Stop the sequence and hand off to a human (you). Never auto-reply.
        db.set_status(prospect["id"], "interested")
        db.add_to_review(prospect["id"], label, snippet, prospect["gmail_thread_id"])
        gmail_client.flag_thread(prospect["gmail_thread_id"])
        print(f"  [FLAGGED: {label}] {prospect['channel_name']} — starred + in review queue")

    elif label == "auto_reply":
        # Out-of-office: leave the sequence running, do nothing.
        print(f"  [auto-reply ignored] {prospect['channel_name']}")


def process_replies():
    """Check every active thread for a new inbound reply, classify, and act."""
    watched = db.active_prospects()
    handled = 0
    for p in watched:
        after_ms = 0
        if p["last_sent_at"]:
            after_ms = int(datetime.fromisoformat(p["last_sent_at"]).timestamp() * 1000)
        text, msg_id = gmail_client.latest_inbound(p["gmail_thread_id"], after_ms)
        if not text:
            continue
        result = classifier.classify(text)
        _act_on(p, result, text, msg_id)
        handled += 1
    print(f"Processed {handled} new repl{'y' if handled == 1 else 'ies'}.")
    return handled


def send_digest():
    """Email YOURSELF a summary of replies waiting in the review queue."""
    rows = db.unresolved_review()
    if not rows:
        print("Review queue is empty. No digest sent.")
        return 0
    lines = [f"{len(rows)} outreach repl{'y' if len(rows)==1 else 'ies'} need you:\n"]
    for r in rows:
        subs = f"{r['subscriber_count']:,}" if r["subscriber_count"] else "?"
        lines.append(
            f"• [{r['classification'].upper()}] {r['channel_name']} ({subs} subs) "
            f"<{r['email']}>\n    \"{r['snippet']}\"\n"
        )
    lines.append("These threads are starred and under the 'Outreach/Review' label.")
    body = "\n".join(lines)
    gmail_client.send(to=gmail_client.address(), subject="Outreach: replies to review", body=body)
    print(f"Digest of {len(rows)} reply(ies) sent to yourself.")
    return len(rows)
