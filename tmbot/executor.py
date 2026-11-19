"""Order executors with one interface.

- PaperExecutor: simulates resting orders against live public market data. No key, no
  money. A resting order fills when the opposite best price or a new last trade reaches
  it. Optimistic (no queue position), so treat paper results as an upper bound.
- LiveExecutor: places real limit orders through the Trading API.

Maker vs taker: the API doesn't report liquidity, so we infer it from price. A resting
(maker) limit fills AT its limit price. An order that crossed the book (taker) fills at the
resting price, which is better than its limit: lower for a buy, higher for a sell. A fill
exactly at the limit is counted as maker, unless the order was sent deliberately as a taker.
"""

from __future__ import annotations

import itertools
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal

from .client import ApiError, GatewayClient
from .increments import meets_min_notional, round_qty
from .ledger import Fill
from .market import Book

D = Decimal
FINAL = {"complete", "canceled", "failed"}
OPEN = {"active", "initialized", "pending"}


@dataclass
class LiveOrder:
    order_id: str
    book: str
    side: str
    price: Decimal
    qty: Decimal
    executed: Decimal = D(0)
    exec_value: Decimal = D(0)  # cumulative executed notional (to derive per-fill prices)
    status: str = "active"
    placed_at: float = 0.0
    last_action_at: float = 0.0
    taker_intent: bool = False
    layer: str = "front"

    @property
    def leaves(self) -> Decimal:
        return self.qty - self.executed

    @property
    def hold_usd(self) -> Decimal:
        return self.leaves * self.price if self.side == "buy" else D(0)


def classify_maker(side: str, limit: Decimal, fill_px: Decimal, taker_intent: bool) -> bool:
    if taker_intent:
        return False
    if side == "buy":
        return fill_px >= limit  # filled at (not below) our bid -> someone sold into us
    return fill_px <= limit


def shrink_factor(err: ApiError) -> Decimal | None:
    """For a 422 insufficient_balance, the fraction of the requested size that would fit."""
    if err.status != 422 or "insufficient" not in (err.code or "") + (err.message or ""):
        return None
    body = err.body if isinstance(err.body, dict) else {}
    meta = body.get("metadata") or {}
    try:
        needed = D(str(meta["needed"]))
        avail = needed - D(str(meta["shortfall"])) if "shortfall" in meta else D(str(meta["cefi_available"]))
    except (KeyError, ArithmeticError, ValueError):
        m = re.search(r"needs ([\d.]+), but only ([\d.]+)", err.message or "")
        if not m:
            return None
        needed, avail = D(m.group(1)), D(m.group(2))
    if needed <= 0 or avail <= 0:
        return D(0)
    return min(avail / needed, D(1)) * D("0.995")  # a hair under, for rounding


class _Base:
    """Shared bookkeeping: our open orders and the instance lock's owned-order list."""

    def __init__(self, lock=None):
        self.orders: dict[str, LiveOrder] = {}
        self.lock = lock

    def _own(self, o: LiveOrder) -> None:
        self.orders[o.order_id] = o
        if self.lock:
            self.lock.add(o.order_id)

    def _forget(self, o: LiveOrder) -> None:
        self.orders.pop(o.order_id, None)
        if self.lock:
            self.lock.discard([o.order_id])

    def bid_holds(self) -> Decimal:
        return sum((o.hold_usd for o in self.orders.values() if o.status in OPEN), D(0))

    def _fill_from(self, o: LiveOrder, executed: Decimal, vwap: Decimal | None, fee: Decimal) -> Fill | None:
        if executed <= o.executed:
            return None
        q = executed - o.executed
        new_value = executed * vwap if vwap else o.exec_value + q * o.price
        px = (new_value - o.exec_value) / q if q else o.price
        px = px.quantize(D("0.000001"))
        o.executed, o.exec_value = executed, new_value
        maker = classify_maker(o.side, o.price, px, o.taker_intent)
        return Fill(time.time(), o.book, o.side, q, px, maker, o.order_id, fee=fee, layer=o.layer)


