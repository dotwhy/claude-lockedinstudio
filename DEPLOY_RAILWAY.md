# Deploying on Railway

Two things about Railway will silently break this if you skip them. Read these first.

> **Already deployed and just want the update steps?** Jump to
> [Rollout runbook](#rollout-runbook) at the bottom.

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

---

# Rollout runbook

Steps in order. Anything that sends email is called out before you run it.

Two variables used throughout — set them in your terminal once:

```bash
export URL="https://YOUR-APP.up.railway.app"   # from step 2
export KEY="YOUR_API_KEY"                      # from step 1
```

---

## Step 1 — Generate an API key

```bash
openssl rand -hex 32
```

Copy the output. The API will **not start** without it, so this is not optional.
(The scheduler still runs without it — you just can't query or trigger anything.)

## Step 2 — Set Railway variables

Railway dashboard → your service → **Variables**. Add:

| Variable | Value |
|---|---|
| `API_KEY` | the key from step 1 |
| `MAX_SUBSCRIBERS` | `5000000` |
| `FASTTRACK_SUBSCRIBERS` | `500000` |
| `ACTIVE_UPLOAD_DAYS` | `30` |
| `GROWTH_WINDOW_DAYS` | `30` |
| `CANDIDATE_REASK_DAYS` | `30` |
| `MAX_SEARCH_FALLBACKS` | `20` |

Confirm these are already set from the original deploy:
`DB_PATH=/data/outreach.db`, `GMAIL_TOKEN_FILE=/data/token.json`,
`GMAIL_CREDENTIALS_FILE=/data/credentials.json`.

**Set `DRY_RUN=true` for now.** You'll flip it back in step 8, after previewing.

While you're here: Settings → **Networking**. If there's no public domain,
click **Generate Domain**. That URL is your `$URL`.

## Step 3 — Deploy

```bash
git add -A
git commit -m "Add discovery, growth detection, candidate digest, and API auth"
git push
```

Railway rebuilds from the push. Watch **Deployments** until it's live.

On boot, `db.init()` migrates the existing database in place: adds the
`candidates` and `meta` tables, adds growth columns to `prospects`, and seeds
the 8 August bounces into `suppression`. Existing rows are untouched. It's
idempotent, so a redeploy is harmless.

## Step 4 — Confirm it's alive

```bash
curl "$URL/health"
# {"ok": true}

curl -H "X-API-Key: $KEY" "$URL/stats"
```

`/stats` returning **401** means auth works and your key is wrong or missing.
`/health` failing means the deploy didn't come up — check the logs.

Read the `pipeline` object in the `/stats` response. That tells you whether the
CSV import ever landed in production:

- `{"parked": 40, "hold": 5}` → it landed, skip step 5
- `{}` or missing → it never landed, do step 5

## Step 5 — Upload the CSV (only if step 4 showed it missing)

```bash
curl -X POST -H "X-API-Key: $KEY" \
  --data-binary @"excel sheet streamer outreach.csv" \
  "$URL/import"
```

Expected:

```json
{"imported": {"new": 0, "parked": 40, "hold": 5,
              "no_email": 6, "suppressed": 8, "already_known": 0}}
```

Re-running is safe — a second upload reports `already_known: 45` and changes
nothing. `suppressed: 8` is correct: those hard-bounced in August and are held
out on purpose.

## Step 6 — Fill in channel data for the CSV rows

The sheet has names and emails but no YouTube channel ids, so there's nothing
to personalize from yet.

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/enrich"
```

Capped at 20 lookups per run (each costs 100 quota units). With ~25 YouTube
rows, **run it twice**, a minute apart. The 17 Twitch rows will never resolve —
that's expected, they're outside the YouTube machinery.

## Step 7 — Write the personalized lines

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/personalize"
```

Writes one line per parked prospect using whatever channel data step 6 found.
Check the Railway logs — each line is printed as it's generated. **Read a few.**
If they're generic or wrong, stop here and tell me; don't send them.

## Step 8 — Preview, then go live

Still on `DRY_RUN=true`:

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/retouch-now"
```

Nothing sends. The logs print the exact emails that *would* go out. Read two or
three in full.

Happy? Railway → Variables → set **`DRY_RUN=false`**. Wait for the redeploy.

## Step 9 — Contact the 40 CSV prospects

> **This sends real email and cannot be undone.**

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/retouch-now"
```

30 send immediately (the daily cap), the remaining 10 go the next day.
The 5 `hold` prospects are **not** touched — those are your live conversations.

## Step 10 — Kick off discovery

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/discover"
curl -X POST -H "X-API-Key: $KEY" "$URL/run/snapshot"
```

Then it's automatic. From here the Monday block runs on its own:
discover 06:00 → enrich 06:30 → snapshot 07:00 → promote 07:30 → digest 08:00
(UTC), and your digest replies are read at 09/14/18 on weekdays.

---

## What to expect afterwards

**Immediately:** the 40 CSV re-touches, plus any discovered channel that
publishes an email, plus any 500k+ channel still posting. Those three skip the
growth gate entirely.

**Week 1:** a digest listing channels that need an address. Reply to it with
`ChannelName: email@host`, one per line. You get a confirmation reply saying
what matched and what didn't.

**Week 2 onward:** growth detection comes online. A channel needs two snapshots
inside the 30-day window before it can be labelled `growing`, so smaller
channels start promoting around here.

## Triggering anything by hand

```bash
curl -X POST -H "X-API-Key: $KEY" "$URL/run/<job>"
```

Jobs: `discover`, `enrich`, `snapshot`, `promote`, `personalize`, `send`,
`replies`, `candidates`, `digest-replies`, `retouch-now`.

Each returns `202` immediately and runs in the background — watch the Railway
logs for the result.
