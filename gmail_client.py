"""Thin Gmail wrapper: auth, threaded send, reply reading, label + star.

First run opens a browser once for OAuth consent and caches token.json.
Scopes: gmail.send (to send) and gmail.modify (to read threads, label, star).
"""
import base64
import os
from email.message import EmailMessage
from email.utils import make_msgid

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import config

_service = None
_addr = None


def service():
    global _service
    if _service:
        return _service
    creds = None
    if os.path.exists(config.GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(config.GMAIL_TOKEN_FILE, config.GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                config.GMAIL_CREDENTIALS_FILE, config.GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(config.GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


def address():
    global _addr
    if _addr is None:
        _addr = service().users().getProfile(userId="me").execute()["emailAddress"]
    return _addr


def _from_header():
    return f"{config.FROM_NAME} <{address()}>" if config.FROM_NAME else address()


def send(to, subject, body, thread_id=None, in_reply_to=None, references=None):
    """Send an email. If thread_id/in_reply_to are given, it threads as a reply.
    Returns (gmail_message_id, thread_id, our_message_id_header).
    Honors DRY_RUN: prints and returns fake ids without touching the network."""
    if config.DRY_RUN:
        print("\n----- [DRY_RUN] would send -----")
        print(f"To: {to}")
        print(f"Subject: {subject or '(reply, keeps thread subject)'}")
        print(body)
        print("--------------------------------\n")
        return ("dry-run", thread_id or "dry-run-thread", make_msgid(domain="dry-run.local"))

    message_id = make_msgid(domain=address().split("@")[-1])

    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = _from_header()
    if subject:
        msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = service().users().messages().send(userId="me", body=payload).execute()
    return (sent["id"], sent["threadId"], message_id)


def _extract_text(payload):
    """Pull plain text out of a Gmail message payload."""
    if payload.get("mimeType", "").startswith("text/plain") and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    for part in payload.get("parts", []) or []:
        txt = _extract_text(part)
        if txt:
            return txt
    return ""


def latest_inbound(thread_id, after_ms):
    """Return (text, message_id) of the newest message in the thread that is
    NOT from us and arrived after `after_ms`. Returns (None, None) if none."""
    thread = service().users().threads().get(userId="me", id=thread_id, format="full").execute()
    me = address().lower()
    best = None
    for m in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        sender = headers.get("from", "").lower()
        internal = int(m.get("internalDate", 0))
        if me in sender:
            continue
        if after_ms and internal <= after_ms:
            continue
        if best is None or internal > best[0]:
            best = (internal, _extract_text(m["payload"]), m["id"])
    if best:
        return best[1].strip(), best[2]
    return None, None


def latest_in_thread(thread_id, exclude_ids=()):
    """Newest message in a thread REGARDLESS of who sent it.

    `latest_inbound` deliberately skips anything we sent, which is right for a
    prospect thread. The digest thread is the opposite case: it goes from you
    to yourself, so your reply is also "from us" and that filter drops the one
    message we actually need. Exclusion is by message id instead — we know
    exactly which message was the digest, because we recorded it when sending.
    """
    thread = service().users().threads().get(
        userId="me", id=thread_id, format="full").execute()
    best = None
    for m in thread.get("messages", []):
        if m["id"] in exclude_ids:
            continue
        internal = int(m.get("internalDate", 0))
        if best is None or internal > best[0]:
            best = (internal, _extract_text(m["payload"]), m["id"])
    if best:
        return (best[1] or "").strip(), best[2]
    return None, None


def _label_id(name):
    """Find or create a label by name, return its id."""
    svc = service()
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labels:
        if lab["name"] == name:
            return lab["id"]
    created = svc.users().labels().create(
        userId="me", body={"name": name, "labelListVisibility": "labelShow",
                            "messageListVisibility": "show"}
    ).execute()
    return created["id"]


def flag_thread(thread_id):
    """Star the thread and apply the review label so it surfaces in your inbox."""
    if config.DRY_RUN:
        print(f"[DRY_RUN] would star + label thread {thread_id} as '{config.REVIEW_LABEL}'")
        return
    label = _label_id(config.REVIEW_LABEL)
    service().users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": [label, "STARRED"]}
    ).execute()


def find_thread_context(to_email):
    """Look up any prior thread with this address and return the most recent
    inbound (their) message text, so drafts can reference real history.
    Returns '' if there's nothing useful."""
    q = f"to:{to_email} OR from:{to_email}"
    res = service().users().threads().list(userId="me", q=q, maxResults=1).execute()
    threads = res.get("threads", [])
    if not threads:
        return ""
    thread = service().users().threads().get(
        userId="me", id=threads[0]["id"], format="full"
    ).execute()
    me = address().lower()
    inbound = ""
    for m in thread.get("messages", []):
        headers = {h["name"].lower(): h["value"] for h in m["payload"].get("headers", [])}
        if me not in headers.get("from", "").lower():
            inbound = _extract_text(m["payload"]).strip()
    return inbound


def create_draft(to, subject, body):
    """Create a Gmail DRAFT (never sends). Returns the draft id.
    Honors DRY_RUN by printing instead of writing."""
    if config.DRY_RUN:
        print("\n----- [DRY_RUN] would create draft -----")
        print(f"To: {to}\nSubject: {subject}\n{body}")
        print("----------------------------------------\n")
        return "dry-run-draft"

    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = _from_header()
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    draft = service().users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return draft["id"]
