"""Fills, positions, PnL and competition points.

Points follow the Orderbook Arena rules: USD volume, maker x3, taker x1,
book of the day x2 on top. The exchange does not tell us maker/taker directly
through the Trading API, so a fill counts as maker if the order was seen resting
(status `active`) before it filled.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

D = Decimal


@dataclass
class Fill:
    ts: float
    book: str
    side: str
    qty: Decimal
    price: Decimal
    maker: bool
    order_id: str
    fee: Decimal = D(0)
    daily_book: bool = False

    @property
    def notional(self) -> Decimal:
        return self.qty * self.price

    @property
    def points(self) -> Decimal:
        p = self.notional * (3 if self.maker else 1)
        return p * 2 if self.daily_book else p


@dataclass
class Position:
    qty: Decimal = D(0)
    cost: Decimal = D(0)  # total cost basis of qty
    realized: Decimal = D(0)

    @property
    def avg(self) -> Decimal:
        return self.cost / self.qty if self.qty else D(0)


@dataclass
class Ledger:
    path: Path = Path("logs/fills.jsonl")
    positions: dict[str, Position] = field(default_factory=lambda: defaultdict(Position))
    fills: list[Fill] = field(default_factory=list)
    fees: Decimal = D(0)

    def __post_init__(self):
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, f: Fill, persist: bool = True) -> None:
        pos = self.positions[f.book]
        if f.side == "buy":
            pos.qty += f.qty
            pos.cost += f.notional
        else:
            sell_qty = min(f.qty, pos.qty)
            avg = pos.avg
            pos.realized += (f.price - avg) * sell_qty
            pos.cost -= avg * sell_qty
            pos.qty -= sell_qty
            if pos.qty <= 0:
                pos.qty, pos.cost = D(0), D(0)
        pos.realized -= f.fee
        self.fees += f.fee
        self.fills.append(f)
        if persist:
            with self.path.open("a") as fh:
                rec = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(f).items()}
                fh.write(json.dumps(rec) + "\n")

    def load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            r = json.loads(line)
            for k in ("qty", "price", "fee"):
                r[k] = D(r[k])
            self.record(Fill(**r), persist=False)

    # ---- reporting ----
    def realized(self) -> Decimal:
        return sum((p.realized for p in self.positions.values()), D(0))

    def unrealized(self, marks: dict[str, Decimal]) -> Decimal:
        return sum(((marks.get(b, p.avg) - p.avg) * p.qty for b, p in self.positions.items()), D(0))

    def summary(self, marks: dict[str, Decimal], since: float = 0.0) -> dict:
        fs = [f for f in self.fills if f.ts >= since]
        maker = sum((f.notional for f in fs if f.maker), D(0))
        taker = sum((f.notional for f in fs if not f.maker), D(0))
        by_book: dict[str, Decimal] = defaultdict(D)
        for f in fs:
            by_book[f.book] += f.notional
        return {
            "fills": len(fs),
            "maker_volume": maker,
            "taker_volume": taker,
            "points": sum((f.points for f in fs), D(0)),
            "realized_pnl": self.realized(),
            "unrealized_pnl": self.unrealized(marks),
            "fees": self.fees,
            "volume_by_book": dict(by_book),
            "positions": {b: (p.qty, p.avg) for b, p in self.positions.items() if p.qty},
        }


def today_start_utc() -> float:
    t = time.gmtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, 0)) - time.timezone
