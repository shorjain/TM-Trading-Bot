"""Exchange price/quantity increments.

Source: https://docs.truemarkets.co/documentation/increment-sizes
Every order's price must be a multiple of the tick and its quantity a multiple of
the lot for the price bucket it falls in. Minimum notional is $1 in every bucket.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

D = Decimal

# (lower_price_inclusive, tick, lot), highest bucket first.
_BUCKETS: list[tuple[Decimal, Decimal, Decimal]] = [
    (D("500000"), D("1"), D("0.0000001")),
    (D("100000"), D("0.5"), D("0.000001")),
    (D("10000"), D("0.1"), D("0.00001")),
    (D("1000"), D("0.01"), D("0.0001")),
    (D("10"), D("0.001"), D("0.001")),
    (D("0.1"), D("0.0001"), D("0.1")),
    (D("0.001"), D("0.000001"), D("10")),
    (D("0.00001"), D("0.00000001"), D("1000")),
    (D("0"), D("0.0000000001"), D("100000")),
]

MIN_NOTIONAL = D("1.00")


def bucket(price: Decimal) -> tuple[Decimal, Decimal]:
    """Return (tick, lot) for a price."""
    for lower, tick, lot in _BUCKETS:
        if price >= lower:
            return tick, lot
    return _BUCKETS[-1][1], _BUCKETS[-1][2]


def tick_for(price: Decimal) -> Decimal:
    return bucket(price)[0]


def lot_for(price: Decimal) -> Decimal:
    return bucket(price)[1]


def round_price(price: Decimal, side: str) -> Decimal:
    """Round to the tick: bids round down, asks round up (never more aggressive)."""
    tick = tick_for(price)
    mode = ROUND_FLOOR if side == "buy" else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=mode) * tick


def round_qty(qty: Decimal, price: Decimal) -> Decimal:
    """Round a base quantity down to the lot for this price."""
    lot = lot_for(price)
    return (qty / lot).to_integral_value(rounding=ROUND_FLOOR) * lot


def qty_for_notional(notional: Decimal, price: Decimal) -> Decimal:
    if price <= 0:
        return D(0)
    return round_qty(notional / price, price)


def meets_min_notional(qty: Decimal, price: Decimal) -> bool:
    return qty > 0 and qty * price >= MIN_NOTIONAL
