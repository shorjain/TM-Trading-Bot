import base64
import json
import time
from decimal import Decimal as D

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from tmbot.auth import KeyFile
from tmbot.bot import Bot
from tmbot.config import Config
from tmbot.executor import PaperExecutor
from tmbot.increments import qty_for_notional, round_price, round_qty, tick_for
from tmbot.ledger import Fill, Ledger
from tmbot.market import Book
from tmbot.strategy import decide


def book(bid="100.0", ask="101.0", ref="100.5", sym="NVDAC", **kw) -> Book:
    return Book(sym, D(bid) if bid else None, D("1"), D(ask) if ask else None, D("1"), 1, 1,
                kw.get("ltp"), D(kw.get("ltq", "0")), kw.get("ltns", 0),
                D(ref) if ref else None, time.time_ns(), fetched_at=time.time())


def cfg(**sizing) -> Config:
    c = Config(books=["NVDAC"])
    for k, v in sizing.items():
        setattr(c.sizing, k, D(str(v)))
    return c


# ---------------- increments ----------------
def test_increments_bucket5():
    assert tick_for(D("178.33")) == D("0.001")
    assert round_price(D("178.3337"), "buy") == D("178.333")
    assert round_price(D("178.3331"), "sell") == D("178.334")
    assert round_qty(D("0.02899"), D("178")) == D("0.028")
    assert qty_for_notional(D(5), D("178.33")) == D("0.028")


# ---------------- strategy ----------------
def test_flat_quotes_bid_only_inside_spread_and_never_crossing():
    d = decide(book(), cfg(), position_qty=D(0), usd_available=D(50), gross_usd=D(0))
    sides = {q.side: q for q in d.quotes}
    assert set(sides) == {"buy"}  # nothing to sell yet
    bid = sides["buy"]
    assert D("100.0") < bid.price < D("101.0")  # inside the spread
    assert bid.price <= d.fair * (1 - D(8) / 10_000)  # keeps min edge
    assert bid.notional >= 1


def test_pennies_best_bid_instead_of_overpaying():
    # fair ~100.5, theoretical bid ~100.30; best bid 100.0 -> we post 100.001
    d = decide(book(), cfg(), D(0), D(50), D(0))
    assert d.quotes[0].price == D("100.001")


def test_with_inventory_quotes_ask_and_respects_holdings():
    d = decide(book(), cfg(), position_qty=D("0.03"), usd_available=D(50), gross_usd=D(3))
    sides = {q.side: q for q in d.quotes}
    assert sides["sell"].qty <= D("0.03")
    assert sides["sell"].price > sides["buy"].price
    assert sides["sell"].price >= d.fair * (1 + D(8) / 10_000)


def test_ask_never_crosses_best_bid():
    # Tight book where fair+edge would be below the bid: ask must stay above best bid.
    d = decide(book(bid="100.50", ask="100.52", ref="100.0"), cfg(), D("0.05"), D(50), D(5))
    for q in d.quotes:
        if q.side == "sell":
            assert q.price > D("100.50")
        else:
            assert q.price < D("100.52")


def test_position_limit_blocks_bid():
    d = decide(book(), cfg(max_position_usd=5), position_qty=D("0.05"), usd_available=D(50), gross_usd=D(5))
    assert all(q.side == "sell" for q in d.quotes)


def test_no_quotes_when_no_capital():
    d = decide(book(), cfg(), position_qty=D(0), usd_available=D("0.5"), gross_usd=D(0))
    assert d.quotes == []


def test_sits_out_when_fair_and_mid_disagree():
    d = decide(book(bid="100", ask="101", ref="110"), cfg(), D(0), D(50), D(0))
    assert d.quotes == [] and d.reasons


def test_daily_book_doubles_size():
    c = cfg()
    c.book_of_the_day = "NVDAC"
    d = decide(book(), c, D(0), D(50), D(0))
    assert d.quotes[0].notional > D(9)


# ---------------- ledger / points ----------------
def test_points_and_pnl():
    led = Ledger(path="/tmp/tmbot-test-fills.jsonl")
    led.path.unlink(missing_ok=True)
    led.record(Fill(0, "NVDAC", "buy", D("0.1"), D(100), maker=True, order_id="a"))
    led.record(Fill(0, "NVDAC", "sell", D("0.1"), D(101), maker=False, order_id="b", daily_book=True))
    s = led.summary({})
    assert s["maker_volume"] == D(10) and s["taker_volume"] == D("10.1")
    assert s["points"] == D(10) * 3 + D("10.1") * 2
    assert s["realized_pnl"] == D("0.1")


# ---------------- paper executor ----------------
def test_paper_fill_when_market_trades_through():
    ex = PaperExecutor(D(50))
    o = ex.place("NVDAC", "buy", D("100.5"), D("0.02"))
    assert ex.poll_fills({"NVDAC": book()}) == []  # rests
    later = book(bid="100.0", ask="100.4")
    fills = ex.poll_fills({"NVDAC": later})
    assert len(fills) == 1 and fills[0].maker and ex.total_holding("NVDAC") == D("0.02")
    assert o.order_id not in ex.orders


# ---------------- own order is not outbid by ourselves ----------------
def test_live_bot_does_not_outbid_itself():
    class FakeEx(PaperExecutor):
        pass
    ex = FakeEx(D(50))
    o = ex.place("NVDAC", "buy", D("100.001"), D("0.04"))
    o.order_id = "live-1"
    ex.orders = {"live-1": o}
    bot = Bot(cfg(), feed=None, executor=ex, ledger=Ledger(path="/tmp/tmbot-x.jsonl"), mode="t")
    adj = bot._without_own(book(bid="100.001"))
    d = decide(adj, cfg(), D(0), D(50), D(0))
    assert d.quotes[0].price == D("100.001")


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
