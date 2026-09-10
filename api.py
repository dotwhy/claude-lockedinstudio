"""Tiny web API for adding prospects without touching CSVs or consoles.

POST /add    {"url": "https://youtube.com/@SomeCreator", "email": "creator@gmail.com"}
POST /add    {"email": "someone@gmail.com", "name": "Creator Name"}
GET  /stats  pipeline counts as JSON
GET  /health alive check

Runs alongside the scheduler on a separate thread.
"""
import json
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            _json_response(self, 200, {"ok": True})
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
            })
        else:
            _json_response(self, 404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/import":
            return self._handle_import()
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


def start(port=None):
    port = port or int(config.__dict__.get("API_PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return port
