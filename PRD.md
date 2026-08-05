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

## Core structure: 3 universe-bots × 4 variants = 12 total

| Universe-bot | Stock universe | Approx size |
|---|---|---|
| `bot_751` | Nifty Total Market (broadest — NSE's closest thing to a "Nifty 1000") | ~751 |
| `bot_551` | Total Market minus Nifty 200 | ~551 |
| `bot_400` | Nifty 500 minus Nifty 100 | ~400 |

`bot_551` and `bot_400` are constructed as, and always remain, **subsets of
`bot_751`'s universe** (set-difference from the same index-constituent lists) —
this is what makes sharing one ranking-data fetch across all three possible.

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

12 variants total (3 universe-bots × 4), one process, one database, one
dashboard — not 12 separate deployments.

## The 2-stage shared data pipeline

**Stage 1 — ranking data (which stocks are today's gainers):**
Fetch today's %-change for all ~751 Total Market stocks ONCE per cycle, split
across 6 parallel workers (across multiple broker accounts — see Architecture.md),
merge + sort into one ranked list. All 3 universe-bots derive their own top-50 by
filtering this ONE shared list down to their own subset — no per-universe API
calls.

**Stage 2 — candle history (for the actual EMA/MACD signal check):**
Merge the 3 universe-bots' top-50 lists, remove duplicate symbols, fetch 5-min
candle history ONLY for that deduplicated set (parallel, same multi-account
pattern), share the result across whichever of the 12 variants need each symbol.

Full data flow: see Architecture.md.

## Who this is for

A single retail trader (the project owner) forward-testing 12 strategy-variant
combinations (3 universes × 2 entry-timings × 2 trailing-exit mechanisms)
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

The project owner already holds 2 Angel One accounts and 2 Groww accounts (Groww's
API access is a paid ₹499+GST/month subscription per account) — these are used as
independent rate-limit pools for the parallel-fetch design, not a new expense
introduced by this project.
