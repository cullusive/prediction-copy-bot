"""Builds the candidate wallet list and backfills the data the scorer needs."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable, Iterator

from .data_api import DataApi
from .db import Store

log = logging.getLogger(__name__)

LEADERBOARD_PERIODS = ("all", "month", "week")
LEADERBOARD_CATEGORIES = ("overall", "politics", "sports", "crypto",
                          "economics", "culture", "tech")


def _wallet_of(item: dict) -> str | None:
    for key in ("proxy_wallet", "proxyWallet", "user_id", "address", "wallet"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("0x"):
            return v.lower()
    return None


def _name_of(item: dict) -> str | None:
    return item.get("user_name") or item.get("name") or item.get("pseudonym")


def normalize_fill(item: dict, wallet: str | None = None) -> dict | None:
    """Map a Data API activity/trade item to a `fills` row."""
    wallet = wallet or _wallet_of(item)
    token = item.get("token_id") or item.get("asset")
    cond = item.get("condition_id") or item.get("conditionId")
    side = (item.get("side") or "").upper()
    if not (wallet and token and cond and side in ("BUY", "SELL")):
        return None
    price = float(item.get("price") or 0)
    size = float(item.get("size") or 0)
    if price <= 0 or size <= 0:
        return None
    usdc = item.get("usdc_size")
    return {
        "wallet": wallet.lower(),
        "tx_hash": item.get("transaction_hash") or item.get("transactionHash") or "",
        "token_id": str(token),
        "condition_id": cond,
        "outcome_index": item.get("outcome_index"),
        "side": side,
        "price": price,
        "size": size,
        "usdc_size": float(usdc) if usdc is not None else price * size,
        "ts": int(item.get("timestamp") or 0),
        "title": item.get("title"),
        "slug": item.get("slug"),
    }


UMA_ONE = 10 ** 18          # UMA reports YES / outcome 0 as 1e18
UMA_UNSET = "69"            # sentinel the v2 API returns before any price is set


def parse_resolution(item: dict) -> tuple[str | None, list[float] | None, int | None]:
    """Extract (condition_id, payout per outcome, resolved_at) from a resolution item.

    Live v2 shape: {"condition_id", "status": "resolved"|"posed"|..., "price":
    "1000000000000000000", "last_update_timestamp": "1789594780"}. `price` is the
    UMA oracle answer for outcome 0: 1e18 -> [1, 0], 0 -> [0, 1], 5e17 -> 50/50.
    Older/alternative shapes (payouts lists, winning index) are still accepted.
    """
    cond = item.get("condition_id") or item.get("conditionId")
    resolved_at = (item.get("settlement_time") or item.get("resolved_at")
                   or item.get("settled_at") or item.get("last_update_timestamp"))
    payouts = None
    for key in ("payouts", "payout_numerators", "payoutNumerators"):
        v = item.get(key)
        if isinstance(v, list) and v:
            nums = [float(x) for x in v]
            total = sum(nums) or 1.0
            payouts = [x / total for x in nums]
            break
    if payouts is None and item.get("price") not in (None, "", UMA_UNSET):
        p = int(item["price"]) / UMA_ONE
        if 0.0 <= p <= 1.0:
            payouts = [p, 1.0 - p]
    if payouts is None:
        win = item.get("winning_outcome_index", item.get("winningOutcomeIndex"))
        n = int(item.get("outcome_count") or 2)
        if win is not None:
            payouts = [1.0 if i == int(win) else 0.0 for i in range(n)]
    status = str(item.get("status") or item.get("state") or "").lower()
    if status and status not in ("resolved", "settled", "finalized"):
        payouts = None
    return cond, payouts, _epoch(resolved_at)


def _epoch(v: Any) -> int | None:
    """Unix seconds from an int, a digit string, or an ISO-8601 string."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or str(v).isdigit():
        return int(v)
    return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())


