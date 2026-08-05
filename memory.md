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
- No GitHub repo, no Supabase project, no broker credentials configured yet —
  all of Phase 5 is still ahead.
