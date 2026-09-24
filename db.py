"""SQLite persistence layer. One file, no server, easy to inspect with any
SQLite browser. All state the autonomous loop needs lives here."""
import sqlite3
from datetime import datetime, timezone, timedelta

import config
import emails

# Prospect lifecycle:
#   new         -> imported/discovered, no personalized line yet
#   queued      -> has a personalized line, ready for the opener
#   active      -> in the middle of the sequence (>=1 step sent, no reply)
#   completed   -> all steps sent, no reply
#   replied     -> they answered; needs classification / your attention
#   interested  -> classifier flagged a positive/question reply (in review queue)
#   not_interested / unsubscribed / bounced -> terminal, never contacted again
TERMINAL = ("not_interested", "unsubscribed", "bounced", "interested")

# Give up writing a line after this many tries. Some channels simply have no
# usable video data, and a line is optional — the email reads fine without it.
MAX_LINE_ATTEMPTS = 2


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

        -- Channels discovery found but couldn't get an email for. They are
        -- tracked and snapshotted like prospects, but can't be emailed yet.
        --
        -- This replaces the old candidates.csv, which lived outside /data and
        -- so was wiped by every Railway redeploy, and which appended blindly
        -- (a channel matching five search terms landed five times).
        --
        --   discovered ──► snapshotted weekly ──► growth_signal computed
        --        │                                       │
        --        │  email found (scrape or your reply)   │ growing?
        --        ▼                                       ▼
        --   promoted_at set ──────────────────────► prospects
        CREATE TABLE IF NOT EXISTS candidates (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id       TEXT UNIQUE,   -- dedup across search terms
            channel_name     TEXT,
            subscriber_count INTEGER,
            latest_video     TEXT,
            profile_url      TEXT,
            email            TEXT,          -- null until scraped or you supply it
            growth_signal    TEXT,          -- denormalized at snapshot time
            growth_pct       REAL,
            fast_track       INTEGER DEFAULT 0,  -- big + active: skip growth gate
            asked_at         TEXT,          -- last time a digest asked you for it
            promoted_at      TEXT,          -- set when it becomes a prospect
            created_at       TEXT,
            updated_at       TEXT
        );

        -- Small key/value store for things the worker has to remember across
        -- restarts but that don't deserve a table — currently just the Gmail
        -- thread id of the last digest, so the reply reader knows where to look.
        CREATE TABLE IF NOT EXISTS meta (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT
        );

        -- One row per scheduled job run. Without this there is no way to tell
        -- a job that ran and found nothing from a job that never fired — which
        -- is exactly how a missed weekly digest went unnoticed for a week.
        CREATE TABLE IF NOT EXISTS job_runs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job        TEXT,
            outcome    TEXT,      -- 'ok' | 'failed'
            detail     TEXT,      -- return value, or the exception
            duration_s REAL,
            ran_at     TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs (job, ran_at);

        CREATE INDEX IF NOT EXISTS idx_candidates_promotable
            ON candidates (growth_signal, promoted_at);
        CREATE INDEX IF NOT EXISTS idx_snapshots_channel
            ON channel_snapshots (channel_id, captured_at);
        """
    )
    for col, typ in [("profile_url", "TEXT"), ("platform", "TEXT"),
                     ("language", "TEXT"), ("contact_method", "TEXT"),
                     # Growth is stored on prospects too, not just candidates —
                     # otherwise there's no way to ask "which of the channels
                     # I'm already talking to is heating up", even though the
                     # snapshots to answer it are already being captured.
                     ("growth_signal", "TEXT"), ("growth_pct", "REAL"),
                     # Signal for the opener line. A bare video title only lets
                     # the model say "I saw your video"; knowing that video did
                     # 3x the channel average lets it say something the creator
                     # themselves would find true and specific.
                     ("latest_video_views", "INTEGER"), ("avg_views", "INTEGER"),
                     # Some channels have nothing concrete to write a line
                     # from. Counting attempts stops the job re-asking Claude
                     # about them on every single run, forever.
                     ("line_attempts", "INTEGER")]:
        try:
            conn.execute(f"ALTER TABLE prospects ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("ALTER TABLE candidates ADD COLUMN fast_track INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Seed the August hard-bounces into suppression. They used to be a constant
    # in import_sheet.py, which meant discovery couldn't see them and would
    # happily re-add an address that had already bounced. suppress() is checked
    # by upsert_prospect() on every intake path, so this covers all of them at
    # once — including any added later. Idempotent: INSERT OR IGNORE on a PK.
    for address in emails.AUGUST_BOUNCES:
        conn.execute(
            "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?,?,?)",
            (address, "hard bounce (August 2026 run)", _now()),
        )
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
    """Everyone who should get a personalized line written for them.

    Covers 'new' (heading for the opener) AND 'parked' (heading for the
    long-loop re-touch). Parked prospects imported from a sheet arrive with no
    line at all, so without this they'd be re-contacted with generic copy —
    which is the exact opposite of why the personalization step exists.

    'hold' is excluded on purpose: those are mid-conversation with you by hand,
    and the machine must not prepare anything to send them.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT * FROM prospects WHERE email IS NOT NULL "
        "AND (status='new' OR (status='parked' AND "
        "     (personalized_line IS NULL OR personalized_line=''))) "
        "AND COALESCE(line_attempts, 0) < ?",
        (MAX_LINE_ATTEMPTS,),
    ).fetchall()
    conn.close()
    return rows


