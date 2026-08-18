"""Portfolio metrics per variant_id ("{universe_bot}/{variant_key}") — each of
the 8 variants has its own independent capital pool, so metrics are always
computed one variant at a time, never pooled across variants.

get_variant_rankings() is the exception: it computes each variant's summary
independently (still no pooling of capital/P&L) and lines the 8 results up
side by side so they can be COMPARED — added 2026-08-18 because the project
had 8 variants running but no way to see which ones were actually earning
their keep (see the 2026-08-13 evaluation-framework discussion in memory.md,
never implemented until now)."""

import pandas as pd

from common.metrics import compute_portfolio_metrics
from engine import config, db


def get_summary(variant_id: str, starting_capital: float) -> dict:
    closed = db.get_closed_trades(variant_id)
    metrics = compute_portfolio_metrics(closed.rename(columns={"net_pnl": "pnl"}) if not closed.empty else closed)

    open_trade = db.get_open_trade(variant_id)
    metrics["open_positions"] = 1 if open_trade else 0
    metrics["current_capital"] = starting_capital + metrics["total_pnl"]
    metrics["total_pnl_pct"] = (metrics["total_pnl"] / starting_capital * 100) if starting_capital else 0.0
    metrics["total_charges"] = (
        (closed["entry_charges"].sum() + closed["exit_charges"].sum()) if not closed.empty else 0.0
    )
    return metrics


def get_variant_rankings(settings: dict) -> pd.DataFrame:
    """One row per variant (8 total), each variant's own get_summary() output
    unpacked into columns, sorted by SQN descending (SQN over raw P&L or
    Profit Factor since it accounts for both trade count and consistency —
    a 2-trade variant that got lucky twice won't outrank a 40-trade variant
    with a smaller but statistically real edge). Every column is still
    computed per-variant in isolation; this only arranges them for
    comparison, it never pools capital or trades across variants."""
    rows = []
    for variant_id in config.all_variant_ids():
        universe_bot_key, variant_key = variant_id.split("/", 1)
        summary = get_summary(variant_id, settings["starting_capital"])
        rows.append({
            "variant_id": variant_id,
            "universe_bot": universe_bot_key,
            "variant": variant_key,
            "total_trades": summary["total_trades"],
            "win_rate_pct": summary["win_rate"],
            "wilson_win_rate_pct": summary["wilson_win_rate"],
            "profit_factor": summary["profit_factor"],
            "sqn": summary["sqn"],
            "expectancy": summary["expectancy"],
            "total_pnl": summary["total_pnl"],
            "total_pnl_pct": summary["total_pnl_pct"],
            "max_drawdown": summary["max_drawdown"],
        })
    df = pd.DataFrame(rows)
    return df.sort_values("sqn", ascending=False).reset_index(drop=True)


def get_equity_curve(variant_id: str) -> pd.DataFrame:
    closed = db.get_closed_trades(variant_id)
    if closed.empty:
        return pd.DataFrame(columns=["exit_time", "symbol", "net_pnl", "cum_pnl"])
    closed = closed.sort_values("exit_time")
    closed["cum_pnl"] = closed["net_pnl"].cumsum()
    return closed[["exit_time", "symbol", "net_pnl", "cum_pnl"]]
