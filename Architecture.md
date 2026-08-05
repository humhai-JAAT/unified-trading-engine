# Architecture — Unified Trading Engine

## High-level flow

```
Streamlit Cloud app process (admin app; separate viewer app deployment planned)
├── app.py (admin UI)
├── viewer_app.py (read-only UI, separate deployment)
│
└── scheduler.py (APScheduler background job)
        │  every scan cycle, only within wake/sleep window
        ▼
    engine.run_cycle()
        │
        ├── STAGE 1 — ranking data (see below)
        │
        ├── STAGE 2 — candle history (see below)
        │
        └── for each of 12 variants (3 universe-bots × 4 entry/exit combos):
                run its own independent strategy-check + position management,
                reading Stage 1/2's shared data — no per-variant re-fetching
```

## Stage 1 — ranking data layer

**Goal:** know today's %-change for every stock in the biggest universe (Total
Market, ~751 stocks) exactly once per cycle, then let all 3 universe-bots derive
their own top-50 from it without any further API calls.

```
751 stocks split into 6 chunks (~125 each)
        │
        ▼
6 parallel workers, split across 2 broker accounts (e.g. 2 Groww accounts):
  Account A: worker 1, 2, 3 — share ONE lock + rate-limit timer (this account's own)
  Account B: worker 4, 5, 6 — share a DIFFERENT lock + rate-limit timer
  (the two accounts run fully independently of each other — no shared state,
   no forced wave-synchronization between them; each proceeds at its own pace)
        │
        ▼
Merge + sort as chunks arrive (streaming merge, for efficiency) —
        │  BUT: the "final rank list" is only marked ready for downstream use
        │  once ALL 6 chunks have arrived, either normally or via fallback below.
        ▼
Final rank list (all ~751 stocks, ranked by % change)
        │
        ▼ (in-memory, shared read-only for the rest of this cycle)
Temp space — ranking data
        │
        ├─▶ bot_751 filters: take top-50 directly
        ├─▶ bot_551 filters: keep only symbols in the (Total Market − Nifty200)
        │       constituent set, then take top-50 of what's left
        └─▶ bot_400 filters: keep only symbols in the (Nifty500 − Nifty100)
                constituent set, then take top-50 of what's left
```

**Fallback (chunk-level, not whole-list):** if any one of the 6 chunk-fetches
fails, ONLY that chunk is retried via the fallback broker (Angel One) — the other
5 successful chunks are never re-fetched. If the fallback also fails, the cycle
proceeds with a smaller rank list and an explicit warning is logged and shown on
the dashboard (see "Error visibility" below) — it must never silently look like a
complete, healthy list.

**Subset-safety check:** `bot_551`/`bot_400`'s constituent lists (from
niftyindices.com CSVs, cached independently per index, 7-day TTL) are assumed to
always be subsets of `bot_751`'s. Because the 4 underlying index CSVs
(nifty100/200/500/totalmarket) can refresh at slightly different times, a rare
index-rebalance day could momentarily break that assumption. If a
`bot_551`/`bot_400` constituent symbol isn't found in Stage 1's fetched rank list,
log a warning rather than silently dropping it or crashing.

## Stage 2 — candle history layer

**Goal:** fetch the 5-min OHLCV history needed for EMA9/EMA30/EMA100/MACD
calculation exactly once per unique symbol per cycle, no matter how many of the
12 variants need it.

```
bot_751 top-50  ┐
bot_551 top-50  ├──▶ MERGE + DEDUPLICATE ──▶ unique symbol set (typically ~100-150,
bot_400 top-50  ┘                             far fewer than 3×50=150 due to overlap)
                                                        │
                                                        ▼
                        Parallel fetch, same per-account lock pattern as Stage 1
                        (candle-history endpoints are 1-symbol-per-call — no
                         batching possible here, unlike Stage 1's quote/ranking
                         endpoints which batch up to 50/call)
                        Primary: Groww · Fallback: Angel One · Dhan available too
                                                        │
                                                        ▼
                        Temp space — candle history (keyed by symbol, shared
                        read-only for the rest of this cycle)
                                                        │
                        ▼ each variant reads only the symbols in its own universe-
                          bot's top-50 — 3 universe-bots × 4 variants each = 12
                          independent strategy-checks, zero redundant fetching
```