def record_line_attempt(prospect_id):
    """Count a personalization attempt, whether or not it produced a line."""
    conn = connect()
    conn.execute(
        "UPDATE prospects SET line_attempts=COALESCE(line_attempts,0)+1, "
        "updated_at=? WHERE id=?", (_now(), prospect_id),
    )
    conn.commit()
    conn.close()


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
    """Attach a line, and move 'new' into the opener queue.

    ONLY 'new' changes status. A parked prospect getting a line must stay
    parked: flipping it to 'queued' would put someone who was already contacted
    in August into the first-contact sequence, and they'd receive an opener
    that greets them as a stranger.
    """
    conn = connect()
    conn.execute(
        "UPDATE prospects SET personalized_line=?, updated_at=?, "
        "status=CASE WHEN status='new' THEN 'queued' ELSE status END "
        "WHERE id=?",
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
    """Every channel the snapshot job should capture — prospects AND candidates.

    Candidates are included because growth is exactly what decides whether a
    candidate is worth chasing an email for. Tracking only prospects would mean
    the weekly digest could never say which email-less channels are rising.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT DISTINCT channel_id, channel_name FROM prospects "
        "WHERE channel_id IS NOT NULL "
        "UNION "
        "SELECT DISTINCT channel_id, channel_name FROM candidates "
        "WHERE channel_id IS NOT NULL"
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


def all_snapshots():
    """Every snapshot, for every channel, in ONE query.

    The weekly job groups these in Python (growth.by_channel) instead of asking
    per channel. At 174 channels that is 1 round trip instead of 175, and the
    gap widens as discovery adds channels.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT channel_id, subscriber_count, captured_at FROM channel_snapshots "
        "ORDER BY channel_id, captured_at"
    ).fetchall()
    conn.close()
    return rows


