# LockedIn outreach engine — plan

**Objective:** a low-touch outbound machine that runs the full sequence itself
and interrupts you only when it genuinely cannot proceed without a human.

The measure of success is not emails sent. It is **how few times per week the
system needs you, and how little is wasted when it does.**

---

## Where it actually stands (24 Sep 2026)

Live on Railway, sending, with 245 tests.

| | |
|---|---|
| In sequence | 69 prospects |
| Parked (contacted Sept, dormant) | 39 |
| Channels tracked | 128 |
| Waiting on an address from you | 40 |
| Promoted from discovery | 5 |
| Unsubscribed / bounced | 3 |

**What works:** discovery finds channels and extracts emails from public text;
62 prospects came from that alone. Personalization writes a specific line and
refuses to ship a bad one. Sending, follow-ups, reply classification and
suppression all run unattended. Job history and a daily health check now exist.

**What this week proved:** four separate bugs all had the same shape — something
silently did nothing, and silence looked identical to success.

1. A weekly job dropped by APScheduler's 1-second misfire window
2. Nothing recorded which jobs ran, so nobody could tell
3. Digest replies were unreadable (self-sent mail was filtered out as "ours")
4. The same reply was re-processed 3x/day, emailing a confirmation each time

All fixed. The pattern is the thing to design against, not the individual bugs.

---

## Decision taken: growth detection comes out of the promotion path

Growth has never promoted a single channel. It asks "did this grow 5% in seven
days?" — which almost nothing does — because the threshold was written for
month-over-month data and is being fed week-over-week snapshots.

Meanwhile the signals that *do* work: a public email (62 prospects), and
size + still-posting (45 queued). Snapshots keep running as a record, but
nothing gates on them.

**Consequence:** discovery's job is now "find reachable channels in band", not
"find channels on the way up". Simpler, and matches reality.

---

## The real problem: you are touched too often, for too little

Right now the system can send you **four different emails a day**: the content
batch, the candidate digest, the health report, and the review digest. Plus the
confirmation replies. That is not low-touch — that is a second inbox.

A low-touch machine sends **one email a day**, and only when there is something
to do in it.

### P0 — Consolidate to a single daily brief

One email, one time, containing only what needs you:

```
LockedIn — Thursday 24 Sep

NEEDS YOU
  • 8 channels need an address    [the batch, inline]
  • 2 warm replies to answer       [links to the threads]

RUNNING
  12 sent · 69 in sequence · 40 queued for addresses
  all jobs healthy

CONTENT (review, then post)
  [today's batch]
```

Nothing arrives when there is nothing to do, except a Monday line so that
silence never becomes ambiguous. Replaces four emails with one.

**Why first:** it is the objective. Everything else is plumbing.

### P0 — The blind spot: external uptime ping

A monitor inside the worker cannot report its own death. Five minutes: point
healthchecks.io or UptimeRobot at `/health` (no auth needed). Covers the one
failure that today's monitoring structurally cannot.

### P0 — Retry transient API failures

Three jobs failed today on SSL and timeout errors talking to Google. Not code
faults, but there is no retry, so a failed `tick` simply does not happen until
the next slot hours later. Wrap the Gmail and YouTube calls in retry with
backoff. Cheap, and removes the most common cause of "it just didn't run".

---

## P1 — Things that are quietly stuck

### The 39 parked contacts

30 were re-touched on 14 Sep. **9 have never been contacted at all**, and none
of the 39 will be touched again until ~13 December, because the 90-day window
is measured from their import date.

`retouch --now` would reach them, but force mode has no recency guard — it
would re-email the 30 people contacted last week. Add a "skip anyone contacted
in the last N days" guard, then the 9 can go safely.

### Deliverability — move off personal Gmail

Unchanged from TODOS item 1, and now urgent rather than theoretical: volume is
no longer bounded by what you import by hand. 3 unsubscribes/bounces already.
Cold email at sustained volume from a personal Gmail damages the inbox you use
for everything else, slowly and irreversibly.

Dedicated domain, SPF/DKIM/DMARC, 2-4 weeks warmup. The suppression and
unsubscribe handling is already built — this is a transport swap.

### Twitch is outside the machine

17 of the imported contacts are Twitch. They have no YouTube channel id, so
they get no enrichment, no personalization signal, and never appear in
discovery. They work as prospects for sending and nothing else. Either accept
that explicitly, or decide Twitch is out of scope and stop carrying them.

---

## P2 — Quality, once the loop is quiet

- **Eval for opener lines.** 57% of the first batch was unsendable (refusals,
  unsupported claims, one verbal tic in 4 of 6). Validation catches the classes
  we know. An eval catches drift in the ones we don't.
- **Warm-lead handoff.** Currently: stop the sequence, star, label, digest. Fine.
  Worth revisiting once there are enough warm replies to see what you actually
  do with them.
- **E2E test** across discover → send, deferred from the original build.

---

## What "low touch" should mean, concretely

A target to design against, and to measure:

| Interruption | Now | Target |
|---|---|---|
| Emails per day | up to 4 | 1, only when actionable |
| Minutes per weekday | unknown | under 10 |
| Decisions only you can make | addresses, warm replies | unchanged — these are the point |
| Things you must remember to check | Railway logs, `/stats` | none |

The last row is the one that matters. Every time this week something broke, it
was found by someone going to look. The system should be the one that notices.

---

## Sequencing

```
1. Single daily brief            ← the objective
2. Uptime ping + retries         ← stop losing runs silently
3. Unstick the 9 + recency guard ← reclaim contacts already paid for
4. Sending domain                ← before volume climbs further
5. Twitch decision               ← stop carrying an unserved case
6. Eval + E2E                    ← quality, once the loop is quiet
```

1-3 are days of work. 4 is a calendar constraint (warmup), so start it early
and let it run in the background.

---

## Open questions

- **Batch size.** Daily nudge is 8. Is that what you can actually source in a
  sitting, or should it be 3, or 15?
- **Metabase.** 174 channels and two months of snapshot history sit unused in
  the other project. Import into `candidates`, or let discovery rebuild it and
  retire the metabase? Currently drifting by default rather than by decision.
- **Volume ceiling.** `MAX_SENDS_PER_DAY=30`. Is 30/day the right rate for a
  studio your size, or is the constraint really how many warm replies you can
  handle in a week?