class PaperExecutor(_Base):
    def __init__(self, usdc: Decimal = D(50), lock=None):
        super().__init__(lock=None)  # paper orders never go in the live lock file
        self.cash = usdc
        self.holdings: dict[str, Decimal] = {}
        self._ids = itertools.count(1)
        self._last_trade_seen: dict[str, int] = {}

    # -- balances --
    def usd_total(self) -> Decimal:
        return self.cash

    def usd_available(self) -> Decimal:
        return self.cash - self.bid_holds()

    def total_holding(self, book: str) -> Decimal:
        return self.holdings.get(book, D(0))

    def holdings_all(self) -> dict[str, Decimal]:
        return {b: q for b, q in self.holdings.items() if q > 0}

    def refresh_balances(self) -> None:
        pass

    def wallet_snapshot(self) -> dict:
        return {}

    # -- orders --
    def place(self, book: str, side: str, price: Decimal, qty: Decimal, taker: bool = False,
              layer: str = "front") -> LiveOrder:
        o = LiveOrder(f"paper-{next(self._ids)}", book, side, price, qty, placed_at=time.time(),
                      last_action_at=time.time(), taker_intent=taker, layer="taker" if taker else layer)
        self._own(o)
        return o

    def modify(self, o: LiveOrder, price: Decimal, qty: Decimal) -> None:
        o.price, o.qty, o.last_action_at = price, max(qty, o.executed), time.time()

    def cancel(self, o: LiveOrder) -> None:
        o.status = "canceled"
        self._forget(o)

    def cancel_own(self) -> int:
        n = len(self.orders)
        for o in list(self.orders.values()):
            self.cancel(o)
        return n

    def poll_fills(self, books: dict[str, Book]) -> list[Fill]:
        fills: list[Fill] = []
        for o in list(self.orders.values()):
            b = books.get(o.book)
            if not b:
                continue
            new_trade = b.last_trade_ns > self._last_trade_seen.get(o.book, 0) and \
                b.last_trade_ns / 1e9 >= o.placed_at
            px = avail = None
            if o.side == "buy":
                if b.ask and b.ask <= o.price:
                    px, avail = (b.ask if o.taker_intent else o.price), b.ask_qty
                elif new_trade and b.last_trade_px and b.last_trade_px <= o.price:
                    px, avail = o.price, b.last_trade_qty
            else:
                if b.bid and b.bid >= o.price:
                    px, avail = (b.bid if o.taker_intent else o.price), b.bid_qty
                elif new_trade and b.last_trade_px and b.last_trade_px >= o.price:
                    px, avail = o.price, b.last_trade_qty
            if px is None:
                continue
            q = min(o.leaves, avail) if avail and avail > 0 else o.leaves
            f = self._fill_from(o, o.executed + q, None, D(0))
            if f is None:
                continue
            f.price = px
            f.maker = classify_maker(o.side, o.price, px, o.taker_intent)
            if o.side == "buy":
                self.cash -= q * px
                self.holdings[o.book] = self.holdings.get(o.book, D(0)) + q
            else:
                self.cash += q * px
                self.holdings[o.book] = self.holdings.get(o.book, D(0)) - q
            fills.append(f)
            if o.leaves <= 0:
                o.status = "complete"
                self._forget(o)
        for sym, b in books.items():
            self._last_trade_seen[sym] = max(self._last_trade_seen.get(sym, 0), b.last_trade_ns)
        return fills


