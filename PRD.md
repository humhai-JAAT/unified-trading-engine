# PRD — Unified Trading Engine

## What we're building

A paper-trading (simulated, zero real capital at risk) **intraday** bot for
NSE-listed stocks that replaces the importance of the older `intraday-trading-bot`
(v1) and `intraday-trading-bot-v2` (v2) projects. Those two are not deleted, but
new work stops going into them — this project is where the strategy-variant testing
now lives, organized around a fundamentally different, shared-data architecture
designed to fix the performance/duplication problems v1 and v2 both hit at their
6-variant scale.

Same ported entry strategy as the sibling bots (EMA9/EMA30 crossover + MACD(12,26,9)
bullish + EMA100 > SMA9(EMA100) trend filter + 0.6% min EMA separation), evaluated
on 5-minute candles.

## Why this project exists (not just "v3 of the intraday bot")

v1 and v2 each independently scan 6 strategy variants (3 universes × 2 exit
styles), and each variant fetches its own gainers ranking and candle data from
scratch — even when 2+ variants want the exact same stock's data in the same
cycle. This caused real production problems (12-minute cycles instead of the
configured 2-minute interval, documented in the old projects' memory.md). Rather
than patch that duplication inside v1/v2's existing per-variant-independent
design, this project restructures the whole data-fetching layer around one
principle: **fetch each unique piece of data exactly once per cycle, then share
it (read-only) across every variant that needs it** — regardless of how many
universes or entry/exit combinations exist.

## Core structure: 2 universe-bots × 4 variants = 8 total

**Redefined 2026-08-11** (was 3 universe-bots/12 total, relative to Nifty
Total Market) — both universes are now defined relative to **Nifty 500**
instead, cutting Stage 1's fetch from ~751 to ~400 symbols (46.8% reduction).
Trade-off: this permanently drops Nifty 100 (largest caps) and Total-Market
ranks 501-751 (smallest/most speculative) from the tradeable universe — the
strategy now trades only the Nifty 500 101-500 mid-cap band. Decided and
implemented same session; production trade/cycle-log history was fully reset
as part of the change (not migrated).

| Universe-bot | Stock universe | Approx size |
|---|---|---|
| `bot_300` | Nifty 500 minus Nifty 200 | ~300 |
| `bot_400` | Nifty 500 minus Nifty 100 | ~400 |

`bot_300` is constructed as, and always remains, a **subset of `bot_400`'s
universe** (set-difference from the same index-constituent lists) — this is
what makes sharing one ranking-data fetch across both possible.

**There is no standalone "fixed exit" variant.** Every variant's stop-loss is a
fixed %, and its target is also a fixed % — but reaching that target does NOT
immediately exit the trade. It flips the position into trailing mode instead
(exactly like the sibling v2 bot), and the trailing MECHANISM (EMA9-close-below
vs ATR-pullback) is what differs between variants. So the exit-style axis has
only 2 values, not 3.

Each universe-bot runs **4 variants** (entry-timing × trailing-exit style):

| Variant key | Entry timing | Exit behavior |
|---|---|---|
| `subh30_trailing_ema` | Subh 30 min — checkpoints at 09:20/09:25/09:30, each evaluated 1 min after its candle closes (09:21/09:26/09:31), v1's original model | Fixed SL; once fixed target % is first reached, flips to EMA9-close-below trailing |
| `subh30_trailing_atr` | Subh 30 min | Fixed SL; once fixed target % is first reached, flips to ATR-pullback trailing |
| `puradin_trailing_ema` | Pura din (continuous all-day scanning — v2's model) | Fixed SL; once fixed target % is first reached, flips to EMA9-close-below trailing |
| `puradin_trailing_atr` | Pura din | Fixed SL; once fixed target % is first reached, flips to ATR-pullback trailing |

8 variants total (2 universe-bots × 4), one process, one database, one
dashboard — not 8 separate deployments.

## The 2-stage shared data pipeline

**Stage 1 — ranking data (which stocks are today's gainers):**
Fetch today's %-change for all ~400 `bot_400`-universe stocks ONCE per cycle,
split across parallel workers (across multiple broker accounts — see
Architecture.md), merge + sort into one ranked list. Both universe-bots
derive their own top-N (`gainers_pool_size`, default **25** as of 2026-08-13,
was 50 — lowered to cut Stage 2's fetch volume and tighten entry-scan
cadence) by filtering this ONE shared list down to their own subset — no
per-universe API calls.

**Stage 2 — candle history (for the actual EMA/MACD signal check):**
Merge both universe-bots' top-N lists, remove duplicate symbols, fetch 5-min
candle history ONLY for that deduplicated set (parallel, same multi-account
pattern), share the result across whichever of the 8 variants need each symbol.

Full data flow: see Architecture.md.

## Who this is for

A single retail trader (the project owner) forward-testing 8 strategy-variant
combinations (2 universes × 2 entry-timings × 2 trailing-exit mechanisms)
against live NSE price action simultaneously, risk-free, to see which universe/
timing/exit combination performs best — before ever considering real capital.

## Explicitly out of scope

- Real order execution / any live broker order-placement integration.
- More than 1 concurrent position per variant (matches the sibling bots' pattern).
- Merging or sharing code/DB with `intraday-trading-bot`, `intraday-trading-bot-v2`,
  `swing-trading-bot`, or `bot-v3` — see `trading_projects_separation.md` in the
  private memory system, updated to include this project.
- A rewrite in a different programming language or a move off Streamlit — both
  were explicitly reviewed and rejected for now (see Architecture.md's "Technology
  decisions" section for the reasoning).

## Known accepted cost

**Finalized 2026-08-08**: 1 Groww account (paid, ₹499+GST/month, already held
by the project owner — not a new expense) as primary broker, 1 Angel One
account (free) as fallback. Originally planned as 2+2 for extra parallelism;
reconsidered once the actual per-cycle request volume (~170 requests) was
weighed against Groww's ~25 req/s rate limit — one account clears a full
cycle in a few seconds, so a 2nd Groww account bought no meaningful speed,
only redundancy against that one account failing, which wasn't judged worth
the extra subscription cost. See Architecture.md's "Multi-broker /
multi-account setup" for the full reasoning.