def iter_holders(groups: Iterable[dict]) -> Iterator[dict]:
    """v2 /holders returns one group per token: {"token_id", "holders": [...]}."""
    for g in groups:
        if isinstance(g.get("holders"), list):
            yield from g["holders"]
        else:
            yield g


# (bucket seconds, approx. retention seconds) observed on the live API, 2026-10.
# 60s is kept ~9 days, 300s ~2 months; 1800s goes back further but not to January.
PRICE_BUCKETS = ((60, 9 * 86400), (300, 65 * 86400), (1800, None))
PRICE_WINDOW_MAX = 15 * 86400     # API limit on end - start
PRICE_PAD = 1800                  # fetch this long after each fill (delays + lag)


def buckets_for(start: int, now: int) -> list[int]:
    """Buckets worth asking for, finest first, given how old the window is."""
    return [b for b, keep in PRICE_BUCKETS if keep is None or now - start <= keep]


def price_windows(fill_ts: Iterable[int], pad: int = PRICE_PAD,
                  max_gap: int = 86400,
                  max_span: int = PRICE_WINDOW_MAX) -> list[tuple[int, int]]:
    """Cover [ts, ts+pad] for each fill with as few windows as possible.

    Fills less than `max_gap` apart share a window, as long as it stays under
    the API's `max_span`; far-apart fills get their own small window rather
    than downloading the quiet days in between."""
    windows: list[list[int]] = []
    for ts in sorted(set(fill_ts)):
        if (windows and ts - windows[-1][1] <= max_gap
                and ts + pad - windows[-1][0] <= max_span):
            windows[-1][1] = ts + pad
        else:
            windows.append([ts, ts + pad])
    return [(a, b) for a, b in windows]


