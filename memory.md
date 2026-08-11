# Memory / Progress Log — Unified Trading Engine

Project-local journal of decisions, bugs, and state — lives in the repo so
anyone (including a future AI session reading the code cold) can get oriented
without needing external context. Append to this over time; don't rewrite
history.

## Current state (2026-08-04)

Architecture fully designed and documented (`PRD.md`, `Architecture.md`,
`rules.md`, `Phase.md`, `design.md`, this file). Phase 3 (core implementation)
and Phase 4 (local verification) both DONE — 42 passing tests, dashboard
live-verified. **UI design system built in Figma and shipped** — see design.md
for the full token table and the [Figma file](https://www.figma.com/design/GdNyRjMeqmv2NAQFO5JoIu).
Local git repo initialized, no commits made yet.

## UI design pass (2026-08-04)

The user explicitly asked for the dashboard to be properly designed, using
Figma if usable — confirmed usable (`whoami` succeeded, a test file created
without issue despite the account showing a "View" seat tier in `whoami`'s
output, which turned out not to block MCP-driven file creation/editing).

Built a "Trading Dashboard Tokens" variable collection (dark fintech palette —
`bg_page #0B0F14`, `bg_surface #141A21`, `bg_surface_raised #1B232C`,
`text_primary #E8EDF2`, `accent #3B82F6`, `success #22C55E`, `danger #EF4444`,
`warning #F59E0B`, `10px` radius) plus a full mockup frame (sidebar + main:
title, warning banner, account chips, metrics grid, open-position card) in a
new Figma file, then translated it into `.streamlit/config.toml` (base theme)
+ `dashboard_view.inject_custom_css()` (the pieces Streamlit's theme system
doesn't reach) + two components rebuilt from `st.metric` grids into custom
HTML/CSS (`render_account_health()`'s chips, the open-position card) since
`st.metric` couldn't produce the compact chip/accent-border look the design
called for.

**Bug hit and fixed during the Figma build**: `figma.createAutoLayout()`
defaults to an opaque white fill — every purely-structural wrapper frame
(sidebar sections, header, position-detail columns) was silently painting a
white rectangle over the dark page background wherever its children didn't
fully cover its bounds, making light-colored text unreadable in those gaps.
Fixed by explicitly clearing `fills = []` on every layout-only frame. Worth
remembering for any future Figma work in this or sibling projects — a newly
created auto-layout frame is NOT transparent by default.

**Live-verified** in the running app (not just eyeballed in Figma) via
`javascript_tool` computed-style reads: page/sidebar/card backgrounds, card
border-radius, and chip styling all matched the token hex values exactly.
The open-position card specifically was checked by inserting a synthetic open
trade into the local dev SQLite DB, reloading, reading the rendered
`.ute-position-card` element's computed background/border/radius, then
deleting the synthetic trade (`db.reset_all_data()`) so it didn't pollute
future test runs.

**Not done**: the `danger` token (`#EF4444`) is defined but not yet bound to
a specific negative-P&L display — `st.metric`'s own built-in red/green delta
coloring is what actually shows today, which is close but not guaranteed to
be the exact same red as the token. Flagged in design.md, not fixed — minor,
didn't seem worth further scope expansion this pass.

## How this project came to exist

Grew out of a long design discussion that started as a debugging session on
the sibling `intraday-trading-bot` (v1) and `intraday-trading-bot-v2` (v2)
projects — both hit the same real production bug: 6 independent strategy
variants each re-fetching largely-overlapping stock data every cycle, causing
v2's cycles to take ~12 minutes against a configured 2-minute interval (root-
caused 2026-07-21, fix proposed but never implemented in v2 itself — see that
project's own memory.md).

Rather than patch v1/v2's existing per-variant-independent design, the decision
was made to design a new, shared-data architecture from scratch and give it its
own project, rather than calling it "v3 of the intraday bot" — the universe
structure, variant-naming convention, and data-fetching approach are different
enough that treating it as a new sibling (not a version bump) was judged
clearer going forward. v1/v2 are not being deprecated or deleted, just no
longer where new variant/performance work lands.

## Key architectural decisions and why (chronological through the design discussion)

- **3 universe-bots (751/551/400) instead of the sibling bots' flat 6-variant
  list** — reorganized around universe first, because `bot_551`/`bot_400` being
  strict subsets of `bot_751` is exactly what makes Stage 1's shared-ranking-
  fetch possible. This wasn't a renaming exercise — it reflects a real data
  dependency the old `v1_1`/`v2_3_2`-style flat naming obscured.
- **2-stage pipeline (ranking data, then candle history) instead of one
  combined per-variant fetch** — the two data types have fundamentally
  different batching characteristics (ranking/quote APIs batch up to 50
  symbols/call; candle-history APIs are 1 symbol/call on every broker
  evaluated — Angel One, Groww, Dhan). Conflating them into one fetch step (as
  the sibling bots effectively do) hides this distinction and was the root
  cause of the sibling bots' duplication bug.
- **Multi-broker-account parallelism (2 Angel One + 2 Groww accounts) instead
  of a single account with more threads** — researched and confirmed that
  Angel One restricts non-registered/retail algo trading to 1 API key per
  account (SEBI compliance, 2026 forum finding) — so more real throughput
  requires more accounts, not more keys on one account. This surfaced a latent
  bug in the sibling bots too: their rate-limiter is a plain dict with no lock,
  which was never actually exercised under real concurrency until this
  project's design forced the question. Fixed here with a per-account
  `threading.Lock`-based `AccountRateLimiter` (see Architecture.md) — this
  pattern should probably be back-ported to the sibling bots too if they ever
  add real parallel fetching, though that's out of scope for this project.
- **Chunk-level fallback, not whole-list fallback** — if 1 of Stage 1's 6
  parallel chunks fails, only that chunk (not the other 5 successful ones) gets
  retried via Angel One. Decided explicitly over the simpler "just retry
  everything" approach to keep fallback fast and avoid re-fetching data that
  was already fine.
- **No strict wave-synchronization between the 2 broker accounts' worker
  groups** — an early sketch of the design paired workers across accounts
  (worker 1↔4, 2↔5, 3↔6) to run in lockstep rounds. Dropped: since the two
  accounts are fully independent, forcing them to wait for each other on every
  round would only slow down whichever account is faster, for no benefit.
  Each account's 3-worker queue now just runs at its own pace; results merge
  as they arrive.
- **Technology stack: Python + Streamlit Community Cloud, both explicitly
  re-litigated and kept** — see Architecture.md's "Technology decisions"
  section. Python was confirmed to have no real bottleneck here (I/O-bound
  workload, GIL releases during network waits). Streamlit Cloud's known
  limitations (≈1GB RAM, no first-class background-worker separation, sleeps
  after 12h idle) were weighed against a Hostinger VPS (Mumbai data center —
  researched specifically because it would reduce latency to Indian broker
  APIs, a real consideration given this design's tight rate-limit budgets) and
  Render (no India region at all, Singapore closest). Kept Streamlit Cloud:
  free, the sibling bots already prove the BackgroundScheduler-in-Streamlit
  pattern works in production, and the actual data footprint here (~100-150
  stocks' candle history/cycle) is small enough that the RAM cap isn't
  expected to bind. Revisit only if RAM or latency becomes a demonstrated
  problem, not preemptively.

## Bugs identified during architecture review (before any code existed)

Found via a structured design review (not live testing, since nothing is built
yet) — recorded here so they're designed around from the start rather than
discovered again in production like several sibling-bot bugs were:

1. Unsafe (non-locked) rate-limiter — addressed via `AccountRateLimiter`.
2. Centralizing Stage 1 into one shared fetch increases blast radius (one
   failure now affects all 12 variants instead of just one bot) — mitigated by
   the multi-account/multi-broker fallback chain, not eliminated entirely; a
   total outage across all configured accounts would still stall the cycle for
   everyone. Accepted trade-off, not solved.
3. A partial parallel-fetch failure (e.g. 1 of 6 chunks missing) could
   silently produce an incomplete-but-plausible-looking rank list — addressed
   by making chunk failures and fallback usage a visible dashboard warning
   (see design.md), not just a log line.
4. Subset-assumption (551/400 ⊂ 751) can theoretically break for a day or two
   around an NSE index rebalance, since the underlying CSVs cache
   independently — addressed with a defensive log-and-continue rather than
   crash if a smaller universe's symbol is missing from the big list.
5. "Copy of data" between the shared temp spaces and each variant must be a
   true copy (or enforced read-only access), not a shared mutable reference —
   flagged as an implementation-level risk to watch in Phase 3, not yet
   something code exists to get wrong.

## Design questions resolved (2026-08-02, same day as initial design)

- **"Subh 30 min" checkpoints**: 09:20/09:25/09:30 (same clock times as v1's
  old design), each evaluated 1 min after its candle closes — i.e. the actual
  checks happen at 09:21/09:26/09:31, not exactly on the checkpoint minute.
- **Trailing-exit mechanism**: the user wants BOTH EMA9-close-below and
  ATR-pullback, as separate variants — not a single generic "trailing exit".
  **There is no standalone "fixed exit" variant** — every variant's target %
  is fixed, but reaching it only triggers trailing mode (same as the sibling
  v2 bot's model: fixed SL always, fixed target flips to trailing instead of
  exiting immediately). So the variant count stays at **4 per universe-bot**
  (`*_trailing_ema`, `*_trailing_atr` × 2 entry-timings), **12 total** (3
  universe-bots × 4) — an earlier same-day pass at this doc briefly
  miscounted this as 6/18 by treating "fixed" as a third independent
  exit-style; corrected same day across PRD.md, Architecture.md, Phase.md, and
  this file once the user caught it.

## Phase 3+4 implementation (2026-08-03) — see Phase.md for the full file-by-file list

Both phases now DONE. Core engine built and verified with 42 passing tests
(`pytest tests/ -v` in the project's `venv/`), covering: `AccountRateLimiter`
under real concurrent threads, Stage 1's chunk-level dedup/fallback, Stage 2's
cross-universe dedup/fallback, `nse_universe.filter_to_universe`'s
subset-safety warning, `db.py`'s per-variant isolation (capital, checkpoints),
`variant_engine`'s subh30 checkpoint-timing state machine, one synthetic
end-to-end `run_full_scan_cycle()` run against fully-mocked accounts that
confirms the whole Stage1→filter→Stage2→12-variant-scan pipeline wires
together and each variant records its own independent trade, both trailing-
exit mechanisms plus the full `manage_open_position` flow (SL/target/hard-
floor/square-off), and a REAL `BackgroundScheduler` instance confirming the
two-job split (`IntervalTrigger` for position management, `CronTrigger` at
the 5-min-boundary+1 offsets for entry scan — not a bare `*/5`, the exact
distinction Architecture.md's docstring warns not to regress).

**Dashboard built and live-verified same day**: `app.py`, `viewer_app.py`,
`engine/dashboard_view.py`, `engine/metrics.py`, `common/metrics.py`. Actually
launched via the browser preview tool (not just read as code) — clicked
"Run Scan Now" for real and watched the warning banner correctly surface both
the "no accounts configured" warning and the resulting subset-safety warnings,
switched the 12-variant sidebar selector and confirmed the panel updates, and
confirmed the viewer app loads independently and shows the same state via the
shared local SQLite file. `engine/broker_accounts.py`'s Groww integration
itself is still unverified against a real account (best-effort from
documentation, explicit caveat in that file's own docstring — same pattern the
sibling bots' Angel One client originally shipped with). No git commit made
yet despite all this code existing — ask before committing, per this
project's own rules.md.

## Open questions / not yet resolved

- Dhan's current data-API terms need re-verification before relying on it in
  code — it was rejected for the sibling bots in 2026-07-16 for requiring a
  paid Data API tier, but research done for this project (2026-08-02) suggests
  that requirement may have been lifted since. Confirm against Dhan's own
  current docs before writing `broker_accounts.py`, don't trust either old
  finding blindly.
- ~~No GitHub repo~~ — DONE 2026-08-05: [github.com/humhai-JAAT/unified-trading-engine](https://github.com/humhai-JAAT/unified-trading-engine),
  pushed via `gh repo create --push` (local git identity had to be set first —
  `git config --local user.name/email`, matching the same values already used
  in the sibling repos, e.g. bot-v3's).
- ~~Broker account count open (2+2 plan)~~ — DECIDED 2026-08-08: **1 Groww
  (paid, primary) + 1 Angel One (free, fallback)**, down from the original
  2+2. Reasoning: per-cycle load is ~170 requests total (Stage 1's 16 batched
  quote-calls + Stage 2's ~100-150 single-symbol candle-calls); a single
  Groww account's ~25 req/s clears that in a few seconds, far inside the
  multi-minute checkpoint window, so a 2nd Groww account would only guard
  against that one account failing (key revoke/suspension) — not worth the
  extra ₹499+GST/month given `stage1_ranking.py`'s existing chunk-level
  fallback to Angel One already covers a full Groww outage. No code change
  required — `broker_accounts.py`'s 2nd-account slot per broker was already
  optional (silently skipped if unconfigured); this decision is just which
  env vars (`GROWW_1_*`, `ANGELONE_1_*`) actually get populated.
- ~~No Supabase project~~ — DONE 2026-08-09: created directly via the Supabase
  MCP connector (org "PARIHAR GROUP", region `ap-south-1`, free tier, ref
  `oosqmkeucbrziplxopyg`). Schema applied with `apply_migration`, generated by
  actually running `python -c "from engine.db import POSTGRES_SCHEMA; ..."` in
  the project's own venv rather than hand-copying — guarantees exact parity
  with what `db.py` expects, all 14 tables (12 variant tables +
  `ute_cycle_log` + `ute_checkpoint_log`) confirmed via `list_tables`.
  `get_advisors` flagged all 14 as RLS-disabled (critical, external-facing —
  anon key could read/write every row via PostgREST) immediately after
  creation; user asked to secure it, so RLS was enabled on all 14 with no
  policies (confirmed correct via `get_advisors` afterward — only the
  informational "enabled but no policy" lint remains). No policies needed
  because the app talks to Postgres directly via `DATABASE_URL`, which
  bypasses RLS entirely — this only closes the anon/authenticated REST-API
  path, which the app never uses anyway. ~~DB password/connection string
  still needs to come from the user~~ — received and live-verified 2026-08-09,
  `.streamlit/secrets.toml` (gitignored) now has a working `DATABASE_URL`.
- **Real broker credentials received and live-tested for the first time,
  2026-08-09** — both accounts' secrets typed directly in chat by the user;
  per this project's own rules.md ("if a credential is ever typed in chat,
  treat it as compromised"), the user was told to rotate the Supabase DB
  password and Groww API key/secret when convenient. Wired into
  `.streamlit/secrets.toml` (gitignored, confirmed via `git check-ignore` —
  never committed). This closed a real gap `broker_accounts.py`'s own
  docstring had flagged since 2026-08-02 ("not live-verified against real
  accounts") and found two real bugs neither Phase 3's synthetic tests nor
  Phase 4's mocked end-to-end test could have caught (both fixed, all 42 tests
  still pass):
  - `AngelOneAccount._headers()` never sent `Authorization: Bearer <jwt>` on
    the post-login secure calls (quote/candle) — only the login call itself
    worked, everything after failed with "Token missing". Angel One is now
    **fully verified live**: login, 3/3 quotes, 365 5-min candles.
  - `GrowwAccount._get_client()` called `GrowwAPI(api_key, api_secret)`, but
    the actual installed SDK (`growwapi==1.5.0`, added to venv — it was in
    `requirements.txt` but never `pip install`ed until now) takes a single
    `token: str`. Real flow: `GrowwAPI.get_access_token(api_key,
    secret=api_secret)` → returns a ~5.08h-lived JWT → `GrowwAPI(token)`.
    Fixed, plus added a 4h `GROWW_SESSION_TTL_SECONDS` re-fetch guard (the old
    code cached the client forever, no refresh — same class of bug the
    sibling bots' unlocked rate-limiter was, just not caught yet since this
    was never live-tested before today). **Groww auth itself now works**
    (`get_user_profile()` succeeds — `active_segments: [CASH, FNO]`) but
    `get_ohlc` (quotes) and candle calls still return "Access forbidden for
    this request." — decoding the exchanged token's JWT shows `role:
    order-basic,non_trading-basic,order_read_only-basic`, no market-data
    scope. Looks like an account-side provisioning gap (the paid Market/Live
    Data API subscription may not be enabled on this specific API app/key),
    not a code bug — **needs the user to check Groww's Developer Console**
    for this app's enabled scopes before Groww can serve real data. Until
    then Angel One is the only broker actually usable, which inverts the
    2026-08-08 broker decision's primary/fallback assumption in practice
    (Groww was meant to be primary) — worth re-checking once Groww's
    permission is sorted, but no urgency since `stage1_ranking.py` already
    falls through to whichever broker is configured and working.
- **Groww fully closed out same day (2026-08-09), later in the session.**
  User asked whether to buy the Market/Live Data subscription flagged above;
  recommended AGAINST it initially — live-tested that Angel One alone clears
  a full Stage1+Stage2 cycle in ~26s, comfortably inside the multi-minute
  checkpoint window, so the subscription wasn't needed just to make the bot
  functional, only for speed margin/redundancy that wasn't needed. User
  bought it anyway (their call, real recurring cost). After purchase, quotes
  started working immediately; candles needed 3 MORE bugs fixed (found via
  the same live-test-against-real-account method, none catchable by mocked
  tests): `candle_interval` needed a string (`"5minute"`) not a bare int;
  `groww_symbol` needed hyphen format (`"NSE-RELIANCE"`) — different from
  `get_ohlc`'s underscore format (`"NSE_RELIANCE"`) despite being the same
  SDK; and the response is actually 7 fields/row (undocumented trailing
  field, always `None` observed) with an ISO-string Datetime already in IST,
  not the 6-column unix-epoch shape originally assumed. All fixed, Groww now
  **fully live-verified**: 3/3 quotes, 395 candles — matches Angel One's
  status, so the 2026-08-08 primary/fallback design is no longer inverted.
  Also surfaced (independently, by the user) that Groww's exchanged token
  expires at a **fixed 06:00:00 IST daily cutoff** — matches what
  `_jwt_exp_timestamp()` was built to handle — and that the current
  secret-based auth flow requires **manual daily re-approval in the Groww
  app**, a real operational gap for a bot meant to run unattended. Recommended
  switching to Groww's TOTP-based key (same `pyotp`-driven pattern Angel One
  already uses, fully automatable) — **not yet done**, user hasn't generated
  a TOTP-based key yet, so daily manual approval is still a live requirement
  until they do. Worth revisiting before this bot is trusted to run
  unattended for real.
- Broker credentials fully typed/wired, both brokers live-verified — rest of
  Phase 5 (Streamlit Cloud deploy, keep-awake automation) still ahead.
- **2026-08-10 — websocket redesign discussion, then a scoped feature actually
  shipped (Phase 5.5).** User initially asked to "redesign everything" onto
  Groww's websocket. Researched `GrowwFeed` first rather than diving into
  code: it's NATS-based (not a plain websocket) and only streams live LTP,
  never historical OHLCV — so Stage 2 (needs candle history for EMA/ATR)
  fundamentally can't move to it, ruling out a literal full redesign.
  Live-tested subscription capacity anyway (user asked specifically): the
  full 751-stock universe subscribed with **zero errors, <1s** — no batch
  limit — but per-symbol data only arrives once that symbol actually trades,
  so coverage builds up over time rather than being instant (66% at 10s, 92%
  at 60s, even large-caps like BAJAJ-AUTO/APOLLOHOSP took >10s for their
  first trade in the sampled window). This ruled out replacing Stage 1's REST
  snapshot too, at least without a "seed via REST, top up via websocket"
  design that wasn't built.
  What DID get scoped and shipped: open-position monitoring (2-min REST →
  tick-driven websocket, ~6s reaction live-verified) — see Phase.md's Phase
  5.5 and Architecture.md's "Live position monitoring" section for the full
  build (`engine/live_feed.py`, `variant_engine.locked_decide_and_exit()`,
  `db.acquire_trade_lock()` finally wired in after existing unused since
  Phase 3). 49/49 tests pass, pushed to GitHub same session (user explicitly
  asked for the push, not just a local commit — deviates from this project's
  usual "never push without being told" default, correctly, since it WAS
  told this time).
  **Pattern worth remembering**: when the user proposes a broad redesign,
  research the actual capability/limits of the underlying API FIRST (here:
  what does the websocket actually give, and at what scale) before scoping
  or building anything — it reshaped "redesign everything" into a much
  smaller, correctly-targeted change, and avoided building something
  (full Stage 1/2 websocket migration) that the API couldn't actually support
  well.
- **2026-08-10, same session — Groww TOTP switch, closing the daily-manual-
  approval gap flagged back in Phase 5's Groww section (2026-08-09).** User's
  own research initially suggested Groww had no TOTP option — verified
  against Groww's official docs AND the installed SDK's actual
  `get_access_token()` source (a real `totp`-branch API call, not a
  vestigial parameter) before trusting either claim; TOTP is real,
  documented, and Groww's own recommended method ("No Expiry", vs. daily
  approval for secret-based keys). **Another instance of the same pattern
  as the websocket research above** — verify a factual claim (from the user
  OR from an assumption of my own) against an authoritative source before
  acting on it, rather than trusting either blindly.
  User generated a TOTP-based key via Groww's console (shared in chat — same
  "treat as compromised, rotate when convenient" note as every other
  credential this session). `GrowwAccount` (`engine/broker_accounts.py`) now
  supports both `totp_secret` (preferred, same `pyotp` pattern
  `AngelOneAccount` uses) and `api_secret` (kept for backward compatibility
  only). Live-verified: 3/3 quotes, 394 candles, zero manual steps needed.
  One nuance worth remembering: the TOTP switch does NOT change how often the
  *access token* itself needs refreshing — that's still the same fixed
  06:00:00 IST daily cutoff `_jwt_exp_timestamp()` already handled correctly.
  What changed is that generating a fresh access token near that cutoff no
  longer needs a human to click "Approve" in the Groww app — the TOTP secret
  lets the code do it alone, indefinitely.
- **2026-08-10, same session — ran a code review (`/code-review`) over the
  live-feed + broker-fix diff, found and fixed 2 real issues (0
  critical/security).** Both were things a single-developer live-testing
  pass wouldn't naturally surface — worth remembering as a category: race/
  contention bugs and repeated-I/O-in-a-hot-loop bugs tend to hide from
  "does it work" testing and need a dedicated pass looking for them.
  1. `db.acquire_trade_lock()` had ONE global advisory-lock key for all 12
     variants — any variant's trailing-exit REST call (held inside the lock)
     blocked every unrelated variant's exit-check too. Fixed: keyed per-
     variant via `zlib.crc32(variant_id)` (`db._lock_key_for_variant`).
     Live-verified against real Postgres that two different variants' locks
     don't block each other. `variant_engine.locked_decide_and_exit()`'s
     call site updated to `db.acquire_trade_lock(variant_id)`.
  2. `live_feed._sync_subscriptions()` called Groww's symbol→token lookup on
     every ~2s poll, which re-reads+parses a ~4k-row CSV from disk every
     single call (that function's own cache is file-based, not in-memory).
     Fixed with a 1h in-memory cache (`live_feed._get_symbol_to_token()`).
  Also: removed a dead `_INSTRUMENT_FIELDS` constant, and added
  `tests/test_broker_accounts.py` — the review flagged that `GrowwAccount`'s
  TOTP-vs-secret auth branching had zero test coverage despite being
  auth-critical (exactly the kind of branch that could silently regress).
  **55/55 tests pass** (49 + 4 new broker_accounts tests + 2 new caching
  tests in test_live_feed.py).
  **User then asked for the review's remaining 2 (lower-severity) findings
  too** — closed same session: added a `threading.Lock` around
  `live_feed._subscribed_tokens` (racy between `stop()`'s main-thread write
  and `_sync_subscriptions()`'s background-thread read/write — benign under
  the GIL but worth closing properly), bracketing only the dict access, not
  the websocket subscribe/unsubscribe network calls. And made explicit in
  `live_feed.py`'s module docstring that it checks price at POLL TIME, not
  continuously — a touch-and-recover within one ~2s poll window can be
  missed by this path specifically (the 2-min REST job's per-candle High/Low
  scan doesn't have this gap, catches it retroactively). Not a bug fix, a
  clarity fix — don't let a future reader assume stronger guarantees than
  what's actually implemented.
- **Phase 5 fully closed out, 2026-08-10.** Both Streamlit Cloud apps deployed
  and live: [unified-trading-engine.streamlit.app](https://unified-trading-engine.streamlit.app)
  (admin) and the viewer app. User verified admin live (scheduler running,
  Angel One #1 + Groww #1 both healthy, correctly "Asleep" outside market
  wake hours). Caught and fixed a real gap while checking the viewer app: it
  let any visitor pick any of the 12 variants — `config.py`'s `public_variant`
  DEFAULTS key existed since Phase 3 specifically to prevent this but was
  never wired up. Fixed: admin app gained a "🌐 Public Viewer" control,
  viewer app's selector removed entirely in favor of just rendering whatever
  the admin picked. Keep-awake automation ported directly from the sibling
  bots' proven `.github/workflows/keep-awake.yml` +
  `scripts/wake_streamlit.py` pattern (Playwright-based — plain HTTP GET
  doesn't work against Streamlit Cloud, confirmed in the sibling repos) —
  this project's `APP_URL` was filled in with the real live URL immediately,
  unlike bot-v3/swing-trading-bot's copies which still carry unfilled
  placeholders. Live-tested the wake script locally against the real
  deployed app before committing.
- **Phase 6 started for real, 2026-08-11 — first live-market debugging
  session, 2 real bugs found and fixed.** User noticed the admin dashboard
  saying "no cycles run yet" ~37 min into market hours and asked for a
  proper investigation ("design test cases, run them, report back") rather
  than a guess-and-patch. Investigated directly against the real production
  Supabase DB (not a local/fake one) — found a REAL trade already open
  (POLICYBZR, 2 `bot_751` puradin variants, entered ~09:52) with ZERO
  matching `entry_scan` cycle_log row, meaning `run_full_scan_cycle()` had
  crashed mid-execution AFTER committing real trades but BEFORE its own
  final `db.log_cycle()` call — a genuinely dangerous class of bug (real
  state changes with no audit trail) that could not be reproduced on demand
  (a manual re-run completed cleanly), so was root-caused from the SYMPTOM
  rather than a captured traceback. While investigating, ALSO caught a
  second live bug: a manual re-run overlapped with the Cloud app's own
  scheduled cron cycle (Stage 1+2 real-network fetches take minutes,
  easily long enough to overlap another trigger), producing a misleading
  cycle-log `entered=[...]` list — verified via direct DB query that this
  did NOT cause an actual duplicate trade (capital-safety held), but was a
  real data-integrity/observability gap.
  Fixed both same-day: (1) `run_full_scan_cycle()` now wraps its body in
  try/except with a guaranteed `db.log_cycle(status="ERROR", ...)` before
  re-raising — a crash can never again vanish without a trace. (2) new
  `db.try_acquire_scan_lock()` (non-blocking `pg_try_advisory_lock`, a
  THIRD lock key in `db.py` now — distinct from the per-variant
  `acquire_trade_lock` used for exits) makes a second overlapping scan
  cycle skip cleanly instead of racing. Both live-verified against real
  Postgres before committing (lock acquire/block/release sequence,
  simulated crash producing a real `ERROR` row) — same rigor as every other
  fix this session, not just unit-tested in isolation. 2 new regression
  tests added, 57/57 pass.
  **Pattern worth remembering, reinforced again**: production Postgres
  live-testing keeps finding real bugs that mocked/local-SQLite tests
  structurally cannot — three separate sessions now (broker auth bugs,
  code-review concurrency findings, and now this crash-visibility +
  overlap bug) where "run it for real, look at what actually happened in
  the DB" was the only way to the real root cause.
  Still open: Phase 6's original scope (Stage 1 timing budget, a REAL
  fallback-trigger test, Stage 2 dedup-savings measurement, and one full
  observed trade lifecycle including an exit) — today only got as far as
  entries, no exit observed live yet.
- **Same day, immediately after — user reported 3 more things after
  actually using the deployed apps.** Two were real bugs, one was a design
  question. **Important process note**: the `public_variant` bug is a
  DIRECT consequence of not having realized, when building that feature
  yesterday, that admin (`app.py`) and viewer (`viewer_app.py`) are TWO
  SEPARATE Streamlit Cloud deployments with SEPARATE filesystems —
  `engine.config`'s `settings.yaml` is a local file, invisible across that
  boundary. The feature looked correct in local testing (single process,
  one filesystem) and even looked correct from the admin side on Cloud (the
  value saved fine) — the break was invisible until someone actually opened
  the SEPARATE viewer app and looked. **Lesson: for any two-Streamlit-app
  project, "did I test this from the OTHER app's perspective, not just the
  one I was editing" is a real question to ask before calling a
  cross-app feature done** — this bit twice in one day pattern (websocket
  research → scoped correctly; this → not caught until the user looked).
  Fixed: new `ute_settings` DB table (`db.get_setting`/`db.set_setting`) —
  the database is genuinely the only thing both apps share; local files and
  in-memory state are not. Second bug: the open-position card only ever
  showed static entry-time fields, no live price/P&L — compared directly
  against the sibling bots' `dashboard_view.py` (bot-v3) to confirm what
  "live tracking" was supposed to look like rather than guessing, then
  matched that pattern (fetch a live quote, show Current + Unrealized P&L)
  plus this project's own peak/trough (already tracked, just never
  displayed). Both fixes live-verified against real Postgres/real market
  data (real open POLICYBZR position: entry ₹1660 vs live ₹1630 → correct
  -1.80% shown). Third item (why 2-min position management) was just a
  design clarification, not a bug — that's the REST safety-net's interval,
  separate from `live_feed.py`'s much-faster websocket path built the day
  before. 3 new tests, 60/60 pass, committed and pushed same session.
- **Same session, immediately after — 2 more real findings from continued
  live debugging.** (1) Root-caused the POLICYBZR entry-timing question
  properly instead of assuming: fetched real candles, ran them through
  `strategy.build_indicators()` directly, and found the user's own
  TradingView reference signal (Aug 10, ~10:50) genuinely matches what our
  strategy code would have computed — but `ute_cycle_log` has ZERO rows for
  all of Aug 10, meaning the scheduler simply wasn't running that entire day
  (killed repeatedly by that day's own heavy Streamlit Cloud redeploy
  churn). Today's trade is a separate, independently legitimate signal on
  the same stock. Not a strategy bug — confirms the "must click Start after
  every push" operational gap is a real, recurring cost, not a one-off.
  (2) A worsening candle-fetch-failure warning (14/85 → 38/77) turned out to
  be a SEPARATE, stricter rate limit specifically on Groww's
  `get_access_token()` endpoint (not the regular market-data rate limit,
  which the code already handles correctly) — exhausted by this session's
  OWN debugging pattern: dozens of short-lived local test scripts, each
  starting with an empty in-memory token cache, each forcing a fresh
  `get_access_token()` call, sharing rate-limit budget with the live Cloud
  app's account and breaking ITS candle fetches too. **Self-inflicted, worth
  remembering**: aggressive live-testing (this session's whole methodology,
  repeatedly validated as valuable for finding real bugs) has a real cost
  when the thing being tested has its OWN separate rate limits on auth/token
  endpoints, not just data endpoints — a single long-running process
  wouldn't hit this, only repeated fresh-process invocations would. Fixed
  with a disk-persisted token cache (`data/groww_token_cache_{account_id}.json`,
  gitignored) so a still-valid token survives a process restart. Could not
  live-verify against the real rate limit same-session (it was still
  cooling down) — deliberately stopped hammering it further rather than
  making the cooldown worse; validated via 5 new mocked tests instead, an
  explicit exception to this session's usual "always live-verify" rule,
  made consciously rather than by default. Also fixed a test-isolation bug
  this surfaced: `test_broker_accounts.py` used the REAL production
  `account_id="groww_1"`, which would have let a real cached token silently
  short-circuit the mocked-`get_access_token` test assertions once real
  cache files existed — switched to an isolated account_id + tmp_path.
  62/62 tests pass.
- **Same session, immediately after — 3 more sharp user questions, 2
  clarified as not-bugs, 1 real fix.** (1) Why bot_551's page shows the same
  warning as bot_751's — by design, `render_warning_banner()` shows the
  latest SHARED entry_scan cycle's status, and Stage 1/2 is one fetch for
  all 12 variants, not per-variant. (2) Why the scan appears to fire at
  :03/:08/:13 instead of the configured :01/:06/:11 — the cron trigger is
  unchanged and correct; `db.log_cycle()`'s `cycle_time` is stamped at
  COMPLETION (after Stage 1+2's real network I/O, 2-4+ min observed this
  session), not at trigger time — a logging-semantics gap, not a scheduling
  bug. Flagged for a future pass (log start time too?) rather than fixed
  today — didn't want to touch `cycle_time`'s meaning without auditing
  `prune_cycle_logs` and anything else that reads it. (3) A REAL bug:
  candle-fetch failures kept climbing (36/73) even through Angel One
  fallback — tested 3 "failed" symbols directly against Angel One in
  isolation, all 3 succeeded immediately, ruling out "these symbols are
  broken." Root cause: `stage2_candles.py`'s fallback pass was a plain
  SEQUENTIAL loop (only the primary pass was ever parallelized) — with
  Groww still degraded from the earlier rate-limit finding, its entire
  ~70-symbol load cascaded onto Angel One as fallback in one sequential
  burst, slow enough that some calls failed under sustained load that
  worked fine individually — a "thundering herd on the fallback path"
  symptom. Fixed by parallelizing the fallback pass with the same
  `ThreadPoolExecutor` pattern the primary pass already used (the fallback
  account's own rate limiter still throttles correctly regardless of
  worker count — that's its whole design). 1 new test (40-symbol bulk
  cascade, correctness only, not timing). 63/63 tests pass.
  **Pattern still holding**: three findings in one exchange, three
  different postures — explain when it's genuinely not a bug rather than
  reflexively "fixing" something that isn't broken, root-cause with a
  targeted isolated test before touching code, and only change behavior
  where a concrete mechanism (sequential fallback under bulk load) was
  actually identified, not just correlated.
