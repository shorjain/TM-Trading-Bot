"""Command line entry point.

  python -m tmbot market                      # live books + what the bot would quote (no key needed)
  python -m tmbot run                         # PAPER trading against live data (default, no key needed)
  python -m tmbot run --live                  # REAL orders with real money (needs TM_KEY_FILE)
  python -m tmbot cross --book GOOGLC --usd 5  # one-shot taker buy at the best ask (asks first)
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


def cmd_cross(args, cfg) -> None:
    """One-shot taker order at the current best price, e.g. to get a first fill.
    Uses a limit at the touch (never a market order), so it can't fill worse than the price shown."""
    from .increments import qty_for_notional, round_qty
    from .ledger import Fill
    book = args.book.upper()
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/live-prod"), [book], cfg.quote_asset)
    b = feed.snapshot().get(book)
    if not b or not b.bid or not b.ask:
        sys.exit(f"no two-sided market for {book}")
    from .increments import round_price
    touch = b.ask if args.side == "buy" else b.bid
    # Allow a small, capped slippage: the book moves in the seconds before the order lands.
    slip = D(args.slip_bps) / D(10_000)
    px = round_price(touch * (1 + slip), "sell") if args.side == "buy" else round_price(touch * (1 - slip), "buy")
    qty = qty_for_notional(D(args.usd), px)
    if qty * px < 1:
        sys.exit("order below the $1 minimum")
    print(f"{book}: bid {b.bid} / ask {b.ask}  (spread {b.spread_bps:.1f} bps)")
    print(f"About to {args.side.upper()} {qty} {book} with limit {px} (best {touch}, max slippage {args.slip_bps} bps)"
          f" = up to ${qty * px:.2f} (taker, real money)")
    if input('Type "yes" to send: ').strip().lower() != "yes":
        sys.exit("aborted")
    gw = _gateway("logs/live-prod")
    ids = _asset_ids(gw, [book])
    resp = gw.create_limit(ids[book], args.side, str(qty), str(px))
    oid = resp["order_id"]
    print(f"order {oid}: {resp.get('status')}")
    d = resp
    for _ in range(20):
        d = gw.order(oid)
        if d.get("status") in ("complete", "canceled", "failed"):
            break
        time.sleep(1.5)
    executed = D(str(d.get("executed_qty") or 0))
    print(f"final: {d.get('status')}  filled {executed} @ {d.get('executed_vwap')}  fee {d.get('fee')}"
          f"  {d.get('failure_reason') or ''}")
    if d.get("status") not in ("complete", "canceled", "failed"):
        print("still resting -> cancelling the remainder")
        gw.cancel(oid)
    if executed > 0:
        led = Ledger(Path("logs/live-prod/fills.jsonl"))
        led.record(Fill(time.time(), book, args.side, executed, D(str(d.get("executed_vwap") or px)),
                        maker=False, order_id=oid, fee=D(str(d.get("fee") or 0))))


def cmd_allocate(args, cfg) -> None:
    """Make a stock you hold in your wallet (DeFi) tradeable on the exchange (CeFi).
    No tokens leave your wallet; the amount is reserved for exchange trading."""
    gw = _gateway("logs/live-prod")
    sym = args.asset.upper()
    row = next((r for r in gw.balances() if r.get("venue") == "defi" and r.get("symbol") == sym), None)
    if not row:
        sys.exit(f"no {sym} in your wallet")
    free = D(str(row.get("available") or 0)) - D(str(row.get("allocated") or 0))
    amount = free if args.amount == "all" else D(args.amount)
    if amount <= 0 or amount > free:
        sys.exit(f"{sym}: {free} free to allocate, asked for {amount}")
    print(f"Allocate {amount} {sym} (wallet on {row.get('chain')}) to exchange trading.")
    if input('Type "yes" to continue: ').strip().lower() != "yes":
        sys.exit("aborted")
    a = gw.post("/custody/allocations", {"asset_id": row["asset_id"], "amount": str(amount)})
    print(f"allocation {a.get('id')}: {a.get('status')}")
    for _ in range(40):
        if a.get("status") in ("completed", "rejected", "stranded"):
            break
        time.sleep(2)
        a = gw.get(f"/custody/allocations/{a['id']}")
    print(f"final: {a.get('status')} {a.get('failure_reason') or ''}")


