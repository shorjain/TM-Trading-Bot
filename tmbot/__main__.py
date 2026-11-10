"""Command line entry point.

  python -m tmbot market                      # live books + what the bot would quote (no key needed)
  python -m tmbot run                         # PAPER trading against live data (default, no key needed)
  python -m tmbot run --live                  # REAL orders with real money (needs TM_KEY_FILE)
  python -m tmbot status                      # balances, open orders (read-only, needs key)
  python -m tmbot cancel-all                  # cancel every open CeFi order (needs key)
  python -m tmbot report [--live]             # volume / points / PnL from the fill log
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .auth import KeyFile
from .bot import Bot
from .client import ExchangeData, GatewayClient
from .config import load_config
from .executor import LiveExecutor, PaperExecutor
from .ledger import Ledger, today_start_utc
from .market import MarketFeed
from .strategy import decide

D = Decimal
CONFIRM = "I UNDERSTAND THIS USES REAL MONEY"


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"{_ts()} {msg}", flush=True)


def _env() -> str:
    env = os.getenv("TM_ENV", "prod")
    if env not in ("prod", "uat"):
        sys.exit(f"TM_ENV must be prod or uat, got {env!r}")
    return env


def _gateway(log_dir: str) -> GatewayClient:
    kf = os.getenv("TM_KEY_FILE")
    if not kf:
        sys.exit("Set TM_KEY_FILE in .env (path to your API key JSON). See README.")
    return GatewayClient(KeyFile.load(kf), env=_env(), log_dir=log_dir)


def _asset_ids(gw: GatewayClient, books: list[str]) -> dict[str, str]:
    ids = {a["symbol"]: a["id"] for a in gw.stock_listings() if a.get("venue") == "cefi"}
    missing = [b for b in books if b not in ids]
    if missing:
        log(f"! no CeFi listing for {missing}; skipping them")
    return {b: ids[b] for b in books if b in ids}


def cmd_market(args, cfg) -> None:
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/market"), cfg.books, cfg.quote_asset)
    books = feed.snapshot()
    print(f"{'book':7} {'bid':>10} {'ask':>10} {'sprd':>6} {'ref':>10} {'fair':>10} | would quote")
    for sym in cfg.books:
        b = books.get(sym)
        if not b:
            print(f"{sym:7} (no market)")
            continue
        d = decide(b, cfg, position_qty=D(0), usd_available=D(args.usd), gross_usd=D(0))
        qs = "  ".join(f"{q.side} {q.qty}@{q.price}" for q in d.quotes) or "; ".join(d.reasons)
        fmt = lambda v: f"{v:.3f}" if v else "-"
        sp = f"{b.spread_bps:.0f}" if b.spread_bps is not None else "-"
        print(f"{sym:7} {fmt(b.bid):>10} {fmt(b.ask):>10} {sp:>6} {fmt(b.reference):>10} {fmt(d.fair):>10} | {qs}")


def cmd_run(args, cfg) -> None:
    if args.books:
        cfg.books = [b.strip().upper() for b in args.books.split(",")]
    if args.book_of_day:
        cfg.book_of_the_day = args.book_of_day.upper()
    env = _env()
    if args.live:
        log_dir = f"logs/live-{env}"
        if env == "prod" and not args.yes:
            print("\nLIVE MODE: the bot will place real limit orders with real funds on True Markets.")
            print(f"Books: {', '.join(cfg.books)}   quote size ${cfg.sizing.quote_notional_usd}"
                  f"   max loss ${cfg.risk.max_loss_usd}")
            if input(f'Type "{CONFIRM}" to continue: ').strip() != CONFIRM:
                sys.exit("aborted")
        gw = _gateway(log_dir)
        ids = _asset_ids(gw, cfg.books)
        cfg.books = list(ids)
        ex = LiveExecutor(gw, ids, cfg.quote_asset)
        log("cancelling any leftover open orders before starting")
        ex.cancel_all()
    else:
        log_dir = "logs/paper"
        ex = PaperExecutor(usdc=D(args.paper_usdc))
    ledger = Ledger(Path(log_dir) / "fills.jsonl")
    ledger.load()
    feed = MarketFeed(ExchangeData(env, log_dir=log_dir), cfg.books, cfg.quote_asset)
    mode = f"LIVE {env}" if args.live else "PAPER"
    log(f"starting {mode} on {', '.join(cfg.books)} (book of the day: {cfg.book_of_the_day or 'not set'})")
    Bot(cfg, feed, ex, ledger, mode, log=log).run(max_minutes=args.minutes)


def cmd_status(args, cfg) -> None:
    gw = _gateway("logs/status")
    print("CeFi balances:")
    for r in gw.balances():
        if r.get("venue") == "cefi" or r.get("symbol") == "USDC":
            print(f"  {r['venue']:5} {r['symbol']:8} total {r.get('total')}  available {r.get('available')}"
                  f"  held {r.get('held')}  allocated {r.get('allocated')}")
    print("Open orders:")
    for o in gw.list_orders(status="active"):
        print(f"  {o['order_id']}  {o.get('asset_symbol') or o.get('base_asset')}  {o['side']} "
              f"{o.get('qty')} @ {o.get('price')}  filled {o.get('executed_qty')}")


def cmd_cancel_all(args, cfg) -> None:
    gw = _gateway("logs/status")
    print(gw.cancel_all("cefi"))


def cmd_report(args, cfg) -> None:
    path = Path("logs") / (f"live-{_env()}" if args.live else "paper") / "fills.jsonl"
    led = Ledger(path)
    led.load()
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/market"), cfg.books, cfg.quote_asset)
    marks = {s: (b.mid or b.reference) for s, b in feed.snapshot().items() if (b.mid or b.reference)}
    for label, since in (("today (UTC)", today_start_utc()), ("all time", 0.0)):
        s = led.summary(marks, since)
        print(f"== {label}")
        for k, v in s.items():
            print(f"  {k:15} {v}")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    os.environ.setdefault("TM_TELEMETRY", "0")
    p = argparse.ArgumentParser(prog="tmbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("market", help="show live books and the quotes the bot would place")
    m.add_argument("--usd", default="50", help="pretend USDC balance for sizing")

    r = sub.add_parser("run", help="run the bot (paper by default)")
    r.add_argument("--live", action="store_true", help="trade real money")
    r.add_argument("--yes", action="store_true", help="skip the live confirmation prompt")
    r.add_argument("--books", help="comma-separated override, e.g. NVDAC,TSLAC")
    r.add_argument("--book-of-day", help="today's 2x book from Discord")
    r.add_argument("--minutes", type=float, help="stop after N minutes")
    r.add_argument("--paper-usdc", default="50")

    sub.add_parser("status", help="balances and open orders (read-only)")
    sub.add_parser("cancel-all", help="cancel every open CeFi order")
    rep = sub.add_parser("report", help="volume, points and PnL from the fill log")
    rep.add_argument("--live", action="store_true")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    {"market": cmd_market, "run": cmd_run, "status": cmd_status,
     "cancel-all": cmd_cancel_all, "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
