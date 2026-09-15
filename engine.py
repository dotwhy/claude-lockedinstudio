"""The autonomous loop. Three jobs:
  send_due()   -> send openers + follow-ups that are due, up to the daily cap
  process_replies() -> read replies, classify, act (drop / suppress / flag)
  send_digest()-> email YOU a summary of replies that need attention
"""
from datetime import datetime, timezone, timedelta

import config
import db
import emails
import gmail_client
import classifier


def _preflight_blocked(what):
    """Refuse to send when the studio config still holds placeholders.

    Checked at the top of every sending path rather than once at startup,
    because a Railway variable can change between boot and send, and the cost
    of being wrong is an email a real creator has already read.
    """
    problems = config.sending_config_problems()
    if not problems:
        return False
    print(f"REFUSING TO SEND ({what}) — configuration is not ready:")
    for problem in problems:
        print(f"  ✗ {problem}")
    print("Set these in the environment, then run again. Nothing was sent.")
    return True


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
    if _preflight_blocked("openers and follow-ups"):
        return 0
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


# --- the weekly "help me find these emails" loop ----------------------------
# Discovery finds channels worth contacting that don't publish an address
# anywhere public. Those are a dead end for the machine but a two-minute job
# for a human, so once a week we ask.
#
#   candidates (actionable, no email)
#        │
#        ▼  send_candidate_digest()   one email, to you
#   "12 channels need emails. Reply with one per line."
#        │
#        ▼  you hit reply, paste addresses
#   process_digest_replies()  parse -> match -> promote -> confirm back
#
# The confirmation is not optional politeness: without it, a typo'd line is
# silently dropped and you believe you submitted a channel that was never
# contacted.
DIGEST_SUBJECT = "Outreach: channels that need an email"

_DIGEST_HELP = (
    "Reply to this email with one channel per line, like:\n\n"
    "  SammyGames: sammy@business.com\n"
    "  BloxKing: contact@bloxking.tv\n\n"
    "Anything I can't match, I'll tell you about in a reply."
)


def send_candidate_digest():
    """Ask you, once a week, for the addresses the scraper couldn't find."""
    rows = db.candidates_needing_email(config.CANDIDATE_REASK_DAYS)
    if not rows:
        print("No candidates need an email. No digest sent.")
        return 0

    lines = [f"{len(rows)} channel{'s' if len(rows) != 1 else ''} worth contacting, "
             "but I couldn't find an address for them:\n"]
    for row in rows:
        subs = f"{row['subscriber_count']:,}" if row["subscriber_count"] else "?"
        if row["fast_track"]:
            why = "500k+ and posting"
        elif row["growth_pct"] is not None:
            why = f"growing {row['growth_pct']:+.1f}%"
        else:
            why = "growing"
        lines.append(f"• {row['channel_name']} ({subs} subs, {why})\n"
                     f"    {row['profile_url'] or ''}")
    lines.append("\n" + _DIGEST_HELP)

    body = "\n".join(lines)
    gmail_id, thread_id, _ = gmail_client.send(
        to=gmail_client.address(), subject=DIGEST_SUBJECT, body=body
    )
    db.mark_candidates_asked([r["channel_id"] for r in rows])
    db.record_digest_thread(thread_id)
    print(f"Digest of {len(rows)} candidate(s) sent to yourself.")
    return len(rows)


def _parse_digest_reply(text):
    """Pull "Channel Name: email@host" pairs out of your reply.

    Tolerant on purpose — you'll be typing this on a phone. Accepts colon,
    dash, or just whitespace between the name and the address, ignores quoted
    text from the original digest, and skips lines with no address at all.

    Returns (pairs, unparsed_lines).
    """
    pairs = []
    unparsed = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(">") or line.startswith("•"):
            continue  # quoted original, or the digest's own bullet list
        address = emails.clean(line)
        if not address:
            # Only complain about lines that look like an attempt — a line with
            # an "@" but no valid address is a typo worth reporting; prose
            # isn't.
            if "@" in line:
                unparsed.append(line)
            continue
        name = line.replace(address, "", 1)
        for token in (address.upper(), address.lower()):
            name = name.replace(token, "")
        name = name.strip(" :-–—\t")
        if not name:
            unparsed.append(line)
            continue
        pairs.append((name, address))
    return pairs, unparsed