def cmd_fund(args, cfg) -> None:
    """Move wallet stablecoins (PYUSD/USDC) onto the exchange as USDC in ONE step.

    How: place a single non-marketable limit buy (well below the market, so it cannot
    fill), sign the funding bridge it returns, wait until it rests, then cancel it. The
    converted USDC stays on the exchange. Doing this once avoids a bridge per bot order
    (money movements are limited to 20/min and 300/day)."""
    from .increments import qty_for_notional, round_price
    book = args.book.upper()
    usd = D(args.usd)
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/live-prod"), [book], cfg.quote_asset)
    b = feed.snapshot().get(book)
    if not b or not b.bid:
        sys.exit(f"no bid on {book}")
    px = round_price(b.bid * D("0.97"), "buy")  # 3% under the best bid: rests, never fills
    qty = qty_for_notional(usd, px)
    print(f"Funding ~${qty * px:.2f} USDC onto the exchange via a resting {book} buy "
          f"{qty} @ {px} (best bid {b.bid}); it will be cancelled right after.")
    if input('Type "yes" to continue: ').strip().lower() != "yes":
        sys.exit("aborted")
    gw = _gateway("logs/live-prod")
    ids = _asset_ids(gw, [book])
    resp = gw.create_limit(ids[book], "buy", str(qty), str(px))
    oid = resp["order_id"]
    d = resp
    for _ in range(40):
        d = gw.order(oid)
        if d.get("status") in ("active", "complete", "canceled", "failed"):
            break
        time.sleep(2)
    print(f"order {oid}: {d.get('status')} {d.get('failure_reason') or ''}")
    if d.get("status") == "active":
        gw.cancel(oid)
        print("cancelled; USDC stays on the exchange")
    time.sleep(3)
    for r in gw.balances():
        if r.get("symbol") in ("USDC", "PYUSD") and D(str(r.get("total") or 0)) > 0:
            print(f"  {r['venue']:5} {r['symbol']:6} total {r.get('total')} available {r.get('available')}"
                  f" allocated {r.get('allocated')}")


def cmd_status(args, cfg) -> None:
    gw = _gateway("logs/status")
    rows = gw.balances()
    print("Exchange (CeFi) balances, non-zero:")
    for r in rows:
        if r.get("venue") == "cefi" and D(str(r.get("total") or 0)) > 0:
            print(f"  {r['symbol']:8} total {r.get('total')}  available {r.get('available')}  held {r.get('held')}")
    print("Wallet (DeFi) balances:")
    for r in rows:
        if r.get("venue") != "cefi" and D(str(r.get("total") or 0)) > 0:
            print(f"  {r['symbol']:8} on {r.get('chain')}  total {r.get('total')}  available {r.get('available')}"
                  f"  allocated {r.get('allocated')}")
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
    # Leaderboard pace (Round 1 ends 2026-11-30 23:59 UTC).
    goal = D(args.goal)
    pts = led.summary(marks, 0.0)["points"]
    end = time.mktime((2026, 11, 30, 23, 59, 0, 0, 0, 0)) - time.timezone
    days_left = max((end - time.time()) / 86400, 0.01)
    need = max(goal - pts, D(0))
    print(f"== leaderboard pace\n  points so far   {pts:.0f} / goal {goal:.0f}"
          f"\n  days left       {days_left:.1f}\n  needed per day  {need / D(str(days_left)):.0f} pts"
          f"  (~${need / D(str(days_left)) / 3:.0f}/day of maker volume)")


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

    c = sub.add_parser("cross", help="one-shot taker order at the best price (e.g. first fill)")
    c.add_argument("--book", required=True)
    c.add_argument("--side", choices=["buy", "sell"], default="buy")
    c.add_argument("--usd", default="5")
    c.add_argument("--slip-bps", default="10", help="max price above the ask (buy) / below the bid (sell)")

    al = sub.add_parser("allocate", help="make a stock in your wallet tradeable on the exchange")
    al.add_argument("--asset", required=True)
    al.add_argument("--amount", default="all")
    fu = sub.add_parser("fund", help="convert wallet stablecoins to exchange USDC in one step")
    fu.add_argument("--usd", required=True)
    fu.add_argument("--book", default="NVDAC")

    sub.add_parser("status", help="balances and open orders (read-only)")
    sub.add_parser("cancel-all", help="cancel every open CeFi order")
    rep = sub.add_parser("report", help="volume, points and PnL from the fill log")
    rep.add_argument("--live", action="store_true")
    rep.add_argument("--goal", default="10000", help="points target for the leaderboard")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    {"market": cmd_market, "run": cmd_run, "status": cmd_status, "cross": cmd_cross,
     "allocate": cmd_allocate, "fund": cmd_fund,
     "cancel-all": cmd_cancel_all, "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
