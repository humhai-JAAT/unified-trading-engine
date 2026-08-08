# Rules — boundaries for AI-assisted work on this project

Drafted from patterns established and explicitly confirmed during the sibling
intraday/swing/bot-v3 build sessions, applied here from the start, plus new rules
specific to this project's multi-broker-account design.

## Absolute boundaries — never do these

- **Never place a real trade or wire up any live broker order-execution API.**
  This project is paper-trading only, permanently.
- **Never commit secrets, API keys, tokens, or passwords to git.** Credentials
  (Angel One × 1 account, Groww × 1 account, Dhan if used, `DATABASE_URL`) live
  in Streamlit Cloud Secrets / `.streamlit/secrets.toml` (gitignored) only. If a
  credential is ever typed in chat, treat it as compromised and tell the user to
  rotate it.
- **Never run a destructive database operation** against production data without
  the user explicitly asking for it in that moment.
- **Never `git push` without being explicitly told to.** Local commits are fine
  and expected; pushing to a remote is a separate, explicit step.
- **Never merge this codebase with `intraday-trading-bot`, `intraday-trading-bot-v2`,
  `swing-trading-bot`, or `bot-v3`**, or add cross-imports between them — own
  venv, own repo, own DB. See `trading_projects_separation.md` in the private
  memory system, updated to include this project as the 5th sibling.
- **Never reintroduce the old `vN`/`vN_M` naming convention** — this project uses
  universe+timing+trailing-exit naming (`bot_751/subh30_trailing_ema`, etc.), a deliberate
  break from the sibling bots. See Architecture.md's "Naming convention" section.
- **Never let a shared-data fetch failure (Stage 1 or Stage 2) fail silently.**
  Partial data or a fallback-path trigger must surface as a visible dashboard
  warning — this was a specific design flaw caught and fixed during this
  project's own architecture review, don't let an implementation quietly drop
  that visibility.
- **Never let two threads share an `AccountRateLimiter` without its lock** — the
  whole point of the per-account lock (see Architecture.md) is to prevent
  exceeding a broker account's real rate limit when multiple workers use the
  same account. Any new parallel-fetch code path must go through it, not
  bypass it "just this once."
- **Before starting a new bot variant anywhere in `D:\schedule EB`, check whether
  an existing project already covers the idea** — the swing bot once duplicated
  `paper-trading-terminal`'s independent rebuild purely from two sessions not
  cross-checking. Don't repeat that.
- **Never delete a project folder without explicit user confirmation**, and
  always verify `git status` is clean and everything is pushed to a remote
  before deleting.

## Library / dependency policy

- Same stack as the sibling bots: `pandas`, `SQLAlchemy`, `requests`,
  `streamlit`, `APScheduler`, `pytz`, `numpy`, plus whichever broker SDKs Groww/
  Angel One/Dhan actually require. Don't introduce a new dependency for
  something this stack already covers.
- Use Python's standard `threading`/`concurrent.futures` for the parallel-fetch
  design — not `asyncio` (see Architecture.md's technology-decisions section for
  why), and not a third-party task-queue library — this is a fixed, small
  (6-worker) concurrency level that doesn't justify one.

## Error handling philosophy

- External API calls (ranking data, candle history) must fail *closed* into the
  chunk-level fallback (see Architecture.md), never crash the whole cycle.
- A failed chunk's fallback must retry ONLY that chunk — never re-fetch chunks
  that already succeeded.
- Financial calculations (target/SL, charges, capital sizing) need an explicit,
  testable formula — don't rely on "this shouldn't happen." Work through the
  algebra with concrete numbers and confirm with the user before implementing
  anything non-obvious.

## What the AI can do without asking

- Write and locally test code (unit tests with synthetic or mocked broker
  responses, safe local-SQLite verification).
- Investigate/research a broker API's current rate limits/pricing before
  building against it (these change — see Dhan's history in this project's
  Architecture.md).
- Flag discovered bugs, dead code, or design tensions proactively.
- Update this project's own memory/progress docs as work happens.

## What the AI must confirm first

- Any destructive or irreversible action (see Absolute Boundaries above).
- Any change to the core strategy's entry/exit rules, capital model, or the
  4-variant structure per universe-bot — these were explicitly specified via a
  long design discussion and are financially consequential.
- Any change to which broker account backs which worker, or the account-count
  assumptions (1 Angel One + 1 Groww, finalized 2026-08-08) this design was
  built around.
- The two open design questions flagged in Architecture.md (exact "Subh 30 min"
  checkpoint schedule, exact trailing-exit mechanism) before implementing the
  variant engine — don't guess a default silently.
- Combining or restructuring this project with any other trading-bot project.