## Multi-broker / multi-account setup

- **2 Angel One accounts** — each account may hold only **1 API key**
  (SEBI compliance restricts non-registered/retail algo API keys to 1 per
  account — confirmed via Angel One's own SmartAPI forum, 2026). Used as the
  Stage 1/2 fallback broker.
- **2 Groww accounts** — primary broker for both stages. Groww's Trade API is a
  paid subscription (₹499+GST/month per account, already held by the project
  owner — not a new cost introduced by this project). Rate limit: ~25
  quote/OHLC requests/sec, up to 50 instruments/call for `get_ohlc`/`get_ltp`
  (today's snapshot only) — but `get_historical_candles` (the actual time-series
  needed for indicators) is 1 symbol per call, same limitation as Angel One.
- **Dhan** — available as an additional option in both stages if needed. Its
  data-API paid-tier requirement (which caused it to be rejected for the sibling
  v1/v2 bots in 2026-07-16) appears to have been lifted since — this should be
  re-verified against Dhan's current terms before relying on it in production,
  not assumed from that older finding.

## Thread-safety design (critical — this was a real bug in the sibling bots)

The sibling bots' `angelone_client.py` has a rate-limiter (`_last_candle_call_at`)
that reads-then-sleeps-then-writes a plain dict with **no lock** — safe only
because those bots never called it from multiple threads at once. This project's
whole design *requires* multiple threads hitting the same account concurrently
(3 workers sharing 1 account), so this pattern must not be carried forward as-is.

**Pattern used here:** one `threading.Lock()` + one "last call time" value **per
broker account** (not one global lock for everything):

```python
class AccountRateLimiter:
    def __init__(self, min_interval_seconds):
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = min_interval_seconds

    def wait_for_turn(self):
        with self._lock:
            elapsed = time.time() - self._last_call
            if elapsed < self._min_interval:
                time.sleep(self._min_interval - elapsed)
            self._last_call = time.time()
```

- Workers sharing the same account's `AccountRateLimiter` instance safely take
  turns (never exceed that account's real safe rate).
- Workers on a *different* account use a *different* `AccountRateLimiter`
  instance — completely independent, genuinely parallel, no cross-account
  blocking.
- Work (which stock-chunk goes to which worker/account) is assigned once at the
  start of the cycle (e.g. round-robin), not decided dynamically mid-run — keeps
  the design simple and predictable.
- `concurrent.futures.ThreadPoolExecutor` is used to run the workers — not
  `asyncio`, since the broker SDKs are synchronous (`requests`-based) and the
  concurrency level here (6 workers) doesn't justify an async rewrite.

## Error visibility (must not fail silently)

- Whenever a chunk's fallback path is used (Stage 1 or Stage 2), or a partial
  parallel-fetch failure occurs, this must be surfaced as a visible warning on
  the dashboard — not just logged. This directly addresses a class of bug found
  during this project's own design review: a partial/incomplete shared dataset
  could otherwise silently produce a wrong or incomplete top-50/candle-set for
  all 12 variants without anyone noticing.

## Naming convention (breaking change from v1/v2)

No more `v1`/`v2`/`v1_1`/`v2_3_2`-style version-number keys. Variants are now
named by **universe + entry-timing + trailing-exit mechanism** (there is no
"fixed exit" variant — see PRD.md's note on this):
`bot_751/subh30_trailing_ema`, `bot_551/puradin_trailing_atr`,
`bot_400/subh30_trailing_atr`, etc. (4 timing/exit combos per universe-bot —
see PRD.md's variant table). Database tables, config keys, and dashboard labels
should all follow this convention consistently — do not reintroduce the old
`vN_M` naming pattern anywhere in this project.

## Technology decisions

**Language: Python.** Explicitly reconsidered during this project's design phase
(the user asked directly) and confirmed as the right choice — this workload is
I/O-bound (waiting on broker network calls, not CPU-heavy computation), so
Python's GIL is not a real bottleneck here (it releases during network waits).
All 3 brokers' SDKs/APIs are Python-friendly, and the existing indicator/pandas
ecosystem carries over directly from the sibling bots. A rewrite in another
language was evaluated and rejected as pure risk with no benefit to the actual
bottleneck (broker-side rate limits, not language speed).

**Deployment: Streamlit Community Cloud (free), same as the sibling bots** —
also explicitly reconsidered. A dedicated VPS (Hostinger, Mumbai data center,
~₹499-599/month realistic ongoing price) was researched and would offer lower
latency to Indian broker APIs and no RAM ceiling, but was decided against for
now: this is a paper-trading side project with no real capital at risk, the
actual data footprint (~100-150 stocks' candle history per cycle) is small
enough that Streamlit Community Cloud's ~1GB RAM cap isn't expected to bind, and
the sibling bots have already run the same BackgroundScheduler-inside-Streamlit
pattern successfully for months. Revisit this decision if RAM or latency
actually becomes a demonstrated problem in production — don't pre-optimize for
it.

## File/folder structure (planned — not yet built)

```
app.py                        Admin Streamlit app — full controls
viewer_app.py                 Read-only Streamlit app — separate Cloud deployment
requirements.txt
config/settings.yaml
.streamlit/secrets.toml       Local secrets — gitignored
data/                         Local SQLite DB + cached CSVs — gitignored
engine/
  config.py                    UNIVERSE_BOTS, VARIANTS (new naming — see above)
  db.py                        All SQL — dual Postgres/SQLite, variant-tagged tables
  scheduler.py                 APScheduler wiring
  stage1_ranking.py            Parallel ranking-data fetch, merge/sort, subset filter
  stage2_candles.py            Merge/dedup top-50s, parallel candle-history fetch
  rate_limiter.py              AccountRateLimiter (per-account lock, see above)
  broker_accounts.py           Account/key registry — which account backs which
                                 worker, Groww/Angel One/Dhan client wiring
  strategy.py                  EMA/MACD signal logic — ported unchanged from the
                                 sibling bots
  variant_engine.py            Per-variant entry-timing (subh30 checkpoint-style /
                                 puradin continuous) + trailing-exit mechanism
                                 (EMA9-close-below / ATR-pullback) logic, run
                                 once per of the 12 variants
  nse_universe.py               Index-constituent CSV fetch (niftyindices.com),
                                 subset derivation (751/551/400)
  dashboard_view.py              Shared rendering, per universe-bot / per variant
common/
  helpers.py, indicators.py, metrics.py   Duplicated from the sibling bots, not
                                            imported cross-project — see rules.md
```

## Design questions — resolved (2026-08-02)

- **"Subh 30 min" checkpoint schedule**: same 3 clock-times as v1's original
  design (09:20, 09:25, 09:30), but each is evaluated 1 minute after its own
  5-min candle closes (09:21, 09:26, 09:31) rather than exactly on the
  checkpoint minute — matches the "wait 1 min after candle close" safety
  buffer used elsewhere in this design to avoid acting on a still-forming
  candle.
- **Trailing-exit mechanism**: BOTH styles exist, as their own separate
  variants — `*_trailing_ema` (EMA9-close-below) and `*_trailing_atr`
  (ATR-pullback), both activating once price first crosses the fixed target %,
  mirroring the sibling v2 bot's two exit styles exactly. There is no separate
  "fixed exit" variant alongside these — the fixed % is only ever the
  activation trigger for trailing, never a standalone exit method (same as v2).
  This is why each universe-bot has 4 variants (2 timings × 2 trailing
  mechanisms), not 6 — an earlier pass at this doc briefly (and incorrectly)
  added `*_fixed` as a third exit-style, corrected the same day (see PRD.md).
