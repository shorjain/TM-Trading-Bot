"""Load config.yaml into typed settings (see config.yaml for what every value means)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

D = Decimal


@dataclass
class StrategyCfg:
    half_spread_bps_regular: Decimal = D(6)
    half_spread_bps_extended: Decimal = D(12)
    min_edge_bps: Decimal = D(2)
    inventory_skew_bps: Decimal = D(10)
    no_cross_ticks: int = 1
    requote_threshold_bps: Decimal = D(2)
    queue_hold_bps: Decimal = D(4)
    reference_weight: Decimal = D("0.4")
    reference_stale_secs: float = 180.0


@dataclass
class SizingCfg:
    quote_pct: Decimal = D("0.06")
    botd_quote_pct: Decimal = D("0.12")
    extended_size_mult: Decimal = D("0.5")
    book_cap_pct: Decimal = D("0.25")
    total_stock_cap_pct: Decimal = D("0.65")
    usdc_buffer_pct: Decimal = D("0.08")
    usdc_buffer_min_usd: Decimal = D(5)
    min_quote_usd: Decimal = D(5)
    min_botd_quote_usd: Decimal = D(8)


@dataclass
class FlowCfg:
    window_minutes: float = 60.0
    low_flow_fills: int = 2
    low_flow_market_trades_per_hour: float = 6.0
    low_flow_size_mult: Decimal = D("0.5")
    low_flow_refresh_secs: float = 30.0


@dataclass
class MarketCfg:
    stale_max_spread_bps: Decimal = D(60)
    dead_book_secs: float = 1800.0


@dataclass
class RiskCfg:
    daily_max_loss_usd: Decimal = D(20)
    round_max_loss_usd: Decimal = D(150)
    max_book_spread_bps: Decimal = D(250)
    max_fair_mid_gap_bps: Decimal = D(150)
    stale_data_secs: float = 120.0
    max_actions_per_minute: int = 40
    vol_move_bps: Decimal = D(40)
    vol_window_secs: float = 30.0
    vol_pause_secs: float = 120.0


@dataclass
class TakerCfg:
    enabled: bool = True
    max_spread_bps: Decimal = D(10)
    daily_cost_budget_usd: Decimal = D(10)
    max_trade_usd: Decimal = D(40)
    min_trade_usd: Decimal = D(2)
    prefer_book_of_the_day: bool = True
    book_cooldown_secs: float = 20.0
    extra_books: list = field(default_factory=list)
    extra_book_cooldown_secs: float = 5.0
    fill_wait_secs: float = 2.5
    cancel_confirm_secs: float = 6.0


@dataclass
class LayersCfg:
    enabled: bool = True
    front_pct: Decimal = D("0.25")
    depth_pct: Decimal = D("0.75")
    depth_ticks: int = 2
    depth_requote_bps: Decimal = D(6)
    depth_refresh_secs: float = 10.0
    single_layer_books: list = field(default_factory=list)


@dataclass
class LoopCfg:
    poll_secs: float = 3.0
    action_budget_secs: float = 2.5


@dataclass
class PaceCfg:
    goal_points: Decimal = D(60000)
    regular: str = "09:30-16:00"
    extended: str = "04:00-09:30,16:00-20:00"
    include_weekends: bool = False
    holidays: list = field(default_factory=list)
    early_closes: dict = field(default_factory=dict)


@dataclass
class Config:
    books: list[str] = field(default_factory=list)
    book_of_the_day: str | None = None
    quote_asset: str = "USDC"
    strategy: StrategyCfg = field(default_factory=StrategyCfg)
    sizing: SizingCfg = field(default_factory=SizingCfg)
    flow: FlowCfg = field(default_factory=FlowCfg)
    market: MarketCfg = field(default_factory=MarketCfg)
    risk: RiskCfg = field(default_factory=RiskCfg)
    taker: TakerCfg = field(default_factory=TakerCfg)
    layers: LayersCfg = field(default_factory=LayersCfg)
    loop: LoopCfg = field(default_factory=LoopCfg)
    pace: PaceCfg = field(default_factory=PaceCfg)
    path: Path | None = None

    def all_books(self) -> list[str]:
        """Configured books plus the book of the day (first, so it gets priority)."""
        out = list(self.books)
        if self.book_of_the_day:
            if self.book_of_the_day in out:
                out.remove(self.book_of_the_day)
            out.insert(0, self.book_of_the_day)
        return out

    def taker_only_books(self) -> list[str]:
        """Books used only for taker round trips (not quoted), when taker mode is on."""
        if not self.taker.enabled:
            return []
        quoted = set(self.all_books())
        return [str(b).upper() for b in self.taker.extra_books if str(b).upper() not in quoted]

    def feed_books(self) -> list[str]:
        return self.all_books() + self.taker_only_books()


def _fill(dc, raw: dict | None):
    """Copy known keys from raw into dataclass dc, coercing to the default's type."""
    for key, value in (raw or {}).items():
        if not hasattr(dc, key):
            raise ValueError(f"unknown config key: {type(dc).__name__}.{key}")
        current = getattr(dc, key)
        if isinstance(current, bool):
            value = bool(value)
        elif isinstance(current, Decimal):
            value = D(str(value))
        elif isinstance(current, int):
            value = int(value)
        elif isinstance(current, float):
            value = float(value)
        elif isinstance(current, list):
            value = list(value or [])
        elif isinstance(current, dict):
            value = dict(value or {})
        setattr(dc, key, value)
    return dc


def read_book_of_the_day(path: str | Path) -> str | None:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    v = raw.get("book_of_the_day")
    return str(v).upper() if v else None


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    cfg = Config(
        books=[str(b).upper() for b in raw.get("books", [])],
        book_of_the_day=(str(raw["book_of_the_day"]).upper() if raw.get("book_of_the_day") else None),
        quote_asset=raw.get("quote_asset", "USDC"),
        path=Path(path),
    )
    for name in ("strategy", "layers", "sizing", "flow", "market", "risk", "taker", "loop", "pace"):
        _fill(getattr(cfg, name), raw.get(name))
    return cfg
