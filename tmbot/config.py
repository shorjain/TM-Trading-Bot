"""Load config.yaml into typed settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

D = Decimal


@dataclass
class StrategyCfg:
    half_spread_bps: Decimal = D(20)
    min_edge_bps: Decimal = D(8)
    no_cross_ticks: int = 1
    inventory_skew_bps: Decimal = D(25)
    requote_threshold_bps: Decimal = D(4)
    reference_weight: Decimal = D("0.7")
    take_enabled: bool = False
    take_edge_bps: Decimal = D(60)


@dataclass
class SizingCfg:
    quote_notional_usd: Decimal = D(5)
    size_multiplier_daily_book: Decimal = D(2)
    max_position_usd: Decimal = D(12)
    max_gross_usd: Decimal = D(40)


@dataclass
class RiskCfg:
    max_loss_usd: Decimal = D(5)
    max_book_spread_bps: Decimal = D(250)
    max_fair_mid_gap_bps: Decimal = D(150)
    stale_data_secs: float = 120.0
    max_actions_per_minute: int = 40


@dataclass
class LoopCfg:
    poll_secs: float = 3.0
    reconcile_every: int = 5


@dataclass
class Config:
    books: list[str] = field(default_factory=list)
    book_of_the_day: str | None = None
    quote_asset: str = "USDC"
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    sizing: SizingCfg = field(default_factory=SizingCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    loop: LoopCfg = field(default_factory=LoopCfg)


def _fill(dc, raw: dict | None):
    """Copy known keys from raw into dataclass dc, coercing to the default's type."""
    for key, value in (raw or {}).items():
        if not hasattr(dc, key):
            raise ValueError(f"unknown config key: {key}")
        current = getattr(dc, key)
        if isinstance(current, Decimal):
            value = D(str(value))
        elif isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, int):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        setattr(dc, key, value)
    return dc


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config(
        books=[b.upper() for b in raw.get("books", [])],
        book_of_the_day=(raw.get("book_of_the_day") or None),
        quote_asset=raw.get("quote_asset", "USDC"),
    )
    if cfg.book_of_the_day:
        cfg.book_of_the_day = cfg.book_of_the_day.upper()
    _fill(cfg.strategy, raw.get("strategy"))
    _fill(cfg.sizing, raw.get("sizing"))
    _fill(cfg.risk, raw.get("risk"))
    _fill(cfg.loop, raw.get("loop"))
    return cfg
