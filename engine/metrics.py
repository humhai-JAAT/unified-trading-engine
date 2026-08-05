"""Portfolio metrics per variant_id ("{universe_bot}/{variant_key}") — each of
the 12 variants has its own independent capital pool, so metrics are always
computed one variant at a time, never pooled across variants."""

import pandas as pd

from common.metrics import compute_portfolio_metrics
from engine import db


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


def get_equity_curve(variant_id: str) -> pd.DataFrame:
    closed = db.get_closed_trades(variant_id)
    if closed.empty:
        return pd.DataFrame(columns=["exit_time", "symbol", "net_pnl", "cum_pnl"])
    closed = closed.sort_values("exit_time")
    closed["cum_pnl"] = closed["net_pnl"].cumsum()
    return closed[["exit_time", "symbol", "net_pnl", "cum_pnl"]]