class LiveExecutor(_Base):
    def __init__(self, client: GatewayClient, asset_ids: dict[str, str], quote_asset: str = "USDC", lock=None):
        super().__init__(lock=lock)
        self.c = client
        self.asset_ids = asset_ids  # book symbol -> CeFi listing id
        self.quote_asset = quote_asset
        self._avail: dict[str, Decimal] = {}
        self._totals: dict[str, Decimal] = {}
        self._wallet: dict[str, Decimal] = {}

    # -- balances (exchange = CeFi; wallet = DeFi, watched but never spent) --
    def refresh_balances(self) -> None:
        avail: dict[str, Decimal] = {}
        totals: dict[str, Decimal] = {}
        wallet: dict[str, Decimal] = {}
        for row in self.c.balances():
            sym = row["symbol"]
            if row.get("venue") == "cefi":
                avail[sym] = avail.get(sym, D(0)) + D(str(row.get("available") or 0))
                totals[sym] = totals.get(sym, D(0)) + D(str(row.get("total") or 0))
            else:
                key = f"{sym}@{row.get('chain')}"
                wallet[key] = D(str(row.get("total") or 0)) - D(str(row.get("allocated") or 0))
        self._avail, self._totals, self._wallet = avail, totals, wallet

    def usd_total(self) -> Decimal:
        """Exchange USDC including what our resting bids hold."""
        return self._totals.get(self.quote_asset, D(0))

    def usd_available(self) -> Decimal:
        # Spend only what is already on the exchange: wallet auto-funding is unreliable
        # (see docs/FEEDBACK.md). TM_USE_WALLET_FUNDS=1 is no longer supported on purpose.
        return self._avail.get(self.quote_asset, D(0))

    def total_holding(self, book: str) -> Decimal:
        return self._totals.get(book, D(0))

    def holdings_all(self) -> dict[str, Decimal]:
        return {s: q for s, q in self._totals.items() if s != self.quote_asset and s in self.asset_ids and q > 0}

    def wallet_snapshot(self) -> dict[str, Decimal]:
        return dict(self._wallet)

    # -- orders --
    def _wait_ack(self, o: LiveOrder) -> None:
        # The gateway rejects any other order action while one is "in flight".
        for _ in range(10):
            if o.status not in ("initialized", "pending"):
                return
            time.sleep(0.5)
            try:
                o.status = self.c.order(o.order_id).get("status", o.status)
            except ApiError:
                pass

    def place(self, book: str, side: str, price: Decimal, qty: Decimal, taker: bool = False,
              layer: str = "front") -> LiveOrder:
        try:
            resp = self.c.create_limit(self.asset_ids[book], side, str(qty), str(price))
        except ApiError as e:
            f = shrink_factor(e)
            if f is None:
                raise
            # Shrink to what the balance can cover instead of retrying the same size.
            smaller = round_qty(qty * f, price)
            if not meets_min_notional(smaller, price):
                raise
            qty = smaller
            resp = self.c.create_limit(self.asset_ids[book], side, str(qty), str(price))
        o = LiveOrder(resp["order_id"], book, side, price, qty, placed_at=time.time(),
                      last_action_at=time.time(), status=resp.get("status", "pending"), taker_intent=taker,
                      layer="taker" if taker else layer)
        self._own(o)
        self._wait_ack(o)
        return o

    def modify(self, o: LiveOrder, price: Decimal, qty: Decimal) -> None:
        # qty = the order's new TOTAL quantity (filled + remaining).
        try:
            self.c.modify(o.order_id, price=str(price), qty=str(qty))
        except ApiError as e:
            f = shrink_factor(e)
            if f is None:
                raise
            leaves = round_qty((qty - o.executed) * f, price)
            if not meets_min_notional(leaves, price):
                raise
            qty = o.executed + leaves
            self.c.modify(o.order_id, price=str(price), qty=str(qty))
        o.price, o.qty, o.last_action_at = price, qty, time.time()

    def cancel(self, o: LiveOrder) -> None:
        try:
            self.c.cancel(o.order_id)
        except ApiError as e:
            if e.code not in ("order_not_open", "already_submitted") and e.status != 404:
                raise
        o.status = "cancel_pending"
        o.last_action_at = time.time()

    def cancel_ids(self, ids) -> int:
        n = 0
        for oid in ids:
            try:
                self.c.cancel(oid)
                n += 1
            except ApiError as e:
                if e.status == 409:
                    time.sleep(1.5)
                    try:
                        self.c.cancel(oid)
                        n += 1
                    except ApiError:
                        pass
            time.sleep(0.3)
        return n

    def cancel_own(self) -> int:
        """Cancel only orders this machine placed (never another instance's)."""
        ids = {o.order_id for o in self.orders.values() if o.status in OPEN}
        if self.lock:
            active = {d["order_id"] for d in self.c.list_orders(status="active")}
            ids |= self.lock.owned() & active
        n = self.cancel_ids(sorted(ids))
        for o in list(self.orders.values()):
            o.status = "cancel_pending"
        return n

    def poll_fills(self, books: dict[str, Book]) -> list[Fill]:
        fills: list[Fill] = []
        # One list call for everything still resting; individual reads only for orders that
        # left the active list (filled, canceled, failed) or haven't shown up in it yet.
        try:
            active = {d["order_id"]: d for d in self.c.list_orders(status="active")}
        except ApiError:
            active = {}
        for o in list(self.orders.values()):
            d = active.get(o.order_id)
            if d is None:
                try:
                    d = self.c.order(o.order_id)
                except ApiError:
                    continue
            status = d.get("status", o.status)
            executed = D(str(d.get("executed_qty") or 0))
            vwap = D(str(d["executed_vwap"])) if d.get("executed_vwap") not in (None, "", "0") else None
            fee_total = D(str(d.get("fee") or 0))
            prev_fee = getattr(o, "_fee_seen", D(0))
            f = self._fill_from(o, executed, vwap, fee_total - prev_fee)
            if f:
                o._fee_seen = fee_total
                fills.append(f)
            if o.status != "cancel_pending" or status in FINAL:
                o.status = status
            if status in FINAL:
                self._forget(o)
        return fills
