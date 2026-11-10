"""HTTP clients.

- GatewayClient: authenticated Trading API (api.truemarkets.co/v1/gateway).
- ExchangeData:  public CeFi exchange market data (api.truex.co/v1/cefi), no key needed.

Every request is appended to logs/api.jsonl with latency, status and request_id, so
anything odd can be turned into a precise bug report for the True Markets team.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx

from .auth import KeyFile, Token

ENVS = {
    "prod": {"api": "https://api.truemarkets.co", "exchange": "https://api.truex.co"},
    "uat": {"api": "https://api.uat.truemarkets.co", "exchange": "https://api.uat.truex.co"},
}

# Error codes worth one retry after a short pause (docs: developer-resources/errors).
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: Any):
        self.method, self.path, self.status, self.body = method, path, status, body
        b = body if isinstance(body, dict) else {}
        err = b.get("error", b) if isinstance(b.get("error"), dict) else b
        self.code = err.get("code") or err.get("type") or err.get("title") or ""
        self.message = err.get("message") or err.get("detail") or ""
        self.request_id = err.get("request_id", "")
        super().__init__(
            f"{method} {path} -> {status} {self.code} {self.message} (request_id={self.request_id})"
        )


class _Logged:
    def __init__(self, log_dir: str | Path = "logs"):
        self.log_path = Path(log_dir) / "api.jsonl"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def _log(self, rec: dict) -> None:
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


class ExchangeData(_Logged):
    """Public market data from the CeFi exchange. Read-only, no credentials."""

    def __init__(self, env: str = "prod", log_dir: str | Path = "logs", timeout: float = 10.0):
        super().__init__(log_dir)
        self.base = ENVS[env]["exchange"]
        self.http = httpx.Client(timeout=timeout, headers={"User-Agent": "tm-trading-bot/0.1"})

    def get(self, path: str, params: dict | None = None) -> Any:
        t0 = time.perf_counter()
        r = self.http.get(self.base + path, params=params)
        ms = (time.perf_counter() - t0) * 1000
        try:
            body = r.json()
        except ValueError:
            body = r.text
        self._log({"ts": time.time(), "host": "exchange", "method": "GET", "path": path,
                   "params": params, "status": r.status_code, "ms": round(ms, 1),
                   "request_id": r.headers.get("x-request-id")})
        if r.status_code >= 400:
            raise ApiError("GET", path, r.status_code, body)
        return body

    def ebbo(self) -> list[dict]:
        """Best bid/offer + last trade for every instrument."""
        return self.get("/v1/cefi/market/quote")

    def instrument(self, symbol: str) -> dict | None:
        """Instrument info by symbol. (The unfiltered list only returns the first 10, with
        a next_cursor that no query parameter accepts, so we look up by symbol.)"""
        data = self.get("/v1/cefi/instruments", {"symbol": symbol}).get("data", [])
        return data[0] if data else None

    def fee_schedule(self) -> dict:
        return self.get("/v1/cefi/operations/fees/schedule")


class GatewayClient(_Logged):
    """Authenticated Trading API client for your own account."""

    def __init__(self, key: KeyFile | None, env: str = "prod", log_dir: str | Path = "logs",
                 timeout: float = 20.0):
        super().__init__(log_dir)
        self.key = key
        self.base = ENVS[env]["api"]
        self.http = httpx.Client(timeout=timeout, headers={"User-Agent": "tm-trading-bot/0.1"})
        self.token: Token | None = None

    # ---- auth ---------------------------------------------------------------
    def _mint(self) -> None:
        if self.key is None:
            raise RuntimeError("no key file loaded; set TM_KEY_FILE")
        body = self._raw("POST", "/v1/auth/api-key/token", self.key.sign_challenge(), auth=False)
        self.token = Token.from_response(body)

    def _ensure_token(self) -> None:
        if self.token is None:
            self._mint()
        elif self.token.expiring():
            if self.token.refresh:
                try:
                    body = self._raw("POST", "/v1/auth/token/refresh",
                                     {"refresh_token": self.token.refresh}, auth=False)
                    self.token = Token.from_response(body)
                    return
                except ApiError:
                    pass
            self._mint()

    # ---- transport ----------------------------------------------------------
    def _raw(self, method: str, path: str, body: Any = None, params: dict | None = None,
             auth: bool = True, retry: bool = True) -> Any:
        headers = {}
        if auth:
            self._ensure_token()
            headers["Authorization"] = f"Bearer {self.token.access}"
        t0 = time.perf_counter()
        r = self.http.request(method, self.base + path, json=body, params=params, headers=headers)
        ms = (time.perf_counter() - t0) * 1000
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = r.text
        rid = data.get("request_id") if isinstance(data, dict) else None
        self._log({"ts": time.time(), "host": "gateway", "method": method, "path": path,
                   "params": params, "status": r.status_code, "ms": round(ms, 1),
                   "request_id": rid or r.headers.get("x-request-id"),
                   "error": data if r.status_code >= 400 else None})
        if r.status_code == 401 and auth and retry:
            self.token = None
            return self._raw(method, path, body, params, auth, retry=False)
        if r.status_code in RETRYABLE_STATUS and retry:
            time.sleep(1.5)
            return self._raw(method, path, body, params, auth, retry=False)
        if r.status_code >= 400:
            raise ApiError(method, path, r.status_code, data)
        return data

    def get(self, path: str, params: dict | None = None, auth: bool = True) -> Any:
        return self._raw("GET", "/v1/gateway" + path, params=params, auth=auth)

    def post(self, path: str, body: Any) -> Any:
        return self._raw("POST", "/v1/gateway" + path, body)

    def patch(self, path: str, body: Any) -> Any:
        return self._raw("PATCH", "/v1/gateway" + path, body)

    # ---- catalog / account ----------------------------------------------------
    def stock_listings(self) -> list[dict]:
        return self.get("/assets", {"asset_class": "stock"}, auth=False)["data"]

    def availability(self) -> Any:
        return self.get("/assets/availability")

    def balances(self) -> list[dict]:
        return self.get("/balances")["data"]

    # ---- orders -----------------------------------------------------------------
    def _sign_and_execute(self, path: str, resp: dict) -> dict:
        payloads = resp.get("payloads") or []
        if not payloads:
            return resp
        sigs = [self.key.stamp(p["payload"]) for p in payloads]
        return self.post(path, {"signatures": sigs, "auth_type": "api_key"})

    def create_limit(self, asset_id: str, side: str, qty: str, price: str) -> dict:
        resp = self.post("/orders", {
            "asset_id": asset_id, "side": side, "type": "limit",
            "qty": qty, "qty_unit": "base", "price": price, "venue": "cefi",
        })
        oid = resp.get("order_id")
        if not oid:
            raise ApiError("POST", "/orders", 201, {"message": f"no order created: {resp.get('quote')}"})
        if resp.get("payloads"):
            executed = self._sign_and_execute(f"/orders/{oid}/execute", resp)
            resp = {**resp, **executed, "order_id": oid}
        return resp

    def modify(self, order_id: str, price: str | None = None, qty: str | None = None) -> dict:
        body = {k: v for k, v in (("price", price), ("qty", qty)) if v is not None}
        return self.patch(f"/orders/{order_id}", body)

    def cancel(self, order_id: str) -> dict:
        resp = self.post(f"/orders/{order_id}/cancel", {})
        return self._sign_and_execute(f"/orders/{order_id}/cancel/execute", resp)

    def cancel_all(self, venue: str = "cefi") -> dict:
        resp = self.post("/orders/all/cancel", {"venue": venue})
        if isinstance(resp, dict) and resp.get("payloads"):
            # Per-order payloads are not documented for cancel-all; fall back to one by one.
            for o in self.list_orders(status="active"):
                self.cancel(o["order_id"])
        return resp

    def order(self, order_id: str) -> dict:
        return self.get(f"/orders/{order_id}")

    def list_orders(self, status: str | None = None, limit: int = 100) -> list[dict]:
        params: dict = {"limit": limit}
        if status:
            params["status"] = status
        return self.get("/orders", params).get("data", [])
