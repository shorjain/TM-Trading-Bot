"""Quoting logic. Pure functions, no I/O, so it is easy to unit test.

Idea: the competition pays 3x for maker volume and the books are thin and wide
(~50-100 bps, 1-2 resting orders). We rest a bid and an ask *inside* the spread
around a fair value anchored on the exchange's reference price, so:
  * we become the best bid/ask and get filled first by any taker (maker volume, 3x),
  * every fill is priced away from fair value, so round trips earn the spread
    instead of paying it,
  * inventory skews quotes so we sell what we bought and stay near flat.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .config import Config
from .increments import meets_min_notional, qty_for_notional, round_price, round_qty, tick_for
from .market import Book

D = Decimal
BPS = D(10_000)


@dataclass
class Quote:
    side: str  # "buy" | "sell"
    price: Decimal
    qty: Decimal
    taker: bool = False  # True = intended to cross (opportunistic take)

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass
class Decision:
    book: str
    fair: Decimal | None
    quotes: list[Quote] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def fair_value(book: Book, reference_weight: Decimal) -> Decimal | None:
    mid = book.mid
    ref = book.reference
    if ref and mid:
        return reference_weight * ref + (1 - reference_weight) * mid
    return ref or mid


def decide(book: Book, cfg: Config, position_qty: Decimal, usd_available: Decimal,
           gross_usd: Decimal, now: float | None = None) -> Decision:
    s, z, r = cfg.strategy, cfg.sizing, cfg.risk
    dec = Decision(book=book.symbol, fair=None)

    if book.age_secs(now) > r.stale_data_secs:
        dec.reasons.append(f"stale data ({book.age_secs(now):.0f}s)")
        return dec
    fair = fair_value(book, s.reference_weight)
    dec.fair = fair
    if not fair:
        dec.reasons.append("no fair value (empty book, no reference)")
        return dec
    if book.spread_bps is not None and book.spread_bps > r.max_book_spread_bps:
        dec.reasons.append(f"book too wide ({book.spread_bps:.0f} bps)")
        return dec
    if book.mid and abs(fair - book.mid) / book.mid * BPS > r.max_fair_mid_gap_bps:
        dec.reasons.append("fair value and mid disagree; sitting out")
        return dec

    tick = tick_for(fair)
    pos_usd = position_qty * fair
    inv = min(max(pos_usd / z.max_position_usd, D(0)), D(1)) if z.max_position_usd > 0 else D(0)
    # Long-only spot: skew both quotes down as inventory grows (buy less eagerly, sell more eagerly),
    # but never let the ask give up the minimum edge.
    skew = min(inv * s.inventory_skew_bps, max(s.half_spread_bps - s.min_edge_bps, D(0)))

    notional = z.quote_notional_usd
    if cfg.book_of_the_day and book.symbol == cfg.book_of_the_day:
        notional *= z.size_multiplier_daily_book

    # ---- opportunistic take: someone is resting clearly through fair value ----
    if s.take_enabled:
        if book.ask and book.ask < fair * (1 - s.take_edge_bps / BPS):
            q = qty_for_notional(min(notional, usd_available), book.ask)
            q = min(q, round_qty(book.ask_qty, book.ask))
            if meets_min_notional(q, book.ask):
                dec.quotes.append(Quote("buy", book.ask, q, taker=True))
                dec.reasons.append("take: ask below fair")
        if book.bid and book.bid > fair * (1 + s.take_edge_bps / BPS) and position_qty > 0:
            q = min(round_qty(position_qty, book.bid), round_qty(book.bid_qty, book.bid))
            if meets_min_notional(q, book.bid):
                dec.quotes.append(Quote("sell", book.bid, q, taker=True))
                dec.reasons.append("take: bid above fair")
        if dec.quotes:
            return dec

    # ---- bid ----
    bid_px = fair * (1 - (s.half_spread_bps + skew) / BPS)
    bid_cap = fair * (1 - s.min_edge_bps / BPS)
    if book.bid:  # no need to be more than one tick better than the current best bid
        bid_px = min(bid_px, book.bid + tick) if book.bid < bid_cap else bid_px
    bid_px = min(bid_px, bid_cap)
    if book.ask:
        bid_px = min(bid_px, book.ask - s.no_cross_ticks * tick)
    bid_px = round_price(bid_px, "buy")

    room = min(z.max_position_usd - pos_usd, z.max_gross_usd - gross_usd, usd_available)
    bid_qty = qty_for_notional(min(notional, room), bid_px) if room > 0 and bid_px > 0 else D(0)
    if meets_min_notional(bid_qty, bid_px):
        dec.quotes.append(Quote("buy", bid_px, bid_qty))
    else:
        dec.reasons.append("no bid: position/capital limit")

    # ---- ask (only what we hold; spot cannot go short) ----
    ask_px = fair * (1 + (s.half_spread_bps - skew) / BPS)
    ask_floor = fair * (1 + s.min_edge_bps / BPS)
    if book.ask:
        ask_px = max(ask_px, book.ask - tick) if book.ask > ask_floor else ask_px
    ask_px = max(ask_px, ask_floor)
    if book.bid:
        ask_px = max(ask_px, book.bid + s.no_cross_ticks * tick)
    ask_px = round_price(ask_px, "sell")

    ask_qty = min(round_qty(position_qty, ask_px), qty_for_notional(notional, ask_px))
    # If the remainder after this quote would be dust (< $1), sell it all now.
    leftover = position_qty - ask_qty
    if leftover > 0 and not meets_min_notional(leftover, ask_px):
        ask_qty = round_qty(position_qty, ask_px)
    if meets_min_notional(ask_qty, ask_px):
        dec.quotes.append(Quote("sell", ask_px, ask_qty))
    elif position_qty > 0:
        dec.reasons.append("no ask: holding below $1 minimum notional")

    return dec
