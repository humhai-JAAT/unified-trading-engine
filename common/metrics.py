"""Portfolio performance metrics: Profit Factor, Max Drawdown, Win Rate,
Expectancy. Duplicated from the sibling bots, not imported cross-project."""

import pandas as pd


def profit_factor(pnl: pd.Series) -> float:
    gross_profit = pnl[pnl > 0].sum()
    gross_loss = -pnl[pnl < 0].sum()
    if gross_loss == 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(pnl: pd.Series) -> float:
    if len(pnl) == 0:
        return 0.0
    return (pnl > 0).sum() / len(pnl) * 100


def max_drawdown(cum_pnl: pd.Series) -> float:
    if len(cum_pnl) == 0:
        return 0.0
    running_max = cum_pnl.cummax()
    drawdown = cum_pnl - running_max
    return drawdown.min()


def avg_win_loss(pnl: pd.Series) -> tuple[float, float]:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    avg_win = wins.mean() if len(wins) > 0 else 0.0
    avg_loss = losses.mean() if len(losses) > 0 else 0.0
    return avg_win, avg_loss


def expectancy(pnl: pd.Series) -> float:
    if len(pnl) == 0:
        return 0.0
    wr = win_rate(pnl) / 100
    avg_win, avg_loss = avg_win_loss(pnl)
    return (wr * avg_win) + ((1 - wr) * avg_loss)


def compute_portfolio_metrics(closed_trades: pd.DataFrame) -> dict:
    """closed_trades must have a 'pnl' column (realized P&L per trade)."""
    if closed_trades.empty:
        return {
            "total_trades": 0, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0,
            "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0, "total_pnl": 0.0,
        }

    pnl = closed_trades["pnl"]
    equity_curve = pnl.cumsum()
    avg_win, avg_loss = avg_win_loss(pnl)

    return {
        "total_trades": len(closed_trades), "win_rate": win_rate(pnl),
        "profit_factor": profit_factor(pnl), "max_drawdown": max_drawdown(equity_curve),
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy(pnl),
        "total_pnl": pnl.sum(),
    }
