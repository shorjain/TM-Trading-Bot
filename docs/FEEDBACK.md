# True Markets: platform feedback

From building an API market-making bot for the Orderbook Arena. Everything below was
reproduced on **2026-11-09 (~03:50 UTC)** against production, using public endpoints only.
Request IDs are in the probe reports (`python tools/probe.py`), so you can find each
call in your logs.

Sorted by how much I think each one matters.

---

## 1. Price dislocations in CeFi mid-price history (likely the most important)

`GET /v1/cefi/market/prices/range?symbol=<SYM>-USDC&window=7d`

| book | 7d min | 7d max | reference now |
|---|---|---|---|
| NVDAC | 106.65 (2026-11-04T13:00Z) | 210.58 (2026-11-03T21:00Z) | 178.38 |
| SPCXC | 70.65 (2026-11-04T13:00Z) | 142.47 | 140.92 |
| AAPLC | **0** (2026-11-04T13:00Z) | 353.84 | 352.01 |
| MUC | **0** (2026-11-04T13:00Z) | 869.02 | 728.78 |

- Four books have their minimum in **the same hour (2026-11-04 13:00 UTC)**, which points to a
  systemic event: a book wipe, maintenance, or an empty side being averaged in as 0.
- A mid of `0`, or a 2x swing on a mega-cap stock, will break any chart, indicator or bot
  that uses this endpoint for fair value or price-band logic.
- Worth checking: what did the books look like at 2026-11-04T13:00Z? Should the mid be
  `null` (or fall back to the reference price) when one side of the book is empty?

## 2. `POST /v1/gateway/quotes` rejects every tokenized-stock pair

The spec says `base_asset` + `quote_asset` quotes a CeFi pair. Every competition book, using
the `cefi_instrument` exactly as the catalog gives it, returns `400 unsupported asset`.
BTC/USDC works.

```bash
curl -s -X POST https://api.truemarkets.co/v1/gateway/quotes -H 'Content-Type: application/json' \
  -d '{"base_asset":"NVDAC","quote_asset":"USDC","qty":"0.01","qty_unit":"base","side":"buy"}'
# {"type":"invalid_argument","message":"unsupported asset", ...}
```

I haven't checked whether the authenticated `asset_id` form works for stocks.

## 3. `GET /v1/cefi/instruments` paginates, but the cursor can't be used

The docs say a call with no parameters returns all instruments. It returns 10 plus
`pagination.next_cursor`, and `cursor`, `next_cursor`, `page_token` and `after` are all
rejected with `400 Invalid parameter supplied`, with an empty `detail`. Today the only
workaround is querying `?symbol=` one at a time, and books like NVDAC, TSLAC, MSTRC and
SPCXC aren't on the first page.

## 4. `GET /v1/cefi/market/auction` rows have no instrument label

With no `instrument_id`, it returns 16 rows like `{"auction_mode": false}`, so there's no
way to tell which row belongs to which book.

## 5. Docs and spec disagree

- **Test mode.** The docs say "There is no test mode", but the Trading spec lists a UAT
  sandbox server (`api.uat.truemarkets.co`) and Paper Trading endpoints
  (`/paper/deposits`, `/paper/withdrawals`). If UAT is open to external developers,
  saying so would save new API users real money. If it isn't, could Arena participants
  get access?
- **Fees.** `/quotes` returns `fee` = 4.0 bps on BTC/USDC, while the CeFi fee schedule
  (page and `GET /v1/cefi/operations/fees/schedule`) is 0/0 for every client type. If
  this is a Gateway/service fee, a line on the fee page would clear it up.
- **Symbols.** The CeFi market-data docs use `BTC-USD` as the example symbol, but the
  exchange lists `BTC-PYUSD`, and `BTC/USD` on `/quotes` is "unsupported asset".
- **Price history coverage.** `prices/range?window=7d` returns 404 for 7 of the 12
  competition books (TSLAC, MSFTC, AMZNC, GOOGLC, METAC, PLTRC, SNDKC), even though they
  have live quotes.

## 6. Two error envelopes

`api.truemarkets.co` returns `{type, message, field_violations, request_id}`.
`api.truex.co` returns RFC 7807 style `{error: {type: "about:blank", title, status, detail, ...}}`.
Clients have to handle both. Converging on one would help, and so would always filling `detail`.

---

## Feature requests, from the point of view of a systematic trader

1. **`post_only` (and `time_in_force`) on limit orders.** Arena scoring pays 3x for maker
   volume, but `CreateOrderRequest` can't guarantee an order won't cross, so bots have to
   keep a safety gap from the opposite side.
2. **A maker/taker flag and per-fill data** on `GET /orders/{id}` (or a `/fills` endpoint).
   Right now you can't tell from the Trading API how a fill will be scored.
3. **An authenticated order/fill stream for Trading API users.** Bots currently have to
   poll `GET /orders/{id}`.
4. **L2 depth for retail.** The public EBBO is top of book only.
5. **An Arena API:** current points, rank, and the book of the day (today it's only in
   Discord), so bots can size up the 2x book automatically.
6. **Clarify the 30% single-counterparty rule.** Most books have 1 to 3 resting orders per
   side, which is probably one house liquidity provider. A participant who only takes
   liquidity could breach 30% without trying. Does the house MM count as a counterparty?

## Onboarding / app UX

- **Arena invites US participants, but US accounts can't trade the books.** My account
  (`GET /assets/availability` → `{"country":"US","allowed_asset_classes":["crypto"]}`) can
  onboard, receive the $50 Arena funding and create an API key, and only then hits
  `403 permission_denied: trading is not available in this country` on the first stock order.
  The check should happen at signup or Arena enrollment, or the Arena page should say up
  front which countries can trade.

- **Blocker: you can't create a passkey on an account that already has a wallet.** On
  app.truemarkets.co/settings/api-keys, the banner says "Passkey required". At /setup-passkey,
  "Set up passkey" fails with `User already has a wallet` (Chrome, macOS, 2026-11-10 ~04:20 UTC).
  Without a passkey, API-key creation is disabled, so an existing user can't reach the Trading API.
  The passkey flow seems to assume a brand-new wallet, when it should attach a passkey to the
  existing one.

- **API keys page, before a passkey exists:** "+ Create" and "+ Create API Key" look
  clickable but do nothing on hover or click. The reason, a passkey being required, only
  shows in the banner above. A disabled style with a tooltip ("Set up a passkey first"),
  or having the button open passkey setup directly, would remove the confusion.
- **Key types are easy to mix up:** keys from console.truemarkets.co are organization (Gateway)
  keys (they download as `truemarkets-org-api-key-*.json`). When you
  use one with the Trading API, the token is minted successfully, but `/balances` returns a bare
  `401 unauthorized`. An error like "this is an organization key; personal trading keys are
  created at app.truemarkets.co" would save new API users time.

## Market-structure observations (Sunday evening ET, US market closed)

- Competition books are **40 to 100 bps wide**, with 1 to 3 orders per level.
- 24h notional per book ranges from **$0 to about $830** (NVDAC is the most active).
- A tight two-sided quote from any participant becomes top of book, which is good for
  makers but means the books are thin for anyone sending larger orders.

---

## Live-trading notes (to fill in after running with real funds)

| date | what I did | expected | actual | request_id |
|---|---|---|---|---|
| | | | | |
