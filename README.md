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
