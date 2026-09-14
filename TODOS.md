# TODOS

Work that was considered and deliberately deferred. Each entry says why, so the
reasoning survives longer than the conversation it came from.

---

## 1. Move sending off personal Gmail

**What:** Send outreach from a dedicated domain with proper warmup instead of
your personal Gmail account.

**Why:** `DEPLOY_RAILWAY.md` already flags this, but automated discovery makes it
urgent rather than theoretical. Today volume is bounded by how many prospects you
import by hand. Once the Monday discover/promote loop runs, volume grows on its
own and `MAX_SENDS_PER_DAY=30` becomes a floor you actually hit rather than a cap
you never reach. Cold email from a personal Gmail at sustained volume damages the
inbox you use for everything else, and the damage is slow and hard to reverse.

**Pros:** Protects your primary inbox. A dedicated domain can be warmed,
monitored, and thrown away if it burns. Bounce and complaint rates become
measurable instead of invisible.

**Cons:** Domain purchase, DNS records (SPF/DKIM/DMARC), and 2-4 weeks of warmup
before meaningful volume. Replaces `gmail_client.py` with an ESP integration.

**Context:** The August run already produced 8 hard bounces (now seeded into
`suppression`). Bounce rate is the metric that gets an account flagged. The
engine's existing suppression and unsubscribe handling is the hard part and it's
already built — this is a transport swap, not a redesign.

**Blocked by:** Nothing. Do it before volume climbs, not after.

---

## 2. End-to-end test: discover through to a dry-run send

**What:** One test driving the full chain — fake YouTube client → `discover()` →
two snapshots → `growing` → promote → personalize (faked) → `DRY_RUN` send →
assert the rendered email body.

**Why:** The unit suite covers all 33 code paths individually, but nothing proves
they connect. The seams between discovery, growth detection, promotion, and
sending are where a refactor breaks things silently — and a break there means the
system quietly sends nothing, which is exactly the failure mode that went
unnoticed for months already.

**Pros:** Catches integration breaks that unit tests structurally cannot. Doubles
as executable documentation of the whole pipeline.

**Cons:** Needs a fake YouTube client with enough surface to satisfy
`search().list()`, `channels().list()`, and `playlistItems().list()`.

**Context:** Deferred during the eng review in favour of full unit coverage
first. The fakes needed here are mostly already written in the unit suite.

**Blocked by:** Nothing — the unit tests build the fakes this would reuse.

---

## 3. Eval for opener line quality

**What:** Score generated opener lines against the rules the prompt claims to
enforce: under 25 words, references something specific, no pitch, no exclamation
marks, doesn't invent facts about the channel.

**Why:** The personalized line is the only part of the email that earns replies,
and it's the one part with no automated check. A prompt edit that quietly makes
lines generic would show up as a falling reply rate months later, with no way to
attribute it.

**Pros:** Makes prompt changes safe to iterate on. Catches hallucinated channel
facts before they reach a creator.

**Cons:** Costs real Anthropic calls per run (~20 channels, pennies, but not
free or offline). Needs a judge prompt, which is its own thing to get right.

**Context:** Worth doing once the copy stabilises. Right now the templates are
still moving, so an eval would be measuring a moving target.

**Blocked by:** Let the new opener run for a few weeks first.

---

## 4. Decide what happens to the 40 parked prospects

**What:** The August "first outreach - no answer" contacts imported from the
sheet. They sit `parked` with `last_sent_at` NULL, so `COALESCE(last_sent_at,
created_at)` gates them on their import date — nothing sends until roughly
**9 December 2026**.

**Why:** This is a decision, not a bug. `run.py retouch --now` forces all 40 now
(30 today, 10 tomorrow under the daily cap). Waiting is also fine. What's not
fine is forgetting the choice exists and being surprised in December.

**Pros of forcing now:** 40 warm-ish contacts working immediately instead of
sitting idle for three months.

**Cons of forcing now:** One-way. They all get the generic re-touch line because
`personalized_line` is empty on sheet-imported rows, and the copy says "reached
out a while back" to people who were last contacted in August — which is true but
thin. Also 40 sends in two days from a personal Gmail, which item 1 warns about.

**Context:** Raised twice during the September session and left open both times.

**Blocked by:** Ideally item 1, if you force them.

---

## 5. Import the metabase's 174 channels

**What:** One-off migration of `roblox-youtuber-metabase/data/channels.db` — 174
channels, 33 with scraped emails, 13 days of snapshot history — into the engine.

**Why:** Deliberately skipped. Railway-side discovery rebuilds an equivalent set
within a week or two, and importing creates two sources of `channel_id` truth
with no dedup between them. The one genuinely lossy part is the 13 days of
snapshot history, which the engine would take 13 days to re-accumulate.

**Pros:** Instant 33-prospect head start and two months of growth history.

**Cons:** Identity collisions between imported and discovered rows. The metabase
schema doesn't map cleanly onto `candidates` (different growth columns, different
enrichment state).

**Context:** If you do this, import into `candidates` and let the normal promote
path handle graduation — do not write straight into `prospects`.

**Blocked by:** Let the new discovery loop run one full week first, so you can
see what it finds on its own before deciding this is worth the mess.

---

## 6. Port the rest of the metabase scoring

**What:** Fit score (0-100), upload frequency per week, and engagement ratio from
`roblox-youtuber-metabase/src/analyze.ts`.

**Why:** Cut from the September build because nothing consumes them. Growth
detection alone answers "should this channel become a prospect." The other 60
points of the scoring model would compute a number nothing reads.

**Pros:** Would let you rank the outreach queue by fit instead of treating every
growing channel equally — useful once volume exceeds what the daily cap can send.

**Cons:** Premature until the queue is genuinely oversubscribed. Upload frequency
needs snapshots spanning 7+ days to mean anything.

**Context:** The logic is already written and tested in TypeScript at
`src/analyze.ts` — `calculateFitScore` and `calculateUploadFrequency`. Porting is
mechanical. Do it when you have more qualified prospects than you can email.

**Blocked by:** Having more growing channels than `MAX_SENDS_PER_DAY` can absorb.
