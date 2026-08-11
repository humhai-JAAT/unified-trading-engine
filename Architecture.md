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
        └── for each of 8 variants (2 universe-bots × 4 entry/exit combos):
                run its own independent strategy-check + position management,
                reading Stage 1/2's shared data — no per-variant re-fetching
```

## Stage 1 — ranking data layer

**Goal:** know today's %-change for every stock in the biggest universe
(`bot_400` = Nifty500 minus Nifty100, ~400 stocks) exactly once per cycle,
then let both universe-bots derive their own top-50 from it without any
further API calls. **Redefined 2026-08-11** (was Total Market, ~751 stocks —
see PRD.md for the trade-off this cut accepted).

```
~400 stocks split into chunks
        │
        ▼
3 parallel workers (WORKERS_PER_ACCOUNT) on the primary broker account
(Groww) — share ONE lock + rate-limit timer (see AccountRateLimiter below)
        │
        ▼
Merge + sort as chunks arrive (streaming merge, for efficiency) —
        │  BUT: the "final rank list" is only marked ready for downstream use
        │  once ALL chunks have arrived, either normally or via fallback below.
        ▼
Final rank list (all ~400 stocks, ranked by % change)
        │
        ▼ (in-memory, shared read-only for the rest of this cycle)
Temp space — ranking data
        │
        ├─▶ bot_400 filters: take top-50 directly
        └─▶ bot_300 filters: keep only symbols in the (Nifty500 − Nifty200)
                constituent set, then take top-50 of what's left
```

**Fallback (chunk-level, not whole-list):** if any one chunk-fetch fails, ONLY
that chunk is retried via the fallback broker (Angel One) — the other
successful chunks are never re-fetched. If the fallback also fails, the cycle
proceeds with a smaller rank list and an explicit warning is logged and shown on
the dashboard (see "Error visibility" below) — it must never silently look like a
complete, healthy list.

**Subset-safety check:** `bot_300`'s constituent list (from niftyindices.com
CSVs, cached independently per index, 7-day TTL) is assumed to always be a
subset of `bot_400`'s. Because the 3 underlying index CSVs (nifty100/200/500)
can refresh at slightly different times, a rare index-rebalance day could
momentarily break that assumption. If a `bot_300` constituent symbol isn't
found in Stage 1's fetched rank list, log a warning rather than silently
dropping it or crashing.

## Stage 2 — candle history layer

**Goal:** fetch the 5-min OHLCV history needed for EMA9/EMA30/EMA100/MACD
calculation exactly once per unique symbol per cycle, no matter how many of the
8 variants need it.

```
bot_300 top-50  ┐
bot_400 top-50  ┴──▶ MERGE + DEDUPLICATE ──▶ unique symbol set (typically ~70-90,
                                              fewer than 2×50=100 due to overlap)
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
                          bot's top-50 — 2 universe-bots × 4 variants each = 8
                          independent strategy-checks, zero redundant fetching
```

## Live position monitoring (added 2026-08-10)

**Goal:** react to an open position's stop-loss/target/trailing-exit within
seconds, not the 2-min REST poll's worst-case lag.

Explored a full websocket redesign first (`engine.live_feed`'s design
discussion) and hit a hard limit: Groww's `GrowwFeed` websocket (NATS-based,
not a plain socket — see broker_accounts.py/live_feed.py) only streams live
LTP ticks, never historical OHLCV. So Stage 1/2 (both need history — Stage 2
explicitly, Stage 1's ranking could theoretically use live LTP but wasn't
changed, see "Not done" below) stay REST, unchanged. Live-tested (2026-08-10,
market hours) that subscribing to the FULL 751-stock universe hits no
server-side limit (750/750 succeeded, connection <1s) — but coverage is
tick-driven, not instant: only ~66% of subscribed symbols had a fresh tick
within 10s, converging toward ~92% by 60s (even large-caps like BAJAJ-AUTO
took >10s for their first trade in the sampled window) — a snapshot-style
REST call doesn't have this gap, so websocket isn't a drop-in replacement for
"give me every symbol's price right now."

**What actually shipped**, scoped to where the gap matters most — open
positions, not the full universe:

```
engine.scheduler.start_scheduler()
        │
        ├──▶ 2-min REST job (unchanged) ─── manage_open_position() ─┐
        │                                                            │
        └──▶ engine.live_feed background thread (NEW, daemon)        ├─▶ locked_decide_and_exit()
             persistent GrowwFeed connection, polls get_all_feed()   │      (SAME function, both paths)
             every ~2s, subscribed ONLY to open positions' symbols ──┘      db.acquire_trade_lock()
             (at most 8, not 400 — no batch-limit concern here)            re-checks trade still open
