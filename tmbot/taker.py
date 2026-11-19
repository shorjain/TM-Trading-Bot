"""Taker round trips (approved by True Markets, 2026-11-19).

Buy at the best ask, then immediately sell the same quantity at the best bid. Points: 1x per
side, so a round trip of notional N earns ~2N points (4N on the book of the day) and costs
~spread x N. Runs only in spare order-action slots, on tight books, within a daily cost budget.

SELF-TRADE GUARD. The API has no client-order-id or self-trade prevention we can rely on. Our
resting orders are often the best bid/ask (front layer), and once those are cancelled our own
depth layer can become the best price. So before ANY taker leg we:
  1. cancel EVERY order of ours on that book (both sides, both layers),
  2. wait until the exchange confirms each one final (canceled/filled), or abort,
  3. re-read a fresh book and re-check that we have no order left on that book,
and only then send the taker order. Maker quotes return on the next loop.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from pathlib import Path

from .client import ApiError
from .increments import meets_min_notional, qty_for_notional, round_qty

D = Decimal
BPS = D(10_000)


def own_conflicts(taker_side: str, limit: Decimal, own_orders) -> list:
    """Our orders that a taker order on `taker_side` at `limit` could trade against:
    a taker buy can hit our sells priced <= limit; a taker sell can hit our buys >= limit.
    Orders whose cancel isn't confirmed yet still count."""
    if taker_side == "buy":
        return [o for o in own_orders if o.side == "sell" and o.price <= limit]
    return [o for o in own_orders if o.side == "buy" and o.price >= limit]


