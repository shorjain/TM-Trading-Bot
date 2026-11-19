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

## Live-trading notes

| date | what I did | expected | actual | request_id |
|---|---|---|---|---|
| 2026-11-19 11:55 ET | CeFi limit buy AAPLC 0.014 @ 349.246 via API, wallet funded only in PYUSD (Solana); signed the funding-bridge payloads and executed | Either the bridge converts PYUSD → USDC and the order rests, or it fails and nothing moves | Execute returned `422 insufficient_balance` ("needs 4.889444, but only 0 … available") and the order went `failed`, **but PYUSD in the wallet still dropped and 4.889444 USDC showed up in the wallet**. An identical order 2s later succeeded. So a failed execute seems to leave a half-done conversion behind, and the error says 0 available while a conversion is in flight. | c1ccfe94-2d7c-9993-8f3a-6017e91bd358 |
| 2026-11-19 12:10 ET | Reconciled the account after the run (`/balances`, `/portfolio`, `/custody/allocations`, `/transactions`) | The cash numbers agree | `/balances` sums to $31.11 cash (26.22 PYUSD + 4.89 USDC on CeFi), but `/portfolio` says `cash_usd: 36`. `/custody/allocations` shows **3 rejected** USDC allocations ("wallet balance does not cover the allocation") and 1 completed, all within 3 s of one order. The wallet USDC row shows `available 4.889444` **and** `allocated 4.889444`, but the spec says available is net of allocated. About $4.89 of the original PYUSD isn't visible in any balance. | allocation ids 31035a73…, 3ac5f9be…, 054d5f2f…, 1aff39a5… |
| 2026-11-19 12:12 ET | **Root cause found.** CeFi limit buy (NVDAC 0.173 @ 144.253, ~$25) with 4.89 USDC on CeFi and PYUSD in the wallet; signed the bridge payloads | Bridge converts PYUSD → USDC, allocates it, order rests | Conversion **happened** (PYUSD 26.22 → 11.04, wallet USDC 4.89 → 20.07), but the two USDC allocations of 20.066325 were **rejected 1 s apart** ("wallet balance does not cover the allocation") because they ran before the conversion settled. The order failed and $15.18 was left unallocated in the wallet. This is the same race as the 11:55 incident. Suggest: allocate only after the swap confirms, or retry the allocation. Note: allocating USDC by hand with `POST /custody/allocations` works. | 49f483ca-b818-96e8-adcb-712b5fc90189 |
| 2026-11-19 12:10 ET | Looked at SNDKC | Book near the reference price | SNDKC bid 801.310 / ask 801.311 (0.01 bps, nearly locked) while `reference_price` was 761.27, a ~5% gap | — |
| 2026-11-19 | Ran `GET /balances` while the bot ran | — | Requests to api.truemarkets.co intermittently took 5–12 s and some TLS handshakes timed out (api.truex.co was fine at the same time) | — |

## API behaviour that isn't documented (found while running live, 2026-11-19)

- **One order action at a time per account.** A second `POST /orders` or `PATCH /orders/{id}`
  while one is still processing returns `409 conflict: user already has an order in flight`.
  This is a big constraint for a market maker quoting several books. Please document it,
  or better, allow some concurrency, or offer a batch order endpoint.
- **`PATCH /orders/{id}` on a just-created order** returns `400 order in invalid state` until the
  order is `active`. Fair enough, but it isn't documented.
- **A CeFi order slightly larger than the CeFi balance silently starts a wallet → CeFi allocation**,
  and then other orders fail with `409 an allocation for this listing is already in progress`.
  An opt-out ("never auto-fund from wallet") would make bot behaviour predictable.