def process_digest_replies():
    """Read your reply to the digest, attach the addresses, promote, confirm."""
    thread_id = db.latest_digest_thread()
    if not thread_id:
        print("No digest thread on record. Nothing to read.")
        return 0

    text, _ = gmail_client.latest_inbound(thread_id, 0)
    if not text:
        print("No reply to the digest yet.")
        return 0

    pairs, unparsed = _parse_digest_reply(text)
    matched, unmatched = [], []
    for name, address in pairs:
        candidate = db.candidate_by_name(name)
        if not candidate:
            unmatched.append(f"{name}: {address}")
            continue
        db.set_candidate_email(candidate["channel_id"], address)
        matched.append(f"{candidate['channel_name']} <{address}>")

    promoted = 0
    for candidate in db.candidates_to_promote():
        if db.promote_candidate(candidate):
            promoted += 1

    _confirm_digest_reply(thread_id, matched, unmatched, unparsed, promoted)
    print(f"Digest reply: {len(matched)} matched, "
          f"{len(unmatched) + len(unparsed)} not matched, {promoted} promoted.")
    return len(matched)


def _confirm_digest_reply(thread_id, matched, unmatched, unparsed, promoted):
    """Tell you exactly what landed and what didn't.

    A silent parser is worse than no parser: you'd believe a channel was queued
    when the line was dropped, and never find out.
    """
    lines = []
    if matched:
        lines.append(f"Added {len(matched)}, {promoted} now queued for the opener:")
        lines += [f"  ✓ {m}" for m in matched]
    if unmatched:
        lines.append("\nI couldn't find a channel by these names "
                     "(check the spelling against the digest):")
        lines += [f"  ? {u}" for u in unmatched]
    if unparsed:
        lines.append("\nI couldn't read these lines:")
        lines += [f"  ! {u}" for u in unparsed]
        lines.append("\nFormat is  ChannelName: email@host")
    if not lines:
        lines.append("I couldn't find any addresses in that reply.\n\n" + _DIGEST_HELP)

    gmail_client.send(to=gmail_client.address(), subject=None,
                      body="\n".join(lines), thread_id=thread_id)


def retouch_due(force=False):
    """Long-loop re-touch: send the concise new opener to parked prospects that
    are due (or all of them, if force=True for a one-time re-enroll). They stay
    'parked' and keep cycling every RETOUCH_WAIT_DAYS until they reply."""
    if _preflight_blocked("re-touch"):
        return 0
    budget = config.MAX_SENDS_PER_DAY - db.sends_today()
    if budget <= 0:
        print(f"Daily cap reached ({config.MAX_SENDS_PER_DAY}). No re-touch sent.")
        return 0

    due = db.parked_due(config.RETOUCH_WAIT_DAYS, force=force)
    sent = 0
    for p in due:
        if sent >= budget:
            print(f"Hit daily cap ({config.MAX_SENDS_PER_DAY}). Remaining re-touches wait for tomorrow.")
            break
        if db.is_suppressed(p["email"]):
            db.set_status(p["id"], "unsubscribed")
            continue
        subject, body = config.retouch(p["first_name"], p["channel_name"], p["personalized_line"] or "")
        gmail_id, thread_id, msg_id = gmail_client.send(to=p["email"], subject=subject, body=body)
        db.record_retouch(p, subject, body, gmail_id, thread_id, msg_id)
        sent += 1
        print(f"  [re-touch] {p['channel_name']} <{p['email']}>")

    print(f"Re-touched {sent} parked prospect(s). {db.sends_today()}/{config.MAX_SENDS_PER_DAY} used today.")
    return sent
