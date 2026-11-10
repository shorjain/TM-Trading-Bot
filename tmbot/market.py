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


def parse_ebbo_row(row: dict, reference: Decimal | None = None) -> Book:
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
    )


class MarketFeed:
    """Polls EBBO for all books; refreshes reference prices less often."""

    def __init__(self, data: ExchangeData, books: list[str], quote_asset: str = "USDC",
                 reference_refresh_secs: float = 30.0):
        self.data = data
        self.books = books
        self.quote_asset = quote_asset
        self.reference: dict[str, Decimal | None] = {}
        self._ref_at = 0.0
        self.reference_refresh_secs = reference_refresh_secs

    def _refresh_reference(self) -> None:
        for b in self.books:
            try:
                inst = self.data.instrument(f"{b}-{self.quote_asset}")
                self.reference[b] = _dec((inst or {}).get("info", {}).get("reference_price"))
            except ApiError:
                self.reference[b] = None
        self._ref_at = time.time()

    def snapshot(self) -> dict[str, Book]:
        if time.time() - self._ref_at > self.reference_refresh_secs:
            self._refresh_reference()
        out: dict[str, Book] = {}
        for row in self.data.ebbo():
            sym, _, quote = row.get("symbol", "").partition("-")
            if quote != self.quote_asset or sym not in self.books:
                continue
            out[sym] = parse_ebbo_row(row, self.reference.get(sym))
        return out
