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
  Streamlit Cloud Secrets. **DATABASE_URL now received and live-verified**
  (2026-08-09) — connected via the project's own `engine.db.init_db()` +
  `get_engine()`, confirmed all 14 `ute_*` tables visible through the pooled
  connection, not just via the MCP connector.
- Broker credentials: 🟡 IN PROGRESS (2026-08-09) — both accounts' credentials
  received and locally wired into `.streamlit/secrets.toml` (gitignored, not
  committed). Live-verified against real accounts for the first time (closes
  the "not live-tested" caveat both `broker_accounts.py`'s docstring and this
  file's Phase 3 notes flagged since 2026-08-03):
  - **Angel One: ✅ fully working.** Login, quote-fetch (3/3 symbols), and
    candle-fetch (365 5-min candles) all confirmed live. Found and fixed a
    real bug in the process — `AngelOneAccount._headers()` never included the
    `Authorization: Bearer <jwt>` header, so every post-login secure call
    (quotes, candles) failed with "Token missing" even though login itself
    succeeded. Fixed by adding the header when `self._jwt_token` is set. All
    42 tests still pass after the fix.
  - **Groww: ✅ fully working (2026-08-09, after several rounds of live
    bug-fixing).** `growwapi` added to venv (was in `requirements.txt`,
    never installed). Bugs found and fixed, in the order hit:
    1. `_get_client()` called `GrowwAPI(api_key, api_secret)`; the real SDK
       constructor is `GrowwAPI(token: str)` — a single session token.
       Real flow: `GrowwAPI.get_access_token(api_key, secret=api_secret)` →
       `GrowwAPI(token)`.
    2. That token's expiry is **not** an N-hours-from-issuance TTL — decoding
       its `exp` claim showed a token minted at 00:55 IST and the daily
       cutoff both land on a **fixed 06:00:00 IST wall-clock expiry**,
       confirmed by the user independently ("Groww API deactivates every day
       at 6am"). Replaced the initial (wrong) 4h-TTL guess with
       `_jwt_exp_timestamp()`, which decodes the token's own `exp` claim —
       robust regardless of what time of day it was issued.
    3. Even after auth worked, `get_ohlc`/candle calls returned "Access
       forbidden" — the account had no Market/Live Data API subscription.
       **User purchased it** (₹499+GST/month, was already the accepted-cost
       assumption in PRD.md) — quotes started working immediately after.
    4. Candles still failed post-subscription, 3 more bugs: `candle_interval`
       needs a string like `"5minute"` (SDK's `CANDLE_INTERVAL_MIN_5`), not a
       bare int; `groww_symbol` needs `"NSE-RELIANCE"` (hyphen) — a
       **different** format than `get_ohlc`'s `"NSE_RELIANCE"` (underscore);
       and the response has **7** fields per candle row (an undocumented
       trailing field, always `None` in testing) with an ISO-string
       Datetime already in IST, not the 6-column unix-epoch shape the old
       code assumed. All fixed and live-verified: 3/3 quotes, 395 5-min
       candles.
    Separately, the user flagged that Groww's **secret-based** auth flow
    requires manually re-approving the app in the Groww console every single
    day (by design, security measure) — recommended switching to Groww's
    **TOTP-based** key option instead. ~~Not yet done~~ — **DONE 2026-08-10**:
    the user initially found conflicting info suggesting Groww had no TOTP
    option; verified against Groww's own official docs
    (groww.in/trade-api/docs/python-sdk) and the installed SDK's actual
    `get_access_token()` implementation (a real `totp` code path hitting
    `https://api.groww.in/v1/token/api/access`, not a vestigial parameter) —
    TOTP is real and Groww-recommended ("No Expiry" per their docs, vs. daily
    approval for secret-based keys). User generated a TOTP-based key via
    Groww's console. `GrowwAccount` now supports both flows (`totp_secret`
    preferred, `api_secret` kept only for backward compatibility) — same
    `pyotp.TOTP(secret).now()` pattern `AngelOneAccount` already used.
    Live-verified: 3/3 quotes, 394 candles, zero manual steps. The exchanged
    access token itself still expires at the same fixed 06:00:00 IST cutoff
    (that part was never the problem — already handled by
    `_jwt_exp_timestamp()`) — it's specifically the TOTP *key* that never
    expires and needs no human approval, unlike the old secret-based key.
- Streamlit Cloud deployment: admin + viewer apps.
- Keep-awake automation, ported from the sibling bots' pattern.

## Phase 5.5 — Live position monitoring via websocket ✅ DONE (2026-08-10)

Not in the original phase plan — added after the user asked to switch open-
position monitoring from the 2-min REST poll to Groww's websocket (`GrowwFeed`)
for faster stop-loss/target/trailing-exit reaction. See Architecture.md's
"Live position monitoring" section for the full design and the websocket
capability research that shaped its scope (subscribing to Groww's live feed
hits no batch-size limit even at the full 751-stock universe, but per-symbol
data only arrives once that symbol actually trades — ~66% coverage at 10s,
~92% at 60s in a live 2026-08-10 test — and historical OHLCV is never
available over this feed at all, only live LTP).

Built:
- `GrowwAccount._load_symbol_to_token()`/`get_client()` (`engine/broker_accounts.py`)
  — 7-day-cached symbol→exchange_token lookup (mirrors `AngelOneAccount`'s
  existing pattern) and a public accessor for the live-feed thread to reuse
  the account's own authenticated session instead of duplicating auth.
- `engine/live_feed.py` (new) — a daemon background thread, started/stopped
  alongside `engine.scheduler.start_scheduler()`/`stop_scheduler()`. Every
  ~2s: re-syncs the websocket subscription to whichever symbols currently
  have an open position (at most 12, never the full universe), reads live
  LTPs, and runs each through `variant_engine.locked_decide_and_exit()` — the
  SAME decision function the REST path uses now (see below), via a synthetic
  single-row OHLC "candle" built from the tick.
- `variant_engine.manage_open_position()` refactored: extracted
  `locked_decide_and_exit()` as a shared entry point wrapped in
  `db.acquire_trade_lock()` (existed in `db.py` since Phase 3, never actually
  called anywhere until now — a real gap, harmless while only one path could
  exit a trade, not harmless once two concurrent paths can). Re-checks the
  trade is still open under the lock before acting, so the REST job and the
  live-feed thread can never double-exit the same position. No-ops on SQLite.
- 7 new tests (`tests/test_live_feed.py`): fail-closed no-Groww-account
  contract, subscription-diffing (add/remove delta only, unknown-symbol
  skip), tick→synthetic-candle shape, exception-swallowing. Plus 4 existing
  `test_manage_open_position.py` tests updated to mock `db.get_open_trade`
  (now required by the new re-check-under-lock behavior). **49/49 tests
  pass.**

**Live-verified end-to-end 2026-08-10** (market hours): real Groww account,
isolated local SQLite (production Supabase untouched) — a fake open RELIANCE
position picked up a live tick, updated `peak_price`, and flipped
`target_hit` within **~6 seconds**, vs. up to 2 minutes via REST alone.
`live_feed.start()`/`stop()` lifecycle also verified against the real
account — `stop()` is best-effort (the underlying `GrowwFeed` connect isn't
cancellable mid-retry; the thread is a daemon so it can never outlive the
process regardless).

**Not done**: Stage 1's ranking fetch stays REST (websocket can't give
history, and the ~66%-at-10s coverage gap makes it unsuitable as a drop-in
replacement for "every symbol's price right now" — see Architecture.md).
~~Groww's secret-based auth still needs daily manual re-approval~~ — DONE
2026-08-10, see Phase 5's Groww section: switched to TOTP.

**Code review 2026-08-10, two findings fixed same day** (see memory.md for
the full review): no critical/security issues, but two real production-hours
concerns —
- `db.acquire_trade_lock()` used a single GLOBAL advisory-lock key for all 12
  variants — meant one variant's trailing-exit REST candle fetch (which
  happens WHILE the lock is held) blocked every OTHER variant's exit-check
  too, even though only same-variant calls can ever actually race. Changed to
  `acquire_trade_lock(variant_id)`, keyed by `zlib.crc32(variant_id)` — live-
  verified against real Postgres (two different variant_ids' locks don't
  block each other; same-variant mutual exclusion still holds).
