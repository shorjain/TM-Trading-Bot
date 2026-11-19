# TM-Trading-Bot

A risk-limited **market-making bot** for tokenized-stock orderbooks on
[True Markets](https://truemarkets.co), built for the **Orderbook Arena** competition,
plus a **platform QA probe** that turns API quirks into bug reports ([docs/FEEDBACK.md](docs/FEEDBACK.md)).

> ⚠️ **Real money.** There is no test mode. The bot runs in **paper mode by default**; live trading
> needs `--live` and a typed confirmation. Run it on **one computer only** (see the instance lock below).

## Scoring

Arena points = **3 × maker USD volume + 1 × taker USD volume**, and the **book of the day** (announced
in Discord) counts **×2** on top. Rules: no self-trading, one entrant, and ≤30% of your volume with any
single counterparty.

## Strategy

- **Fair value** = 0.4 × exchange reference price + 0.6 × book mid. When the reference is stale
  (underlying closed or the feed stopped), it uses the mid only and quotes wider.
- **Queue priority, never closer than the edge.** Improve the best bid/ask by exactly one tick if
  that is still ≥ `half_spread` (6 bps regular, 12 bps off-hours) from fair; otherwise join the best
  price; otherwise sit at our limit behind it. A quote already at the best price isn't touched
  (a modify can lose queue priority).
- **Maker-only.** The API has no post-only flag, so every place/modify is re-checked against a
  **fresh** book right before it's sent and skipped if it would cross. Our own bid and ask can never
  cross each other.
- **Long-only inventory** with skew: as we accumulate, both quotes move down so we sell back to flat.
- **Sizes are fractions of equity** (exchange USDC + stocks): 6% per quote, 12% for the book of the
  day, ≤25% held per book, ≤65% in stocks total, 8% USDC buffer never committed (min $5).
- **Two layers per side.** A small **front** order (25%) at the best price, and a larger **depth**
  order (75%) 2 ticks behind it. That matches what True Markets saw: the #1 maker takes the small
  orders at the very front, and bigger orders sweep through into makers resting just behind.
  Caps and the cash buffer apply to both layers combined, and all front-layer upkeep runs before any
  depth-layer upkeep.
- **Flow-weighted.** Books with fills get the single order-action slot first. Quiet books quote half
  size and are re-priced at most every 30 s. The book of the day always goes first.
- **Taker round trips** (approved by True Markets 2026-11-19). In spare action slots, on books with
  spreads ≤ 10 bps (book of the day first), the bot buys at the ask and immediately sells at the bid.
  That's 2N points for about spread × N in cost, capped at $10/day, and the cost counts toward the loss limits.

## Safety

| Guard | What it does |
|---|---|
| Instance lock | One bot per machine, plus a list of order ids this machine placed. If the account has open orders this machine didn't place (e.g. the other computer is running), it **refuses to start**; while running, it **stops**. `--force` cancels them after a typed confirmation. |
| Loss limits | Daily max loss $20, round max loss $150, persisted in `state/risk.json`, so a restart doesn't reset them. On breach: cancel all, stop, print `!!`. `--reset-risk` clears a halt (asks first). |
| Volatility pause | If a book's mid moves >40 bps in 30 s, its quotes are pulled for 2 min. |
| Stale / closed markets | One-sided or empty book, stale reference + wide book, or a long-dead book → that book's quotes are cancelled until it's live again. |
| USDC buffer | Never commits the last 8% of equity, so orders don't trigger wallet auto-funding. On a 422 "insufficient balance" it shrinks the order to fit instead of retrying. |
| Wallet watch | Every 5 min, logs any wallet→exchange movement the bot didn't make to `logs/live-prod/wallet_moves.jsonl`. |
| Keep awake | On Windows the bot stops the PC from sleeping while it runs (restored on exit). |
| Exit | Ctrl+C, `--until` or a limit → cancels **only this bot's** orders. |

**Self-trade guard for taker trades.** The API has no client-order-id, so before any taker order the
bot cancels **every** order of ours on that book (both sides, both layers), waits until the exchange
confirms them gone, re-reads a fresh book, and only then trades. If a cancel isn't confirmed it
skips the trade. Maker quotes come back on the next loop.

## Windows (PowerShell) commands

Run from the `TM-Trading-Bot` folder.

```powershell
git pull
.venv\Scripts\python -m pip install -r requirements.txt     # after every pull
.venv\Scripts\python -m pytest -q                           # tests

.venv\Scripts\python -m tmbot market                        # live books + what the bot would quote (no key)
.venv\Scripts\python -m tmbot status                        # balances + open orders (read-only)
.venv\Scripts\python -m tmbot report --live                 # points, pace, per-book flow + markouts

# regular hours, stop at 15:55 ET
.venv\Scripts\python -m tmbot run --live --book-of-day NVDAC --until 15:55
# overnight / continuous, stop at 09:25 ET tomorrow
.venv\Scripts\python -m tmbot run --live --book-of-day NVDAC --until 09:25
# run until Ctrl+C
.venv\Scripts\python -m tmbot run --live --continuous

.venv\Scripts\python -m tmbot cancel-all                    # EMERGENCY: cancel every order on the account
```

Paper mode (no key, no money): drop `--live`, e.g. `.venv\Scripts\python -m tmbot run --until 15:55`.

On macOS, use `.venv/bin/python` instead, and `caffeinate -i` in front to keep the Mac awake.

### Reading the output

| Line | Meaning |
|---|---|
| `+ BOOK buy qty @ px` | order placed |
| `~ BOOK ...` | order re-priced |
| `x BOOK ... (reason)` | order cancelled (reason shown) |
| `* FILL ... maker/TAKER [layer] BOTD (+pts)` | a trade (layer = front / depth / taker) |
| `> TAKER ...` / `< TAKER ... +pts for $cost` | a taker round trip started / finished |
| `- BOOK ... skipped to stay maker` | the fresh book moved; we didn't send a crossing order |
| `. ... exchange busy` | normal: True Markets takes one order action at a time |
| `!` / `!!` | error / the bot stopped itself (read the reason) |

## Setup (first time)

```powershell
git clone https://github.com/shorjain/TM-Trading-Bot.git
cd TM-Trading-Bot
git checkout feat/market-maker-bot
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
```

Create a personal API key at <https://app.truemarkets.co/settings/api-keys> and save it as
`~\.truemarkets\api-key.json` (outside the repo). **This repo is public: never commit a key.**
Fund the exchange (CeFi) with USDC; the bot spends only exchange USDC.

## Tuning

Everything is in [config.yaml](config.yaml), with a comment on every setting. Set `book_of_the_day`
there to change it without restarting (the running bot re-reads it every minute).

## Layout

```
tmbot/
  strategy.py   quoting rules (pure, unit tested)
  bot.py        loop: data -> fills -> risk -> plan -> prioritized actions (fresh cross check)
  executor.py   paper/live orders, maker/taker inference, 422 shrink
  market.py     EBBO snapshots, reference age, single-book fresh fetch
  analytics.py  flow per book, volatility pause, markouts
  taker.py      taker round trips + self-trade guard
  state.py      instance lock + persisted risk limits
  sessions.py   ET sessions, --until, tradeable hours
  keepawake.py  Windows sleep prevention
  ledger.py     fills, PnL, Arena points
tools/probe.py  read-only platform QA -> Markdown bug report
docs/           FEEDBACK.md, BEN_UPDATES.md, HANDOFF.md
```