# --- candidates -------------------------------------------------------------
def upsert_candidate(channel_id, channel_name=None, subscriber_count=None,
                     latest_video=None, profile_url=None, email=None,
                     fast_track=False):
    """Record an email-less channel found by discovery.

    Returns True if a new row was created. Dedups on channel_id, so a channel
    matching five search terms lands once. If the row already exists, refreshes
    the volatile fields (name and sub count change; the channel doesn't).
    """
    if not channel_id:
        return False
    conn = connect()
    try:
        existing = conn.execute(
            "SELECT id FROM candidates WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if existing:
            # fast_track is re-evaluated on every discovery pass: a channel
            # that crossed 500k, or went quiet, should change status rather
            # than keep whatever it was first seen as.
            conn.execute(
                """UPDATE candidates SET channel_name=COALESCE(?, channel_name),
                   subscriber_count=COALESCE(?, subscriber_count),
                   latest_video=COALESCE(?, latest_video),
                   email=COALESCE(?, email), fast_track=?, updated_at=?
                   WHERE channel_id=?""",
                (channel_name, subscriber_count, latest_video, email,
                 1 if fast_track else 0, _now(), channel_id),
            )
            conn.commit()
            return False
        conn.execute(
            """INSERT INTO candidates
               (channel_id, channel_name, subscriber_count, latest_video,
                profile_url, email, fast_track, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (channel_id, channel_name, subscriber_count, latest_video,
             profile_url, email, 1 if fast_track else 0, _now(), _now()),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_candidate_growth(channel_id, signal, pct):
    """Store the growth label for one candidate."""
    conn = connect()
    conn.execute(
        "UPDATE candidates SET growth_signal=?, growth_pct=?, updated_at=? "
        "WHERE channel_id=?",
        (signal, pct, _now(), channel_id),
    )
    conn.commit()
    conn.close()


def apply_growth(labels):
    """Write growth labels for many channels in two bulk statements.

    `labels` is {channel_id: (signal, pct)}.

    Growth lands on BOTH tables: candidates (where it gates promotion) and
    prospects (where it answers "which channel I'm already talking to is
    heating up"). Both are written with executemany rather than a query per
    channel — the earlier version looped and issued one UPDATE per tracked
    channel, most of which matched nothing.

    Returns (candidates_updated, prospects_updated) — actual row counts, so the
    log can't overstate what happened.
    """
    if not labels:
        return 0, 0
    rows = [(signal, pct, _now(), channel_id)
            for channel_id, (signal, pct) in labels.items()]
    conn = connect()
    candidates = conn.executemany(
        "UPDATE candidates SET growth_signal=?, growth_pct=?, updated_at=? "
        "WHERE channel_id=?", rows,
    ).rowcount
    prospects = conn.executemany(
        "UPDATE prospects SET growth_signal=?, growth_pct=?, updated_at=? "
        "WHERE channel_id=?", rows,
    ).rowcount
    conn.commit()
    conn.close()
    return max(candidates, 0), max(prospects, 0)


def set_video_stats(channel_id, latest_video_views, avg_views):
    """Store the numbers the opener line reasons about.

    Kept as two plain integers rather than a blob of recent videos: the only
    question the line needs answered is "did their latest upload beat their
    usual", and two numbers answer it without a schema to version.
    """
    conn = connect()
    conn.execute(
        "UPDATE prospects SET latest_video_views=?, avg_views=?, updated_at=? "
        "WHERE channel_id=?",
        (latest_video_views, avg_views, _now(), channel_id),
    )
    conn.commit()
    conn.close()


def set_candidate_email(channel_id, email):
    """Attach an email to a candidate — from a later scrape or your digest reply."""
    conn = connect()
    conn.execute(
        "UPDATE candidates SET email=?, updated_at=? WHERE channel_id=?",
        (email.strip().lower() if email else None, _now(), channel_id),
    )
    conn.commit()
    conn.close()


# A candidate is worth acting on if it is rising OR if it is already big and
# active. Fast-tracked rows skip the growth gate entirely, which is the whole
# point: an established channel shouldn't wait two snapshot cycles to be
# contacted just so we can confirm something we already know.
_ACTIONABLE = "(growth_signal='growing' OR fast_track=1)"


def candidates_to_promote():
    """Actionable candidates that now have an email and haven't been promoted."""
    conn = connect()
    rows = conn.execute(
        f"SELECT * FROM candidates WHERE {_ACTIONABLE} "
        "AND email IS NOT NULL AND promoted_at IS NULL"
    ).fetchall()
    conn.close()
    return rows


def candidates_waiting_count(reask_after_days=30):
    """How many actionable channels still have no address, asked or not."""
    conn = connect()
    n = conn.execute(
        f"SELECT COUNT(*) FROM candidates WHERE {_ACTIONABLE} "
        "AND email IS NULL AND promoted_at IS NULL"
    ).fetchone()[0]
    conn.close()
    return n


def candidates_needing_email(reask_after_days=30, limit=None):
    """Actionable channels with no email, that we haven't recently asked about.

    The asked_at gate is what stops the weekly digest from listing the same 12
    channels every Monday forever. A channel you never resolve comes back after
    the re-ask window, not the next week.

    Fast-tracked channels sort first: they're the biggest and they're not
    waiting on a growth signal, so they're the ones worth your time chasing.

    `limit` keeps the daily ask to a size you can actually act on. Sourcing
    addresses is rate-limited on your side, so a list of 45 is functionally the
    same as a list of zero — you work through a handful and the rest scroll
    away. Small batch, every day, beats everything at once.
    """
    conn = connect()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=reask_after_days)).isoformat()
    sql = (f"SELECT * FROM candidates WHERE {_ACTIONABLE} "
           "AND email IS NULL AND promoted_at IS NULL "
           "AND (asked_at IS NULL OR asked_at <= ?) "
           # Never-asked first, then longest-waiting, then biggest.
           "ORDER BY (asked_at IS NOT NULL), fast_track DESC, subscriber_count DESC")
    params = [cutoff]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def mark_candidates_asked(channel_ids):
    """Stamp asked_at after a digest goes out, so we don't re-ask next week."""
    if not channel_ids:
        return 0
    conn = connect()
    conn.executemany(
        "UPDATE candidates SET asked_at=?, updated_at=? WHERE channel_id=?",
        [(_now(), _now(), cid) for cid in channel_ids],
    )
    conn.commit()
    conn.close()
    return len(channel_ids)


def promote_candidate(candidate):
    """Move a candidate into prospects. Returns True if a prospect was created.

    Marks promoted_at either way: if upsert_prospect refuses (already a
    prospect, or suppressed/bounced), the candidate should still stop appearing
    in the promote queue, or we retry it forever.
    """
    created = upsert_prospect(
        channel_id=candidate["channel_id"],
        channel_name=candidate["channel_name"],
        email=candidate["email"],
        subscriber_count=candidate["subscriber_count"],
        latest_video=candidate["latest_video"],
        profile_url=candidate["profile_url"] if "profile_url" in candidate.keys() else None,
        source="discovery",
    )
    conn = connect()
    conn.execute(
        "UPDATE candidates SET promoted_at=?, updated_at=? WHERE channel_id=?",
        (_now(), _now(), candidate["channel_id"]),
    )
    conn.commit()
    conn.close()
    return created


def record_job_run(job, outcome, detail=None, duration_s=None):
    """Log that a scheduled job ran, and how it went."""
    conn = connect()
    conn.execute(
        "INSERT INTO job_runs (job, outcome, detail, duration_s, ran_at) "
        "VALUES (?,?,?,?,?)",
        (job, outcome, (str(detail)[:500] if detail is not None else None),
         duration_s, _now()),
    )
    # Keep the table small: a year of runs is plenty to spot a pattern.
    conn.execute(
        "DELETE FROM job_runs WHERE id NOT IN "
        "(SELECT id FROM job_runs ORDER BY ran_at DESC LIMIT 2000)"
    )
    conn.commit()
    conn.close()


def job_history():
    """Last run of every job that has ever run, newest first.

    Answers the question that cost a week: did this job fire at all?
    """
    conn = connect()
    rows = conn.execute(
        """SELECT job, outcome, detail, duration_s, ran_at FROM job_runs
           WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job)
           ORDER BY ran_at DESC"""
    ).fetchall()
    conn.close()
    out = []
    now = datetime.now(timezone.utc)
    for row in rows:
        try:
            age_h = (now - datetime.fromisoformat(row["ran_at"])).total_seconds() / 3600
        except ValueError:
            age_h = None
        out.append({
            "job": row["job"],
            "outcome": row["outcome"],
            "detail": row["detail"],
            "last_run": row["ran_at"],
            "hours_ago": round(age_h, 1) if age_h is not None else None,
        })
    return out


def recent_failures(limit=10):
    conn = connect()
    rows = conn.execute(
        "SELECT job, detail, ran_at FROM job_runs WHERE outcome='failed' "
        "ORDER BY ran_at DESC LIMIT ?", (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def prep_progress():
    """How far the sheet-imported prospects are through preparation.

    enrich fills channel_id; personalize fills the line. Both run in the
    background and log to stdout, so this is the only way to check them from
    outside the container.
    """
    conn = connect()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN channel_id IS NOT NULL THEN 1 ELSE 0 END) AS enriched,
                  SUM(CASE WHEN personalized_line IS NOT NULL
                            AND personalized_line != '' THEN 1 ELSE 0 END) AS personalized,
                  SUM(CASE WHEN platform='twitch' THEN 1 ELSE 0 END) AS twitch
             FROM prospects WHERE status IN ('new','queued','parked')"""
    ).fetchone()
    conn.close()
    return {
        "contactable": row["total"] or 0,
        "enriched": row["enriched"] or 0,
        "personalized": row["personalized"] or 0,
        # Twitch rows have no YouTube channel to enrich from, so they cap what
        # "enriched" can ever reach. Surfaced so a shortfall isn't mistaken
        # for a failure.
        "twitch_cannot_enrich": row["twitch"] or 0,
    }


def discovery_progress():
    """Candidate pipeline: found, scored, waiting on an address from you."""
    conn = connect()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN growth_signal IS NOT NULL THEN 1 ELSE 0 END) AS scored,
                  SUM(CASE WHEN growth_signal='growing' THEN 1 ELSE 0 END) AS growing,
                  SUM(CASE WHEN fast_track=1 THEN 1 ELSE 0 END) AS fast_track,
                  SUM(CASE WHEN promoted_at IS NOT NULL THEN 1 ELSE 0 END) AS promoted
             FROM candidates"""
    ).fetchone()
    snapshots = conn.execute(
        "SELECT COUNT(DISTINCT substr(captured_at,1,10)) AS days FROM channel_snapshots"
    ).fetchone()
    conn.close()
    return {
        "candidates": row["total"] or 0,
        "scored": row["scored"] or 0,
        "growing": row["growing"] or 0,
        "fast_track": row["fast_track"] or 0,
        "promoted": row["promoted"] or 0,
        # Growth needs two snapshots on different days before it can say
        # anything at all.
        "snapshot_days": snapshots["days"] or 0,
    }


def find_people(term):
    """Search prospects AND candidates by channel name or email.

    Used to answer "who is this and are we about to email them" before pulling
    someone out of the pipeline.
    """
    like = f"%{(term or '').strip().lower()}%"
    conn = connect()
    prospects = conn.execute(
        "SELECT id, channel_name, email, status, step, platform, profile_url "
        "FROM prospects WHERE lower(channel_name) LIKE ? OR lower(email) LIKE ?",
        (like, like),
    ).fetchall()
    candidates = conn.execute(
        "SELECT id, channel_name, email, growth_signal, fast_track, profile_url "
        "FROM candidates WHERE lower(channel_name) LIKE ? OR lower(COALESCE(email,'')) LIKE ?",
        (like, like),
    ).fetchall()
    conn.close()
    return prospects, candidates


def remove_person(term, reason="manually removed"):
    """Take someone out of the pipeline permanently.

    Suppression is the mechanism rather than deletion: `upsert_prospect` checks
    the suppression table on every intake path, so a removed creator can't be
    silently re-added next time discovery finds their channel. Deleting the row
    would let them back in on the next run.

    Matches on channel name or email, across prospects and candidates.
    """
    prospects, candidates = find_people(term)
    conn = connect()
    removed = []

    for row in prospects:
        if row["email"]:
            conn.execute(
                "INSERT OR IGNORE INTO suppression (email, reason, created_at) "
                "VALUES (?,?,?)", (row["email"], reason, _now()),
            )
        conn.execute(
            "UPDATE prospects SET status='unsubscribed', updated_at=? WHERE id=?",
            (_now(), row["id"]),
        )
        removed.append({"kind": "prospect", "channel": row["channel_name"],
                        "email": row["email"], "was": row["status"]})

    for row in candidates:
        if row["email"]:
            conn.execute(
                "INSERT OR IGNORE INTO suppression (email, reason, created_at) "
                "VALUES (?,?,?)", (row["email"], reason, _now()),
            )
        # promoted_at stops it surfacing in the digest or promote queue again.
        conn.execute(
            "UPDATE candidates SET promoted_at=?, updated_at=? WHERE id=?",
            (_now(), _now(), row["id"]),
        )
        removed.append({"kind": "candidate", "channel": row["channel_name"],
                        "email": row["email"], "was": "candidate"})

    conn.commit()
    conn.close()
    return removed


# Statuses you can move someone to by hand. 'hold' is the useful one: the
# machine never emails a hold, but you still can — it means "mine to handle",
# not "never contact again".
SETTABLE_STATUSES = ("new", "parked", "hold", "queued", "unsubscribed",
                     "not_interested", "bounced")


def set_person_status(term, status):
    """Move someone to a status by name or email.

    Clearing suppression matters here. `remove_person` suppresses, and
    suppression is checked on every intake path — so moving someone back to a
    non-terminal status while a suppression row lingers would leave them
    silently blocked, looking present in the pipeline but unable to ever be
    contacted or re-added.
    """
    if status not in SETTABLE_STATUSES:
        raise ValueError(f"unknown status '{status}'")
    prospects, _ = find_people(term)
    if not prospects:
        return []

    terminal = status in TERMINAL or status == "unsubscribed"
    conn = connect()
    changed = []
    for row in prospects:
        conn.execute("UPDATE prospects SET status=?, updated_at=? WHERE id=?",
                     (status, _now(), row["id"]))
        if not terminal and row["email"]:
            conn.execute("DELETE FROM suppression WHERE email=?", (row["email"],))
        changed.append({"channel": row["channel_name"], "email": row["email"],
                        "was": row["status"], "now": status,
                        "suppression_cleared": bool(not terminal and row["email"])})
    conn.commit()
    conn.close()
    return changed


def set_meta(key, value):
    conn = connect()
    conn.execute(
        "INSERT INTO meta (key, value, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value, _now()),
    )
    conn.commit()
    conn.close()


def get_meta(key):
    conn = connect()
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def record_digest_thread(thread_id, message_id=None):
    """Remember where the last digest went, and which message it was.

    The message id matters because the digest thread is self-to-self: the only
    way to tell your reply from our original is to exclude ours by id.
    """
    if thread_id:
        set_meta("digest_thread_id", thread_id)
    if message_id:
        seen = [i for i in (get_meta("digest_message_ids") or "").split(",") if i]
        seen.append(message_id)
        set_meta("digest_message_ids", ",".join(seen[-20:]))


def latest_digest_thread():
    return get_meta("digest_thread_id")


def digest_message_ids():
    """Messages WE sent in the digest thread; anything else is your reply."""
    return [i for i in (get_meta("digest_message_ids") or "").split(",") if i]


def candidate_by_name(name):
    """Find an unpromoted candidate by channel name.

    Used by the digest reply parser, where you retype a channel name from the
    digest and it has to match a row. Exact match is tried first; failing that,
    a unique partial match, because channel names are full of emoji, spacing
    and casing that nobody retypes precisely — and a near-miss silently
    dropping an address you sourced by hand is the worst outcome here.

    Returns None when nothing matches, or when a partial match is ambiguous —
    guessing between two channels would attach your address to the wrong one.
    """
    if not name or not name.strip():
        return None
    cleaned = name.strip()
    conn = connect()
    try:
        exact = conn.execute(
            "SELECT * FROM candidates WHERE lower(channel_name)=lower(?) "
            "AND promoted_at IS NULL", (cleaned,),
        ).fetchone()
        if exact:
            return exact
        partial = conn.execute(
            "SELECT * FROM candidates WHERE promoted_at IS NULL "
            "AND (lower(channel_name) LIKE lower(?) OR lower(?) LIKE "
            "     '%' || lower(channel_name) || '%')",
            (f"%{cleaned}%", cleaned),
        ).fetchall()
        return partial[0] if len(partial) == 1 else None
    finally:
        conn.close()