- `live_feed._sync_subscriptions()` called `account._load_symbol_to_token()`
  on every ~2s poll — that function re-reads+parses its ~4k-row instrument-
  master CSV from disk every call (its own cache is file-based only). Added
  an in-memory cache (`_get_symbol_to_token()`, 1h TTL) inside `live_feed.py`
  to stop the redundant disk I/O within a thread's lifetime.
- Also addressed: removed an unused `_INSTRUMENT_FIELDS` constant, added
  `tests/test_broker_accounts.py` (4 tests) covering the TOTP-vs-secret
  branch in `GrowwAccount._get_client()` — flagged as untested despite being
  auth-critical.
- **Remaining 2 of the review's 6 findings, closed same day**: (1)
  `_subscribed_tokens` (module-global, read/written by both the main thread's
  `stop()` and the background thread's `_sync_subscriptions()`) now guarded
  by a `threading.Lock` — bracketed around the dict read/write only, never
  the `subscribe_ltp`/`unsubscribe_ltp` network calls, same "don't hold a
  lock across I/O" principle as the advisory-lock fix above. (2) Documented
  (module docstring) that this thread checks price AT POLL TIME, not every
  tick since the last poll — a stop-loss/target touched-and-recovered within
  one `poll_interval_seconds` window (default 2s) can be missed here,
  caught retroactively by the 2-min REST job's per-candle scan instead. Not
  a code fix (there wasn't a bug to fix, just an easy-to-misread guarantee)
  — just made explicit so a future reader doesn't assume stronger protection
  than what actually exists. **55/55 tests pass.**

## Streamlit Cloud deployment ✅ DONE (2026-08-10)

Both apps live: [unified-trading-engine.streamlit.app](https://unified-trading-engine.streamlit.app)
(admin) and [unified-trading-engine-viewer-app.streamlit.app](https://unified-trading-engine-viewer-app.streamlit.app)
(viewer). `runtime.txt` (`python-3.12`) added to pin the Cloud runtime to
match local dev. Admin verified live by the user: scheduler running, Angel
One #1 + Groww #1 both showing healthy (green), #2 slots correctly showing
unconfigured, bot correctly "Asleep" outside the 09:00–16:00 IST wake window.

**Found and fixed a real design gap while verifying the viewer app**: it let
any visitor pick any of the 12 variants via the same 2-level selector the
admin app uses — never the intent (`config.py`'s `public_variant` DEFAULTS
key existed since Phase 3 specifically for this, but was never wired up).
Fixed: `app.py` gained a "🌐 Public Viewer" sidebar control (persists via
`config.save_settings()`) letting the admin pick exactly one variant (or
none) to expose; `viewer_app.py`'s selector was removed entirely — it now
just reads `settings["public_variant"]` and renders that one variant, with
an info message if none is set. Verified working locally (localhost:8512)
by the user before push. 55/55 tests still pass (no test coverage added for
this — both files are Streamlit UI scripts, not covered by the existing
pytest suite, matching how the rest of app.py/viewer_app.py's UI code
already wasn't unit-tested).

## Keep-awake automation ✅ DONE (2026-08-10)

Ported directly from the sibling bots' proven pattern (`bot-v3`/
`intraday-trading-bot`/`intraday-trading-bot-v2`/`swing-trading-bot` all have
an identical copy) — `.github/workflows/keep-awake.yml` pings the ADMIN app
3x/day (08:50/16:50/00:50 IST, ~8h spacing to stay under Streamlit Cloud's
12h sleep threshold) via `scripts/wake_streamlit.py`, a Playwright-driven
headless-browser script — plain HTTP GET doesn't work (verified in the
sibling repos: hits an infinite redirect loop), Streamlit only serves real
content over a JS-driven WebSocket and a sleeping app's wake screen needs a
real click. Only the admin app is pinged, not the viewer — the viewer is
read-only display, sleeping doesn't affect trading, just costs a visitor a
cold-start delay if they open it while asleep. `APP_URL` filled in directly
with the real deployed URL (unlike bot-v3/swing-trading-bot's copies, which
still have unfilled `REPLACE-WITH-...` placeholders — this project's actually
live).

**Live-verified locally** (installed `playwright` + chromium in the venv just
for this test, not added to `requirements.txt` — it's a CI-only dependency,
would bloat the Cloud deploy for no reason): ran
`scripts/wake_streamlit.py` against the real deployed admin URL, correctly
detected it as already awake and exited 0. Confirms the detection logic
works; the actual "wake a sleeping app" branch will get its first real
exercise on the next scheduled GitHub Actions run (or `workflow_dispatch`).

**This closes out Phase 5 entirely** — GitHub repo, broker setup, Supabase,
both Streamlit Cloud apps, and keep-awake are all done. Only Phase 6 (live
verification) remains.

## Phase 6 — Live verification 🟡 IN PROGRESS (started 2026-08-11)

**First real in-market-hours session, 2026-08-11.** User noticed the admin
dashboard showing "No entry-scan cycle has run yet" ~37 minutes into market
hours and asked for it to be investigated. Found and fixed 2 real production
bugs, both confirmed against the live Supabase Postgres DB (real broker
accounts, real market data, no fake/local DB used for this investigation):

1. **Crash left zero cycle-log trace.** A real trade (POLICYBZR) had already
   been opened in 2 variants (`bot_751/puradin_trailing_ema`,
   `bot_751/puradin_trailing_atr`, entries at 09:52:17/09:52:47) but
   `ute_cycle_log` had ZERO `entry_scan` rows — only `position_management`
   rows existed. Root cause: `run_full_scan_cycle()` had no crash-handling —
   an exception anywhere after Stage 1/2 (real per-DB-write trade entries
   already committed by then) would abort the function before it ever
   reached its own `db.log_cycle(status="OK", ...)` call at the end, leaving
   the dashboard's cycle log silently blank despite live trading having
   happened. Could not reproduce the exact original crash (a manual re-run
   completed cleanly), so root-caused via the SYMPTOM, not a captured
   traceback. **Fixed**: wrapped the cycle body in try/except,
   `db.log_cycle(status="ERROR", stage="entry_scan", error=str(e))` before
   re-raising — a crash is now always visible in the dashboard.
2. **Overlapping cycles.** While investigating #1, a manually-triggered
   `run_full_scan_cycle()` call overlapped with the Cloud app's own
   scheduled cron run (a scan cycle can take minutes of real network I/O,
   easily overlapping a manual "Run Scan Now" click or a slow-running
   scheduled instance). Observed: the cycle-log's `entered=[...]` list
   incorrectly included 2 variants that were NOT newly entered by that
   cycle (they already had open positions from an earlier cycle) — verified
   via direct DB query that no duplicate trade row was actually created
   (`was_flat` still correctly gated the real write), so this wasn't a
   capital-correctness bug, but cycle logs became misleading and Stage 1/2
   API load doubled up needlessly. **Fixed**: `db.try_acquire_scan_lock()`
   (new, non-blocking `pg_try_advisory_lock`, distinct key from the
   per-variant exit lock) — a second overlapping call now skips cleanly
   (`status: "skipped"`, logged as `SKIPPED`) instead of racing.

Both fixes live-verified directly against production Postgres before being
committed: the lock was acquired/blocked/released correctly across 3
sequential attempts, and a simulated crash correctly produced an `ERROR`
cycle_log row. 2 new regression tests added
(`tests/test_end_to_end_cycle.py`, SQLite-backed) — **57/57 tests pass**.

Also confirmed working correctly during this session: position-management
job firing reliably every ~2 min; Stage 1 fetching all 751 symbols (2 benign
`DUMMYINXGN`/`DUMMYTRVN` placeholder-symbol misses, expected); Stage 2
dedup (85 requested → 83 fetched, 2 real-symbol Angel One fallback misses).

**Still open for Phase 6**: Stage 1's 6-worker parallel fetch timing budget,
chunk-level fallback triggering under a REAL failure (not just the
already-observed DUMMY-symbol case), Stage 2's dedup savings measured
against the sibling bots' baseline, and a full trade lifecycle (entry →
target-hit → trailing-exit or stop-loss → close) observed live end-to-end —
today only got as far as open entries, no exits observed yet.

**Same day, 3 more issues reported by the user after checking the deployed
apps — all fixed:**

1. **`public_variant` never reached the viewer app.** The "🌐 Public Viewer"
   control (built earlier today) wrote to `engine.config`'s `settings.yaml`
   — a LOCAL FILE. Root cause: admin (`app.py`) and viewer (`viewer_app.py`)
   are TWO SEPARATE Streamlit Cloud deployments, each with its OWN isolated
   filesystem — a value admin saved was invisible to viewer's container.
   **Fixed**: new `ute_settings` key/value table (Postgres/SQLite, via
   `db.get_setting()`/`db.set_setting()`) — the one thing both apps actually
   share. Both `app.py` and `viewer_app.py` switched to it. Live-verified
   against real Postgres: table auto-created via `init_db()`, set/get/update
   round-trip all correct.
2. **Open-position card showed no live tracking** — only static entry-time
   fields (Symbol, Entry, Mode, Qty, Capital, Entered). Compared against the
   sibling bots (`bot-v3/botv3/dashboard_view.py`), which fetch a live quote
   and show Current price + Unrealized P&L — this project's card never had
   that. **Fixed**: `render_variant_panel()` now fetches a live quote (fails
   closed to the entry price if the fetch errors) and shows Current price,
   Unrealized P&L (₹ and %), and — unique to this project — Peak/Trough
   (already tracked on the trade row for the trailing-exit hard floor, just
   never displayed). Live-verified against the real open POLICYBZR position:
   entry ₹1660 vs a live ₹1630 quote → correctly showed -1.80% unrealized.
3. **"Why does position-management only run every 2 min?"** — clarified,
   not a bug: that's the REST safety-net's designed interval (unrelated to
   `engine/live_feed.py`'s ~2-SECOND websocket tick loop built yesterday,
   which reacts far faster for open positions when a Groww account is
   configured — as it is here). No code change.

3 new regression tests (`tests/test_db_and_config.py`, SQLite-backed) for
`get_setting`/`set_setting`. **60/60 tests pass.**