```

Both the REST path (`manage_open_position`, per-1-min-candle since entry) and
the live-feed path (`live_feed._check_tick`, one synthetic single-row
"candle" per tick: Open=High=Low=Close=ltp) funnel into the SAME
`variant_engine.locked_decide_and_exit()` — no duplicated decision logic —
which wraps the actual check+write in `db.acquire_trade_lock()` (existed in
db.py since Phase 3 but was never actually called anywhere until this; a real
gap, since two genuinely concurrent exit paths now exist). Re-fetches the
trade fresh under the lock rather than trusting a possibly-stale snapshot, so
the REST job and the live-feed thread can never double-exit the same trade.
No-ops on SQLite (advisory locks are Postgres-only), so local dev is
unaffected.

**Fail-closed by design**: `live_feed.start()` is a no-op (returns `False`)
if no Groww account is configured — Angel One has no websocket equivalent in
this codebase — and the 2-min REST job keeps running regardless either way.
If the feed thread disconnects or was never started, behavior silently
degrades back to 2-min-only reaction, never below that.

**Live-verified end-to-end 2026-08-10** (market hours, isolated local SQLite
so the real Supabase DB wasn't touched): a fake open RELIANCE position with
entry_price=1.0 correctly picked up a live tick (~₹1327), updated
`peak_price`, and flipped `target_hit` to `1` within **~6 seconds** of
opening — vs. up to 2 minutes via REST alone.

**Not done**: Stage 1's ranking fetch is still pure REST — a hybrid
(websocket LTP for the full universe + REST as the missing-data fallback)
was discussed but not built, since the ~66%-in-10s coverage gap would need
careful handling and Stage 1 isn't the latency-sensitive path today (see
"Not yet decided" in memory.md if this gets revisited).

## Multi-broker / multi-account setup

**Finalized 2026-08-08** (superseding the original 2+2 plan below): **1 Groww
account (primary) + 1 Angel One account (fallback)**. Reasoning — total
per-cycle load is ~400 quote-fetches (Stage 1, batched 50/call = 8 requests)
+ ~70-90 candle-fetches (Stage 2, 1 symbol/call), ≈80-100 requests/cycle. A
single Groww account's ~25 req/s rate clears this in a few seconds, well
inside the multi-minute checkpoint window — a 2nd Groww account would only
help if that specific account failed (key revoked, suspended), not for raw
throughput, and the code's existing chunk-level fallback to Angel One already
covers a full Groww-side outage. Not worth the extra ₹499+GST/month for that
narrow edge case. No code change needed — `broker_accounts.py`'s account
registry already treats the 2nd slot of each broker as optional (silently
skipped if unconfigured).

- **1 Angel One account** — each account may hold only **1 API key**
  (SEBI compliance restricts non-registered/retail algo API keys to 1 per
  account — confirmed via Angel One's own SmartAPI forum, 2026). Used as the
  Stage 1/2 fallback broker.
- **1 Groww account** — primary broker for both stages. Groww's Trade API is a
  paid subscription (₹499+GST/month, already held by the project
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
  all 8 variants without anyone noticing.

## Naming convention (breaking change from v1/v2)

No more `v1`/`v2`/`v1_1`/`v2_3_2`-style version-number keys. Variants are now
named by **universe + entry-timing + trailing-exit mechanism** (there is no
"fixed exit" variant — see PRD.md's note on this):
`bot_300/subh30_trailing_ema`, `bot_300/puradin_trailing_atr`,
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
                                 once per of the 8 variants
  nse_universe.py               Index-constituent CSV fetch (niftyindices.com),
                                 subset derivation (bot_300/bot_400, Nifty500-based)
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
