"""Order executors with one interface.

- PaperExecutor: simulates resting orders against live public market data. No key,
  no money. A resting order fills when the opposite best price or a new last trade
  reaches it. This is optimistic (no queue position), so treat paper results as an
  upper bound.
- LiveExecutor: places real limit orders through the Trading API.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from decimal import Decimal

from .client import ApiError, GatewayClient
from .ledger import Fill
from .market import Book

D = Decimal
FINAL = {"complete", "canceled", "failed"}


@dataclass
class LiveOrder:
    order_id: str
    book: str
    side: str
    price: Decimal
    qty: Decimal
    executed: Decimal = D(0)
    status: str = "active"
    seen_resting: bool = False
    placed_at: float = 0.0
    taker_intent: bool = False

    @property
    def leaves(self) -> Decimal:
        return self.qty - self.executed


class PaperExecutor:
    def __init__(self, usdc: Decimal = D(50)):
        self.cash = usdc
        self.holdings: dict[str, Decimal] = {}
        self.orders: dict[str, LiveOrder] = {}
        self._ids = itertools.count(1)
        self._last_trade_seen: dict[str, int] = {}

    # -- balances --
    def usd_available(self) -> Decimal:
        held = sum((o.leaves * o.price for o in self.orders.values() if o.side == "buy"), D(0))
        return self.cash - held

    def holding(self, book: str) -> Decimal:
        held = sum((o.leaves for o in self.orders.values() if o.side == "sell" and o.book == book), D(0))
        return self.holdings.get(book, D(0)) - held

    def total_holding(self, book: str) -> Decimal:
        return self.holdings.get(book, D(0))

    # -- orders --
    def place(self, book: str, side: str, price: Decimal, qty: Decimal, asset_id: str = "",
              taker: bool = False) -> LiveOrder:
        oid = f"paper-{next(self._ids)}"
        o = LiveOrder(oid, book, side, price, qty, placed_at=time.time(), taker_intent=taker)
        self.orders[oid] = o
        return o

    def modify(self, o: LiveOrder, price: Decimal, qty: Decimal) -> None:
        o.price, o.qty = price, max(qty, o.executed)
        o.seen_resting = True

    def cancel(self, o: LiveOrder) -> None:
        o.status = "canceled"
        self.orders.pop(o.order_id, None)

    def cancel_all(self) -> None:
        for o in list(self.orders.values()):
            self.cancel(o)

    def poll_fills(self, books: dict[str, Book]) -> list[Fill]:
        fills: list[Fill] = []
        for o in list(self.orders.values()):
            b = books.get(o.book)
            if not b:
                continue
            px = None
            new_trade = b.last_trade_ns > self._last_trade_seen.get(o.book, 0) and \
                b.last_trade_ns / 1e9 >= o.placed_at
            if o.side == "buy":
                if b.ask and b.ask <= o.price:
                    px, avail = o.price, b.ask_qty
                elif new_trade and b.last_trade_px and b.last_trade_px <= o.price:
                    px, avail = o.price, b.last_trade_qty
            else:
                if b.bid and b.bid >= o.price:
                    px, avail = o.price, b.bid_qty
                elif new_trade and b.last_trade_px and b.last_trade_px >= o.price:
                    px, avail = o.price, b.last_trade_qty
            if px is None:
                o.seen_resting = True
                continue
            q = min(o.leaves, avail) if avail > 0 else o.leaves
            o.executed += q
            maker = o.seen_resting and not o.taker_intent
            if o.side == "buy":
                self.cash -= q * px
                self.holdings[o.book] = self.holdings.get(o.book, D(0)) + q
            else:
                self.cash += q * px
                self.holdings[o.book] = self.holdings.get(o.book, D(0)) - q
            fills.append(Fill(time.time(), o.book, o.side, q, px, maker, o.order_id))
            if o.leaves <= 0:
                o.status = "complete"
                self.orders.pop(o.order_id, None)
        for sym, b in books.items():
            self._last_trade_seen[sym] = max(self._last_trade_seen.get(sym, 0), b.last_trade_ns)
        return fills

    def refresh_balances(self) -> None:
        pass


class LiveExecutor:
    def __init__(self, client: GatewayClient, asset_ids: dict[str, str], quote_asset: str = "USDC"):
        self.c = client
        self.asset_ids = asset_ids  # book symbol -> CeFi listing id
        self.quote_asset = quote_asset
        self.orders: dict[str, LiveOrder] = {}
        self._bal: dict[str, Decimal] = {}

    # -- balances (CeFi trading account) --
    def refresh_balances(self) -> None:
        bal: dict[str, Decimal] = {}
        for row in self.c.balances():
            if row.get("venue") != "cefi":
                continue
            bal[row["symbol"]] = D(str(row.get("available") or 0))
        self._bal = bal

    def usd_available(self) -> Decimal:
        return self._bal.get(self.quote_asset, D(0))

    def holding(self, book: str) -> Decimal:
        return self._bal.get(book, D(0))

    def total_holding(self, book: str) -> Decimal:
        held = sum((o.leaves for o in self.orders.values() if o.side == "sell" and o.book == book), D(0))
        return self._bal.get(book, D(0)) + held

    # -- orders --
    def place(self, book: str, side: str, price: Decimal, qty: Decimal, asset_id: str = "",
              taker: bool = False) -> LiveOrder:
        resp = self.c.create_limit(self.asset_ids[book], side, str(qty), str(price))
        o = LiveOrder(resp["order_id"], book, side, price, qty, placed_at=time.time(),
                      status=resp.get("status", "active"), taker_intent=taker)
        o.seen_resting = o.status == "active"
        self.orders[o.order_id] = o
        return o

    def modify(self, o: LiveOrder, price: Decimal, qty: Decimal) -> None:
        # qty is sent as the order's new *total* quantity (filled + remaining). The docs don't
        # say whether PATCH qty means total or remaining; check this on the first live run.
        self.c.modify(o.order_id, price=str(price), qty=str(qty))
        o.price, o.qty = price, qty

    def cancel(self, o: LiveOrder) -> None:
        try:
            self.c.cancel(o.order_id)
        except ApiError as e:
            if e.code not in ("order_not_open", "already_submitted"):
                raise
        o.status = "cancel_pending"

    def cancel_all(self) -> None:
        try:
            self.c.cancel_all("cefi")
        finally:
            for o in list(self.orders.values()):
                o.status = "cancel_pending"

    def poll_fills(self, books: dict[str, Book]) -> list[Fill]:
        fills: list[Fill] = []
        # One list call for everything still resting; individual reads only for orders that
        # left the active list (filled, canceled, failed) or have not shown up in it yet.
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
            if executed > o.executed:
                q = executed - o.executed
                px = D(str(d.get("executed_vwap") or o.price))
                fee = D(str(d.get("fee") or 0))
                maker = o.seen_resting and not o.taker_intent
                fills.append(Fill(time.time(), o.book, o.side, q, px, maker, o.order_id, fee=fee if status in FINAL else D(0)))
                o.executed = executed
            if status == "active":
                o.seen_resting = True
            o.status = status
            if status in FINAL:
                self.orders.pop(o.order_id, None)
        return fills
