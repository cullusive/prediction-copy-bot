"""Builds the candidate wallet list and backfills the data the scorer needs."""

from __future__ import annotations

import logging
import time
from typing import Any, Iterable

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


def parse_resolution(item: dict) -> tuple[str | None, list[float] | None, int | None]:
    """Extract (condition_id, payout per outcome, resolved_at) from a resolution item.

    The v2 schema for this object isn't documented in detail yet, so this
    accepts the shapes we'd reasonably expect. Verify on the first live run.
    """
    cond = item.get("condition_id") or item.get("conditionId")
    resolved_at = item.get("settlement_time") or item.get("resolved_at") or item.get("settled_at")
    payouts = None
    for key in ("payouts", "payout_numerators", "payoutNumerators"):
        v = item.get(key)
        if isinstance(v, list) and v:
            nums = [float(x) for x in v]
            total = sum(nums) or 1.0
            payouts = [x / total for x in nums]
            break
    if payouts is None:
        win = item.get("winning_outcome_index", item.get("winningOutcomeIndex"))
        n = int(item.get("outcome_count") or 2)
        if win is not None:
            payouts = [1.0 if i == int(win) else 0.0 for i in range(n)]
    status = str(item.get("status") or item.get("state") or "").lower()
    if status and status not in ("resolved", "settled", "finalized"):
        payouts = None
    return cond, payouts, int(resolved_at) if resolved_at else None


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
            for h in self.api.holders(ids[i:i + 20], max_items=per_market * 20):
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

    def backfill_all(self, max_fills: int = 50_000) -> None:
        wallets = self.store.wallets()
        for i, w in enumerate(wallets, 1):
            try:
                n = self.backfill_wallet(w["address"], w["backfilled_to"], max_fills)
                log.info("[%d/%d] %s: +%d fills", i, len(wallets), w["address"], n)
            except Exception as exc:
                log.warning("backfill %s failed: %s", w["address"], exc)

    def refresh_resolutions(self) -> int:
        now = int(time.time())
        ids = self.store.unresolved_condition_ids()
        resolved = 0
        for i in range(0, len(ids), 20):
            batch = ids[i:i + 20]
            try:
                items = self.api.resolutions(batch)
            except Exception as exc:
                log.warning("resolutions batch failed: %s", exc)
                continue
            seen = set()
            for item in items:
                cond, payouts, at = parse_resolution(item)
                if cond:
                    seen.add(cond)
                    self.store.set_resolution(cond, payouts, at, now)
                    resolved += payouts is not None
            for cond in set(batch) - seen:
                self.store.set_resolution(cond, None, None, now)
        return resolved

    def fetch_prices(self, bucket_seconds: int = 60) -> None:
        """1-minute price history per token, used to replay delayed copies."""
        for token in self.store.token_ids():
            if self.store.has_prices(token):
                continue
            try:
                pts = self.api.price_history(token, bucket_seconds=bucket_seconds)
            except Exception as exc:
                log.warning("prices %s failed: %s", token, exc)
                continue
            self.store.add_prices(token, (_price_point(p) for p in pts))


def _price_point(p: dict[str, Any]) -> tuple[int, float]:
    ts = p.get("t") or p.get("ts") or p.get("timestamp")
    price = p.get("p") if p.get("p") is not None else p.get("price")
    return int(ts), float(price)
