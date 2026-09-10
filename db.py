"""SQLite persistence layer. One file, no server, easy to inspect with any
SQLite browser. All state the autonomous loop needs lives here."""
import sqlite3
from datetime import datetime, timezone, timedelta

import config

# Prospect lifecycle:
#   new         -> imported/discovered, no personalized line yet
#   queued      -> has a personalized line, ready for the opener
#   active      -> in the middle of the sequence (>=1 step sent, no reply)
#   completed   -> all steps sent, no reply
#   replied     -> they answered; needs classification / your attention
#   interested  -> classifier flagged a positive/question reply (in review queue)
#   not_interested / unsubscribed / bounced -> terminal, never contacted again
TERMINAL = ("not_interested", "unsubscribed", "bounced", "interested")


def _now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init():
    conn = connect()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS prospects (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id        TEXT,
            channel_name      TEXT,
            first_name        TEXT,
            email             TEXT UNIQUE,
            subscriber_count  INTEGER,
            latest_video      TEXT,
            personalized_line TEXT,
            profile_url       TEXT,
            platform          TEXT,
            language          TEXT,
            contact_method    TEXT,
            status            TEXT DEFAULT 'new',
            step              INTEGER DEFAULT 0,
            gmail_thread_id   TEXT,
            last_message_id   TEXT,
            references_chain  TEXT DEFAULT '',
            last_sent_at      TEXT,
            source            TEXT,
            created_at        TEXT,
            updated_at        TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id      INTEGER,
            direction        TEXT,          -- 'out' or 'in'
            step             INTEGER,
            subject          TEXT,
            body             TEXT,
            gmail_message_id TEXT,
            created_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS suppression (
            email      TEXT PRIMARY KEY,
            reason     TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS review_queue (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            prospect_id     INTEGER,
            classification  TEXT,
            snippet         TEXT,
            gmail_thread_id TEXT,
            resolved        INTEGER DEFAULT 0,
            created_at      TEXT
        );

        -- The "scanner": one row per channel per capture, so you can chart
        -- subscriber growth over time (delta between snapshots).
        CREATE TABLE IF NOT EXISTS channel_snapshots (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id       TEXT,
            channel_name     TEXT,
            subscriber_count INTEGER,
            view_count       INTEGER,
            video_count      INTEGER,
            latest_video     TEXT,
            captured_at      TEXT
        );
        """
    )
    for col, typ in [("profile_url", "TEXT"), ("platform", "TEXT"),
                     ("language", "TEXT"), ("contact_method", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE prospects ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


# --- prospects --------------------------------------------------------------
def upsert_prospect(channel_id=None, channel_name=None, first_name=None, email=None,
                    subscriber_count=None, latest_video=None, source="import",
                    status="new", profile_url=None, platform=None, language=None,
                    contact_method=None):
    """Insert a prospect. Skips silently if the email is already known or
    suppressed. Returns True if a new row was created."""
    if not email:
        return False
    email = email.strip().lower()
    conn = connect()
    try:
        if is_suppressed(email, conn):
            return False
        exists = conn.execute("SELECT 1 FROM prospects WHERE email=?", (email,)).fetchone()
        if exists:
            return False
        if not first_name:
            first_name = (channel_name or email.split("@")[0]).split()[0]
        conn.execute(
            """INSERT INTO prospects
               (channel_id, channel_name, first_name, email, subscriber_count,
                latest_video, source, status, profile_url, platform, language,
                contact_method, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (channel_id, channel_name, first_name, email, subscriber_count,
             latest_video, source, status, profile_url, platform, language,
             contact_method, _now(), _now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def prospects_by_status(status):
    conn = connect()
    rows = conn.execute("SELECT * FROM prospects WHERE status=?", (status,)).fetchall()
    conn.close()
    return rows


def prospects_needing_line():
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM prospects WHERE status='new' AND email IS NOT NULL"
    ).fetchall()
    conn.close()
    return rows


def active_prospects():
    """Everyone the reply-checker should watch: any thread we've sent on that
    isn't terminal — includes parked prospects we've re-touched."""
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM prospects WHERE status IN ('active','completed','parked') "
        "AND gmail_thread_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return rows


def parked_due(wait_days, force=False):
    """Parked prospects due for a long-loop re-touch. force=True returns all of
    them (used for the one-time re-enroll); otherwise only those whose last
    contact (or import, if never contacted by us) is older than wait_days."""
    conn = connect()
    if force:
        rows = conn.execute("SELECT * FROM prospects WHERE status='parked'").fetchall()
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=wait_days)).isoformat()
        rows = conn.execute(
            "SELECT * FROM prospects WHERE status='parked' "
            "AND COALESCE(last_sent_at, created_at) <= ?", (cutoff,)
        ).fetchall()
    conn.close()
    return rows


def record_retouch(prospect, subject, body, gmail_message_id, thread_id, message_id_header):
    """Log a re-touch send. Status stays 'parked' so the prospect keeps cycling
    through the long loop until they reply."""
    conn = connect()
    conn.execute(
        """UPDATE prospects SET gmail_thread_id=?, last_message_id=?,
           last_sent_at=?, updated_at=? WHERE id=?""",
        (thread_id, message_id_header, _now(), _now(), prospect["id"]),
    )
    conn.execute(
        """INSERT INTO messages (prospect_id, direction, step, subject, body,
           gmail_message_id, created_at) VALUES (?, 'out', NULL, ?, ?, ?, ?)""",
        (prospect["id"], subject, body, gmail_message_id, _now()),
    )
    conn.commit()
    conn.close()


def set_line(prospect_id, line):
    conn = connect()
    conn.execute(
        "UPDATE prospects SET personalized_line=?, status='queued', updated_at=? WHERE id=?",
        (line, _now(), prospect_id),
    )
    conn.commit()
    conn.close()


def set_status(prospect_id, status):
    conn = connect()
    conn.execute(
        "UPDATE prospects SET status=?, updated_at=? WHERE id=?", (status, _now(), prospect_id)
    )
    conn.commit()
    conn.close()


def record_send(prospect, step, subject, body, gmail_message_id, thread_id, message_id_header):
    conn = connect()
    refs = (prospect["references_chain"] or "").strip()
    refs = (refs + " " + message_id_header).strip()
    new_status = "active" if step < config.TOTAL_STEPS else "completed"
    conn.execute(
        """UPDATE prospects SET step=?, status=?, gmail_thread_id=?, last_message_id=?,
           references_chain=?, last_sent_at=?, updated_at=? WHERE id=?""",
        (step, new_status, thread_id, message_id_header, refs, _now(), _now(), prospect["id"]),
    )
    conn.execute(
        """INSERT INTO messages (prospect_id, direction, step, subject, body,
           gmail_message_id, created_at) VALUES (?, 'out', ?, ?, ?, ?, ?)""",
        (prospect["id"], step, subject, body, gmail_message_id, _now()),
    )
    conn.commit()
    conn.close()


def record_inbound(prospect_id, body, gmail_message_id):
    conn = connect()
    conn.execute(
        """INSERT INTO messages (prospect_id, direction, step, subject, body,
           gmail_message_id, created_at) VALUES (?, 'in', NULL, NULL, ?, ?, ?)""",
        (prospect_id, body, gmail_message_id, _now()),
    )
    conn.commit()
    conn.close()


def sends_today():
    conn = connect()
    today = datetime.now(timezone.utc).date().isoformat()
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE direction='out' AND substr(created_at,1,10)=?",
        (today,),
    ).fetchone()[0]
    conn.close()
    return n


