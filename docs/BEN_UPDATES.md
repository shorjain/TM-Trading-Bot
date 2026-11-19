# Notes for Ben (True Markets)

Running list of what to tell, ask, or follow up on with Ben (Discord ticket #ticket-0053).
Newest first. Tick items off once they're sent.

## To send next

- [ ] **Bonus check.** Ask whether my first API trade has registered for the $50 first-trade
      bonus. Also ask how I'll know when I cross $1,000 of volume (the second $50).
- [ ] **30% counterparty rule.** Does the house market maker count as a counterparty? Most books
      have only 1 or 2 resting orders, so almost every trade is with them.
      → This decides whether we can add taker volume to climb the leaderboard.
- [ ] **One order in flight per account.** A second order or modify while one is processing gets
      `409 user already has an order in flight`. It's undocumented and slows down any bot quoting
      several books. Ask whether there's a batch endpoint or plans to allow concurrency.
- [ ] **Leaderboard.** Is there a live leaderboard or points API, or only the Discord video?
      Can I see my own points?
- [ ] **The $4.89 is resolved.** The money wasn't lost; it was sitting unallocated in the wallet.
      Worth saying so they don't go hunting for it.

## Already drafted or sent (2026-11-19)

- [x] Asked what error I was getting (Ben asked). Answer: no error; AAPLC traded ~0.4% above
      the underlying, so my bids never filled. I retuned the bot.
- [x] **Funding-bridge race bug.** The bridge converts PYUSD → USDC, but the USDC allocation runs
      before the conversion settles and gets rejected, so the order fails and the USDC is stranded
      in the wallet. Happened twice. request_id 49f483ca-b818-96e8-adcb-712b5fc90189.
- [x] US geo-block → Ben added me to the test group. Stock trading is now allowed on my account.
- [x] Passkey setup failed with "User already has a wallet".

## Bigger feedback to send once the round is going well

Everything in [FEEDBACK.md](FEEDBACK.md), especially:
1. Mid-price dislocations in price history (NVDAC 106–210, AAPLC/MUC min 0, all at 2026-11-04 13:00 UTC).
2. `POST /quotes` rejects every stock pair.
3. No `post_only` flag, and no maker/taker flag on fills.
4. `/balances` and `/portfolio` disagree, and DeFi `available` isn't net of `allocated`.