class Collector:
    def __init__(self, api: DataApi, store: Store):
        self.api = api
        self.store = store

    # Candidate discovery ---------------------------------------------------

    def discover_from_boards(self, per_board: int = 500) -> int:
        now = int(time.time())
        found = 0
        for period in LEADERBOARD_PERIODS:
            for cat in LEADERBOARD_CATEGORIES:
                try:
                    for e in self.api.leaderboard(period, cat, max_items=per_board):
                        if (w := _wallet_of(e)):
                            self.store.add_wallet(w, "leaderboard", now, _name_of(e))
                            found += 1
                except Exception as exc:  # unknown category names etc.
                    log.warning("leaderboard %s/%s failed: %s", period, cat, exc)
        for period in ("month", "all"):
            for e in self.api.biggest_winners(period, max_items=per_board):
                if (w := _wallet_of(e)):
                    self.store.add_wallet(w, "winners", now, _name_of(e))
                    found += 1
        return found

    def discover_from_holders(self, condition_ids: Iterable[str],
                              per_market: int = 50) -> int:
        """Top holders of the given markets (e.g. currently active ones)."""
        now = int(time.time())
        ids = list(condition_ids)
        found = 0
        for i in range(0, len(ids), 20):
            groups = self.api.holders(ids[i:i + 20], max_items=per_market * 20)
            for h in iter_holders(groups):
                if (w := _wallet_of(h)):
                    self.store.add_wallet(w, "holders", now, _name_of(h))
                    found += 1
        return found

    def discover_from_trade_scan(self, days: int = 14, min_cash: float = 250,
                                 max_trades: int = 200_000) -> int:
        """Wallets making sizeable trades recently. This is how we find the
        quiet, profitable wallets that never reach a leaderboard."""
        now = int(time.time())
        found = 0
        for t in self.api.trades(max_items=max_trades, start=now - days * 86400,
                                 filter_type="CASH", filter_amount=min_cash,
                                 taker_only=False):
            if (w := _wallet_of(t)):
                self.store.add_wallet(w, "scan", now, _name_of(t))
                found += 1
        return found

    # Backfill ----------------------------------------------------------------

    def backfill_wallet(self, wallet: str, since: int | None = None,
                        max_fills: int = 50_000) -> int:
        rows = []
        last_ts = since or 0
        for item in self.api.wallet_trades(wallet, start=since, max_items=max_fills):
            row = normalize_fill(item, wallet)
            if row:
                rows.append(row)
                last_ts = max(last_ts, row["ts"])
        added = self.store.add_fills(rows)
        self.store.set_backfilled(wallet, last_ts)
        return added

    def backfill_all(self, max_fills: int = 50_000, max_wallets: int | None = None,
                     since: int | None = None) -> None:
        """`since` bounds the first fetch for wallets not yet backfilled; the
        API returns oldest first, so without it a capped fetch misses recent fills."""
        wallets = self.store.wallets(max_wallets)
        for i, w in enumerate(wallets, 1):
            try:
                start = w["backfilled_to"] or since
                n = self.backfill_wallet(w["address"], start, max_fills)
                log.info("[%d/%d] %s: +%d fills", i, len(wallets), w["address"], n)
            except Exception as exc:
                log.warning("backfill %s failed: %s", w["address"], exc)

    def refresh_resolutions(self, workers: int = 4) -> int:
        """Batches of 20 (the API maximum), fetched on a few threads since a
        few hundred thousand conditions is normal after a wide backfill."""
        now = int(time.time())
        ids = self.store.unresolved_condition_ids()
        batches = [ids[i:i + 20] for i in range(0, len(ids), 20)]
        apis = [DataApi(self.api.base_url) for _ in range(workers)]

        def fetch(job: tuple[int, list[str]]) -> tuple[list[str], list[dict] | None]:
            n, batch = job
            try:
                return batch, apis[n % workers].resolutions(batch)
            except Exception as exc:
                log.warning("resolutions batch failed: %s", exc)
                return batch, None

        resolved = 0
        with ThreadPoolExecutor(workers) as pool:
            for n, (batch, items) in enumerate(pool.map(fetch, enumerate(batches)), 1):
                if items is None:
                    continue
                seen = set()
                for item in items:
                    cond, payouts, at = parse_resolution(item)
                    if cond:
                        seen.add(cond)
                        self.store.set_resolution(cond, payouts, at, now, commit=False)
                        resolved += payouts is not None
                for cond in set(batch) - seen:
                    self.store.set_resolution(cond, None, None, now, commit=False)
                self.store.conn.commit()
                if n % 500 == 0:
                    log.info("resolutions: %d/%d batches, %d resolved", n, len(batches), resolved)
        return resolved

    def fetch_prices(self, tokens: Iterable[str] | None = None) -> None:
        """Finest available price history around each token's fills, used to
        replay delayed copies. The API caps a request at 15 days and keeps
        fine buckets only for recent data, so we try 60s, then 300s, then 1800s."""
        now = int(time.time())
        fill_ts = self.store.fill_times_by_token()
        if tokens is not None:
            wanted = set(tokens)
            fill_ts = {t: v for t, v in fill_ts.items() if t in wanted}
        for i, (token, times) in enumerate(fill_ts.items(), 1):
            if self.store.has_prices(token):
                continue
            pts: list[tuple[int, float]] = []
            for start, end in price_windows(times):
                for bucket in buckets_for(start, now):
                    try:
                        got = self.api.price_history(token, start=start, end=end,
                                                     bucket_seconds=bucket)
                    except Exception as exc:
                        log.warning("prices %s failed: %s", token, exc)
                        got = []
                    if got:
                        pts.extend(_price_point(p) for p in got)
                        break
            self.store.add_prices(token, pts)
            if i % 100 == 0:
                log.info("prices: %d/%d tokens", i, len(fill_ts))


def _price_point(p: dict[str, Any]) -> tuple[int, float]:
    ts = p.get("t") or p.get("ts") or p.get("timestamp")
    price = p.get("p") if p.get("p") is not None else p.get("price")
    return int(ts), float(price)
