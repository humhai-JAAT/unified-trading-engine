# Codebase Reference — Unified Trading Engine

A complete, file-by-file, function-by-function catalog of everything built in
this project. Written 2026-08-13, current as of commit `be8923c`.

**How this differs from the other root docs**: `Architecture.md` explains
*why* things are designed the way they are (the 2-stage pipeline, the
thread-safety model, the broker fallback chain). `PRD.md` explains the
product intent. This document is the *what and where* — a map of every
source file and every function in it, so you can find the right place to
make a change without re-reading the whole codebase. `Phase.md`/`memory.md`
are the running history of how it got here.

**Scope note on tests**: `tests/` is documented per-file (what each file
covers) rather than per-test-function — there are ~100 individual test
functions across 12,000+ lines; listing each would bloat this doc without
adding much you can't get from the file's own name and a quick read.

---

## Root files

### `app.py` — Admin dashboard (Streamlit)
The main, full-control dashboard. Loads secrets into `os.environ` (`DATABASE_URL`,
`ANGELONE_{1,2}_*`, `GROWW_{1,2}_*`), then renders:
- **Sidebar**: Start/Stop scheduler buttons, manual "Run Position Mgmt Now" /
  "Run Scan Now" triggers, bot awake/asleep + market-open/closed status, a
  2-level universe-bot/variant selector for the main panel, the "🌐 Public
  Viewer" control (writes to `db.set_setting("public_variant", ...)` — the
  only channel that reaches the separate viewer app), a Strategy Settings
  expander (capital, pool size, target/SL %, ATR params, wake/sleep/square-off
  times — writes to `config/settings.yaml` via `config.save_settings()`), and
  a Danger Zone (`db.reset_all_data()`, checkbox-gated).
- **Main panel**: `live_panel()` (an `@st.fragment` that auto-refreshes every
  15s while any variant has an open position) renders the warning banner,
  account-health chips, and the selected variant's full panel
  (`dashboard_view.render_variant_panel(..., show_force_exit=True)`).
- Below the fragment: the latest 20 rows of `ute_cycle_log` as a raw table.

### `viewer_app.py` — Read-only viewer dashboard (Streamlit, separate deployment)
Deployed as its own Streamlit Cloud app (same repo, different entry point).
No sidebar, no controls. Reads `db.get_setting("public_variant")` and renders
exactly that one variant via the same `dashboard_view.render_variant_panel(...,
show_force_exit=False)`. If nothing is set, shows "No variant is currently
public." Needs its own `DATABASE_URL` secret (same value as `app.py`'s) since
it's a physically separate container/filesystem.

### `requirements.txt`
`streamlit`, `pandas`, `numpy`, `APScheduler`, `PyYAML`, `plotly`, `pytz`,
`pyarrow`, `requests`, `pyotp`, `sqlalchemy`, `psycopg2-binary`, `pytest`,
`growwapi`. (Playwright, used only by `scripts/wake_streamlit.py` in CI, is
deliberately **not** in here — it's a CI-only dependency, would bloat the
Cloud deploy for no runtime benefit.)

### `runtime.txt`
Pins the Streamlit Cloud Python runtime to `python-3.12`, matching local dev.

### `config/settings.yaml`
Not checked in with real values — created on first "💾 Save Settings" click
in the admin sidebar. Falls back to `engine.config.DEFAULTS` if absent.

### `.streamlit/config.toml`
Streamlit's own `[theme]` block — sets the base dark palette so native
widgets (inputs, radios, sidebar) match the Figma-designed dark theme without
needing custom CSS for everything.

### `.streamlit/secrets.toml` (gitignored)
Local-only: `DATABASE_URL`, `ANGELONE_{1,2}_*`, `GROWW_{1,2}_*`. Mirrored as
Streamlit Cloud "Secrets" in the deployed apps.

### `.github/workflows/keep-awake.yml`
Pings the **admin** app 3x/day (08:50/16:50/00:50 IST) via
`scripts/wake_streamlit.py` to stay under Streamlit Cloud's ~12h sleep
threshold. Only the admin app — the viewer is read-only display, a cold
start there just costs a visitor a delay, not a trading gap.

