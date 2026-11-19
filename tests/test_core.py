import base64
import json
import time
import types
from datetime import datetime
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from tmbot.analytics import Markouts, VolGuard, markout_summary
from tmbot.auth import KeyFile
from tmbot.bot import Bot
from tmbot.client import ApiError
from tmbot.config import Config, load_config
from tmbot.executor import PaperExecutor, classify_maker, shrink_factor
from tmbot.increments import qty_for_notional, round_price, round_qty, tick_for
from tmbot.ledger import Fill, Ledger
from tmbot.market import Book
from tmbot.sessions import ET, parse_until, session
from tmbot.state import InstanceLock, InstanceLockError, RiskState
from tmbot.strategy import Params, decide, would_cross


def book(bid="100.0", ask="101.0", ref="100.5", sym="NVDAC", ref_age=10.0, quiet=5.0, **kw) -> Book:
    now = time.time()
    return Book(sym, D(bid) if bid else None, D("1"), D(ask) if ask else None, D("1"), 1, 1,
                kw.get("ltp"), D(kw.get("ltq", "0")), kw.get("ltns", 0),
                D(ref) if ref else None, int((now - quiet) * 1e9), fetched_at=now,
                reference_changed_at=now - ref_age)


def params(**kw) -> Params:
    base = dict(position_qty=D(0), usd_budget=D(500), quote_usd=D(33), book_cap_usd=D(137),
                stock_room_usd=D(357))
    base.update(kw)
    return Params(**base)


def fake_feed(books: dict, fresh=None):
    return types.SimpleNamespace(snapshot=lambda: books, fresh=fresh or (lambda s: books.get(s)),
                                 set_books=lambda b: None)


@pytest.fixture
def cfg() -> Config:
    c = load_config("config.yaml")
    c.book_of_the_day = None
    c.path = None
    return c


# ---------------- increments ----------------
def test_increments_bucket5():
    assert tick_for(D("178.33")) == D("0.001")
    assert round_price(D("178.3337"), "buy") == D("178.333")
    assert round_price(D("178.3331"), "sell") == D("178.334")
    assert round_qty(D("0.02899"), D("178")) == D("0.028")
    assert qty_for_notional(D(5), D("178.33")) == D("0.028")


# ---------------- strategy: queue rules ----------------
def test_improves_best_bid_by_one_tick_when_inside_edge(cfg):
    d = decide(book(), cfg, params())  # wide book: best bid 100.0 is far from fair ~100.5
    bid = next(q for q in d.quotes if q.side == "buy")
    assert bid.price == D("100.001")


def test_joins_best_when_improving_would_breach_edge(cfg):
    # fair = 100.05 (ref = mid); 6 bps -> cap 99.98997; best bid 99.989: +1 tick would breach -> join
    d = decide(book(bid="99.989", ask="100.111", ref="100.05"), cfg, params())
    bid = next(q for q in d.quotes if q.side == "buy")
    assert bid.price == D("99.989")


def test_sits_behind_when_best_is_too_close_to_fair(cfg):
    d = decide(book(bid="100.04", ask="100.06", ref="100.05"), cfg, params())
    bid = next(q for q in d.quotes if q.side == "buy")
    assert bid.price < D("100.04")
    assert bid.price <= D("100.05") * (1 - D(6) / 10_000)


def test_ask_only_with_inventory_and_above_bid(cfg):
    assert all(q.side == "buy" for q in decide(book(), cfg, params()).quotes)
    d = decide(book(), cfg, params(position_qty=D("0.2")))
    sides = {q.side: q for q in d.quotes}
    assert sides["sell"].qty <= D("0.2")
    assert sides["sell"].price > sides["buy"].price


def test_caps_block_bid(cfg):
    d = decide(book(), cfg, params(position_qty=D("1.4")))  # ~$140 held > $137 book cap
    assert all(q.side == "sell" for q in d.quotes)
    d2 = decide(book(), cfg, params(usd_budget=D("0.5")))
    assert d2.quotes == []


