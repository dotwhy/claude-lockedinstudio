"""Tiny web API for adding prospects without touching CSVs or consoles.

POST /add    {"url": "https://youtube.com/@SomeCreator", "email": "creator@gmail.com"}
POST /add    {"email": "someone@gmail.com", "name": "Creator Name"}
GET  /stats  pipeline counts as JSON
GET  /health alive check

Runs alongside the scheduler on a separate thread.
"""
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

import config
import db
import import_sheet
import youtube_sourcing


def _json_response(handler, code, data):
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    handler.wfile.write(json.dumps(data).encode())


def _authed(handler):
    """Every route except /health requires the shared secret.

    These endpoints write into `prospects`, and anything in `prospects` gets
    cold-emailed from a personal Gmail account. An open endpoint here is a spam
    relay with your name and your sending reputation on it.

    /health stays open because Railway's healthcheck calls it and can't carry
    a header.
    """
    if not config.API_KEY:
        return False
    return handler.headers.get("X-API-Key") == config.API_KEY


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            _json_response(self, 200, {"ok": True})
        elif not _authed(self):
            _json_response(self, 401, {"error": "missing or invalid X-API-Key"})
        elif self.path == "/stats":
            db.init()
            counts = {r["status"]: r["n"] for r in db.counts_by_status()}
            review = len(db.unresolved_review())
            _json_response(self, 200, {
                "dry_run": config.DRY_RUN,
                "daily_cap": config.MAX_SENDS_PER_DAY,
                "sent_today": db.sends_today(),
                "pipeline": counts,
                "awaiting_review": review,
                # Background jobs return 202 and log to stdout, which is
                # invisible to anything but the Railway console. Without these
                # there is no way to tell from outside whether enrich or
                # personalize actually did anything.
                # Placeholder config already reached two real creators. If
                # this list is non-empty, every send path refuses.
                "config_problems": __import__("copy_audit").audit(),
                "prep": db.prep_progress(),
                "discovery": db.discovery_progress(),
            })
        elif self.path.startswith("/preview"):
            self._handle_preview()
        elif self.path.startswith("/who"):
            self._handle_who()
        else:
            _json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        if not _authed(self):
            _json_response(self, 401, {"error": "missing or invalid X-API-Key"})
            return
        if self.path == "/import":
            return self._handle_import()
        if self.path.startswith("/run/"):
            return self._handle_run(self.path[len("/run/"):])
        if self.path == "/remove":
            return self._handle_remove()
        if self.path == "/status":
            return self._handle_status()
        if self.path != "/add":
            _json_response(self, 404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        email = (body.get("email") or "").strip()
        name = (body.get("name") or "").strip()
        url = (body.get("url") or "").strip()

        if not email and not url:
            _json_response(self, 400, {"error": "need at least 'email' or 'url'"})
            return

        channel_name = name
        subs = None
        latest = None

        if url and "youtu" in url.lower():
            try:
                info = youtube_sourcing._find_channel(
                    url.split("@")[-1].split("/")[-1] if "@" in url else url
                )
                if info:
                    channel_name = channel_name or info["channel_name"]
                    subs = info["subs"]
                    latest = info["latest_video"]
                    if not email:
                        email = youtube_sourcing.extract_email(
                            info["channel_id"],
                            "",
                        )
            except Exception:
                pass

        if not email:
            _json_response(self, 422, {
                "error": "no email found or provided",
                "channel": channel_name,
                "hint": "pass 'email' explicitly if YouTube didn't have one",
            })
            return

        db.init()
        created = db.upsert_prospect(
            channel_name=channel_name,
            email=email,
            subscriber_count=subs,
            latest_video=latest,
            profile_url=url,
            source="api",
        )
        _json_response(self, 201 if created else 200, {
            "created": created,
            "email": email,
            "channel": channel_name,
            "subs": subs,
        })


    def _handle_preview(self):
        """GET /preview?n=3 — render the emails that WOULD be sent next.

        Read-only. The generated lines otherwise exist only in the background
        job's stdout, which means the one review gate that matters — reading
        the copy before it goes to real creators — requires scrolling a log
        console. This puts the finished email where it can actually be read.
        """
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(self.path).query)
        try:
            limit = max(1, min(int(params.get("n", ["3"])[0]), 20))
        except ValueError:
            limit = 3

        db.init()
        import config as cfg
        rows = db.prospects_by_status("parked") + db.prospects_by_status("queued")
        rows = [r for r in rows if r["personalized_line"]][:limit]

        previews = []
        for row in rows:
            if row["status"] == "parked":
                subject, body = cfg.retouch(row["first_name"], row["channel_name"],
                                            row["personalized_line"] or "")
                kind = "re-touch (contacted before)"
            else:
                subject, body = cfg.opener(row["first_name"], row["channel_name"],
                                           row["personalized_line"] or "")
                kind = "opener (first contact)"
            previews.append({
                "channel": row["channel_name"],
                "to": row["email"],
                "kind": kind,
                "line": row["personalized_line"],
                "subject": subject,
                "body": body,
            })
        _json_response(self, 200, {"dry_run": cfg.DRY_RUN, "previews": previews})

    def _handle_who(self):
        """GET /who?q=nuno — who matches, and are we about to email them."""
        from urllib.parse import parse_qs, urlparse
        term = parse_qs(urlparse(self.path).query).get("q", [""])[0]
        if not term.strip():
            _json_response(self, 400, {"error": "pass ?q=<name or email>"})
            return
        db.init()
        prospects, candidates = db.find_people(term)
        _json_response(self, 200, {
            "query": term,
            "prospects": [dict(r) for r in prospects],
            "candidates": [dict(r) for r in candidates],
        })

    def _handle_remove(self):
        """POST /remove {"q": "nuno"} — take someone out permanently.

        Suppresses rather than deletes, so discovery can't quietly re-add them
        the next time it finds their channel.
        """
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        term = (body.get("q") or body.get("channel") or body.get("email") or "").strip()
        if not term:
            _json_response(self, 400, {"error": "pass 'q' (name or email)"})
            return
        db.init()
        removed = db.remove_person(term, body.get("reason") or "manually removed")
        _json_response(self, 200, {"query": term, "removed": removed,
                                   "count": len(removed)})

    def _handle_status(self):
        """POST /status {"q": "nuno", "status": "hold"} — move someone by hand."""
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        term = (body.get("q") or body.get("channel") or body.get("email") or "").strip()
        status = (body.get("status") or "").strip()
        if not term or not status:
            _json_response(self, 400, {"error": "pass 'q' and 'status'",
                                       "statuses": list(db.SETTABLE_STATUSES)})
            return
        db.init()
        try:
            changed = db.set_person_status(term, status)
        except ValueError as exc:
            _json_response(self, 400, {"error": str(exc),
                                       "statuses": list(db.SETTABLE_STATUSES)})
            return
        _json_response(self, 200, {"query": term, "changed": changed,
                                   "count": len(changed)})

    def _handle_run(self, job):
        """Trigger a scheduled job on demand: POST /run/<job>.

        Railway has no shell, so without this there is no way to run a one-off
        against the production database on the volume — you'd be waiting for a
        cron slot to do something you want done now.

        Deliberately a whitelist, not a dispatch on arbitrary names: this route
        is authenticated, but a job runner that can call anything is one
        mistake away from being a remote shell.

        Jobs run on a thread and the response returns immediately, because
        discovery can take minutes and an HTTP client will time out first.
        """
        runner = JOBS.get(job)
        if runner is None:
            _json_response(self, 404, {
                "error": f"unknown job '{job}'", "available": sorted(JOBS)
            })
            return
        threading.Thread(target=_run_safely, args=(job, runner), daemon=True).start()
        _json_response(self, 202, {
            "started": job,
            "note": "running in the background — check the Railway logs for the result",
        })

    def _handle_import(self):
        """Accept CSV text in the request body and run import_sheet.load() on it."""
        import tempfile, os
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            _json_response(self, 400, {"error": "send CSV text as the request body"})
            return
        csv_data = self.rfile.read(length)
        db.init()
        tmp = tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False)
        tmp.write(csv_data)
        tmp.close()
        try:
            counts = import_sheet.load(tmp.name)
            _json_response(self, 200, {"imported": counts})
        except Exception as e:
            _json_response(self, 500, {"error": str(e)})
        finally:
            os.unlink(tmp.name)


