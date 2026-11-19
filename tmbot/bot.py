"""Main loop: market data -> fills -> risk -> strategy -> prioritized order actions.

The exchange processes ONE order action per account at a time, so every loop builds a
prioritized list of actions (risk pulls first, then the book of the day, then books by
flow) and works through it within a small time budget. Before every place/modify the
order is re-checked against a fresh book so it can't cross (no post-only flag exists).
"""

from __future__ import annotations

import collections
import json
import signal
import time
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from .analytics import FlowTracker, Markouts, VolGuard
from .client import ApiError
from .config import Config, read_book_of_the_day
from .executor import OPEN, LiveOrder
from .increments import tick_for
from .keepawake import KeepAwake
from .ledger import Ledger, today_start_utc
from .market import Book, MarketFeed
from .sessions import ET, now_et, session
from .strategy import BPS, Decision, Params, Quote, decide, would_cross

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


@dataclass
class Action:
    priority: float           # lower runs first
    book: str
    kind: str                 # "cancel" | "place" | "modify"
    side: str
    order: LiveOrder | None = None
    quote: Quote | None = None
    why: str = ""


class Bot:
    def __init__(self, cfg: Config, feed: MarketFeed, executor, ledger: Ledger, mode: str,
                 log: Callable[[str], None] = print, lock=None, risk=None,
                 log_dir: str | Path = "logs/paper", live: bool = False):
        self.cfg, self.feed, self.ex, self.ledger, self.mode, self.log = cfg, feed, executor, ledger, mode, log
        self.lock, self.risk, self.live = lock, risk, live
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter(cfg.risk.max_actions_per_minute)
        self.flow = FlowTracker(cfg.flow.window_minutes)
        self.flow.seed(ledger.fills)
        self.vol = VolGuard(cfg.risk.vol_move_bps, cfg.risk.vol_window_secs, cfg.risk.vol_pause_secs)
        self.markouts = Markouts(self.log_dir / "markouts.jsonl")
        self.running = True
        self.stop_reason = ""
        self.start_ts = time.time()
        self.marks: dict[str, Decimal] = {}
        self.books_now: dict[str, Book] = {}
        self._backoff_until = 0.0
        self._botd_checked = 0.0
        self._wallet_checked = 0.0
        self._wallet0: dict | None = None
        self.botd_from_cli = False
        self.last_decisions: dict[str, Decision] = {}
        self.feed.set_books(cfg.feed_books())
        from .taker import TakerEngine  # local import: taker.py depends on this module's types
        self.taker = TakerEngine(self) if cfg.taker.enabled else None

    # ------------------------------------------------------------------ helpers
    def _ours(self, book: str, side: str, layer: str | None = None) -> list[LiveOrder]:
        return [o for o in self.ex.orders.values() if o.book == book and o.side == side and o.status in OPEN
                and (layer is None or o.layer == layer)]

    def own_orders_on(self, book: str) -> list[LiveOrder]:
        """Every order of ours on a book that could still trade (incl. cancels not yet confirmed)."""
        return [o for o in self.ex.orders.values() if o.book == book]

    def _equity(self) -> tuple[Decimal, Decimal]:
        """(equity, stock value) on the exchange, stocks marked at mid."""
        stocks = sum((q * self.marks.get(b, D(0)) for b, q in self.ex.holdings_all().items()), D(0))
        return self.ex.usd_total() + stocks, stocks

    def _without_own(self, b: Book) -> Book:
        """The EBBO includes our own resting orders. If ours is the best price, pretend the level
        beneath is one tick worse, so we hold our price instead of outbidding ourselves."""
        changes = {}
        for side, attr in (("buy", "bid"), ("sell", "ask")):
            top = getattr(b, attr)
            for o in self._ours(b.symbol, side):
                if top is not None and o.price == top and not o.order_id.startswith("paper-"):
                    tick = tick_for(top)
                    changes[attr] = top - tick if side == "buy" else top + tick
        return replace(b, **changes) if changes else b

    def stop(self, reason: str = "") -> None:
        if reason and not self.stop_reason:
            self.stop_reason = reason
        self.running = False

    # ------------------------------------------------------------------ book of the day
    def _maybe_reload_botd(self) -> None:
        if self.botd_from_cli or not self.cfg.path or time.time() - self._botd_checked < 60:
            return
        self._botd_checked = time.time()
        try:
            botd = read_book_of_the_day(self.cfg.path)
        except Exception:
            return
        if botd and botd != self.cfg.book_of_the_day:
            self.log(f"** book of the day changed in config.yaml: {self.cfg.book_of_the_day} -> {botd}")
            if self.live and botd not in getattr(self.ex, "asset_ids", {}):
                self.log(f"!  {botd} isn't loaded in this run; restart the bot to trade it")
                return
            self.cfg.book_of_the_day = botd
            self.feed.set_books(self.cfg.feed_books())

    # ------------------------------------------------------------------ safety checks
    def _check_foreign(self) -> None:
        if not (self.live and self.lock):
            return
        try:
            foreign = self.lock.foreign(self.ex.c.list_orders(status="active"))
        except ApiError:
            return
        if foreign:
            ids = ", ".join(o["order_id"][:8] for o in foreign[:5])
            self.log(f"!! {len(foreign)} open order(s) on this account were NOT placed by this bot ({ids}...).")
            self.log("!! Another instance (e.g. the other computer) may be trading. Stopping to avoid self-trading.")
            self.stop("foreign orders detected")

    def _check_wallet(self) -> None:
        """Log wallet -> exchange movements the bot didn't make (it never allocates)."""
        if not self.live or time.time() - self._wallet_checked < 300:
            return
        self._wallet_checked = time.time()
        snap = self.ex.wallet_snapshot()
        moves = []
        if self._wallet0 is not None:
            for k, v in snap.items():
                v0 = self._wallet0.get(k, D(0))
                if v < v0:
                    moves.append({"asset": k, "wallet_before": str(v0), "wallet_now": str(v)})
        self._wallet0 = snap
        try:
            allocs = self.ex.c.get("/custody/allocations").get("data", [])
        except ApiError:
            allocs = []
        if self.risk is not None:
            known = self.risk.known_allocations()
            first_time = not known and "allocations_seen" not in self.risk.data
            if not first_time:
                for a in allocs:
                    if a.get("id") not in known:
                        moves.append({"allocation": a.get("id"), "asset": a.get("asset_symbol"),
                                      "amount": a.get("amount"), "status": a.get("status"),
                                      "created_at": a.get("created_at"), "failure_reason": a.get("failure_reason")})
            self.risk.remember_allocations(a["id"] for a in allocs if a.get("id"))
        if moves:
            path = self.log_dir / "wallet_moves.jsonl"
            with path.open("a") as fh:
                for mv in moves:
                    fh.write(json.dumps({"ts": time.time(), **mv}) + "\n")
            self.log(f"!  wallet -> exchange movement the bot did not make: {moves[:3]}  (logged to {path})")

    def _check_risk(self) -> bool:
        if not self.risk:
            return True
        realized, unrealized = self.ledger.realized(), self.ledger.unrealized(self.marks)
        reason = self.risk.check(realized, unrealized, self.cfg.risk.daily_max_loss_usd,
                                 self.cfg.risk.round_max_loss_usd)
        if reason:
            self.log(f"!! {reason}. Cancelling everything and stopping.")
            self.stop(reason)
            return False
        return True

    # ------------------------------------------------------------------ planning
    def _sell_only_books(self) -> list[str]:
        """Taker-only books where a round trip left shares behind: quote asks until flat."""
        return [b for b in self.cfg.taker_only_books() if self.ex.total_holding(b) > 0]

    def _priority_books(self) -> list[str]:
        books = self.cfg.all_books() + self._sell_only_books()
        botd = self.cfg.book_of_the_day
        rest = sorted((b for b in books if b != botd), key=lambda b: -self.flow.score(b))
        return ([botd] if botd in books else []) + rest

    def _low_flow(self, book: str) -> bool:
        if book == self.cfg.book_of_the_day:
            return False
        st = self.flow.stats(book)
        return (st["fills"] < self.cfg.flow.low_flow_fills
                and st["market_trades_per_hour"] < self.cfg.flow.low_flow_market_trades_per_hour)

    def plan(self, books: dict[str, Book], now: datetime | None = None) -> list[Action]:
        cfg, z = self.cfg, self.cfg.sizing
        now = now or now_et()
        extended = session(now, cfg.pace) != "regular"
        equity, stock_val = self._equity()
        buffer = max(equity * z.usdc_buffer_pct, z.usdc_buffer_min_usd)
        cash_budget = self.ex.usd_total() - buffer          # includes our current bid holds
        stock_room = equity * z.total_stock_cap_pct - stock_val
        actions: list[Action] = []
        for rank, sym in enumerate(self._priority_books()):
            prio = float(rank) + 1
            b = books.get(sym)
            paused = b is not None and self.vol.update(sym, b.mid)
            if b is None or paused:
                why = "no market data" if b is None else "volatility pause"
                for side in ("buy", "sell"):
                    for o in self._ours(sym, side):
                        actions.append(Action(0, sym, "cancel", side, order=o, why=why))
                continue
            pct = z.botd_quote_pct if sym == cfg.book_of_the_day else z.quote_pct
            quote_usd = equity * pct
            if extended:
                quote_usd *= z.extended_size_mult
            low = self._low_flow(sym)
            if low:
                quote_usd *= cfg.flow.low_flow_size_mult
            floor = z.min_botd_quote_usd if sym == cfg.book_of_the_day else z.min_quote_usd
            quote_usd = max(quote_usd, floor)  # small accounts: stay above the $1 exchange minimum
            sell_only = sym not in cfg.all_books()
            layered = cfg.layers.enabled and sym not in cfg.layers.single_layer_books and not sell_only
            p = Params(position_qty=self.ex.total_holding(sym),
                       usd_budget=D(0) if sell_only else max(cash_budget, D(0)),
                       quote_usd=quote_usd, book_cap_usd=equity * z.book_cap_pct,
                       stock_room_usd=max(stock_room, D(0)), extended=extended, layered=layered)
            d = decide(self._without_own(b), cfg, p)
            self.last_decisions[sym] = d
            want = {(q.side, q.layer): q for q in d.quotes}
            cash_budget -= sum((q.notional for q in d.quotes if q.side == "buy"), D(0))
            for side in ("buy", "sell"):
                # front-layer upkeep for every book runs before ANY depth-layer upkeep
                actions += self._side_actions(sym, side, "front", want.get((side, "front")), b, prio, low, d)
                actions += self._side_actions(sym, side, "depth", want.get((side, "depth")), b, 100 + prio, low, d)
            # orders from a layer we no longer use (e.g. layering switched off) are cancelled
            for o in [o for o in self.ex.orders.values() if o.book == sym and o.status in OPEN
                      and o.layer not in ("front", "depth")]:
                actions.append(Action(0.5, sym, "cancel", o.side, order=o, why=f"stray {o.layer} order"))
        actions.sort(key=lambda a: a.priority)
        return actions

    def _side_actions(self, book: str, side: str, layer: str, want: Quote | None, raw: Book, prio: float,
                      low_flow: bool, d: Decision) -> list[Action]:
        live = self._ours(book, side, layer)
        out = [Action(0.5, book, "cancel", side, order=o, why=f"duplicate {layer}") for o in live[1:]]
        cur = live[0] if live else None
        if want is None:
            if cur:
                out.append(Action(0.5 if d.pull else prio, book, "cancel", side, order=cur,
                                  why=f"{layer}: " + ("; ".join(d.reasons) or "no quote wanted")))
            return out
        if cur is None:
            out.append(Action(prio, book, "place", side, quote=want))
            return out
        if cur.status != "active":
            return out  # still being accepted; can't modify yet
        s, lay = self.cfg.strategy, self.cfg.layers
        gap_bps = abs(cur.price - want.price) / want.price * BPS
        downsize = cur.leaves > want.qty * D("1.25")
        upsize = cur.leaves < want.qty * D("0.6")
        since = time.time() - cur.last_action_at
        if layer == "depth":
            moved = gap_bps > lay.depth_requote_bps
            if since < lay.depth_refresh_secs and not downsize:
                return out
        else:
            moved = gap_bps > s.requote_threshold_bps
            at_best = (raw.bid == cur.price) if side == "buy" else (raw.ask == cur.price)
            # Queue priority: a modify can lose our place in line. Leave the front order alone while
            # it's at the best price and close to target, unless it must shrink (capital/inventory).
            if at_best and gap_bps < s.queue_hold_bps and not downsize:
                return out
        if low_flow and since < self.cfg.flow.low_flow_refresh_secs and not downsize:
            return out
        if moved or downsize or upsize:
            out.append(Action(prio + 0.1, book, "modify", side, order=cur, quote=want))
        return out

    # ------------------------------------------------------------------ execution
    def _safe_to_send(self, a: Action) -> bool:
        """Maker-only + no-self-trade guard against a FRESH book."""
        q = a.quote
        if q.taker:
            return True
        fresh = self.feed.fresh(a.book) or self.books_now.get(a.book)
        if fresh is None:
            self.log(f"  - {a.book} {a.side}: no fresh book; skipped")
            return False
        fb = self._without_own(fresh)
        if would_cross(a.side, q.price, fb.bid, fb.ask, self.cfg.strategy.no_cross_ticks):
            self.log(f"  - {a.book} {a.side} {q.price}: would cross fresh book "
                     f"({fresh.bid}/{fresh.ask}); skipped to stay maker")
            return False
        opp = "sell" if a.side == "buy" else "buy"
        for o in self._ours(a.book, opp):
            if (a.side == "buy" and q.price >= o.price) or (a.side == "sell" and q.price <= o.price):
                self.log(f"  - {a.book} {a.side} {q.price}: would cross our own {opp} @ {o.price}; skipped")
                return False
        return True

    def _do(self, a: Action) -> bool:
        if time.time() < self._backoff_until:
            return False
        if a.kind in ("place", "modify") and not self._safe_to_send(a):
            return False
        if not self.limiter.allow():
            self.log("  rate limit reached; waiting")
            return False
        try:
            if a.kind == "cancel":
                self.ex.cancel(a.order)
                self.log(f"  x {a.book:7} {a.side:4} cancel {a.order.price} [{a.order.layer}] ({a.why})")
            elif a.kind == "place":
                o = self.ex.place(a.book, a.side, a.quote.price, a.quote.qty, False, a.quote.layer)
                self.log(f"  + {a.book:7} {a.side:4} {o.qty} @ {o.price} [{o.layer}]")
            else:
                old = a.order.price
                self.ex.modify(a.order, a.quote.price, a.order.executed + a.quote.qty)
                self.log(f"  ~ {a.book:7} {a.side:4} {old} -> {a.order.price} qty {a.order.leaves} [{a.order.layer}]")
            return True
        except ApiError as e:
            if e.status == 409:
                self._backoff_until = time.time() + 2.0
                self.log(f"  . {a.book} {a.kind}: exchange busy ({e.message}); retrying shortly")
                return False
            if e.status == 400 and "invalid state" in (e.message or ""):
                return False  # order changed state under us; the next loop re-reads it
            self.log(f"  ! {a.book} {a.kind} {a.side} failed: {e}")
            if e.status in (401, 403):
                self.log("!! account is not permitted to do this; stopping instead of retrying")
                self.stop("permission denied")
            return False

    def execute(self, actions: list[Action]) -> bool:
        """Run actions in priority order within the time budget. Returns True if the queue was
        fully worked through (so there is an idle slot left for taker trades)."""
        t0 = time.time()
        for idx, a in enumerate(actions):
            if not self.running or time.time() - t0 > self.cfg.loop.action_budget_secs:
                return False
            self._do(a)
            if time.time() < self._backoff_until:
                return False
        return True

    # ------------------------------------------------------------------ one iteration
    def step(self, i: int) -> None:
        self._maybe_reload_botd()
        books = self.feed.snapshot()
        self.books_now = books
        for sym, b in books.items():
            if b.mid:
                self.marks[sym] = b.mid
            elif b.reference:
                self.marks[sym] = b.reference
            self.flow.observe_trade(sym, b.last_trade_ns)

        for f in self.ex.poll_fills(books):
            self.record_fill(f)
        self.markouts.update(self.marks)

        try:
            self.ex.refresh_balances()
        except ApiError as e:
            self.log(f"  ! balance refresh failed: {e}")
            return
        if i % 10 == 0:
            self._check_foreign()
        self._check_wallet()
        if not self.running or not self._check_risk():
            return
        idle = self.execute(self.plan(books))
        if idle and self.taker is not None and self.running and time.time() >= self._backoff_until:
            self.taker.maybe_trade(books)

    def record_fill(self, f) -> None:
        f.daily_book = f.book == self.cfg.book_of_the_day
        self.ledger.record(f)
        self.flow.add_fill(f)
        self.markouts.on_fill(f, self.marks.get(f.book))
        self.log(f"  * FILL {f.book} {f.side} {f.qty} @ {f.price} {'maker' if f.maker else 'TAKER'} [{f.layer}]"
                 f"{' BOTD' if f.daily_book else ''} (+{f.points:.1f} pts)")

    # ------------------------------------------------------------------ status / run
    def status_line(self) -> str:
        s = self.ledger.summary(self.marks, since=today_start_utc())
        eq, stocks = self._equity()
        sess = session(now_et(), self.cfg.pace)
        return (f"[{self.mode} {sess}] equity ${eq:.2f} (stocks ${stocks:.2f})  fills {s['fills']}  "
                f"maker ${s['maker_volume']:.2f} taker ${s['taker_volume']:.2f}  pts today {s['points']:.0f}  "
                f"pnl r {s['realized_pnl']:+.2f} u {s['unrealized_pnl']:+.2f}  open {len(self.ex.orders)}")

    def run(self, until: datetime | None = None, max_minutes: float | None = None) -> None:
        signal.signal(signal.SIGINT, lambda *_: self.stop("Ctrl+C"))
        try:
            signal.signal(signal.SIGTERM, lambda *_: self.stop("terminated"))
        except (ValueError, AttributeError):
            pass
        try:
            self.ex.refresh_balances()
        except ApiError as e:
            self.log(f"! could not read balances: {e}")
        i, last = 0, time.time()
        with KeepAwake() as ka:
            if getattr(ka, "active", False):
                self.log("keeping this PC awake while the bot runs")
            try:
                while self.running:
                    t0 = time.time()
                    try:
                        self.step(i)
                    except ApiError as e:
                        self.log(f"! step error: {e}")
                    except Exception as e:  # keep running, but make it visible
                        self.log(f"! unexpected error: {type(e).__name__}: {e}")
                    if i % 20 == 0:
                        self.log(self.status_line())
                    i += 1
                    if self.risk:
                        self.risk.add_run_seconds(time.time() - last)
                    last = time.time()
                    if until and datetime.now(ET) >= until:
                        self.stop(f"reached --until {until:%H:%M} ET")
                    if max_minutes and time.time() - self.start_ts > max_minutes * 60:
                        self.stop("time limit reached")
                    time.sleep(max(0.0, self.cfg.loop.poll_secs - (time.time() - t0)))
            finally:
                self.log(f"shutting down ({self.stop_reason or 'stopped'}): cancelling this bot's open orders")
                try:
                    n = self.ex.cancel_own()
                    self.log(f"cancelled {n} order(s)")
                except Exception as e:
                    self.log(f"! cancel failed: {e}  -> run `python -m tmbot cancel-all`")
                if self.lock:
                    self.lock.release()
                self.log(self.status_line())
