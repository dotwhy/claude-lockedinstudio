# Deploying on Railway

Two things about Railway will silently break this if you skip them. Read these first.

## Gotcha 1 — the filesystem is ephemeral. Use a Volume.
Every deploy (and every native cron run) starts a **fresh container**. Your
SQLite database (`outreach.db`), your Gmail `token.json`, and `candidates.csv`
all live on disk — so without a persistent Volume they reset on every run. That
means the tool would forget who's been contacted and **re-draft / re-send
everyone**.

Fix: add a Railway **Volume**, mount it at `/data`, and point everything there:
```
DB_PATH=/data/outreach.db
GMAIL_TOKEN_FILE=/data/token.json
GMAIL_CREDENTIALS_FILE=/data/credentials.json
```

## Gotcha 2 — OAuth can't run on Railway. Authorize locally first.
Gmail's consent flow opens a browser (`run_local_server`). Railway has no
browser, so it can't authorize there. Do it once on your laptop:
```
python run.py stats     # triggers the one-time Gmail login locally
```
This writes `token.json`. Then upload `credentials.json` and `token.json` into
the Railway Volume (Railway dashboard → Volume → upload), so the worker starts
already authorized. The token auto-refreshes after that.

---

## Deploy (recommended: one worker + internal scheduler)
1. Push this folder to a GitHub repo, create a Railway project from it.
2. Add a **Volume** mounted at `/data`.
3. Set env vars (from `.env.example`) in Railway, including the `/data` paths above
   and `DRY_RUN=false` when you're ready to go live.
4. Railway reads `railway.json` and runs `python scheduler.py` — one always-on
   service that fires every job on its schedule (see `scheduler.py`).

The schedule (UTC — edit in `scheduler.py` for your timezone):
| Job | When | What |
|-----|------|------|
| tick | Mon–Fri 09/13/17 | personalize → send due steps → process replies |
| replies | Mon–Fri hourly 10–18 | fast reply flagging |
| content | daily 08:00 | emails you the day's posts to review |
| digest | Mon–Fri 18:30 | emails you replies needing attention |
| enrich | Mon 06:30 | fill channel data for new prospects |
| snapshot | Mon 07:00 | record subs/views for growth tracking |

`drafts` is intentionally **not** scheduled — run `python run.py drafts` yourself
when you want a fresh batch to review and send.

## Alternative: native Railway cron (serverless, cheaper)
If you'd rather not run an always-on worker, skip `scheduler.py` and instead
create one Railway **cron service** per job (same repo, override the start
command + set a cron schedule in the service settings):
```
python run.py tick        cron: 0 9,13,17 * * 1-5
python run.py content      cron: 0 8 * * *
python run.py digest       cron: 30 18 * * 1-5
python run.py snapshot     cron: 0 7 * * 1
```
Same Volume + OAuth rules apply. The worker approach is simpler to manage; native
cron is cheaper because containers only spin up when a job runs.
