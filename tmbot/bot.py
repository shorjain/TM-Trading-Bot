"""Main loop: market data -> strategy -> reconcile orders -> track fills -> risk checks."""

from __future__ import annotations

import collections
import signal
import time
from dataclasses import replace
from decimal import Decimal

from .client import ApiError
from .increments import tick_for
from .config import Config
from .executor import LiveOrder
from .ledger import Ledger, today_start_utc
from .market import Book, MarketFeed
from .strategy import BPS, Decision, Quote, decide

D = Decimal


class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.events: collections.deque[float] = collections.deque()

    def allow(self) -> bool:
        now = time.time()
        while self.events and now - self.events[0] > 60:
            self.events.popleft()
        if len(self.events) >= self.per_minute:
            return False
        self.events.append(now)
        return True


class Bot:
    def __init__(self, cfg: Config, feed: MarketFeed, executor, ledger: Ledger, mode: str,
                 log=print):
        self.cfg, self.feed, self.ex, self.ledger, self.mode, self.log = cfg, feed, executor, ledger, mode, log
        self.limiter = RateLimiter(cfg.risk.max_actions_per_minute)
        self.running = True
        self.start_ts = time.time()
        self.marks: dict[str, Decimal] = {}
        self.last_decisions: dict[str, Decision] = {}

    # ---- helpers ----
    def _ours(self, book: str, side: str) -> list[LiveOrder]:
        return [o for o in self.ex.orders.values() if o.book == book and o.side == side
                and o.status in ("active", "initialized", "pending")]

    def _gross_usd(self) -> Decimal:
        return sum((self.ex.total_holding(b) * self.marks.get(b, D(0)) for b in self.cfg.books), D(0))

    def _without_own(self, b: Book) -> Book:
        """The EBBO includes our own resting orders. If we are the best bid/ask, pretend the
        level beneath us is one tick worse, so we hold our price instead of outbidding ourselves.
        (Paper orders are not in the real book, so this only triggers live.)"""
        changes = {}
        for side, attr in (("buy", "bid"), ("sell", "ask")):
            top = getattr(b, attr)
            for o in self._ours(b.symbol, side):
                if top is not None and o.price == top and not o.order_id.startswith("paper-"):
                    tick = tick_for(top)
                    changes[attr] = top - tick if side == "buy" else top + tick
        return replace(b, **changes) if changes else b

    def _act(self, what: str, fn, *args) -> bool:
        if not self.limiter.allow():
            self.log(f"  rate limit: skipped {what}")
            return False
        try:
            fn(*args)
            return True
        except ApiError as e:
            self.log(f"  ! {what} failed: {e}")
            return False

    # ---- reconcile one side of one book ----
    def _reconcile_side(self, book: str, side: str, want: Quote | None) -> None:
        live = self._ours(book, side)
        for extra in live[1:]:  # keep at most one order per side
            self._act(f"cancel extra {book} {side}", self.ex.cancel, extra)
        cur = live[0] if live else None
        if want is None:
            if cur:
                self._act(f"cancel {book} {side}", self.ex.cancel, cur)
            return
        if cur is None:
            if self._act(f"place {book} {side} {want.qty}@{want.price}", self.ex.place, book, side,
                         want.price, want.qty, "", want.taker):
                self.log(f"  + {book:7} {side:4} {want.qty} @ {want.price}{' (take)' if want.taker else ''}")
            return
        moved = abs(cur.price - want.price) / want.price * BPS > self.cfg.strategy.requote_threshold_bps
        resized = cur.leaves > 0 and abs(cur.leaves - want.qty) / want.qty > D("0.25")
        if moved or resized:
            new_qty = cur.executed + want.qty
            if self._act(f"modify {book} {side}", self.ex.modify, cur, want.price, new_qty):
                self.log(f"  ~ {book:7} {side:4} {cur.price} -> {want.price} qty {want.qty}")

    # ---- one iteration ----
    def step(self, i: int) -> None:
        books: dict[str, Book] = self.feed.snapshot()
        for sym, b in books.items():
            if b.mid:
                self.marks[sym] = b.mid
            elif b.reference:
                self.marks[sym] = b.reference

        for f in self.ex.poll_fills(books):
            f.daily_book = f.book == self.cfg.book_of_the_day
            self.ledger.record(f)
            self.log(f"  * FILL {f.book} {f.side} {f.qty} @ {f.price} {'maker' if f.maker else 'taker'}"
                     f" (+{f.points:.2f} pts)")

        if i % self.cfg.loop.reconcile_every == 0:
            try:
                self.ex.refresh_balances()
            except ApiError as e:
                self.log(f"  ! balance refresh failed: {e}")

        # Risk: hard stop on loss.
        pnl = self.ledger.realized() + self.ledger.unrealized(self.marks)
        if pnl < -self.cfg.risk.max_loss_usd:
            self.log(f"!! max loss hit (pnl {pnl:.2f}); cancelling everything and stopping")
            self.stop()
            return

        gross = self._gross_usd()
        for sym in self.cfg.books:
            b = books.get(sym)
            if b is None:
                for side in ("buy", "sell"):
                    self._reconcile_side(sym, side, None)
                continue
            # What we could deploy if we replaced our own resting orders on this book.
            own_bid_usd = sum((o.leaves * o.price for o in self._ours(sym, "buy")), D(0))
            usd = self.ex.usd_available() + own_bid_usd
            pos = self.ex.total_holding(sym)
            d = decide(self._without_own(b), self.cfg, pos, usd, gross)
            self.last_decisions[sym] = d
            want = {q.side: q for q in d.quotes}
            for side in ("buy", "sell"):
                self._reconcile_side(sym, side, want.get(side))

    def status_line(self) -> str:
        s = self.ledger.summary(self.marks, since=today_start_utc())
        return (f"[{self.mode}] fills {s['fills']}  maker ${s['maker_volume']:.2f}  taker ${s['taker_volume']:.2f}"
                f"  points {s['points']:.1f}  pnl r {s['realized_pnl']:+.3f} u {s['unrealized_pnl']:+.3f}"
                f"  open orders {len(self.ex.orders)}")

    def stop(self) -> None:
        self.running = False

    def run(self, max_minutes: float | None = None) -> None:
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
        try:
            self.ex.refresh_balances()
        except ApiError as e:
            self.log(f"! could not read balances: {e}")
        i = 0
        try:
            while self.running:
                t0 = time.time()
                try:
                    self.step(i)
                except ApiError as e:
                    self.log(f"! step error: {e}")
                except Exception as e:  # keep running, but make it visible
                    self.log(f"! unexpected error: {type(e).__name__}: {e}")
                if i % 10 == 0:
                    self.log(self.status_line())
                i += 1
                if max_minutes and time.time() - self.start_ts > max_minutes * 60:
                    self.log("time limit reached")
                    break
                time.sleep(max(0.0, self.cfg.loop.poll_secs - (time.time() - t0)))
        finally:
            self.log("shutting down: cancelling all open orders")
            try:
                self.ex.cancel_all()
            except Exception as e:
                self.log(f"! cancel_all failed: {e}  -> run `python -m tmbot cancel-all`")
            self.log(self.status_line())
