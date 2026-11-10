"""Platform QA probe: read-only checks against the True Markets APIs.

Writes a Markdown report to reports/probe-<timestamp>.md that you can paste into
Discord or an email to the engineers. Every check logs request_ids so they can find
the exact request in their logs.

    python tools/probe.py            # public endpoints only, no key needed
    python tools/probe.py --auth     # also read-only account checks (needs TM_KEY_FILE)

Nothing here places, modifies or cancels orders.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

from tmbot.client import ENVS  # noqa: E402

D = Decimal
COMP_BOOKS = ["NVDAC", "TSLAC", "AAPLC", "MSFTC", "AMZNC", "GOOGLC", "METAC", "PLTRC",
              "MSTRC", "MUC", "SNDKC", "SPCXC"]


class Probe:
    def __init__(self, env: str):
        self.api, self.ex = ENVS[env]["api"], ENVS[env]["exchange"]
        self.http = httpx.Client(timeout=15, headers={"User-Agent": "tm-trading-bot-probe/0.1"})
        self.findings: list[tuple[str, str, str]] = []  # (severity, title, detail)
        self.notes: list[str] = []
        self.lat: dict[str, list[float]] = {}

    def req(self, method: str, url: str, **kw):
        t0 = time.perf_counter()
        r = self.http.request(method, url, **kw)
        self.lat.setdefault(f"{method} {url.split('?')[0]}", []).append((time.perf_counter() - t0) * 1000)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        return r, body

    def find(self, sev: str, title: str, detail: str):
        self.findings.append((sev, title, detail))
        print(f"[{sev}] {title}")

    @staticmethod
    def rid(body) -> str:
        if isinstance(body, dict):
            e = body.get("error") if isinstance(body.get("error"), dict) else body
            return e.get("request_id", "")
        return ""

    # ------------------------------------------------------------------ checks
    def catalog_vs_exchange(self):
        _, cat = self.req("GET", f"{self.api}/v1/gateway/assets", params={"asset_class": "stock"})
        cefi = {a["symbol"]: a for a in cat.get("data", []) if a.get("venue") == "cefi"}
        _, ebbo = self.req("GET", f"{self.ex}/v1/cefi/market/quote")
        ebbo_syms = {m["symbol"] for m in ebbo}
        self.notes.append(f"Catalog CeFi stock listings: {len(cefi)}; exchange EBBO instruments: {len(ebbo_syms)}")
        for b in COMP_BOOKS:
            a = cefi.get(b)
            if not a:
                self.find("medium", f"{b}: competition book missing from CeFi catalog", "GET /v1/gateway/assets?asset_class=stock")
                continue
            inst = a.get("cefi_instrument")
            if inst not in ebbo_syms:
                self.find("medium", f"{b}: catalog cefi_instrument {inst} not in exchange EBBO", "")
            if "limit" not in (a.get("order_types") or []):
                self.find("high", f"{b}: limit orders not enabled", f"order_types={a.get('order_types')}")
        return cefi

    def instruments_pagination(self):
        r, body = self.req("GET", f"{self.ex}/v1/cefi/instruments")
        n = len(body.get("data", [])) if isinstance(body, dict) else 0
        nxt = (body.get("pagination") or {}).get("next_cursor") if isinstance(body, dict) else None
        if nxt:
            tried = []
            for p in ("cursor", "next_cursor", "page_token", "after"):
                r2, b2 = self.req("GET", f"{self.ex}/v1/cefi/instruments", params={p: nxt})
                tried.append(f"`{p}` -> {r2.status_code} (request_id {self.rid(b2)})")
                if r2.status_code == 200:
                    break
            else:
                self.find("medium", "GET /v1/cefi/instruments returns only the first page and the cursor cannot be used",
                          f"Docs say no params returns ALL instruments; it returned {n} plus next_cursor={nxt}. "
                          f"Tried: {'; '.join(tried)}. Workaround: query each ?symbol=.")

    def auction_mapping(self):
        _, body = self.req("GET", f"{self.ex}/v1/cefi/market/auction")
        if isinstance(body, list) and body and not any(("instrument_id" in x or "symbol" in x or "id" in x) for x in body):
            self.find("low", "GET /v1/cefi/market/auction (all instruments) has no instrument id/symbol per row",
                      f"Returned {len(body)} rows like {body[0]}; a client cannot tell which row is which book.")

    def public_quotes(self, cefi: dict):
        bad = []
        for b in COMP_BOOKS:
            inst = (cefi.get(b) or {}).get("cefi_instrument", f"{b}-USDC")
            base, _, quote = inst.partition("-")
            r, body = self.req("POST", f"{self.api}/v1/gateway/quotes",
                               json={"base_asset": base, "quote_asset": quote, "qty": "0.01",
                                     "qty_unit": "base", "side": "buy"})
            if r.status_code != 200:
                bad.append(f"{inst}: {r.status_code} {body.get('message') if isinstance(body, dict) else body} "
                           f"(request_id {self.rid(body)})")
        r, body = self.req("POST", f"{self.api}/v1/gateway/quotes",
                           json={"base_asset": "BTC", "quote_asset": "USDC", "qty": "0.001",
                                 "qty_unit": "base", "side": "buy"})
        ctrl = f"Control BTC/USDC -> {r.status_code} {body}"
        if bad:
            self.find("medium", f"POST /v1/gateway/quotes (base_asset/quote_asset) rejects {len(bad)}/12 stock pairs",
                      "The spec says base_asset+quote_asset quotes a CeFi pair. " + ctrl + "\n" +
                      "\n".join(f"  - {x}" for x in bad))
        if r.status_code == 200 and isinstance(body, dict) and D(body.get("fee") or 0) > 0:
            bps = D(body["fee"]) / D(body["notional"]) * 10000
            _, fees = self.req("GET", f"{self.ex}/v1/cefi/operations/fees/schedule")
            self.find("low", f"Quote fee ({bps:.1f} bps) vs published CeFi fee schedule (0 bps)",
                      f"BTC/USDC quote fee={body['fee']} on notional={body['notional']}; schedule={fees}. "
                      "If this is a Gateway/service fee it would help to document it next to the exchange fee schedule.")

    def book_health(self):
        _, ebbo = self.req("GET", f"{self.ex}/v1/cefi/market/quote")
        now = time.time()
        rows = []
        for m in ebbo:
            sym, _, q = m["symbol"].partition("-")
            if sym not in COMP_BOOKS:
                continue
            i = m.get("info", {})
            bb, ba = i.get("best_bid") or {}, i.get("best_ask") or {}
            bid, ask = D(bb.get("price") or 0), D(ba.get("price") or 0)
            spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid and ask else None
            age = now - int(i.get("last_update") or 0) / 1e9
            rows.append(f"| {sym} | {bid} x {bb.get('qty')} ({bb.get('order_count')}) | {ask} x {ba.get('qty')} "
                        f"({ba.get('order_count')}) | {spread and round(spread, 1)} | {age/60:.0f} min |")
            if bid and ask and bid >= ask:
                self.find("high", f"{sym}: crossed/locked book bid {bid} >= ask {ask}", "")
        self.notes.append("| book | best bid x qty (orders) | best ask x qty (orders) | spread bps | since last change |\n"
                          "|---|---|---|---|---|\n" + "\n".join(rows))

    def price_history(self):
        """Mid-price range over 7 days per book vs the exchange reference price.
        A huge range on a thin book means the mid was dislocated at some point."""
        rows = []
        for b in COMP_BOOKS:
            r, body = self.req("GET", f"{self.api}/v1/cefi/market/prices/range",
                               params={"symbol": f"{b}-USDC", "window": "7d"})
            if r.status_code != 200:
                rows.append(f"| {b} | - | - | - | {r.status_code} |")
                continue
            lo, hi = D(body["min_price"]), D(body["max_price"])
            _, inst = self.req("GET", f"{self.ex}/v1/cefi/instruments", params={"symbol": f"{b}-USDC"})
            ref = D(((inst.get("data") or [{}])[0].get("info") or {}).get("reference_price") or 0)
            rng = f"{(hi / lo - 1) * 100:.0f}%" if lo > 0 else "n/a"
            rows.append(f"| {b} | {lo} ({body['min_timestamp']}) | {hi} ({body['max_timestamp']}) | {ref:.3f} | "
                        f"{rng} |")
            if lo == 0 or (lo > 0 and hi / lo > D("1.4")):
                self.find("medium", f"{b}: 7-day mid ranged {lo} - {hi} vs reference {ref:.2f}",
                          f"GET /v1/cefi/market/prices/range?symbol={b}-USDC&window=7d -> {body}. "
                          "The underlying did not move this much; this looks like the mid being computed from "
                          "a one-sided or near-empty book (or a bad print). Worth checking what traded/quoted at "
                          "those timestamps, and whether price bands should key off the reference price when the "
                          "book is thin.")
        self.notes.append("\n7-day mid range (CeFi prices/range):\n\n| book | min | max | reference now | range |\n"
                          "|---|---|---|---|---|\n" + "\n".join(rows))

    def error_envelopes(self):
        _, a = self.req("POST", f"{self.api}/v1/gateway/quotes", json={})
        _, b = self.req("GET", f"{self.ex}/v1/cefi/market/quote", params={"bogus": "1"})
        if isinstance(a, dict) and isinstance(b, dict) and set(a) != set(b):
            self.find("low", "Two different error envelopes across api.truemarkets.co and api.truex.co",
                      f"gateway: {a}\nexchange: {b}\nA client has to parse both shapes; the exchange one also returns "
                      "an empty `detail` for unknown params in some cases.")

    def latency(self, n: int = 8):
        for _ in range(n):
            self.req("GET", f"{self.ex}/v1/cefi/market/quote")
            self.req("GET", f"{self.api}/v1/gateway/assets", params={"asset_class": "stock"})

    def auth_checks(self):
        from tmbot.auth import KeyFile
        from tmbot.client import ApiError, GatewayClient
        gw = GatewayClient(KeyFile.load(os.environ["TM_KEY_FILE"]), log_dir="logs/probe")
        for name, fn in (("availability", gw.availability), ("balances", gw.balances),
                         ("open orders", lambda: gw.list_orders(status="active"))):
            try:
                out = fn()
                self.notes.append(f"**{name}**: `{str(out)[:600]}`")
            except ApiError as e:
                self.find("medium", f"Authenticated {name} failed", str(e))

    # ------------------------------------------------------------------ report
    def report(self) -> Path:
        out = Path("reports")
        out.mkdir(exist_ok=True)
        p = out / f"probe-{time.strftime('%Y%m%d-%H%M%S')}.md"
        lines = [f"# True Markets platform probe - {time.strftime('%Y-%m-%d %H:%M %Z')}", "",
                 "## Findings", ""]
        order = {"high": 0, "medium": 1, "low": 2}
        for sev, title, detail in sorted(self.findings, key=lambda f: order[f[0]]):
            lines += [f"### [{sev}] {title}", "", "```", detail or "-", "```", ""]
        lines += ["## Market snapshot", ""] + self.notes + ["", "## Latency (ms)", "",
                  "| endpoint | n | p50 | p95 | max |", "|---|---|---|---|---|"]
        for k, v in self.lat.items():
            if len(v) >= 3:
                q = statistics.quantiles(v, n=20, method="inclusive")
                lines.append(f"| {k} | {len(v)} | {statistics.median(v):.0f} | {q[18]:.0f} | {max(v):.0f} |")
        p.write_text("\n".join(lines) + "\n")
        return p


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--auth", action="store_true", help="also run read-only authenticated checks")
    args = ap.parse_args()
    pr = Probe(os.getenv("TM_ENV", "prod"))
    cefi = pr.catalog_vs_exchange()
    pr.instruments_pagination()
    pr.auction_mapping()
    pr.public_quotes(cefi)
    pr.book_health()
    pr.price_history()
    pr.error_envelopes()
    pr.latency()
    if args.auth:
        pr.auth_checks()
    print(f"\nreport: {pr.report()}")


if __name__ == "__main__":
    main()
