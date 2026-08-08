# Phases — Unified Trading Engine

Retrospective + forward-looking breakdown. Update the status markers as work lands.

## Phase 0 — Architecture design ✅ DONE (2026-08-02)

Designed from scratch across an extended discussion (not copied from a sibling
bot this time, unlike v2/swing/bot-v3's copy-then-diverge pattern) — the trigger
was a real production performance problem in the sibling `intraday-trading-bot`
and `intraday-trading-bot-v2` (6-variant duplicate/sequential data-fetching
causing 12-minute cycles against a configured 2-minute interval).

Key decisions made and locked in during this phase:
- 3 universe-bots (751/551/400 stocks) × 4 variants (entry-timing × trailing-exit mechanism)
  = 12 total, replacing the sibling bots' `v1`/`v2` × 6-variant structure.
- 2-stage shared-data pipeline: Stage 1 (ranking data, once per cycle, filtered
  per-universe) and Stage 2 (candle history, deduplicated across all 3
  universes' top-50 lists) — see Architecture.md for the full design.
- Multi-broker-account parallel fetch (2 Angel One + 2 Groww accounts, Dhan
  optional), each account protected by its own thread-safe rate-limiter.
- 5 architecture bugs identified and designed around before any code was
  written: unsafe (non-locked) rate-limiter, single-point-of-failure blast
  radius, silent partial-fetch failures, subset-assumption cache-timing edge
  case, and shared-mutable-object risk on the "copy of data" step. See
  Architecture.md for how each is addressed.
- Technology stack reconsidered and confirmed: Python (language) and Streamlit
  Community Cloud (deployment) — both explicitly re-evaluated against
  alternatives (a language rewrite, Render, Hostinger VPS) and kept as-is, with
  the reasoning recorded in Architecture.md so a future session doesn't
  re-litigate it from scratch.

No code written yet — this phase was documentation/architecture only.

## Phase 1 — Standard project docs ✅ DONE (2026-08-02)

Added the 6 standard root-level docs (`PRD.md`, `Architecture.md`, `rules.md`,
`Phase.md`, `design.md`, `memory.md`), matching the pattern established in the
sibling bots, per the user's standing preference for every project repo to
carry these. Local git repo initialized (`git init`), no commits made yet.

## Phase 2 — Open design questions ✅ DONE (2026-08-02)

Both resolved (see Architecture.md's "Design questions — resolved"):
1. "Subh 30 min" checkpoints are 09:20/09:25/09:30, each evaluated at
   09:21/09:26/09:31 (1 min after candle close).
2. Trailing exit is BOTH EMA9-close-below and ATR-pullback, as separate
   variants (`*_trailing_ema`, `*_trailing_atr`) — no separate "fixed exit"
   variant exists (fixed % is only the trailing-activation trigger, same as
   the sibling v2 bot). 4 variants per universe-bot, 12 total. An earlier pass
   at this doc briefly miscounted this as 6/18 (treating "fixed" as a third
   exit-style); corrected the same day across PRD.md and Architecture.md.

## Phase 3 — Core implementation ✅ DONE (2026-08-03)

**Built and passing 26 unit/integration tests** (`venv/`, `pytest tests/ -v`):
- `common/helpers.py`, `common/indicators.py` — ported unchanged from bot-v3
  (added `atr()`, needed for the ATR-trailing variants, not present in the
  sibling's indicators.py).
- `engine/strategy.py` — ported unchanged from v1 (`check_entry`, arm-cycle
  dedup, freshness guard). Verified with a synthetic breakout dataset:
  correctly fires True exactly on the breakout bar, False one bar earlier,
  and correctly blocks a repeat on the same `arm_cycle_id`.
- `engine/costs.py` — ported unchanged from the sibling intraday bots
  (MIS/intraday charge model, not bot-v3/swing-bot's delivery model).
- `engine/config.py` — new: `UNIVERSE_BOTS` (751/551/400), `VARIANTS` (4, no
  `_fixed`), `SUBH30_CHECKPOINTS`, settings defaults. Tested: `all_variant_ids()`
  produces exactly the 12 expected `{universe}/{variant}` ids, no `vN`-style
  leftovers.
- `engine/rate_limiter.py` — new: `AccountRateLimiter`. Tested under REAL
  concurrent load (10 threads hammering one instance) to confirm the min-
  interval guarantee holds, and that two independent instances never block
  each other.
- `engine/broker_accounts.py` — new: `AngelOneAccount`, `GrowwAccount`, account
  registry from numbered env vars. **Not live-tested against real credentials**
  (none were available while writing this) — Groww's exact request/response
  shapes are best-effort from documentation research, flagged in the module's
  own docstring for verification once real accounts are wired up (Phase 5).
- `engine/nse_universe.py` — ported the index-CSV-fetch mechanics, added
  `filter_to_universe()` (Stage 1's per-universe filter + subset-safety
  warning). Tested: correctly excludes non-universe symbols even when they'd
  otherwise rank highest, and surfaces missing-symbol warnings instead of
  silently dropping them.
- `engine/stage1_ranking.py` — new. Tested: dedup+sort correctness,
  chunk-level fallback retries ONLY the missing symbols (not the whole
  chunk/list), and warnings surface when fallback also fails.
- `engine/stage2_candles.py` — new. Tested: `merge_unique_symbols` actually
  dedupes overlapping top-50s, per-symbol fallback triggers only for the
  symbol that failed.
- `engine/broker.py` — ported unchanged (position sizing/entry/exit), adapted
  to `variant_id` naming.
- `engine/db.py` — new: 12 variant-tagged trade tables (name-mangled from
  `{universe}/{variant}` to `ute_trades_{universe}__{variant}` since SQL table
  names can't contain `/`). Tested: full open→mark-target-hit→close round trip,
  capital isolation between variants, checkpoint tracking isolation between
  universe-bots.
- `engine/variant_engine.py` — new: subh30 checkpoint gating (09:20/25/30,
  checked at 09:21/26/31, 10-min grace) and both trailing-exit mechanisms
  (EMA9-close-below, ATR-pullback), ported from the sibling v2 bot's logic.
  Tested: checkpoint due/not-due/grace-expired/already-used/next-checkpoint
  transitions, all pass.
- `engine/scheduler.py` — new: the two-job split (fast position management +
  5-min-boundary-aligned entry scan) discussed and locked in during Phase 0 —
  see Architecture.md. `run_full_scan_cycle`/`run_position_management_cycle`
  are the orchestration entry points APScheduler calls.
- **Synthetic end-to-end test** (mirroring the sibling bots' own Phase 0
  verification style): a full `run_full_scan_cycle()` run against fully-mocked
  broker accounts (no real network calls) confirms Stage 1 → per-universe
  filter → Stage 2 → all 12 variants' entry scan wires together correctly, and
  that a deliberately-planted signal on one symbol gets entered independently
  by every one of the 12 variants, each recorded in its own DB row.

**Dashboard built and live-verified 2026-08-03** (via the browser preview
tool, not just pytest): `app.py` (admin), `viewer_app.py` (read-only),
`engine/dashboard_view.py`, `engine/metrics.py`, `common/metrics.py` — ported
the sibling bots' metrics formulas unchanged. Verified live at
http://localhost:8512 (admin, `.claude/launch.json` entry `ute-admin`) and
:8513 (viewer, `ute-viewer`):
- Sidebar's 2-level selector (universe-bot dropdown, then a 4-way variant
  radio) correctly switches the main panel between all 12 variants.
- Clicking **Run Scan Now** actually executes Stage 1 → per-universe filter →
  Stage 2 → all 12 variants' scan, end to end, against the real (no-credentials)
  `broker_accounts` registry — confirmed the warning banner correctly surfaces
  BOTH "no broker accounts configured" AND the resulting subset-safety
  warnings (since an empty Stage 1 rank list makes every universe symbol
  "missing" by definition) instead of hiding either.
- Account-health panel correctly showed all 4 broker-account slots as
  "⚪ Not set up" (no credentials in this environment).
- Latest Cycle Log grid populated with the real row written by the click
  above.
- Viewer app loads independently on its own port, reads the same local
  SQLite file (same behavior as the sibling bots' local dev setup), shows the
  same warning banner, has no sidebar controls beyond the variant selector.

`config/settings.yaml` still doesn't exist (fine — `config.load_settings()`
falls back to `DEFAULTS`); saving settings from the sidebar will create it.

## Phase 4 — Local verification ✅ DONE (2026-08-03)

**42 tests passing** (`pytest tests/ -v`), covering everything Phase 3 built:
rate-limiter concurrency, Stage 1/2 dedup+fallback logic, subset-filter
correctness, DB round-trips/isolation, checkpoint-timing gating, one
synthetic end-to-end cycle, **and the two gaps closed today**:
- `test_manage_open_position.py` (10 tests) — both trailing mechanisms
  (`check_ema9_trail_exit`, `check_atr_trail_exit`) standalone, plus the full
  `manage_open_position` flow: stop-loss exit, target-hit → trailing flip,
  the hard floor (a trail signal below the original target gets clamped up to
  it, never letting a winner round-trip into a smaller win than target),
  square-off after hours, and the no-account-configured hold path.
- `test_scheduler.py` (6 tests) — a REAL `BackgroundScheduler` instance (not
  just calling `run_*_cycle()` directly): confirms the position-management
  job is a plain `IntervalTrigger` while the entry-scan job is a `CronTrigger`
  at the 5-min-boundary+1 offsets (1,6,11...56) rather than a bare `*/5` —
  protects the exact design decision Architecture.md's scheduler docstring
  warns not to regress ("do NOT collapse these back into one job"). Also
  covers: restarting doesn't duplicate jobs, stop actually stops, wake/sleep
  window and market-hours detection.

Also live-verified via the browser preview tool (not just pytest) — see
Phase 3's dashboard section above. Nothing outstanding in this phase.

## Phase 3.5 — UI design ✅ DONE (2026-08-04)

Not in the original phase plan — added after the user asked for the
dashboard's design to be done properly, in Figma. Built a design-token
collection + full mockup in a new Figma file
([link](https://www.figma.com/design/GdNyRjMeqmv2NAQFO5JoIu)), then shipped
it as `.streamlit/config.toml` + `dashboard_view.inject_custom_css()` +
2 rebuilt components (account chips, open-position card). Live-verified via
computed-style checks against the running app, not just visual inspection.
See design.md for the full token table and memory.md for the build log
(including a Figma `createAutoLayout()` default-fill gotcha hit and fixed
along the way). All 42 tests still pass after this change.

## Phase 5 — Deployment 🟡 IN PROGRESS

- GitHub repo: ✅ DONE (2026-08-05) — [github.com/humhai-JAAT/unified-trading-engine](https://github.com/humhai-JAAT/unified-trading-engine),
  created and pushed via `gh repo create ... --push` (GitHub CLI installed +
  authenticated same session). Initial commit: 36 files, all Phase 3/4 code +
  the Figma-designed UI.
- Broker account count: ✅ DECIDED (2026-08-08) — 1 Groww (paid, primary) + 1
  Angel One (free, fallback), down from the original 2+2 plan. See
  Architecture.md's "Multi-broker / multi-account setup" for the reasoning.
- Supabase project: ✅ DONE (2026-08-09) — `unified-trading-engine` project
  created via the Supabase MCP connector (org "PARIHAR GROUP", region
  `ap-south-1`, free tier, project ref `oosqmkeucbrziplxopyg`). Schema applied
  via `apply_migration` using the exact SQL generated from `engine/db.py`'s own
  `POSTGRES_SCHEMA` (not hand-transcribed) — all 14 tables live (12 variant
  trade tables + `ute_cycle_log` + `ute_checkpoint_log`), 0 rows. RLS enabled
  on all 14 (no policies needed — the app connects via `DATABASE_URL`
  directly, which bypasses RLS; this just closes anon/authenticated access via
  the REST API). Still waiting on the user to fetch the Transaction Pooler
  connection string from the dashboard (Settings → Database) — DB password
  isn't retrievable via MCP — before `DATABASE_URL` can be wired into
  Streamlit Cloud Secrets.
- Broker credentials: 1 Groww + 1 Angel One accounts' API keys/secrets need to
  be configured in Streamlit Cloud Secrets.
- Streamlit Cloud deployment: admin + viewer apps.
- Keep-awake automation, ported from the sibling bots' pattern.

## Phase 6 — Live verification ⬜ NOT STARTED

A genuine in-market-hours run confirming: Stage 1's 6-worker parallel fetch
completes within budget, chunk-level fallback actually triggers and recovers
correctly on a simulated failure, Stage 2's dedup measurably reduces candle
fetches vs the sibling bots' baseline, and all 12 variants scan/trade/exit
correctly off the shared data.