def _job_retouch_now():
    """Force the long-loop re-touch for every parked prospect.

    Separate from the scheduled 'retouch' job, which respects the wait window.
    This is the deliberate "contact them now" button — one way, so it is its
    own named job rather than a flag on the normal one.
    """
    import engine
    return engine.retouch_due(force=True)


# Whitelist of jobs the API may trigger. Imports are inside the lambdas so a
# missing optional dependency can't stop the API from starting.
JOBS = {
    "discover": lambda: __import__("youtube_sourcing").discover(),
    "enrich": lambda: __import__("youtube_sourcing").enrich_prospects(),
    "snapshot": lambda: __import__("youtube_sourcing").snapshot_all(),
    "promote": lambda: __import__("youtube_sourcing").promote_growing(),
    "personalize": lambda: __import__("personalize").run(),
    "revalidate": lambda: __import__("personalize").revalidate_stored(),
    "audit": lambda: __import__("copy_audit").report(),
    "send": lambda: __import__("engine").send_due(),
    "replies": lambda: __import__("engine").process_replies(),
    "candidates": lambda: __import__("engine").send_candidate_digest(),
    "digest-replies": lambda: __import__("engine").process_digest_replies(),
    "retouch-now": _job_retouch_now,
}


def _run_safely(name, runner):
    log = logging.getLogger("api")
    log.info(f"[run/{name}] started")
    try:
        db.init()
        result = runner()
        log.info(f"[run/{name}] done: {result}")
    except Exception:
        log.exception(f"[run/{name}] FAILED")


def start(port=None):
    """Start the intake API. Returns the port, or None if it stayed down.

    Fails closed on the ENDPOINT but open on the WORKER: with no API_KEY set we
    refuse to serve rather than expose an unauthenticated write route, but we
    must not raise, because start() runs before the scheduler's blocking loop.
    Raising here would take every outreach job down over a missing env var —
    trading an exposed endpoint for total silence, which is worse.
    """
    if not config.API_KEY:
        logging.getLogger("api").error(
            "API_KEY is not set — intake API will NOT start. "
            "The scheduler continues; set API_KEY in the environment to enable "
            "/stats, /add and /import."
        )
        return None
    port = port or config.API_PORT
    server = HTTPServer(("0.0.0.0", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return port
