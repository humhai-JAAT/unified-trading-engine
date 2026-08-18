"""Portfolio performance metrics: Profit Factor, Max Drawdown, Win Rate,
Expectancy, SQN, Wilson-lower-bound Win Rate. Duplicated from the sibling
bots, not imported cross-project."""

import math

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


def sqn(pnl: pd.Series) -> float:
    """System Quality Number (Van Tharp): sqrt(n) * mean(pnl) / stdev(pnl).
    Needs >=2 trades for a stdev; a single trade or zero-variance series
    (all trades identical) returns 0.0 rather than dividing by zero — with
    this few samples the number isn't meaningful either way."""
    n = len(pnl)
    if n < 2:
        return 0.0
    std = pnl.std()
    if not std:
        return 0.0
    return math.sqrt(n) * pnl.mean() / std


def wilson_lower_bound_win_rate(pnl: pd.Series, z: float = 1.96) -> float:
    """Wilson score interval's lower bound on the win rate (95% CI by
    default) — a conservative "true" win rate estimate that discounts small
    samples, unlike the naive win_rate() above (e.g. 3/3 wins reads as a
    literal 100% from win_rate(), but this correctly reports it as an
    uncertain ~44% until more trades accumulate). Returns 0.0 for n=0."""
    n = len(pnl)
    if n == 0:
        return 0.0
    wins = int((pnl > 0).sum())
    phat = wins / n
    denom = 1 + z ** 2 / n
    center = phat + z ** 2 / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z ** 2 / (4 * n)) / n)
    return max(0.0, (center - margin) / denom) * 100


def compute_portfolio_metrics(closed_trades: pd.DataFrame) -> dict:
    """closed_trades must have a 'pnl' column (realized P&L per trade)."""
    if closed_trades.empty:
        return {
            "total_trades": 0, "win_rate": 0.0, "wilson_win_rate": 0.0, "profit_factor": 0.0,
            "max_drawdown": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "expectancy": 0.0,
            "sqn": 0.0, "total_pnl": 0.0,
        }

    pnl = closed_trades["pnl"]
    equity_curve = pnl.cumsum()
    avg_win, avg_loss = avg_win_loss(pnl)

    return {
        "total_trades": len(closed_trades), "win_rate": win_rate(pnl),
        "wilson_win_rate": wilson_lower_bound_win_rate(pnl),
        "profit_factor": profit_factor(pnl), "max_drawdown": max_drawdown(equity_curve),
        "avg_win": avg_win, "avg_loss": avg_loss, "expectancy": expectancy(pnl),
        "sqn": sqn(pnl), "total_pnl": pnl.sum(),
    }
