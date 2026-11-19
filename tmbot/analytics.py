"""Per-book flow, volatility pause and fill markouts."""

from __future__ import annotations

import collections
import json
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .ledger import Fill

D = Decimal
BPS = D(10_000)


class FlowTracker:
    """Rolling fills/hour and volume/hour per book (ours), plus market trades/hour seen on the
    public feed. Used to give the single order-action slot and the capital to books with flow."""

    def __init__(self, window_minutes: float = 60.0):
        self.window = window_minutes * 60
        self.fills: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.market: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self._last_trade_ns: dict[str, int] = {}

    def _trim(self, dq: collections.deque, now: float) -> None:
        while dq and now - dq[0][0] > self.window:
            dq.popleft()

    def add_fill(self, f: Fill) -> None:
        self.fills[f.book].append((f.ts, f.notional))

    def seed(self, fills: list[Fill], now: float | None = None) -> None:
        now = now or time.time()
        for f in fills:
            if now - f.ts <= self.window:
                self.add_fill(f)

    def observe_trade(self, book: str, trade_ns: int, now: float | None = None) -> None:
        if trade_ns and trade_ns != self._last_trade_ns.get(book):
            if book in self._last_trade_ns:  # skip the first sighting (could be hours old)
                self.market[book].append((now or time.time(), D(0)))
            self._last_trade_ns[book] = trade_ns

    def stats(self, book: str, now: float | None = None) -> dict:
        now = now or time.time()
        f, m = self.fills[book], self.market[book]
        self._trim(f, now)
        self._trim(m, now)
        hours = self.window / 3600
        return {"fills": len(f), "fills_per_hour": len(f) / hours,
                "volume_per_hour": float(sum((n for _, n in f), D(0))) / hours,
                "market_trades_per_hour": len(m) / hours}

    def score(self, book: str, now: float | None = None) -> float:
        s = self.stats(book, now)
        return s["fills_per_hour"] + 0.5 * s["market_trades_per_hour"]


class VolGuard:
    """Pull a book's quotes for `pause_secs` if its mid moved more than `move_bps` within
    `window_secs`."""

    def __init__(self, move_bps: Decimal, window_secs: float, pause_secs: float):
        self.move_bps, self.window, self.pause = move_bps, window_secs, pause_secs
        self.mids: dict[str, collections.deque] = collections.defaultdict(collections.deque)
        self.paused_until: dict[str, float] = {}

    def update(self, book: str, mid: Decimal | None, now: float | None = None) -> bool:
        """Record a mid; returns True if the book is (now) paused."""
        now = now or time.time()
        if mid:
            dq = self.mids[book]
            dq.append((now, mid))
            while dq and now - dq[0][0] > self.window:
                dq.popleft()
            lo = min(m for _, m in dq)
            hi = max(m for _, m in dq)
            if lo > 0 and (hi - lo) / lo * BPS > self.move_bps:
                if now >= self.paused_until.get(book, 0):
                    self.paused_until[book] = now + self.pause
                dq.clear()
                dq.append((now, mid))
        return now < self.paused_until.get(book, 0)


@dataclass
class _Pending:
    fill: Fill
    mid_at_fill: Decimal
    due: list[int]


class Markouts:
    """Adverse-selection measurement: the mid 10 s and 60 s after each fill, in bps, signed so
    that positive = the market moved in our favour (good) and negative = we were picked off."""

    HORIZONS = (10, 60)

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.pending: list[_Pending] = []

    def on_fill(self, f: Fill, mid: Decimal | None) -> None:
        self.pending.append(_Pending(f, mid or f.price, list(self.HORIZONS)))

    @staticmethod
    def value_bps(side: str, fill_px: Decimal, mid_later: Decimal) -> Decimal:
        sign = 1 if side == "buy" else -1
        return sign * (mid_later - fill_px) / fill_px * BPS

    def update(self, mids: dict[str, Decimal], now: float | None = None) -> None:
        now = now or time.time()
        keep = []
        for p in self.pending:
            mid = mids.get(p.fill.book)
            while p.due and mid and now - p.fill.ts >= p.due[0]:
                h = p.due.pop(0)
                rec = {"ts": p.fill.ts, "book": p.fill.book, "side": p.fill.side, "price": str(p.fill.price),
                       "qty": str(p.fill.qty), "maker": p.fill.maker, "horizon_s": h,
                       "mid_at_fill": str(p.mid_at_fill), "mid_later": str(mid),
                       "markout_bps": str(round(self.value_bps(p.fill.side, p.fill.price, mid), 3))}
                with self.path.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")
            if p.due:
                keep.append(p)
        self.pending = keep


def markout_summary(path: str | Path) -> dict[str, dict[int, tuple[float, int]]]:
    """{book: {horizon: (avg_bps, n)}} from a markouts.jsonl file."""
    acc: dict[str, dict[int, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            r = json.loads(line)
            acc[r["book"]][int(r["horizon_s"])].append(float(r["markout_bps"]))
    return {b: {h: (sum(v) / len(v), len(v)) for h, v in hs.items()} for b, hs in acc.items()}
