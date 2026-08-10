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
