"""Quoting logic. Pure functions, no I/O, so it is easy to unit test.

Arena scoring pays 3x for maker volume, so we rest a bid and an ask at the front of the
queue, but never closer to fair value than `half_spread` (plus inventory skew):

  * if best + 1 tick is still at least `half_spread` from fair -> improve by exactly one tick
  * else if the best price itself is far enough from fair   -> join it
  * else                                                     -> sit at our limit, behind

Long-only spot: we only offer what we hold, and as inventory builds both quotes shift
down so we sell back toward flat (the reducing side may go as close as `min_edge`).
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
    taker: bool = False  # True only for taker-assist (deliberately crosses)

    @property
    def notional(self) -> Decimal:
        return self.price * self.qty


@dataclass
class Params:
    """Per-book inputs the bot computes each loop (sizes in USD)."""
    position_qty: Decimal = D(0)
    usd_budget: Decimal = D(0)        # cash this book's bid may use (after buffer + other books)
    quote_usd: Decimal = D(5)         # target size of each quote
    book_cap_usd: Decimal = D(15)     # max inventory in this book
    stock_room_usd: Decimal = D(30)   # remaining room under the total stock cap
    extended: bool = False            # off-hours: wider half-spread
    taker_budget_usd: Decimal = D(0)  # remaining daily taker-assist cost budget


@dataclass
class Decision:
    book: str
    fair: Decimal | None
    quotes: list[Quote] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    pull: bool = False             # True = don't quote this book at all right now
    reference_stale: bool = False


def fair_value(book: Book, weight: Decimal) -> Decimal | None:
    mid, ref = book.mid, book.reference
    if ref and mid:
        return weight * ref + (1 - weight) * mid
    return mid or ref


def decide(book: Book, cfg: Config, p: Params, now: float | None = None) -> Decision:
    s, r, m, ta = cfg.strategy, cfg.risk, cfg.market, cfg.taker_assist
    dec = Decision(book=book.symbol, fair=None)

    def pull(reason: str) -> Decision:
        dec.pull = True
        dec.reasons.append(reason)
        return dec

    if book.age_secs(now) > r.stale_data_secs:
        return pull(f"our market data is stale ({book.age_secs(now):.0f}s)")
    if not book.bid or not book.ask:
        return pull("one-sided or empty book")
    spread = book.spread_bps
    if spread is not None and spread > r.max_book_spread_bps:
        return pull(f"book too wide ({spread:.0f} bps)")

    ref_stale = book.reference is None or book.reference_age_secs(now) > s.reference_stale_secs
    dec.reference_stale = ref_stale
    if ref_stale:
        if spread is not None and spread > m.stale_max_spread_bps:
            return pull(f"reference stale and book {spread:.0f} bps wide: no trustworthy fair value")
        if book.quiet_secs(now) > m.dead_book_secs:
            return pull("reference stale and book unchanged for a long time (market closed?)")
    fair = fair_value(book, D(0) if ref_stale else s.reference_weight)
    dec.fair = fair
    if not fair:
        return pull("no fair value")
    if not ref_stale and abs(fair - book.mid) / book.mid * BPS > r.max_fair_mid_gap_bps:
        return pull("fair value and mid disagree; sitting out")

    half = s.half_spread_bps_extended if (p.extended or ref_stale) else s.half_spread_bps_regular
    tick = tick_for(fair)
    pos_usd = p.position_qty * fair
    inv = min(max(pos_usd / p.book_cap_usd, D(0)), D(1)) if p.book_cap_usd > 0 else D(0)
    skew = inv * s.inventory_skew_bps
    n = s.no_cross_ticks

    # ---- taker assist (off by default): flatten inventory above the book cap ----
    if ta.enabled and pos_usd > p.book_cap_usd and spread is not None and spread <= ta.max_spread_bps:
        excess_qty = round_qty((pos_usd - p.book_cap_usd) / book.bid, book.bid)
        q = min(excess_qty, round_qty(book.bid_qty, book.bid))
        cost = q * book.bid * spread / 2 / BPS
        if meets_min_notional(q, book.bid) and cost <= p.taker_budget_usd:
            dec.quotes.append(Quote("sell", book.bid, q, taker=True))
            dec.reasons.append(f"taker-assist: flatten {q} (est. cost ${cost:.4f})")
            return dec

    # ---- bid ----
    bid_cap = fair * (1 - (half + skew) / BPS)
    if book.bid + tick <= bid_cap:
        bid_px = book.bid + tick          # improve by exactly one tick
    elif book.bid <= bid_cap:
        bid_px = book.bid                 # join
    else:
        bid_px = bid_cap                  # best is too close to fair: sit behind
    bid_px = round_price(min(bid_px, book.ask - n * tick), "buy")

    room = min(p.book_cap_usd - pos_usd, p.stock_room_usd, p.usd_budget)
    bid_qty = qty_for_notional(min(p.quote_usd, room), bid_px) if room > 0 and bid_px > 0 else D(0)
    bid = Quote("buy", bid_px, bid_qty) if meets_min_notional(bid_qty, bid_px) else None
    if bid is None:
        dec.reasons.append("no bid: inventory/capital limit")

    # ---- ask (only what we hold) ----
    ask_floor = fair * (1 + max(half - skew, s.min_edge_bps) / BPS)
    if book.ask - tick >= ask_floor:
        ask_px = book.ask - tick
    elif book.ask >= ask_floor:
        ask_px = book.ask
    else:
        ask_px = ask_floor
    ask_px = round_price(max(ask_px, book.bid + n * tick), "sell")

    ask_qty = min(round_qty(p.position_qty, ask_px), qty_for_notional(p.quote_usd, ask_px))
    leftover = p.position_qty - ask_qty
    if leftover > 0 and not meets_min_notional(leftover, ask_px):
        ask_qty = round_qty(p.position_qty, ask_px)  # don't leave sub-$1 dust behind
    ask = Quote("sell", ask_px, ask_qty) if meets_min_notional(ask_qty, ask_px) else None
    if ask is None and p.position_qty > 0:
        dec.reasons.append("no ask: holding below $1 minimum")

    # ---- never let our own quotes cross each other (self-trade) ----
    if bid and ask and bid.price >= ask.price:
        bid = None
        dec.reasons.append("bid dropped: would cross our own ask")

    dec.quotes = [q for q in (bid, ask) if q]
    return dec


def would_cross(side: str, price: Decimal, best_bid: Decimal | None, best_ask: Decimal | None,
                no_cross_ticks: int = 1) -> bool:
    """True if a resting limit at `price` would trade immediately (become a taker) against the
    given best prices, or sit closer than `no_cross_ticks` to them."""
    tick = tick_for(price)
    if side == "buy":
        return best_ask is not None and price > best_ask - no_cross_ticks * tick
    return best_bid is not None and price < best_bid + no_cross_ticks * tick
