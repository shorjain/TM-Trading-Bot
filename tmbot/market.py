"""Market snapshots built from the public exchange EBBO feed."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from .client import ApiError, ExchangeData

D = Decimal


@dataclass
class Book:
    symbol: str  # e.g. "NVDAC"
    bid: Decimal | None
    bid_qty: Decimal
    ask: Decimal | None
    ask_qty: Decimal
    bid_orders: int
    ask_orders: int
    last_trade_px: Decimal | None
    last_trade_qty: Decimal
    last_trade_ns: int
    reference: Decimal | None
    updated_ns: int  # exchange's last book change (quiet books can be old; that is fine)
    fetched_at: float = 0.0  # when we fetched it (this is what staleness means for us)
    reference_changed_at: float = 0.0  # unix secs the reference price last changed

    @property
    def mid(self) -> Decimal | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_bps(self) -> Decimal | None:
        m = self.mid
        if m:
            return (self.ask - self.bid) / m * 10_000
        return None

    def age_secs(self, now: float | None = None) -> float:
        """Seconds since we fetched this snapshot."""
        now = time.time() if now is None else now
        return now - self.fetched_at if self.fetched_at else float("inf")

    def reference_age_secs(self, now: float | None = None) -> float:
        """Seconds since the reference price last changed (stale = market for the underlying
        is closed or the feed stopped)."""
        now = time.time() if now is None else now
        return now - self.reference_changed_at if self.reference_changed_at else float("inf")

    def quiet_secs(self, now: float | None = None) -> float:
        """Seconds since the exchange last changed this book."""
        now = time.time() if now is None else now
        return now - self.updated_ns / 1e9 if self.updated_ns else float("inf")


def _dec(v) -> Decimal | None:
    try:
        d = D(str(v))
        return d if d > 0 else None
    except Exception:
        return None


def parse_ebbo_row(row: dict, reference: Decimal | None = None, reference_changed_at: float = 0.0) -> Book:
    info = row.get("info", {})
    bb, ba, lt = info.get("best_bid") or {}, info.get("best_ask") or {}, info.get("last_trade") or {}
    sym = row["symbol"].split("-")[0]
    return Book(
        symbol=sym,
        bid=_dec(bb.get("price")), bid_qty=_dec(bb.get("qty")) or D(0),
        ask=_dec(ba.get("price")), ask_qty=_dec(ba.get("qty")) or D(0),
        bid_orders=int(bb.get("order_count") or 0), ask_orders=int(ba.get("order_count") or 0),
        last_trade_px=_dec(lt.get("price")), last_trade_qty=_dec(lt.get("qty")) or D(0),
        last_trade_ns=int(lt.get("timestamp") or 0),
        reference=reference,
        updated_ns=int(info.get("last_update") or 0),
        fetched_at=time.time(),
        reference_changed_at=reference_changed_at,
    )


class MarketFeed:
    """Polls EBBO for all books; refreshes reference prices less often.

    Reference staleness: the first time we see an instrument we take its `updated_timestamp`;
    after that, the reference counts as changed when its value changes."""

    def __init__(self, data: ExchangeData, books: list[str], quote_asset: str = "USDC",
                 reference_refresh_secs: float = 30.0):
        self.data = data
        self.books = list(books)
        self.quote_asset = quote_asset
        self.reference: dict[str, Decimal | None] = {}
        self.reference_changed_at: dict[str, float] = {}
        self.instrument_ids: dict[str, str] = {}
        self._ref_at = 0.0
        self.reference_refresh_secs = reference_refresh_secs

    def set_books(self, books: list[str]) -> None:
        if list(books) != self.books:
            self.books = list(books)
            self._ref_at = 0.0  # fetch references for any new book right away

    def _refresh_reference(self) -> None:
        now = time.time()
        for b in self.books:
            try:
                inst = self.data.instrument(f"{b}-{self.quote_asset}") or {}
            except ApiError:
                continue
            if inst.get("id"):
                self.instrument_ids[b] = str(inst["id"])
            ref = _dec(inst.get("info", {}).get("reference_price"))
            if b not in self.reference_changed_at:
                upd = int(inst.get("updated_timestamp") or 0)
                self.reference_changed_at[b] = upd / 1e9 if upd else now
            elif ref != self.reference.get(b):
                self.reference_changed_at[b] = now
            self.reference[b] = ref
        self._ref_at = now

    def _book(self, row: dict) -> Book:
        sym = row.get("symbol", "").partition("-")[0]
        return parse_ebbo_row(row, self.reference.get(sym), self.reference_changed_at.get(sym, 0.0))

    def snapshot(self) -> dict[str, Book]:
        if time.time() - self._ref_at > self.reference_refresh_secs:
            self._refresh_reference()
        out: dict[str, Book] = {}
        for row in self.data.ebbo():
            sym, _, quote = row.get("symbol", "").partition("-")
            if quote != self.quote_asset or sym not in self.books:
                continue
            out[sym] = self._book(row)
        return out

    def fresh(self, sym: str) -> Book | None:
        """One book, fetched right now (~0.1 s). Used for the pre-trade cross check."""
        iid = self.instrument_ids.get(sym)
        try:
            rows = self.data.get("/v1/cefi/market/quote", {"instrument_id": iid}) if iid else self.data.ebbo()
        except ApiError:
            return None
        for row in rows or []:
            if row.get("symbol") == f"{sym}-{self.quote_asset}":
                return self._book(row)
        return None
