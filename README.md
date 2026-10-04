# prediction-copy-bot

Finds consistently profitable Polymarket wallets, including quiet ones that never reach a leaderboard, and backtests whether copying them would make money **after our delay and slippage**.

This is phase 1 of the plan: research and backtest only. It reads public data from the Polymarket Data API and never places orders. Later phases add live wallet watching, per-trade scoring, Discord approvals, execution on Polymarket US, and a Windows .exe build.

## How it works

1. `discover` builds a candidate list from the leaderboards (several periods and categories), the biggest-winners board, and a scan of recent large trades.
2. `backfill` downloads every candidate's fills, market resolutions, and one-minute price history.
3. `score` matches each wallet's buys to its sells (FIFO) or to the resolution payout, then replays every buy as if we had entered 10s, 60s and 5 min later at the price then, plus slippage. Wallets are ranked by that copy return, shrunk toward zero for small samples. Wallets are rejected for too few markets, profit concentrated in one market, scalping or market-making, inactivity, or an edge that is gone after the delay.
4. `walkforward --split YYYY-MM-DD` is the go/no-go test. It picks wallets using only data before the split date, then measures how copying them would have done afterwards.

## Usage

```
python -m copybot discover
python -m copybot backfill -v
python -m copybot score --top 30
python -m copybot walkforward --split 2026-07-01
```

Everything is stored in `copybot.db` (SQLite) in the current folder. The code uses only the Python standard library. Tests need pytest: `python -m pytest`.

## Still to verify on the first live run

- The exact shape of `/v2/resolutions` items. `parse_resolution` accepts the likely shapes; check one real response.
- The field names in `/v2/holders` and `/v2/prices-history` items.
- Whether `/v2/activity?type=TRADE` returns maker fills as well as taker fills.

## Paper trading (phase 2)

`python -m copybot paper run` watches the eligible wallets (plus the top pre-screened ones) and copies their new trades with fake money. Fills are simulated against the live order book, so spreads, depth and our delay are all real. No orders are ever placed.

Every leader buy gets a 0-100 score built from: the leader's backtested copy edge and sample size, the bet size relative to their usual size, whether other watched wallets agree or bet the other way, how far the price has moved since their fill, the spread and depth for our stake, and how expensive the entry is. Hard rules skip tiny trades, prices outside 5-90c, moves of more than 4c past the leader, and wide spreads. Red flags (an unproven leader, an unusually large bet from a short record, top wallets on the other side) never auto-execute.

- `--mode auto` executes scores of 70 or more and asks for approval between 45 and 70.
- `--mode approval` asks for everything that isn't skipped.
- Approve or reject with `python -m copybot paper approve ID` / `reject ID`. Requests expire after 5 minutes.
- When a leader sells, we sell the same fraction. Positions held to resolution settle automatically.
- Risk limits: 2% of equity per trade ($5-30), 10% per market, 15% per leader, 60% total open, and trading pauses for the day after a 5% loss. `paper pause` / `paper resume` stop and start it by hand.
- `python -m copybot paper status` shows equity, PnL, win rate and signal counts.

Alerts go to Discord if `COPYBOT_DISCORD_WEBHOOK` is set to a channel webhook URL, and to the console otherwise. Keep the webhook URL out of the repo.
