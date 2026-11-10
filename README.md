# TM-Trading-Bot

A small, risk-limited **market-making bot** for tokenized-stock orderbooks on
[True Markets](https://truemarkets.co), built for the **Orderbook Arena** competition,
plus a **platform QA probe** that turns API quirks into bug reports.

> ⚠️ **Real money.** True Markets has no public test mode, so every live order moves real funds.
> The bot runs in **paper mode by default**. Live trading needs `--live` and a typed confirmation.
> Per the Arena page, trading is **not available in the US or UK**. Check your eligibility
> with True Markets before trading live.

## How it scores

Arena points = USD volume, with **maker fills ×3** and taker fills ×1. The **book of the day** counts ×2 on top.
The 12 eligible books are NVDAC, TSLAC, AAPLC, MSFTC, AMZNC, GOOGLC, METAC, PLTRC, MSTRC, MUC, SNDKC and SPCXC.
Rules: no self-trading, wallets funded from the same address count as one entrant,
and at most 30% of your volume with any single counterparty.

## Strategy

The books are thin and wide: 40 to 100 bps, with 1 to 3 resting orders. So the bot:

1. **Computes fair value** as a blend of the exchange's reference price (which tracks the
   real stock) and the book mid.
2. **Quotes inside the spread.** It posts a bid one tick above the best bid, but never closer
   to fair than `min_edge_bps`. Our order becomes top of book, so takers fill against us
   first (maker ×3), and every fill sits on the right side of fair value.
3. **Only sells what it holds.** Spot has no shorting. As inventory builds, both quotes skew
   down, so it sells back toward flat and recycles the same $50 into more volume.
4. **Never crosses.** It stays at least one tick behind the opposite best price, because the API
   has no post-only flag. If its own order is the best price, it holds there instead of
   outbidding itself.
5. **Risk limits:** per-book and gross position caps, a max-loss kill switch, a sit-out when
   fair value and mid disagree (the probe found big historical mid dislocations), an API
   action rate limit, and **cancel-all on exit**.

Optional opportunistic taking (`take_enabled`) hits resting orders that sit far through fair value.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

For live trading only:

1. Create an API key at <https://app.truemarkets.co/settings/api-keys>. Save the JSON **outside
   this repo** (e.g. `~/.truemarkets/api-key.json`), then run `chmod 600` on it.
2. Set `TM_KEY_FILE` in `.env`. `.env` and key files are git-ignored. **This repo is public, so never
   commit a key.**
3. Fund the account. The arena gives new traders $50 USDC through a Discord ticket. Limit orders
   trade on the CeFi venue, so make sure USDC is available to CeFi trading.
   `python -m tmbot status` shows CeFi balances.

## Commands

```bash
.venv/bin/python -m tmbot market                 # live books + what the bot would quote (no key)
.venv/bin/python -m tmbot run                    # PAPER trading on live data (no key)
.venv/bin/python -m tmbot run --book-of-day NVDAC --minutes 60
.venv/bin/python -m tmbot report                 # volume / points / PnL (paper)

.venv/bin/python -m tmbot status                 # balances + open orders (key, read-only)
.venv/bin/python -m tmbot run --live             # REAL orders
.venv/bin/python -m tmbot report --live
.venv/bin/python -m tmbot cancel-all             # emergency stop

.venv/bin/python tools/probe.py                  # platform QA report -> reports/
.venv/bin/python -m pytest -q
```

Paper fills are optimistic: an order fills as soon as the market trades through its price,
with no queue position. Treat paper volume as an upper bound.

## Tuning (`config.yaml`)

| key | default | effect |
|---|---|---|
| `books` | all 12 | Fewer books means bigger quotes per book from the same $50 |
| `book_of_the_day` | null | Set daily from Discord; doubles size there |
| `strategy.half_spread_bps` | 20 | Target distance from fair; lower = more fills, less edge |
| `strategy.min_edge_bps` | 8 | Never quote closer to fair than this |
| `sizing.quote_notional_usd` | 5 | USDC per quote (exchange minimum is $1) |
| `sizing.max_position_usd` | 12 | Max holding per stock |
| `risk.max_loss_usd` | 5 | Kill switch on marked-to-market PnL |

## Layout

```
tmbot/
  auth.py        API-key token minting (ES256) + payload stamping (Turnkey P-256)
  client.py      Trading API + public exchange clients; every call logged to logs/*/api.jsonl
  market.py      EBBO snapshots + reference prices
  strategy.py    pure quoting logic (unit tested)
  executor.py    PaperExecutor / LiveExecutor
  ledger.py      fills, positions, PnL, Arena points
  bot.py         main loop: data -> strategy -> reconcile -> fills -> risk
tools/probe.py   read-only platform QA -> Markdown bug report
docs/FEEDBACK.md feedback for the True Markets team
```

## API references

- Docs: <https://docs.truemarkets.co> (Trading API, CeFi exchange docs, increments, price bands)
- Trading REST: `https://api.truemarkets.co/v1/gateway`
- Public exchange market data: `https://api.truex.co/v1/cefi/market/quote`
