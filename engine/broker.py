"""Paper trading engine: single position per variant, full available capital
per trade, capital compounds intraday. Ported from the sibling intraday bots,
adapted to this project's `variant_id` ("{universe_bot}/{variant_key}") naming.
"""

from engine import costs, db
from common.helpers import get_logger

logger = get_logger(__name__)


def available_capital(variant_id: str, starting_capital: float) -> float:
    return db.get_starting_capital(variant_id, starting_capital)


def enter_position(variant_id: str, symbol: str, price: float, starting_capital: float,
                    arm_cycle_id: str | None, leverage: float = 1.0) -> dict:
    margin = available_capital(variant_id, starting_capital)
    buying_power = margin * leverage
    quantity = int(buying_power // price)
    if quantity <= 0:
        raise ValueError(f"Capital {margin:.2f} (leverage {leverage}x) insufficient to buy 1 share "
                          f"of {symbol} at {price:.2f}")

    order_value = quantity * price
    charge_breakdown = costs.calc_charges(order_value, "buy")

    trade_id = db.open_trade(
        variant_id=variant_id, symbol=symbol, entry_price=price, quantity=quantity, capital_used=margin,
        entry_charges=charge_breakdown.total, arm_cycle_id=str(arm_cycle_id) if arm_cycle_id is not None else None,
        leverage=leverage,
    )
    logger.info(f"[{variant_id}] OPENED {symbol} qty={quantity} @ {price:.2f} (margin={margin:.2f}, "
                f"leverage={leverage}x, notional={order_value:.2f}, charges={charge_breakdown.total:.2f})")
    return {"trade_id": trade_id, "symbol": symbol, "quantity": quantity, "entry_price": price,
            "capital_used": margin, "leverage": leverage, "entry_charges": charge_breakdown.total}


def exit_position(variant_id: str, trade_id: int, quantity: int, price: float, reason: str) -> dict:
    order_value = quantity * price
    charge_breakdown = costs.calc_charges(order_value, "sell")
    db.close_trade(variant_id, trade_id, price, reason, charge_breakdown.total)
    logger.info(f"[{variant_id}] CLOSED trade #{trade_id} @ {price:.2f} reason={reason} "
                f"(charges={charge_breakdown.total:.2f})")
    return {"trade_id": trade_id, "exit_price": price, "exit_reason": reason, "exit_charges": charge_breakdown.total}


def update_extremes(variant_id: str, trade_id: int, current_peak: float, current_trough: float,
                     high: float, low: float) -> tuple[float, float]:
    new_peak, new_trough = max(current_peak, high), min(current_trough, low)
    if new_peak != current_peak or new_trough != current_trough:
        db.update_price_extremes(variant_id, trade_id, new_peak, new_trough)
    return new_peak, new_trough