# ---------------- maker-only cross check ----------------
def test_would_cross():
    assert would_cross("buy", D("100.5"), D("100"), D("100.5"))      # at the ask = taker
    assert would_cross("buy", D("100.5"), D("100"), D("100.4"))      # through the ask
    assert not would_cross("buy", D("100.399"), D("100"), D("100.4"))
    assert would_cross("sell", D("100.0"), D("100.0"), D("101"))
    assert not would_cross("sell", D("100.001"), D("100.0"), D("101"))


def test_strategy_never_quotes_a_crossing_price(cfg):
    for bid, ask in (("100.000", "100.001"), ("100.000", "100.002"), ("99.5", "100.5")):
        d = decide(book(bid=bid, ask=ask, ref="100.0"), cfg, params(position_qty=D("0.3")))
        for q in d.quotes:
            assert not would_cross(q.side, q.price, D(bid), D(ask)), (bid, ask, q)


def test_bot_skips_order_that_would_cross_fresh_book(cfg):
    cfg.books = ["NVDAC"]
    ex = PaperExecutor(D(500))
    stale = book(bid="100.0", ask="101.0")
    moved = book(bid="100.0", ask="100.001")  # the ask dropped right before we send
    bot = Bot(cfg, fake_feed({"NVDAC": stale}, fresh=lambda s: moved), ex,
              Ledger(path="/tmp/tmbot-t1.jsonl"), "t", log=lambda *_: None)
    bot.step(1)
    assert not ex.orders  # the bid at 100.001 would have crossed; it was skipped


def test_maker_classification_from_price():
    assert classify_maker("buy", D("100"), D("100"), False)          # filled at our bid
    assert not classify_maker("buy", D("100"), D("99.9"), False)     # crossed: got the ask
    assert not classify_maker("buy", D("100"), D("100"), True)       # deliberate taker
    assert classify_maker("sell", D("100"), D("100"), False)
    assert not classify_maker("sell", D("100"), D("100.2"), False)


# ---------------- buffer + 422 shrink ----------------
def test_shrink_factor_from_metadata_and_message():
    e = ApiError("PATCH", "/orders/x", 422, {"type": "failed_precondition", "code": "insufficient_balance",
                                              "message": "x", "metadata": {"needed": "4", "shortfall": "1"}})
    assert D("0.74") < shrink_factor(e) < D("0.75")
    e2 = ApiError("PATCH", "/orders/x", 422, {"code": "insufficient_balance",
                  "message": "insufficient USDC: the order needs 3.47838, but only 2.736355 of your 20.4 balance is available"})
    assert D("0.78") < shrink_factor(e2) < D("0.79")
    assert shrink_factor(ApiError("POST", "/x", 409, {"message": "in flight"})) is None


def test_usdc_buffer_is_never_committed(cfg):
    cfg.books = ["NVDAC", "METAC", "PLTRC", "AAPLC"]
    cfg.loop.action_budget_secs = 10
    ex = PaperExecutor(D(100))
    books = {s: book(sym=s) for s in cfg.books}
    bot = Bot(cfg, fake_feed(books), ex, Ledger(path="/tmp/tmbot-t2.jsonl"), "t", log=lambda *_: None)
    bot.step(1)
    committed = sum(o.leaves * o.price for o in ex.orders.values() if o.side == "buy")
    buffer = max(D(100) * cfg.sizing.usdc_buffer_pct, cfg.sizing.usdc_buffer_min_usd)
    assert D(0) < committed <= D(100) - buffer


# ---------------- book of the day ----------------
def test_botd_added_first_and_doubles_size(cfg):
    cfg.books = ["NVDAC", "METAC"]
    cfg.book_of_the_day = "PLTRC"
    assert cfg.all_books()[0] == "PLTRC"
    ex = PaperExecutor(D(500))
    books = {s: book(sym=s) for s in cfg.all_books()}
    bot = Bot(cfg, fake_feed(books), ex, Ledger(path="/tmp/tmbot-t3.jsonl"), "t", log=lambda *_: None)
    actions = bot.plan(books, now=datetime(2026, 11, 20, 11, 0, tzinfo=ET))  # regular hours
    places = [a for a in actions if a.kind == "place"]
    assert places[0].book == "PLTRC"
    botd = next(a for a in places if a.book == "PLTRC").quote.notional
    other = next(a for a in places if a.book == "NVDAC").quote.notional
    assert botd > other * D("1.8")


