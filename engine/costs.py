"""Groww intraday equity charges simulation (brokerage + statutory charges + GST).
Ported unchanged from the sibling intraday bots (v1/v2) — this project is
intraday (5-min candles, MIS-style square-off), so the intraday charge model
applies, not bot-v3/swing-bot's delivery/CNC model.

Rates per Groww's published intraday pricing (groww.in/pricing) and standard
NSE/SEBI statutory rates for intraday equity, as last verified by the sibling
bots (July 2026) — re-verify against groww.in/pricing before treating as
authoritative if this project is revisited much later:
  Brokerage:  min(Rs 20, 0.1% of order value) per executed order, floored at
              min(Rs 5, 2.5% of order value) if that's below Rs 5.
  STT:        0% on buy, 0.025% on sell.
  Stamp Duty: 0.003% on buy, 0% on sell.
  Exchange transaction charge (NSE): 0.00297% on both legs.
  SEBI turnover charge:              0.0001% on both legs.
  IPFT (NSE):                        0.0001% on both legs.
  GST:                               18% on (brokerage + exchange charge + SEBI charge + IPFT).
"""

from dataclasses import dataclass

BROKERAGE_CAP = 20.0
BROKERAGE_PCT = 0.001
BROKERAGE_MIN = 5.0
BROKERAGE_MIN_PCT = 0.025

STT_SELL_PCT = 0.00025
STAMP_BUY_PCT = 0.00003
EXCHANGE_PCT = 0.0000297
SEBI_PCT = 0.000001
IPFT_PCT = 0.000001
GST_RATE = 0.18


@dataclass
class ChargeBreakdown:
    order_value: float
    brokerage: float
    stt: float
    stamp_duty: float
    exchange_charge: float
    sebi_charge: float
    ipft_charge: float
    gst: float
    total: float


def calc_charges(order_value: float, side: str) -> ChargeBreakdown:
    """side: 'buy' or 'sell'. Returns a full breakdown for display + the total to deduct."""
    if order_value <= 0:
        return ChargeBreakdown(order_value, 0, 0, 0, 0, 0, 0, 0, 0)

    brokerage = min(BROKERAGE_CAP, order_value * BROKERAGE_PCT)
    if brokerage < BROKERAGE_MIN:
        brokerage = min(BROKERAGE_MIN, order_value * BROKERAGE_MIN_PCT)

    stt = order_value * STT_SELL_PCT if side == "sell" else 0.0
    stamp_duty = order_value * STAMP_BUY_PCT if side == "buy" else 0.0
    exchange_charge = order_value * EXCHANGE_PCT
    sebi_charge = order_value * SEBI_PCT
    ipft_charge = order_value * IPFT_PCT
    gst = GST_RATE * (brokerage + exchange_charge + sebi_charge + ipft_charge)

    total = brokerage + stt + stamp_duty + exchange_charge + sebi_charge + ipft_charge + gst

    return ChargeBreakdown(
        order_value=order_value, brokerage=brokerage, stt=stt, stamp_duty=stamp_duty,
        exchange_charge=exchange_charge, sebi_charge=sebi_charge, ipft_charge=ipft_charge,
        gst=gst, total=total,
    )


def round_trip_charges(entry_value: float, exit_value: float) -> tuple[float, float]:
    """Returns (entry_charges, exit_charges) for a buy-then-sell round trip."""
    entry = calc_charges(entry_value, "buy")
    exit_ = calc_charges(exit_value, "sell")
    return entry.total, exit_.total