# --- suppression ------------------------------------------------------------
def suppress(email, reason):
    email = email.strip().lower()
    conn = connect()
    conn.execute(
        "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?,?,?)",
        (email, reason, _now()),
    )
    conn.commit()
    conn.close()


def is_suppressed(email, conn=None):
    close = conn is None
    conn = conn or connect()
    row = conn.execute("SELECT 1 FROM suppression WHERE email=?", (email.strip().lower(),)).fetchone()
    if close:
        conn.close()
    return row is not None


# --- review queue -----------------------------------------------------------
def add_to_review(prospect_id, classification, snippet, thread_id):
    conn = connect()
    conn.execute(
        """INSERT INTO review_queue (prospect_id, classification, snippet,
           gmail_thread_id, created_at) VALUES (?,?,?,?,?)""",
        (prospect_id, classification, snippet, thread_id, _now()),
    )
    conn.commit()
    conn.close()


def unresolved_review():
    conn = connect()
    rows = conn.execute(
        """SELECT r.*, p.channel_name, p.email, p.subscriber_count
           FROM review_queue r JOIN prospects p ON p.id = r.prospect_id
           WHERE r.resolved=0 ORDER BY r.created_at DESC"""
    ).fetchall()
    conn.close()
    return rows


def counts_by_status():
    conn = connect()
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM prospects GROUP BY status ORDER BY n DESC"
    ).fetchall()
    conn.close()
    return rows


# --- enrichment + scanner ---------------------------------------------------
def all_emailable():
    """Every prospect with an email that isn't terminal/suppressed."""
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM prospects WHERE email IS NOT NULL "
        "AND status NOT IN ('unsubscribed','not_interested','bounced')"
    ).fetchall()
    conn.close()
    return rows


def set_channel_data(prospect_id, channel_id, subs, latest_video):
    conn = connect()
    conn.execute(
        "UPDATE prospects SET channel_id=?, subscriber_count=?, latest_video=?, updated_at=? WHERE id=?",
        (channel_id, subs, latest_video, _now(), prospect_id),
    )
    conn.commit()
    conn.close()


def record_snapshot(channel_id, channel_name, subs, views, videos, latest_video):
    conn = connect()
    conn.execute(
        """INSERT INTO channel_snapshots
           (channel_id, channel_name, subscriber_count, view_count, video_count,
            latest_video, captured_at) VALUES (?,?,?,?,?,?,?)""",
        (channel_id, channel_name, subs, views, videos, latest_video, _now()),
    )
    conn.commit()
    conn.close()


def channels_to_track():
    conn = connect()
    rows = conn.execute(
        "SELECT DISTINCT channel_id, channel_name FROM prospects WHERE channel_id IS NOT NULL"
    ).fetchall()
    conn.close()
    return rows


def growth(channel_id):
    """Return snapshots for one channel, oldest first, to compute deltas."""
    conn = connect()
    rows = conn.execute(
        "SELECT subscriber_count, captured_at FROM channel_snapshots "
        "WHERE channel_id=? ORDER BY captured_at", (channel_id,),
    ).fetchall()
    conn.close()
    return rows