class TakerEngine:
    def __init__(self, bot):
        self.bot = bot
        self.cfg = bot.cfg.taker
        self.last_trade: dict[str, float] = {}
        self.path = Path(bot.log_dir) / "taker.jsonl"

    # ------------------------------------------------------------------ helpers
    def budget_left(self) -> Decimal:
        spent = self.bot.risk.taker_cost_today() if self.bot.risk else getattr(self, "_spent", D(0))
        return self.cfg.daily_cost_budget_usd - spent

    def _charge(self, usd: Decimal) -> None:
        if self.bot.risk:
            self.bot.risk.add_taker_cost(usd)
        else:
            self._spent = getattr(self, "_spent", D(0)) + usd

    def candidates(self, books: dict) -> list[str]:
        out = []
        now = time.time()
        quoted = set(self.bot.cfg.all_books())
        for sym in self.bot.cfg.all_books() + self.bot.cfg.taker_only_books():
            b = books.get(sym)
            d = self.bot.last_decisions.get(sym)
            if b is None or not b.bid or not b.ask or (d is not None and d.pull):
                continue
            # quoted books already fed their mid to the volatility guard in plan(); others feed it here
            paused = self.bot.vol.paused_until.get(sym, 0) > now if sym in quoted else self.bot.vol.update(sym, b.mid)
            if paused:
                continue
            if sym not in quoted and self.bot.ex.total_holding(sym) > 0:
                continue  # leftover from a previous trip: let the sell-only quote flatten it first
            cooldown = self.cfg.book_cooldown_secs if sym in quoted else self.cfg.extra_book_cooldown_secs
            if now - self.last_trade.get(sym, 0) < cooldown:
                continue
            if b.spread_bps is None or b.spread_bps > self.cfg.max_spread_bps:
                continue
            out.append(sym)
        botd = self.bot.cfg.book_of_the_day
        # book of the day first (2x points), then books we DON'T quote (no quotes to clear), then tightest
        out.sort(key=lambda s_: (not (self.cfg.prefer_book_of_the_day and s_ == botd), s_ in quoted,
                                 books[s_].spread_bps))
        return out

    def _poll(self, sym: str) -> None:
        """Process fills/status for our orders (records any fills through the bot)."""
        fresh = self.bot.feed.fresh(sym)
        books = {sym: fresh} if fresh else self.bot.books_now
        for f in self.bot.ex.poll_fills(books):
            self.bot.record_fill(f)

    def clear_book(self, sym: str) -> bool:
        """Self-trade guard steps 1-2: cancel all our orders on `sym` and wait for confirmation."""
        for o in list(self.bot.own_orders_on(sym)):
            if o.status != "cancel_pending":
                try:
                    self.bot.ex.cancel(o)
                except ApiError as e:
                    self.bot.log(f"  - taker {sym}: couldn't cancel our {o.side} [{o.layer}] ({e}); skipping")
                    return False
        deadline = time.time() + self.cfg.cancel_confirm_secs
        while self.bot.own_orders_on(sym):
            if time.time() > deadline:
                self.bot.log(f"  - taker {sym}: our cancels not confirmed in time; skipping (self-trade guard)")
                return False
            time.sleep(0.4)
            self._poll(sym)
        return True

    def _leg(self, sym: str, side: str, qty: Decimal, price: Decimal):
        """Send one taker leg (limit at the touch), wait briefly, cancel any remainder.
        Returns (filled_qty, avg_price)."""
        o = self.bot.ex.place(sym, side, price, qty, True, "taker")
        start_exec = D(0)
        deadline = time.time() + self.cfg.fill_wait_secs
        while o.order_id in self.bot.ex.orders and time.time() < deadline:
            time.sleep(0.3)
            self._poll(sym)
        if o.order_id in self.bot.ex.orders:  # still resting: don't leave a taker order behind
            try:
                self.bot.ex.cancel(o)
            except ApiError:
                pass
            end = time.time() + self.cfg.cancel_confirm_secs
            while o.order_id in self.bot.ex.orders and time.time() < end:
                time.sleep(0.4)
                self._poll(sym)
        filled = o.executed - start_exec
        avg = (o.exec_value / o.executed) if o.executed else price
        return filled, avg

    def _guard_ok(self, sym: str, side: str, limit: Decimal) -> bool:
        """Self-trade guard step 3: nothing of ours left that this taker order could hit."""
        own = self.bot.own_orders_on(sym)
        if own or own_conflicts(side, limit, own):
            self.bot.log(f"  - taker {sym} {side}: we still have {len(own)} order(s) on the book; skipped")
            return False
        return True

    # ------------------------------------------------------------------ round trip
    def maybe_trade(self, books: dict) -> bool:
        if self.budget_left() <= 0:
            return False
        for sym in self.candidates(books):
            if self.round_trip(sym):
                return True
        return False

    def round_trip(self, sym: str) -> bool:
        bot, cfg = self.bot, self.cfg
        self.last_trade[sym] = time.time()
        fb = bot.feed.fresh(sym)
        if not fb or not fb.bid or not fb.ask or fb.spread_bps is None or fb.spread_bps > cfg.max_spread_bps:
            return False
        equity, _ = bot._equity()
        z = bot.cfg.sizing
        buffer = max(equity * z.usdc_buffer_pct, z.usdc_buffer_min_usd)
        own_bid_hold = sum((o.hold_usd for o in bot.own_orders_on(sym)), D(0))  # freed when we cancel
        cash = bot.ex.usd_total() - bot.ex.bid_holds() + own_bid_hold - buffer
        usd = min(cfg.max_trade_usd, cash)
        qty = min(qty_for_notional(usd, fb.ask), round_qty(fb.ask_qty, fb.ask), round_qty(fb.bid_qty, fb.bid))
        if qty * fb.ask < max(cfg.min_trade_usd, D(1)) or not meets_min_notional(qty, fb.bid):
            return False
        est_cost = (fb.ask - fb.bid) * qty
        if est_cost > self.budget_left():
            return False

        # --- self-trade guard: clear the book of our orders, then re-read it ---
        if not self.clear_book(sym):
            return False
        fb = bot.feed.fresh(sym)
        if not fb or not fb.bid or not fb.ask or fb.spread_bps is None or fb.spread_bps > cfg.max_spread_bps:
            bot.log(f"  - taker {sym}: spread moved after clearing our quotes; skipped")
            return False
        qty = min(qty, round_qty(fb.ask_qty, fb.ask), round_qty(fb.bid_qty, fb.bid))
        if not meets_min_notional(qty, fb.bid) or not self._guard_ok(sym, "buy", fb.ask):
            return False

        # --- leg 1: buy at the ask ---
        bot.log(f"  > TAKER {sym}: buy {qty} @ {fb.ask} then sell @ ~{fb.bid} (spread {fb.spread_bps:.1f} bps)")
        try:
            q1, buy_px = self._leg(sym, "buy", qty, fb.ask)
        except ApiError as e:
            bot.log(f"  ! taker {sym} buy failed: {e}")
            return False
        if q1 <= 0:
            bot.log(f"  - taker {sym}: buy didn't fill (ask moved)")
            return True

        # --- leg 2: sell the same quantity at the bid, right away ---
        fb2 = bot.feed.fresh(sym) or fb
        sold, sell_px = D(0), D(0)
        loss = (buy_px - fb2.bid) * q1 if fb2.bid else None
        if fb2.bid and loss is not None and loss <= self.budget_left() and self._guard_ok(sym, "sell", fb2.bid):
            try:
                sold, sell_px = self._leg(sym, "sell", round_qty(q1, fb2.bid), fb2.bid)
            except ApiError as e:
                bot.log(f"  ! taker {sym} sell failed: {e}")
        if sold < q1:
            bot.log(f"  - taker {sym}: {q1 - sold} left after the round trip; maker quotes will sell it near fair")
        cost = (buy_px - sell_px) * sold if sold else D(0)
        self._charge(max(cost, D(0)))
        mult = 2 if sym == bot.cfg.book_of_the_day else 1
        pts = (q1 * buy_px + sold * sell_px) * mult
        rec = {"ts": time.time(), "book": sym, "qty": str(q1), "buy_px": str(buy_px), "sold": str(sold),
               "sell_px": str(sell_px), "cost": str(cost), "points": str(pts), "botd": mult == 2}
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        bot.log(f"  < TAKER {sym}: +{pts:.0f} pts for ${cost:.4f}"
                f"{f' (${cost / pts * 1000:.3f} per 1k pts)' if pts else ''}; budget left ${self.budget_left():.2f}")
        return True