def test_botd_fill_counts_double():
    f = Fill(0, "PLTRC", "buy", D("0.1"), D(100), maker=True, order_id="a", daily_book=True)
    assert f.points == D(10) * 3 * 2


# ---------------- stale / closed markets ----------------
def test_one_sided_book_pulls(cfg):
    d = decide(book(ask=None), cfg, params())
    assert d.pull and not d.quotes


def test_stale_reference_uses_mid_and_wider_spread(cfg):
    fresh = decide(book(bid="99.9", ask="100.1", ref="100.0"), cfg, params())
    stale = decide(book(bid="99.9", ask="100.1", ref="105.0", ref_age=10_000), cfg, params())
    assert stale.reference_stale and stale.fair == D("100.0")  # ignores the stale 105 reference
    fb = next(q for q in fresh.quotes if q.side == "buy").price
    sb = next(q for q in stale.quotes if q.side == "buy").price
    assert sb < fb  # wider (12 bps) when stale


def test_stale_reference_and_wide_book_pulls(cfg):
    assert decide(book(bid="99", ask="101", ref="100", ref_age=10_000), cfg, params()).pull


def test_dead_market_pulls(cfg):
    d = decide(book(bid="99.99", ask="100.01", ref="100", ref_age=10_000, quiet=4000), cfg, params())
    assert d.pull


def test_sessions():
    p = load_config("config.yaml").pace
    assert session(datetime(2026, 11, 20, 10, 0, tzinfo=ET), p) == "regular"
    assert session(datetime(2026, 11, 20, 17, 0, tzinfo=ET), p) == "extended"
    assert session(datetime(2026, 11, 20, 23, 0, tzinfo=ET), p) == "closed"
    assert session(datetime(2026, 11, 26, 10, 0, tzinfo=ET), p) != "regular"    # Thanksgiving
    assert session(datetime(2026, 11, 27, 12, 0, tzinfo=ET), p) == "regular"
    assert session(datetime(2026, 11, 27, 14, 0, tzinfo=ET), p) != "regular"    # early close 13:00
    u = parse_until("07:30", datetime(2026, 11, 20, 22, 0, tzinfo=ET))
    assert (u.day, u.hour, u.minute) == (21, 7, 30)


def test_volatility_pause():
    v = VolGuard(D(40), 30, 120)
    assert not v.update("X", D(100), now=1)
    assert v.update("X", D("100.5"), now=5)      # 50 bps in 4 s -> pause
    assert v.update("X", D("100.5"), now=100)
    assert not v.update("X", D("100.5"), now=200)


# ---------------- risk persistence ----------------
def test_risk_limits_persist(tmp_path):
    path = tmp_path / "risk.json"
    r = RiskState(path)
    now = datetime(2026, 11, 20, 12, 0, tzinfo=ET)
    assert r.check(D(0), D(0), D(20), D(150), now) is None           # sets the day baseline
    reason = r.check(D(-15), D(-6), D(20), D(150), now)
    assert reason and "daily" in reason
    assert RiskState(path).halted(now) is not None                   # survives a "restart"
    assert RiskState(path).halted(datetime(2026, 11, 21, 0, 1, tzinfo=ET)) is None  # next day clears
    assert "round" in RiskState(path).check(D(-140), D(-11), D(1000), D(150), now)
    assert RiskState(path).halted(datetime(2026, 12, 5, tzinfo=ET))  # round halt never expires


# ---------------- instance lock ----------------
def test_instance_lock(tmp_path):
    a = InstanceLock(tmp_path)
    a.acquire()
    a.add("mine-1")
    active = [{"order_id": "mine-1"}, {"order_id": "pc-9"}, {"order_id": "svc", "placed_by": "service"}]
    assert [o["order_id"] for o in a.foreign(active)] == ["pc-9"]
    # a second live process on the same machine is refused
    (tmp_path / "instance.lock").write_text(json.dumps({"machine": a.machine, "pid": 1, "started": "x"}))
    with pytest.raises(InstanceLockError):
        InstanceLock(tmp_path).acquire()
    # a dead pid's lock is taken over
    (tmp_path / "instance.lock").write_text(json.dumps({"machine": a.machine, "pid": 999999, "started": "x"}))
    InstanceLock(tmp_path).acquire()


