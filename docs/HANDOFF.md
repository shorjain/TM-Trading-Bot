# Handoff: paste everything below into a new Claude chat

> Updated 2026-11-20: the bot now sizes as % of equity, has an instance lock, `--until` and
> `--continuous`, and persisted loss limits. The README's "Windows (PowerShell) commands" section is the
> current reference; it takes precedence over the older commands below.

---

I'm continuing a project from Claude Code on my Mac. I'm now on a **Windows PC** with no
Claude Code. Please help me set up and run my trading bot here, step by step. I'll paste
terminal output and screenshots back to you. You can't run commands, so give me exact
PowerShell commands one at a time.

## Context
- **What:** a Python market-making bot for the **True Markets Orderbook Arena** (tokenized-stock
  orderbooks such as NVDAC and AAPLC, trading against USDC). Repo: https://github.com/shorjain/TM-Trading-Bot,
  branch **feat/market-maker-bot**. Arena scoring: USD volume, maker fills ×3, taker ×1, and
  "book of the day" ×2 (announced in Discord). Round 1 ends Nov 30 2026 23:59 UTC.
- **Goal:** about 10,000 points by Nov 30 to make the top 10. #10 had 2,958 pts on Nov 15.
  Also earn the program bonuses: $50 for the first API trade and $50 at $1,000 volume.
  Long-term goal: a summer internship at True Markets, so I'm also sending them good bug reports.
- **Contact:** Ben from True Markets (Discord "skaar", ticket #ticket-0053). I'm US-based;
  Ben added me to a test group so stock trading is allowed on my account. Don't suggest a VPN.
- **Account as of Nov 19, 12:20pm ET:** exchange (CeFi) has ~$20.07 USDC and 0.088 NVDAC.
  Wallet has ~$11.04 PYUSD and ~$4.89 USDC, which the bot ignores on purpose. No fills yet.

## How the bot works (short)
It posts buy and sell limit orders just inside the spread around a fair value (a blend of the
exchange's reference price and the book mid), so it's first in line and earns maker credit.
It only sells shares it holds, and it skews quotes as inventory builds. Settings are in `config.yaml`:
6 bps from fair, $8 quotes, $15 max per stock, $30 max in stocks, $3 max loss (then it stops).
It only spends USDC already on the exchange.

## Known True Markets quirks the bot already handles
- Only **one order action at a time per account** (409 "order in flight") → the bot waits and backs off.
- Orders can't be modified until they're `active` (400 "invalid state") → the bot skips those.
- Auto-funding from the wallet has a race bug (PYUSD → USDC converts, then the allocation is
  rejected) → the bot never relies on it. Fund the exchange with `tmbot allocate --asset USDC`.
- The account server is sometimes slow (5–12 s) → 30 s timeouts and safe retries.

## Hard rules
1. **Never run the bot on two computers at once.** It could trade against itself, which is
   banned self-trading. The Mac bot is stopped before the PC starts.
2. **Never share or paste the API key file contents** anywhere: chat, email, Discord or GitHub.
   Make a separate key on the PC instead.
3. Run only during US market hours (9:30am–4pm ET) and set `--minutes` so it stops before 4pm.

## Windows setup steps
1. Install Python 3.10+ from python.org (tick **Add Python to PATH**), and Git from git-scm.com.
2. In PowerShell:
   ```
   cd ~\Documents
   git clone https://github.com/shorjain/TM-Trading-Bot.git
   cd TM-Trading-Bot
   git checkout feat/market-maker-bot
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
3. Create a new API key at app.truemarkets.co/settings/api-keys (name it "TM-TradingBot-PC"), then:
   ```
   mkdir ~\.truemarkets
   move ~\Downloads\truemarkets-api-key-*.json ~\.truemarkets\api-key.json
   copy .env.example .env
   ```
4. Test (read-only): `.venv\Scripts\python -m tmbot status`
5. Windows Settings → System → Power → Sleep: **Never** (while plugged in).
6. Run: `.venv\Scripts\python -m tmbot run --live --book-of-day XXX --until 15:55`
   (`XXX` = today's book from Discord; `--until HH:MM` is US-Eastern). It asks you to type
   `I UNDERSTAND THIS USES REAL MONEY`.

## Commands (Windows)
| Command | What |
|---|---|
| `.venv\Scripts\python -m tmbot status` | balances + open orders (read-only) |
| `.venv\Scripts\python -m tmbot market` | live books + what the bot would quote |
| `.venv\Scripts\python -m tmbot run --live ...` | run with real money; Ctrl+C stops and cancels everything |
| `.venv\Scripts\python -m tmbot report --live` | fills, volume, points, PnL, leaderboard pace |
| `.venv\Scripts\python -m tmbot cancel-all` | emergency: cancel every open order |
| `.venv\Scripts\python -m tmbot allocate --asset USDC` | move wallet USDC onto the exchange |

## Reading the bot output
- `+ BOOK buy qty @ price`: placed an order. `~`: adjusted it. `* FILL`: a trade happened (points!).
- `. ... exchange busy; retrying shortly`: normal.
- `! ...`: an error (send it). `!!`: the bot stopped itself (send the last 20 lines).
- A key-file "readable by other users" warning is harmless on Windows.

## Notes
- Things to tell Ben are in `docs/BEN_UPDATES.md`. Platform bugs found so far are in `docs/FEEDBACK.md`.
- After 4pm, I'll paste `report --live` and `status` so we can review and plan tomorrow.

Please start by walking me through step 1.