### `PRD.md`, `Architecture.md`, `rules.md`, `Phase.md`, `design.md`, `memory.md`
The 6 standard project docs (this project's own convention, per
`standard_project_docs` in the assistant's memory system) — product intent,
design rationale, hard rules never to violate, phase-by-phase build log,
UI/UX design tokens, and the free-form running narrative memory,
respectively.

---

## `common/` — duplicated-on-purpose primitives (never imported cross-project, see `rules.md`)

### `common/helpers.py`
- `PROJECT_ROOT` — `Path`, the repo root (parent of `common/`).
- `format_currency(value)` → `"₹1,234.56"` style string.
- `format_pct(value)` → `"+1.23%"` style string (always signed).
- `get_logger(name)` → a configured `logging.Logger` (timestamped,
  `[LEVEL] name: message` format), handler attached only once per name.

### `common/indicators.py`
- `ema(series, period)` — exponential moving average (`ewm(span=period, adjust=False)`).
- `sma(series, period)` — simple moving average (rolling mean).
- `macd(series, fast=12, slow=26, signal=9)` → DataFrame with `macd`,
  `macd_signal`, `macd_hist` columns.
- `atr(df, period=14)` — Average True Range from `High`/`Low`/`Close` columns,
  EMA-smoothed true range.

### `common/metrics.py`
Portfolio-level math, operates on a plain `pnl: pd.Series`:
- `profit_factor(pnl)` — gross profit / gross loss (`inf` if no losses and
  some profit, `0.0` if neither).
- `win_rate(pnl)` — % of trades with positive P&L.
- `max_drawdown(cum_pnl)` — most negative (cumulative − running-max) value.
- `avg_win_loss(pnl)` → `(avg_win, avg_loss)` tuple.
- `expectancy(pnl)` — `win_rate * avg_win + (1 - win_rate) * avg_loss`.
- `compute_portfolio_metrics(closed_trades)` — the one-stop dict (`total_trades`,
  `win_rate`, `profit_factor`, `max_drawdown`, `avg_win`, `avg_loss`,
  `expectancy`, `total_pnl`) that everything else composes from. Expects a
  `pnl` column.

---

## `engine/` — the actual bot

### `engine/config.py` — the structural source of truth
- `UNIVERSE_BOTS` — the 2 universe-bots: `bot_300` (Nifty500−Nifty200, ~300
  stocks) and `bot_400` (Nifty500−Nifty100, ~400 stocks, the shared Stage-1
  superset). `bot_300` is always a strict subset of `bot_400`.
- `VARIANTS` — the 4 entry-timing × trailing-exit combos:
  `subh30_trailing_ema`, `subh30_trailing_atr`, `puradin_trailing_ema`,
  `puradin_trailing_atr`.
- `all_variant_ids()` → the 8 `"{universe_bot}/{variant_key}"` strings
  (2×4), purely derived — shrinks/grows automatically if `UNIVERSE_BOTS` or
  `VARIANTS` ever change.
- `SUBH30_CHECKPOINTS` = `["09:20", "09:25", "09:30"]`,
  `SUBH30_CHECKPOINT_GRACE_MINUTES` = `10`.
- `DEFAULTS` — the full tunable-settings dict (capital, leverage, target/SL %,
  wake/sleep/square-off times, pool size, scan intervals, ATR params,
  `public_variant`).
- `load_settings()` — `DEFAULTS` merged with `config/settings.yaml` if it exists.
- `save_settings(settings)` — writes `settings.yaml`.

### `engine/nse_universe.py` — index-membership lookups (no live market data)
- `_fetch_index_symbols(index_key, force_refresh=False)` — downloads/caches
  (7-day TTL, disk CSV) one Nifty index's constituent list from
  niftyindices.com (`nifty100`/`nifty200`/`nifty500`).
- `get_universe_symbols(universe, force_refresh=False)` → `set[str]` — resolves
  `"n500_minus_200"` or `"n500_minus_100"` to its symbol set via index
  set-difference.
- `filter_to_universe(rank_list, universe, top_n, force_refresh=False)` →
  `(top_n_dataframe, missing_symbols)` — Stage 1's per-universe filter step
  (no network call), plus the subset-safety check (symbols that *should* be
  in the universe but weren't found in the shared rank list, usually an
  index-CSV cache-timing mismatch around a rebalance).

### `engine/broker_accounts.py` — multi-broker, multi-account registry
- `_jwt_exp_timestamp(token)` — decodes a JWT's `exp` claim without signature
  verification (only need to know our own token's expiry).
- `QuoteResult` (dataclass) — `symbol`, `last_price`, `pct_change`.
- `BrokerAccount` (base class) — `is_configured()`, `fetch_quotes_batch(symbols)`,
  `fetch_candles(symbol, interval, period_days)`; owns its own
  `quote_limiter`/`candle_limiter` (`AccountRateLimiter` instances).
- `AngelOneAccount(BrokerAccount)`:
  - `_login()` — TOTP password login, caches JWT ~2h.
  - `_ensure_session()` — re-logs-in if the cached JWT is stale.
  - `_load_symbol_to_token(force_refresh=False)` — 7-day-cached NSE-EQ
    symbol→token map from Angel One's instrument master JSON.
  - `fetch_quotes_batch(symbols)` — batched (50/call) `/market/v1/quote`.
  - `fetch_candles(symbol, interval, period_days)` — `/historical/v1/getCandleData`.
- `GrowwAccount(BrokerAccount)` — the primary broker:
  - `_get_client()` — returns a live `GrowwAPI` instance; tries an in-memory
    client, then a disk-persisted token cache
    (`data/groww_token_cache_{account_id}.json` — survives process
    restarts, added after the 2026-08-11 rate-limit incident), then a fresh
    `get_access_token()` call (TOTP-based if `totp_secret` set, else
    secret-based). Token expiry is read from the JWT's own `exp` claim
    (Groww's real expiry is a fixed 06:00:00 IST daily cutoff, not
    N-hours-from-issuance).
  - `_load_cached_token()` / `_save_cached_token()` — the disk-cache pair.
  - `get_client()` — public accessor, used by `engine.live_feed` to reuse
    this account's session for `GrowwFeed` instead of re-authenticating.
  - `_load_symbol_to_token(force_refresh=False)` — 7-day-cached NSE/CASH
    `trading_symbol`→`exchange_token` map (needed for `GrowwFeed` subscriptions).
  - `fetch_quotes_batch(symbols)` — batched `get_ohlc()`.
  - `fetch_candles(symbol, interval, period_days)` — `get_historical_candles()`
    (note: `"NSE-{symbol}"` hyphen format here, vs. `"NSE_{symbol}"`
    underscore for `get_ohlc` — a real gotcha found live 2026-08-09).
- `_env(prefix, suffix)` — `os.environ.get(f"{prefix}_{suffix}")`.
- `get_configured_accounts()` → `{"angelone": [...], "groww": [...]}` — builds
  `ANGELONE_1`/`ANGELONE_2`/`GROWW_1`/`GROWW_2` accounts from env vars,
  silently skipping any with incomplete credentials.

### `engine/rate_limiter.py`
- `AccountRateLimiter(min_interval_seconds)` — one instance per broker
  *account* (never global, never per-worker):
  - `wait_for_turn()` — blocks the calling thread until `min_interval_seconds`
    have passed since the last call, under a lock (so concurrent threads can
    never both think it's their turn).
  - `call(fn, *args, **kwargs)` — `wait_for_turn()` then calls `fn`.

### `engine/stage1_ranking.py` — Stage 1: shared ranking fetch
- `Stage1Result` (dataclass) — `rank_list`, `warnings`, `chunks_total`,
  `chunks_fallback_used`, `chunks_failed`.
- `_chunk(symbols, n_chunks)` — splits into `n_chunks` roughly-equal pieces.
- `_build_workers(primary_accounts)` — `WORKERS_PER_ACCOUNT` (3) worker slots
  per configured account.
- `_fetch_chunk(account, chunk)` — one account's `fetch_quotes_batch` call.
- `fetch_ranking_data(symbols, fallback_accounts=None, primary_accounts=None)`
  → `Stage1Result` — parallel chunked fetch across primary accounts
  (default: configured Groww), chunk-level fallback (default: configured
  Angel One) retrying only the *missing* symbols from a partially-failed
  chunk, never the whole chunk. Dedupes + sorts the final rank list by
  `pct_change` descending.

### `engine/stage2_candles.py` — Stage 2: shared, deduplicated candle fetch
- `Stage2Result` (dataclass) — `candles_by_symbol`, `warnings`,
  `symbols_requested`, `symbols_fetched`.
- `merge_unique_symbols(top_lists)` — union of symbols across all
  universe-bots' top-N lists, order-preserving, deduped.
- `_fetch_one(account, symbol, interval, period_days)` — one account's
  `fetch_candles` call.
- `fetch_candle_history(symbols, interval="5m", period_days=5,
  primary_accounts=None, fallback_accounts=None)` → `Stage2Result` — parallel
  fetch (`CANDLE_WORKERS_PER_ACCOUNT`=3 per account) across primary accounts,
  then a **parallel** (not sequential — fixed 2026-08-11 after a live
  "thundering herd" finding) per-symbol fallback pass for anything that
  failed.

### `engine/strategy.py` — the entry signal (ported Pine Script "EMA-MACD V2.1.2")
- `MIN_BARS_REQUIRED` = 109 (EMA100 + its 9-period SMA warm-up).
- `MAX_ARM_CYCLE_AGE_DAYS` = 3 — how stale the EMA9/30 crossover bar may be
  relative to the trigger bar (added/loosened 2026-08-13, was same-day-only).
- `build_indicators(df)` — computes `ema_fast`/`ema_slow`/`ema_trend`/
  `ema_trend_sma`, `macd`/`macd_signal`, `ema_sep_pct`, the `armed` latch
  (True on bullish EMA9/30 crossover, False on bearish crossunder, `ffill`'d
  between), `arm_cycle_id` (the crossover bar's own timestamp, `ffill`'d),
  and the final `entry_signal` boolean column.
- `EntryCheck` (dataclass) — `signal`, `arm_cycle_id`, `close`, `reason`.
- `check_entry(df, used_arm_cycles=frozenset(), today=None)` → `EntryCheck` —
  evaluates the last completed bar, in order: insufficient history → no
  signal → **signal not fresh** (was also true on the previous bar — blocks
  re-entering an ongoing signal) → **arm cycle stale** (crossover older than
  `MAX_ARM_CYCLE_AGE_DAYS`) → **arm cycle already used** (this exact
  crossover already entered once today, per `used_arm_cycles`) → `"entry"`.

### `engine/variant_engine.py` — per-variant entry-timing + trailing-exit logic
- `next_due_subh30_checkpoint(used, now)` — returns which of
  `09:20`/`09:25`/`09:30` is currently due (1 min after its own candle close,
  stays due for `SUBH30_CHECKPOINT_GRACE_MINUTES`), or `None`.
- `check_ema9_trail_exit(candle_df)` — exit price if the last 5-min close is
  below its own EMA9, else `None`.
- `check_atr_trail_exit(candle_df, peak_price, atr_period, atr_multiplier)` —
  exit price if price has pulled back `>= atr_multiplier * ATR` from peak,
  else `None`.
- `_position_data_accounts()` — Groww then Angel One, in priority order
  (added 2026-08-12 — previously Groww-only with no fallback).
- `manage_open_position(variant_id, variant_cfg, trade, settings, now)` —
  tries each account in order for 1-min candles until one succeeds; on total
  failure returns `{"action": "hold", "reason": "no_price_data", ...}`
  (visible in cycle-log warnings since 2026-08-12). On success, delegates to:
- `locked_decide_and_exit(...)` — the shared entry point for BOTH the 2-min
  REST job and `engine.live_feed`'s tick-driven thread; wraps the decision in
  `db.acquire_trade_lock(variant_id)` and re-checks the trade is still open
  under the lock (prevents double-exit races).
- `_decide_and_exit(...)` — the actual SL/target/trailing/square-off decision:
  checks stop-loss and target against every 1-min bar since entry, flips to
  trailing mode on target hit (`db.mark_target_hit`), checks the trailing
  mechanism (EMA9 or ATR, per `variant_cfg["exit_style"]`) once target is
  hit, applies the hard floor (trailing exit can never go below the original
  target price), and force-exits at square-off time.
- `scan_for_entry(universe_bot_key, variant_cfg, settings, now, top_n_df,
  candles_by_symbol, was_flat)` — the entry-scan path: gates on the subh30
  checkpoint (if applicable) and `was_flat`, then walks `top_n_df`'s symbols
  against `candles_by_symbol` calling `strategy.check_entry`, entering on the
  first signal found via `broker.enter_position`.

### `engine/broker.py` — the paper-trading execution layer
- `available_capital(variant_id, starting_capital)` — `db.get_starting_capital`.
- `enter_position(variant_id, symbol, price, starting_capital, arm_cycle_id,
  leverage=1.0)` — sizes the position (full available capital × leverage,
  floor-divided by price), computes buy-side charges (`costs.calc_charges`),
  writes the open trade (`db.open_trade`).
- `exit_position(variant_id, trade_id, quantity, price, reason)` — computes
  sell-side charges, closes the trade (`db.close_trade`).
- `update_extremes(variant_id, trade_id, current_peak, current_trough, high,
  low)` — `max`/`min` peak/trough, persists only if changed.

### `engine/costs.py` — Groww intraday equity charges model
- `ChargeBreakdown` (dataclass) — `order_value`, `brokerage`, `stt`,
  `stamp_duty`, `exchange_charge`, `sebi_charge`, `ipft_charge`, `gst`, `total`.
- `calc_charges(order_value, side)` → `ChargeBreakdown` — brokerage
  (`min(₹20, 0.1%)`, floored at `min(₹5, 2.5%)`), STT (sell-only, 0.025%),
  stamp duty (buy-only, 0.003%), exchange charge (0.00297%), SEBI charge
  (0.0001%), IPFT (0.0001%), 18% GST on (brokerage + exchange + SEBI + IPFT).
- `round_trip_charges(entry_value, exit_value)` → `(entry_charges,
  exit_charges)` tuple.

### `engine/db.py` — dual-mode (Postgres/SQLite) trade storage
- `_now()` — always IST, never naive `datetime.now()`.
- `_variant_to_table_suffix(variant_id)` / `VARIANT_TABLES` / `_table(variant_id)` —
  `"bot_400/subh30_trailing_ema"` → table `ute_trades_bot_400__subh30_trailing_ema`.
- `_variant_tables_sql(pk)` — generates `CREATE TABLE IF NOT EXISTS` **plus** a
  partial unique index (`WHERE status='OPEN'`) per variant table, guaranteeing
  at most one open position per variant at the DB level (added 2026-08-12
  after a real duplicate-open-position incident).
- `SQLITE_SCHEMA` / `POSTGRES_SCHEMA` — the full schema strings (all 8 variant
  trade tables + `ute_cycle_log` + `ute_checkpoint_log` + `ute_settings`).
- `get_engine()` — cached SQLAlchemy engine, Postgres if `DATABASE_URL` is
  set, else local SQLite at `data/unified_trading_engine.db`.
- `_lock_key_for_variant(variant_id)` — CRC32 of the variant_id, used as a
  Postgres advisory-lock key.
- `acquire_trade_lock(variant_id)` — context manager, `pg_advisory_xact_lock`
  (blocking, per-variant) on Postgres, a no-op on SQLite.
- `try_acquire_scan_lock()` — context manager, `pg_try_advisory_lock`
  (non-blocking, single global key) — a second overlapping `run_full_scan_cycle`
  call skips instead of racing.
- `init_db()` — runs the full schema (idempotent `CREATE ... IF NOT EXISTS`).
- `open_trade(...)` / `update_price_extremes(...)` / `mark_target_hit(...)` /
  `close_trade(...)` — the trade lifecycle writes.
- `get_open_trade(variant_id)` — `SELECT ... WHERE status='OPEN' ORDER BY id
  DESC LIMIT 1` (the "latest wins" query whose duplicate-row edge case is now
  prevented by the unique index above).
- `get_closed_trades(variant_id)` / `get_all_trades(variant_id)` — DataFrame reads.
- `get_arm_cycles_used_today(variant_id, symbol)` — today's already-used
  `arm_cycle_id`s for this symbol/variant.
- `get_starting_capital(variant_id, default)` — `default` + today's realized
  net P&L for this variant.
- `log_cycle(...)` / `get_cycle_logs(limit=30)` / `prune_cycle_logs(retention_days=7)` —
  the cycle-log table.
- `get_setting(key, default=None)` / `set_setting(key, value)` — the shared
  `ute_settings` key/value store (the only channel between the two separate
  Streamlit Cloud deployments).
- `get_checkpoints_used_today(variant_id)` / `mark_checkpoint_used(...)` —
  the subh30 checkpoint log.
- `reset_all_data()` — wipes every variant table + both logs (irreversible,
  confirm-gated in the dashboard).

### `engine/scheduler.py` — APScheduler wiring (2 separate jobs)
- `market_status(now=None)` → `"open"` / `"closed_hours"` / `"closed_weekend"`.
- `is_awake(settings, now)` — bot's own wake/sleep window (default 09:00–16:00 IST).
- `run_position_management_cycle(settings=None)` — for every variant with an
  open trade, calls `variant_engine.manage_open_position` inside a per-variant
  try/except (one crash doesn't starve the rest), then logs the cycle with
  `managed=<non-hold count>` and any abnormal-hold reasons as warnings.
  Whole-function try/except logs an `ERROR` cycle row and re-raises on a
  total crash.
- `run_full_scan_cycle(settings=None)` — Stage 1 fetch → per-universe filter
  → Stage 2 fetch → all 8 variants' `scan_for_entry`, guarded by
  `db.try_acquire_scan_lock()` and a crash-visibility try/except.
- `_position_job()` / `_scan_job()` — the actual APScheduler callables (check
  `is_awake` first, swallow+log exceptions so one bad cycle doesn't kill the
  scheduler thread).
- `get_scheduler()` — the module-level singleton `BackgroundScheduler`.
- `start_scheduler()` — registers both jobs (`IntervalTrigger` for position
  management, `CronTrigger` at minute `1,6,11,...,56` for entry scan — NOT a
  bare `*/5`), starts the scheduler, and starts `live_feed`.
- `stop_scheduler()` — shuts down the scheduler and stops `live_feed`.
- `is_running()`.

### `engine/live_feed.py` — Groww websocket tick-driven exit reaction
- `_groww_account()` — first configured Groww account, or `None`.
- `_all_open_trades()` — `{variant_id: trade}` across all 8 variants.
- `_instrument(token)` — `{"exchange": "NSE", "segment": "CASH",
  "exchange_token": token}` shape `GrowwFeed` expects.
- `_get_symbol_to_token(account)` — in-memory-cached (1h TTL) wrapper around
  the account's own 7-day file-cached lookup.
- `_sync_subscriptions(account, feed, open_trades)` — diffs current vs.
  wanted subscriptions, subscribes/unsubscribes only the delta.
- `_check_tick(variant_id, variant_cfg, trade, settings, now, account,
  symbol, ltp)` — builds a synthetic single-row candle from the tick and
  runs it through `variant_engine.locked_decide_and_exit`.
- `_poll_once(account, feed)` — one poll cycle: sync subscriptions, read
  `feed.get_all_feed()`, check each open position's tick.
- `_run_loop(poll_interval_seconds)` — the thread's main loop (connects
  `GrowwFeed`, polls every `poll_interval_seconds` until stopped).
- `start(poll_interval_seconds=2.0)` — idempotent; `False` if no Groww
  account configured (2-min REST job remains the only path).
- `stop(timeout_seconds=5.0)` — best-effort (can't force-cancel a blocked
  `GrowwFeed` connect; the thread is a daemon so it can't outlive the process).
- `is_running()`.

### `engine/dashboard_view.py` — shared rendering between admin + viewer
- `TOKENS` — the Figma-sourced dark-theme design tokens dict.
- `inject_custom_css()` — the CSS Streamlit's own theme system doesn't reach
  (metric cards, alert banners, the open-position card, account chips).
- `render_warning_banner()` — shows the latest `entry_scan` cycle's data
  quality AND (since 2026-08-12) the latest `position_management` cycle's
  warnings, if any.
- `render_account_health()` — 4 status chips (Angel One #1/#2, Groww #1/#2)
  showing which broker account slots are actually configured.
- `render_variant_panel(universe_bot_key, variant_cfg, settings,
  show_force_exit=False)` — the full per-variant panel: metrics row (capital,
  P&L, trades, win rate, profit factor, max drawdown), the open-position card
  (live quote fetch, unrealized P&L, peak/trough, optional Force Exit
  button), an Equity Curve expander (Plotly), and a Trade Log expander.
- `get_refresh_interval()` — `15` (seconds) if any variant has an open
  position, else `None` (no auto-refresh).

### `engine/metrics.py` — per-variant portfolio summary
- `get_summary(variant_id, starting_capital)` — wraps
  `common.metrics.compute_portfolio_metrics` with variant-specific fields
  (`open_positions`, `current_capital`, `total_pnl_pct`, `total_charges`).
- `get_equity_curve(variant_id)` — closed trades sorted by exit time with a
  cumulative-P&L column.

---

## `scripts/`

### `scripts/wake_streamlit.py`
Playwright-driven headless-browser script (plain HTTP GET doesn't work
against a sleeping Streamlit Cloud app — infinite redirect loop, verified).
- `body_text_lower(page)` / `looks_asleep(text)` / `looks_loaded(text)` —
  page-state detection.
- `click_wake_button(page)` — tries several selectors for the "Yes, get this
  app back up!" button.
- `main()` — polls up to `TOTAL_TIMEOUT_S` (150s), clicking the wake button
  once if found, until the page looks loaded. Exit code 0/1. Reads `APP_URL`
  from the environment (set by `keep-awake.yml`).

---

## `tests/` — what each file covers

| File | Covers |
|---|---|
| `test_broker_accounts.py` | `AngelOneAccount`/`GrowwAccount` auth flows (TOTP vs. secret-based), JWT `exp` decoding |
| `test_db_and_config.py` | `all_variant_ids()` shape, SQLite trade open/close round-trip, `ute_settings` get/set, variant capital isolation, checkpoint isolation, the one-open-position-per-variant unique constraint |
| `test_end_to_end_cycle.py` | Synthetic full `run_full_scan_cycle()` (mocked accounts, no real network) — Stage 1 → filter → Stage 2 → all 8 variants entering a planted breakout signal; crash-visibility (an `ERROR` cycle-log row on a mid-cycle exception) |
| `test_live_feed.py` | Subscription diffing, tick→synthetic-candle shape, fail-closed no-account contract, exception swallowing |
| `test_manage_open_position.py` | Both trailing mechanisms standalone; the full `manage_open_position` flow (stop-loss, target→trailing flip, hard floor, square-off, no-account hold); the Groww→Angel One fallback added 2026-08-12 |
| `test_nse_universe.py` | `filter_to_universe`'s top-N selection and missing-symbol (subset-safety) reporting |
| `test_position_management_cycle.py` | `run_position_management_cycle`'s per-variant crash isolation, whole-cycle crash visibility, abnormal-hold-reason surfacing (all added 2026-08-12) |
| `test_rate_limiter.py` | `AccountRateLimiter`'s min-interval guarantee under real concurrent load, independence between separate instances |
| `test_scheduler.py` | Real `BackgroundScheduler` job registration (`IntervalTrigger` vs. `CronTrigger`, not a bare `*/5`), no duplicate jobs on restart, start/stop, `is_awake`/`market_status` |
| `test_stage1_stage2.py` | Chunk splitting, dedup+sort, chunk-level and per-symbol fallback (including the 2026-08-11 parallelized-fallback-under-bulk-load fix) |
| `test_strategy.py` | `check_entry`'s freshness guard (only the bar the signal first turns true), the arm-cycle staleness bound (added/loosened 2026-08-13), arm-cycle reuse, previous-day rejection |
| `test_variant_engine.py` | Subh30 checkpoint due/not-due/grace-expired/already-used state transitions |

**77 tests total, all passing as of this document.**

---

## Data flow, top to bottom

```
scheduler.start_scheduler()
  ├─ IntervalTrigger (every 2 min) ──▶ run_position_management_cycle()
  │                                      ├─ for each variant with an open trade:
  │                                      │    variant_engine.manage_open_position()
  │                                      │      ├─ _position_data_accounts() (Groww, then Angel One)
  │                                      │      └─ locked_decide_and_exit() ──▶ db.acquire_trade_lock()
  │                                      │           └─ _decide_and_exit() ──▶ broker.exit_position() (if triggered)
  │                                      └─ db.log_cycle(stage="position_management", ...)
  │
  ├─ CronTrigger (:01,:06,...,:56) ────▶ run_full_scan_cycle()
  │                                      ├─ db.try_acquire_scan_lock()
  │                                      ├─ nse_universe.get_universe_symbols("n500_minus_100")
  │                                      ├─ stage1_ranking.fetch_ranking_data()  [Groww → Angel One fallback]
  │                                      ├─ nse_universe.filter_to_universe() × 2 (bot_300, bot_400)
  │                                      ├─ stage2_candles.merge_unique_symbols() + fetch_candle_history()
  │                                      ├─ for each of 8 variants: variant_engine.scan_for_entry()
  │                                      │    └─ strategy.check_entry() ──▶ broker.enter_position() (if signal)
  │                                      └─ db.log_cycle(stage="entry_scan", ...)
  │
  └─ live_feed.start() (daemon thread, ~2s poll, Groww only)
       └─ _poll_once() ──▶ _check_tick() ──▶ variant_engine.locked_decide_and_exit() [same path as above]

app.py / viewer_app.py (Streamlit, separate processes)
  └─ dashboard_view.render_variant_panel() ──▶ engine.metrics.get_summary() / db.* reads
```
