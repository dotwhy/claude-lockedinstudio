# LockedIn creator-outreach system

An autonomous outreach engine for Roblox YouTubers. It sources prospects,
writes a personalized opener per creator, runs a 3-step sequence from your
Gmail, drops anyone who unsubscribes or says no, and flags the good replies
for you to handle personally.

## What it does on its own
- **Discovers** Roblox channels via the YouTube API and keeps the ones in band.
- **Finds emails** in the channel's own public text — About description and
  recent video descriptions ("Business enquiries: ..."). No captcha, no Apollo.
- **Tracks growth** weekly. A channel is worth contacting when it's rising, or
  when it's already 500k+ and still posting.
- **Asks you** once a week for the addresses it couldn't find, in one email.
  Reply with `ChannelName: email@host` and it takes it from there.
- **Personalizes** one opening line per creator with Claude — the part that
  actually earns replies.
- **Sends** the opener + two follow-ups on a fixed cadence, in one thread.
- **Reads replies** and classifies each: interested / question / not interested
  / unsubscribe / auto-reply / other.
- **Drops** not-interested and unsubscribed (the latter added to a permanent
  suppression list — never contacted again).
- **Flags** interested + question replies: stops the sequence, stars the thread,
  labels it `Outreach/Review`, and can email you a digest. **It never
  auto-replies to a warm lead** — that handoff is always yours.

## What it does NOT do (on purpose)
- It does **not** send anything while `DRY_RUN=true` (the shipped default).
- It does **not** answer positive replies for you.
- It does **not** touch the captcha-gated "View email address" button on the
  About page. It doesn't need to — most creators paste the same address into
  their descriptions, which the API returns as plain text.
- It does **not** serve the HTTP API without `API_KEY` set. The routes write
  into `prospects`, and anything there gets emailed from your Gmail.

## How a channel becomes a prospect

```
discover ──► has a public email? ──yes──► prospect ──► personalize ──► send
                     │
                     no
                     ▼
                 candidate ──► snapshot weekly ──► growing, or 500k+ and active?
                                                            │
                                    ┌───────────────────────┘
                                    ▼
                          weekly digest asks you for the address
                                    │
                          you reply ──► prospect ──► personalize ──► send
```

---

## Setup (one time)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env
python run.py init
```

Fill in `.env`:
- `ANTHROPIC_API_KEY` — from console.anthropic.com.
- `YOUTUBE_API_KEY` — a Google Cloud project with *YouTube Data API v3* enabled
  (only needed if you use `run.py source`).
- Gmail OAuth: in Google Cloud Console, create an OAuth **Desktop app** client,
  download the JSON as `credentials.json` into this folder. First time you send
  or read, a browser opens once to authorize; the token caches to `token.json`.
- `PORTFOLIO_URL`, `STUDIO_ADDRESS`, `FROM_NAME` — used in every email.

---

## Getting prospects in

**Option A — import a list you already have (recommended):**
```bash
python run.py import prospects.csv
```
Required column: `email`. Optional: `channel_name`, `first_name`,
`subscriber_count`, `latest_video`. See `prospects.sample.csv`.

**Option B — discover with YouTube, then enrich:**
```bash
python run.py source
```
Channels *with* an email are added directly. Channels *without* one are written
to `candidates.csv` — run those through Apollo/Clay to get emails, then
`run.py import` the result. (Or implement `enrich_email()` in
`youtube_sourcing.py` to close the loop automatically.)

---

## Running it

Test everything first with `DRY_RUN=true` — it prints the exact emails it would
send:
```bash
python run.py tick        # personalize -> send -> replies
python run.py stats       # pipeline counts + how many need your review
```

When the previews look right, set `DRY_RUN=false` in `.env` and it sends for real.

Individual steps if you want them:
```bash
python run.py personalize   # generate lines for new prospects
python run.py send          # send openers + due follow-ups (respects daily cap)
python run.py replies       # read + classify + act on replies
python run.py digest        # email yourself the review queue
```

## Making it autonomous

Schedule `python run.py tick` a few times a day. The daily cap in `.env`
(`MAX_SENDS_PER_DAY`) controls volume regardless of how often it runs.

**cron (Linux/Mac), 3x per weekday at 9/13/17:00:**
```
0 9,13,17 * * 1-5  cd /path/to/lockedin-outreach && ./.venv/bin/python run.py tick >> tick.log 2>&1
0 18 * * 1-5       cd /path/to/lockedin-outreach && ./.venv/bin/python run.py digest >> tick.log 2>&1
```

**Railway / any always-on box:** run the same commands on a schedule
(Railway cron, GitHub Actions on a schedule, or a small `while True: sleep`
wrapper — cron is simplest).

---

## Deliverability note
Cold-emailing from a personal Gmail at volume will eventually hurt your inbox
reputation. Keep `MAX_SENDS_PER_DAY` low, and when this proves out, move sending
to a dedicated domain (the same reasoning behind using Lemlist for the school
side). Every email already includes an unsubscribe line and your postal address
to stay on the right side of CAN-SPAM.

## Files
| file | role |
|------|------|
| `run.py` | CLI entrypoint |
| `config.py` | all settings + email templates |
| `db.py` | SQLite state (prospects, messages, suppression, review queue) |
| `youtube_sourcing.py` | prospect discovery |
| `personalize.py` | per-creator opening line (Claude) |
| `gmail_client.py` | Gmail send / read / label |
| `classifier.py` | reply intent classification (Claude) |
| `engine.py` | the loop: send, process replies, digest |
