# Design — Unified Trading Engine

## Visual design system (2026-08-04)

Designed in Figma — **[Unified Trading Engine Dashboard](https://www.figma.com/design/GdNyRjMeqmv2NAQFO5JoIu)**
(file key `GdNyRjMeqmv2NAQFO5JoIu`), a "Trading Dashboard Tokens" variable
collection plus a full-page mockup (sidebar + main content: title, warning
banner, account-health chips, metrics grid, open-position card), then
translated into the actual app — this is the first sibling-bot dashboard to
break from plain Streamlit defaults + emoji-only styling (see "What's new
here" below for what specifically changed and why).

**Dark theme, fintech-professional palette** (`engine/dashboard_view.py`'s
`TOKENS` dict is the single source of truth in code — keep the Figma file and
this dict in sync if the palette ever changes):

| Token | Hex | Used for |
|---|---|---|
| `bg_page` | `#0B0F14` | App background (`.streamlit/config.toml`'s `backgroundColor`) |
| `bg_surface` | `#141A21` | Sidebar background |
| `bg_surface_raised` | `#1B232C` | Metric cards, account chips, position card |
| `border` | `#232B34` | Card/chip borders |
| `text_primary` | `#E8EDF2` | Headings, metric values |
| `text_secondary` | `#8A96A3` | Labels, captions |
| `accent` | `#3B82F6` | Primary buttons, open-position card border |
| `success` | `#22C55E` | Profit, "configured" account dot |
| `danger` | `#EF4444` | Loss (not yet wired into a specific element) |
| `warning` | `#F59E0B` | Warning banner |
| `radius` | `10px` | All cards/chips/alerts |

Implementation split: `.streamlit/config.toml`'s `[theme]` block sets the base
palette so native widgets (inputs, radios, buttons, the sidebar itself) pick
it up automatically; `dashboard_view.inject_custom_css()` (called once near
the top of `app.py`/`viewer_app.py`) layers CSS on top for the pieces
Streamlit's theme system doesn't reach — metric-card styling, alert-banner
radius, and two fully custom HTML/CSS components (account-health chips,
open-position card) that replaced their original `st.metric`-grid
implementations because `st.metric` couldn't produce the compact chip look or
the accent-bordered card the Figma design called for.

**Live-verified** (not just visually inspected in Figma) via
`javascript_tool` computed-style checks against the running app: page
background, sidebar background, metric-card background/radius, chip
background, and the open-position card's background/accent-border/radius all
read back exactly the hex values in the table above — confirmed by inserting
a synthetic open trade into the local dev DB, reloading, and reading the
rendered `.ute-position-card` element's computed styles (then cleaned up).

## What's new here vs the sibling bots' dashboards

Every sibling bot (`intraday-trading-bot`, `intraday-trading-bot-v2`,
`swing-trading-bot`, `bot-v3`) uses Streamlit's defaults + emoji status
markers, explicitly documented as a known, deliberate gap in each of their own
design.md files ("not requested, not planned"). This project is the first to
actually build a real design system — triggered by the user's explicit
request (2026-08-04) to design the UI properly using Figma, not a
retroactive decision made without being asked. Plus 3 functional differences:

- **12 variants across 3 universe-bots, not up to 6 in one flat list** — built
  as a 2-level sidebar selector (universe-bot dropdown, then a 4-way variant
  radio within it), not bot-v3's `st.columns(2)` pattern, which wouldn't scale
  past ~2-3 variants shown side by side. Live-verified: switching either level
  correctly swaps the main panel's content (Phase 3/4).
- **A dashboard-level warning banner is a hard requirement, not a nice-to-have**
  (see Architecture.md's "Error visibility" section) — whenever Stage 1 or
  Stage 2 used a fallback path, or a chunk failed and couldn't be recovered,
  this must be visibly shown, not just logged. None of the sibling bots had
  this as an explicit, load-bearing requirement before (their data-source
  banner shows *which* source served data, but doesn't flag "this cycle's
  data is incomplete"). Built as `render_warning_banner()`, live-verified by
  actually triggering a real scan cycle with no accounts configured and
  confirming the resulting warning text appeared.
- **Per-account health visibility** — since this project spans multiple
  broker accounts (2 Angel One + 2 Groww, optionally Dhan), `render_
  account_health()` shows which account(s) are configured/healthy vs missing,
  as compact status chips, so a real account-level gap is visible at a glance
  rather than buried in logs.

## Not yet decided

- Whether the viewer (read-only) app shows all 12 variants or a curated
  subset (the sibling bots' `public_variant` setting pattern may carry over)
  — currently the viewer exposes the same full 2-level selector as admin.
- `danger` token (`#EF4444`) is defined but not yet wired into a specific
  dashboard element (no loss-colored P&L display exists yet — `Total P&L`
  currently renders in Streamlit's default metric-delta color, not a custom
  token-bound one).
- Mobile-responsive layout check — not tested at narrow viewport widths.
