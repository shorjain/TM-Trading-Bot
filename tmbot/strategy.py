"""Quoting logic. Pure functions, no I/O, so it is easy to unit test.

Arena scoring pays 3x for maker volume, so we rest a bid and an ask at the front of the
queue, but never closer to fair value than `half_spread` (plus inventory skew):

  * if best + 1 tick is still at least `half_spread` from fair -> improve by exactly one tick
  * else if the best price itself is far enough from fair   -> join it
  * else                                                     -> sit at our limit, behind

Layers: each side has a small "front" order (the rules above) and a larger "depth" order
`depth_ticks` behind the best price. Big takers sweep the front and fill the depth.

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
    taker: bool = False  # True only for taker round trips (deliberately crosses)
    layer: str = "front"  # "front" | "depth" | "taker"

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
    layered: bool = True              # quote front + depth layers on this book


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
    s, r, m, lay = cfg.strategy, cfg.risk, cfg.market, cfg.layers
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

    # ---- bid (front) ----
    bid_cap = fair * (1 - (half + skew) / BPS)
    if book.bid + tick <= bid_cap:
        bid_px = book.bid + tick          # improve by exactly one tick
    elif book.bid <= bid_cap:
        bid_px = book.bid                 # join
    else:
        bid_px = bid_cap                  # best is too close to fair: sit behind
    bid_px = round_price(min(bid_px, book.ask - n * tick), "buy")
    # depth: `depth_ticks` behind the best bid, always behind our front and inside the cap
    depth_bid_px = round_price(min(book.bid - lay.depth_ticks * tick, bid_px - tick, bid_cap), "buy")

    room = min(p.book_cap_usd - pos_usd, p.stock_room_usd, p.usd_budget)
    bid_usd = min(p.quote_usd, room) if room > 0 else D(0)
    bids = _split("buy", bid_usd, bid_px, depth_bid_px, p.layered, lay)
    if not bids:
        dec.reasons.append("no bid: inventory/capital limit")

    # ---- ask (front), only what we hold ----
    ask_floor = fair * (1 + max(half - skew, s.min_edge_bps) / BPS)
    if book.ask - tick >= ask_floor:
        ask_px = book.ask - tick
    elif book.ask >= ask_floor:
        ask_px = book.ask
    else:
        ask_px = ask_floor
    ask_px = round_price(max(ask_px, book.bid + n * tick), "sell")
    depth_ask_px = round_price(max(book.ask + lay.depth_ticks * tick, ask_px + tick, ask_floor), "sell")

    ask_qty = min(round_qty(p.position_qty, ask_px), qty_for_notional(p.quote_usd, ask_px))
    leftover = p.position_qty - ask_qty
    if leftover > 0 and not meets_min_notional(leftover, ask_px):
        ask_qty = round_qty(p.position_qty, ask_px)  # don't leave sub-$1 dust behind
    asks = _split("sell", ask_qty * ask_px, ask_px, depth_ask_px, p.layered, lay, max_qty=ask_qty)
    if not asks and p.position_qty > 0:
        dec.reasons.append("no ask: holding below $1 minimum")

    # ---- never let any of our bids meet any of our asks (self-trade), across layers ----
    if asks:
        lowest_ask = min(q.price for q in asks)
        kept = [q for q in bids if q.price < lowest_ask]
        if len(kept) < len(bids):
            dec.reasons.append("bid dropped: would cross our own ask")
        bids = kept

    dec.quotes = bids + asks
    return dec


def _split(side: str, usd: Decimal, front_px: Decimal, depth_px: Decimal, layered: bool, lay,
           max_qty: Decimal | None = None) -> list[Quote]:
    """Split one side's size into a small front order and a larger depth order. Falls back to a
    single front order when layering is off or either piece would be under the $1 minimum."""
    if usd <= 0 or front_px <= 0:
        return []
    total_qty = qty_for_notional(usd, front_px) if max_qty is None else max_qty
    if not meets_min_notional(total_qty, front_px):
        return []
    if layered and depth_px > 0:
        front_qty = round_qty(total_qty * lay.front_pct, front_px)
        depth_qty = round_qty(total_qty - front_qty, depth_px)
        if side == "buy":  # cash: the depth layer must fit what's left at its own price
            depth_qty = min(depth_qty, qty_for_notional(usd - front_qty * front_px, depth_px))
        if meets_min_notional(front_qty, front_px) and meets_min_notional(depth_qty, depth_px):
            return [Quote(side, front_px, front_qty, layer="front"),
                    Quote(side, depth_px, depth_qty, layer="depth")]
    return [Quote(side, front_px, total_qty, layer="front")]


def would_cross(side: str, price: Decimal, best_bid: Decimal | None, best_ask: Decimal | None,
                no_cross_ticks: int = 1) -> bool:
    """True if a resting limit at `price` would trade immediately (become a taker) against the
    given best prices, or sit closer than `no_cross_ticks` to them."""
    tick = tick_for(price)
    if side == "buy":
        return best_ask is not None and price > best_ask - no_cross_ticks * tick
    return best_bid is not None and price < best_bid + no_cross_ticks * tick