def test_bot_stops_when_foreign_orders_appear(cfg, tmp_path):
    lock = InstanceLock(tmp_path)
    ex = PaperExecutor(D(100))
    ex.c = types.SimpleNamespace(list_orders=lambda status=None: [{"order_id": "other-computer"}])
    bot = Bot(cfg, fake_feed({}), ex, Ledger(path="/tmp/tmbot-t4.jsonl"), "t", log=lambda *_: None,
              lock=lock, live=True)
    bot._check_foreign()
    assert not bot.running and "foreign" in bot.stop_reason


# ---------------- markouts ----------------
def test_markouts(tmp_path):
    m = Markouts(tmp_path / "mk.jsonl")
    f = Fill(1000.0, "NVDAC", "buy", D("0.1"), D(100), maker=True, order_id="a")
    m.on_fill(f, D(100))
    m.update({"NVDAC": D("100.1")}, now=1011)   # +10 bps after 10 s
    m.update({"NVDAC": D("99.9")}, now=1061)    # -10 bps after 60 s
    s = markout_summary(tmp_path / "mk.jsonl")
    assert round(s["NVDAC"][10][0], 2) == 10.0 and round(s["NVDAC"][60][0], 2) == -10.0


# ---------------- ledger / paper ----------------
def test_points_and_pnl():
    led = Ledger(path="/tmp/tmbot-test-fills.jsonl")
    led.path.unlink(missing_ok=True)
    led.record(Fill(0, "NVDAC", "buy", D("0.1"), D(100), maker=True, order_id="a"))
    led.record(Fill(0, "NVDAC", "sell", D("0.1"), D(101), maker=False, order_id="b", daily_book=True))
    s = led.summary({})
    assert s["maker_volume"] == D(10) and s["taker_volume"] == D("10.1")
    assert s["points"] == D(10) * 3 + D("10.1") * 2
    assert s["realized_pnl"] == D("0.1")


def test_paper_fill_is_maker_at_limit():
    ex = PaperExecutor(D(50))
    o = ex.place("NVDAC", "buy", D("100.5"), D("0.02"))
    assert ex.poll_fills({"NVDAC": book()}) == []
    fills = ex.poll_fills({"NVDAC": book(bid="100.0", ask="100.4")})
    assert len(fills) == 1 and fills[0].maker and fills[0].price == D("100.5")
    assert o.order_id not in ex.orders


def test_live_bot_does_not_outbid_itself(cfg):
    ex = PaperExecutor(D(500))
    o = ex.place("NVDAC", "buy", D("100.001"), D("0.04"))
    o.order_id = "live-1"
    ex.orders = {"live-1": o}
    bot = Bot(cfg, fake_feed({}), ex, Ledger(path="/tmp/tmbot-x.jsonl"), "t", log=lambda *_: None)
    d = decide(bot._without_own(book(bid="100.001")), cfg, params())
    assert next(q for q in d.quotes if q.side == "buy").price == D("100.001")


# ---------------- auth signing ----------------
def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def test_challenge_signature_verifies(tmp_path):
    pk = ec.generate_private_key(ec.SECP256R1())
    nums = pk.private_numbers()
    jwk = {"kty": "EC", "crv": "P-256", "d": _b64(nums.private_value),
           "x": _b64(nums.public_numbers.x), "y": _b64(nums.public_numbers.y)}
    f = tmp_path / "key.json"
    f.write_text(json.dumps({"key_id": "00000000-0000-0000-0000-000000000000", "private_key": jwk}))
    f.chmod(0o600)
    kf = KeyFile.load(f)
    body = kf.sign_challenge(1_700_000_000)
    raw = base64.urlsafe_b64decode(body["signature"] + "==")
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    msg = f"{body['key_id']}.{body['timestamp']}".encode()
    pk.public_key().verify(encode_dss_signature(r, s), msg, ec.ECDSA(hashes.SHA256()))
    stamp = json.loads(base64.urlsafe_b64decode(kf.stamp('{"x":1}\n') + "=="))
    assert stamp["scheme"] == "SIGNATURE_SCHEME_TK_API_P256" and len(stamp["publicKey"]) == 66
