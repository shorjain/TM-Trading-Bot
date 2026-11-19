"""Command line entry point.

  python -m tmbot market                          # live books + what the bot would quote (no key)
  python -m tmbot run                             # PAPER trading on live data (no key)
  python -m tmbot run --live --until 15:55        # REAL money, stop at 15:55 ET
  python -m tmbot run --live --continuous         # REAL money, run until Ctrl+C (overnight)
  python -m tmbot status                          # balances, open orders (read-only)
  python -m tmbot report --live                   # points, pace, markouts, flow per book
  python -m tmbot cancel-all                      # EMERGENCY: cancel every open order on the account
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .analytics import markout_summary
from .auth import KeyFile
from .bot import Bot
from .client import ExchangeData, GatewayClient
from .config import load_config
from .executor import LiveExecutor, PaperExecutor
from .ledger import Ledger, today_start_utc
from .market import MarketFeed
from .sessions import ET, now_et, parse_until, session, tradeable_hours_between
from .state import InstanceLock, InstanceLockError, RiskState
from .strategy import Params, decide

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
    if args.book_of_day:
        cfg.book_of_the_day = args.book_of_day.upper()
    books_list = cfg.all_books()
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/market"), books_list, cfg.quote_asset)
    books = feed.snapshot()
    equity = D(args.usd)
    sess = session(now_et(), cfg.pace)
    print(f"session: {sess}   equity assumed ${equity}   book of the day: {cfg.book_of_the_day or '-'}")
    print(f"{'book':7} {'bid':>10} {'ask':>10} {'sprd':>5} {'ref':>10} {'ref age':>8} {'fair':>10} | would quote")
    for sym in books_list:
        b = books.get(sym)
        if not b:
            print(f"{sym:7} (no market)")
            continue
        pct = cfg.sizing.botd_quote_pct if sym == cfg.book_of_the_day else cfg.sizing.quote_pct
        q_usd = equity * pct * (cfg.sizing.extended_size_mult if sess != "regular" else 1)
        p = Params(usd_budget=equity * (1 - cfg.sizing.usdc_buffer_pct), quote_usd=q_usd,
                   book_cap_usd=equity * cfg.sizing.book_cap_pct,
                   stock_room_usd=equity * cfg.sizing.total_stock_cap_pct, extended=sess != "regular")
        d = decide(b, cfg, p)
        qs = "  ".join(f"{q.side} {q.qty}@{q.price}" for q in d.quotes) or ("PULL: " if d.pull else "") + "; ".join(d.reasons)
        fmt = lambda v: f"{v:.3f}" if v else "-"
        sp = f"{b.spread_bps:.0f}" if b.spread_bps is not None else "-"
        age = b.reference_age_secs()
        age_s = f"{age:.0f}s" if age != float("inf") else "-"
        print(f"{sym:7} {fmt(b.bid):>10} {fmt(b.ask):>10} {sp:>5} {fmt(b.reference):>10} {age_s:>8} "
              f"{fmt(d.fair):>10} | {qs}")


def cmd_run(args, cfg) -> None:
    if args.books:
        cfg.books = [b.strip().upper() for b in args.books.split(",")]
    if args.book_of_day:
        cfg.book_of_the_day = args.book_of_day.upper()
    if not cfg.book_of_the_day:
        print("\n" + "!" * 78)
        print("!! NO BOOK OF THE DAY SET. Its volume counts 2x. Check Discord, then either pass")
        print("!!   --book-of-day XXXX   or set `book_of_the_day:` in config.yaml (picked up live).")
        print("!" * 78 + "\n")
    until = None
    if args.until:
        until = parse_until(args.until)
    elif not args.continuous and not args.minutes:
        sys.exit("Say how long to run: --until HH:MM (ET), --minutes N, or --continuous.")
    env = _env()
    lock = risk = None
    if args.live:
        log_dir = f"logs/live-{env}"
        risk = RiskState("state/risk.json")
        if args.reset_risk:
            if input('Type "reset" to clear the persisted loss halt: ').strip() != "reset":
                sys.exit("aborted")
            risk.reset()
        halted = risk.halted()
        if halted:
            sys.exit(f"!! trading is halted: {halted}\n   (use --reset-risk only if you understand why it stopped)")
        lock = InstanceLock("state")
        try:
            lock.acquire()
        except InstanceLockError as e:
            sys.exit(f"!! {e}")
        gw = _gateway(log_dir)
        foreign = lock.foreign(gw.list_orders(status="active"))
        if foreign:
            print(f"\n!! {len(foreign)} open order(s) on this account were not placed by this machine's bot:")
            for o in foreign[:10]:
                print(f"   {o['order_id']}  {o.get('asset_symbol')} {o['side']} {o.get('qty')} @ {o.get('price')}")
            print("!! Another bot (the other computer?) or a manual order may be live. Running now could")
            print("!! make the two trade against each other (self-trading = disqualification).")
            if not args.force:
                lock.release()
                sys.exit("Stop the other bot first, or re-run with --force to cancel these orders.")
            if input('Type "cancel them" to cancel these orders and start: ').strip() != "cancel them":
                lock.release()
                sys.exit("aborted")
            LiveExecutor(gw, {}, cfg.quote_asset).cancel_ids([o["order_id"] for o in foreign])
        if env == "prod" and not args.yes:
            print("\nLIVE MODE: the bot will place real limit orders with real funds on True Markets.")
            stop_at = f"until {until:%a %H:%M} ET" if until else ("continuous" if args.continuous else f"{args.minutes} min")
            print(f"Books: {', '.join(cfg.all_books())}   quote {cfg.sizing.quote_pct:.0%} of equity"
                  f" (BOTD {cfg.sizing.botd_quote_pct:.0%})   daily max loss ${cfg.risk.daily_max_loss_usd}"
                  f"   {stop_at}")
            if input(f'Type "{CONFIRM}" to continue: ').strip() != CONFIRM:
                lock.release()
                sys.exit("aborted")
        ids = _asset_ids(gw, cfg.all_books())
        cfg.books = [b for b in cfg.books if b in ids]
        if cfg.book_of_the_day and cfg.book_of_the_day not in ids:
            log(f"!! book of the day {cfg.book_of_the_day} has no exchange listing; ignoring it")
            cfg.book_of_the_day = None
        ex = LiveExecutor(gw, ids, cfg.quote_asset, lock=lock)
        n = ex.cancel_own()
        if n:
            log(f"cancelled {n} leftover order(s) from this machine's previous run")
    else:
        log_dir = "logs/paper"
        ex = PaperExecutor(usdc=D(args.paper_usdc))
    ledger = Ledger(Path(log_dir) / "fills.jsonl")
    ledger.load()
    feed = MarketFeed(ExchangeData(env, log_dir=log_dir), cfg.all_books(), cfg.quote_asset)
    mode = f"LIVE {env}" if args.live else "PAPER"
    end = f"until {until:%a %H:%M} ET" if until else ("continuous" if args.continuous else f"{args.minutes} min")
    log(f"starting {mode} on {', '.join(cfg.all_books())} (book of the day: {cfg.book_of_the_day or 'NOT SET'}), {end}")
    bot = Bot(cfg, feed, ex, ledger, mode, log=log, lock=lock, risk=risk, log_dir=log_dir, live=args.live)
    bot.botd_from_cli = bool(args.book_of_day)
    bot.run(until=until, max_minutes=args.minutes)


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
    print("NOTE: this is a deliberate TAKER trade (1x points, pays the spread, counts toward the 30% "
          "single-counterparty rule).")
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
    """Emergency stop for the whole account (also cancels another computer's bot orders)."""
    gw = _gateway("logs/status")
    print(gw.cancel_all("cefi"))
    InstanceLock("state").discard(InstanceLock("state").owned())


def cmd_report(args, cfg) -> None:
    base = Path("logs") / (f"live-{_env()}" if args.live else "paper")
    led = Ledger(base / "fills.jsonl")
    led.load()
    books_list = sorted({f.book for f in led.fills} | set(cfg.all_books()))
    feed = MarketFeed(ExchangeData(_env(), log_dir="logs/market"), books_list, cfg.quote_asset)
    marks = {s_: (b.mid or b.reference) for s_, b in feed.snapshot().items() if (b.mid or b.reference)}
    for label, since in (("today (UTC)", today_start_utc()), ("all time", 0.0)):
        sm = led.summary(marks, since)
        print(f"== {label}")
        for k, v in sm.items():
            if isinstance(v, Decimal):
                v = f"{v:.2f}"
            print(f"  {k:15} {v}")

    run_h = RiskState("state/risk.json").run_hours() if args.live else 0.0
    allp = led.summary(marks, 0.0)
    pts = allp["points"]
    print("== per book (all time)")
    mk = markout_summary(base / "markouts.jsonl")
    print(f"  {'book':7} {'fills':>5} {'maker $':>9} {'taker $':>8} {'pts':>8} {'$/run-h':>8} "
          f"{'mk10s bps':>10} {'mk60s bps':>10}")
    for b in books_list:
        fs = [f for f in led.fills if f.book == b]
        if not fs:
            continue
        mv = sum((f.notional for f in fs if f.maker), D(0))
        tv = sum((f.notional for f in fs if not f.maker), D(0))
        bp = sum((f.points for f in fs), D(0))
        vph = f"{float(mv + tv) / run_h:.0f}" if run_h else "-"
        m10 = mk.get(b, {}).get(10)
        m60 = mk.get(b, {}).get(60)
        f10 = f"{m10[0]:+.1f} (n{m10[1]})" if m10 else "-"
        f60 = f"{m60[0]:+.1f} (n{m60[1]})" if m60 else "-"
        print(f"  {b:7} {len(fs):5} {mv:9.2f} {tv:8.2f} {bp:8.0f} {vph:>8} {f10:>10} {f60:>10}")
    print("  markout = mid move after our fill, + is good for us, - means we were picked off")

    goal = D(args.goal) if args.goal else cfg.pace.goal_points
    round_end = datetime(2026, 11, 30, 23, 59, tzinfo=timezone.utc).astimezone(ET)
    hours_left = tradeable_hours_between(now_et(), round_end, cfg.pace)
    need = max(goal - pts, D(0))
    print("== leaderboard pace")
    print(f"  points (this machine's log)  {pts:.0f} / goal {goal:.0f}")
    if run_h:
        print(f"  bot run time                 {run_h:.1f} h  ->  {float(pts) / run_h:.0f} pts/h, "
              f"${float(allp['maker_volume'] + allp['taker_volume']) / run_h:.0f} volume/h")
    print(f"  tradeable hours left         {hours_left:.1f} h (regular + extended hours per config.yaml `pace`)")
    if hours_left:
        per_h = need / D(str(hours_left))
        print(f"  needed per hour              {per_h:.0f} pts/h  (~${per_h / 3:.0f}/h maker volume)")


def main(argv: list[str] | None = None) -> None:
    load_dotenv()
    os.environ.setdefault("TM_TELEMETRY", "0")
    p = argparse.ArgumentParser(prog="tmbot", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("market", help="show live books and the quotes the bot would place")
    m.add_argument("--usd", default="550", help="pretend equity for sizing")
    m.add_argument("--book-of-day", help="include and size today's 2x book")

    r = sub.add_parser("run", help="run the bot (paper by default)")
    r.add_argument("--live", action="store_true", help="trade real money")
    r.add_argument("--yes", action="store_true", help="skip the live confirmation prompt")
    r.add_argument("--books", help="comma-separated override, e.g. NVDAC,TSLAC")
    r.add_argument("--book-of-day", help="today's 2x book from Discord")
    r.add_argument("--minutes", type=float, help="stop after N minutes")
    r.add_argument("--until", help="stop at HH:MM US-Eastern (next occurrence), e.g. 15:55 or 07:30")
    r.add_argument("--continuous", action="store_true", help="run until Ctrl+C (overnight)")
    r.add_argument("--force", action="store_true",
                   help="cancel open orders this machine didn't place, then start (asks first)")
    r.add_argument("--reset-risk", action="store_true", help="clear a persisted loss halt (asks first)")
    r.add_argument("--paper-usdc", default="550")

    c = sub.add_parser("cross", help="one-shot taker order at the best price (e.g. first fill)")
    c.add_argument("--book", required=True)
    c.add_argument("--side", choices=["buy", "sell"], default="buy")
    c.add_argument("--usd", default="5")
    c.add_argument("--slip-bps", default="10", help="max price above the ask (buy) / below the bid (sell)")

    al = sub.add_parser("allocate", help="make a stock in your wallet tradeable on the exchange")
    al.add_argument("--asset", required=True)
    al.add_argument("--amount", default="all")

    sub.add_parser("status", help="balances and open orders (read-only)")
    sub.add_parser("cancel-all", help="cancel every open CeFi order")
    rep = sub.add_parser("report", help="volume, points and PnL from the fill log")
    rep.add_argument("--live", action="store_true")
    rep.add_argument("--goal", help="points target (default: pace.goal_points in config.yaml)")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    {"market": cmd_market, "run": cmd_run, "status": cmd_status, "cross": cmd_cross,
     "allocate": cmd_allocate,
     "cancel-all": cmd_cancel_all, "report": cmd_report}[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
