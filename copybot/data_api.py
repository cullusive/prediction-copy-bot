"""Read-only client for the Polymarket (international) Data API v2.

No authentication is needed. We only ever read public on-chain data here;
orders are never placed against this venue.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

BASE_URL = "https://data-api.polymarket.com/v2"
MAX_LIMIT = 1000


class DataApiError(RuntimeError):
    pass


class DataApi:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 30.0,
                 max_retries: int = 5, min_interval: float = 0.15):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._last_request = 0.0

    def get(self, path: str, **params: Any) -> dict:
        query = {k: _fmt(v) for k, v in params.items() if v is not None}
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        for attempt in range(self.max_retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "prediction-copy-bot/0.1",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # 429 = busy (honour Retry-After); 503 = DB budget timeout.
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    retry_after = e.headers.get("Retry-After")
                    time.sleep(float(retry_after) if retry_after else 2 ** attempt)
                    continue
                raise DataApiError(f"{e.code} for {url}: {e.read()[:300]!r}") from e
            except urllib.error.URLError as e:
                if attempt < self.max_retries:
                    time.sleep(2 ** attempt)
                    continue
                raise DataApiError(f"network error for {url}: {e}") from e
        raise DataApiError(f"gave up on {url}")

    def paginate(self, path: str, max_items: int | None = None,
                 **params: Any) -> Iterator[dict]:
        """Yield items across cursor pages (`pagination.next_cursor`)."""
        params.setdefault("limit", MAX_LIMIT)
        seen = 0
        cursor = None
        while True:
            page = self.get(path, cursor=cursor, **params)
            data = page.get("data") or []
            if isinstance(data, dict):  # single-entry responses
                data = [data]
            for item in data:
                yield item
                seen += 1
                if max_items is not None and seen >= max_items:
                    return
            pagination = page.get("pagination") or {}
            cursor = pagination.get("next_cursor")
            if not cursor or not pagination.get("has_more", True) or not data:
                return

    # Endpoint helpers -----------------------------------------------------

    def leaderboard(self, time_period: str = "all", category: str = "overall",
                    sort_by: str = "PNL", max_items: int = 1000) -> Iterator[dict]:
        return self.paginate("leaderboard", max_items=max_items,
                             time_period=time_period, category=category,
                             sort_by=sort_by)

    def biggest_winners(self, time_period: str = "month",
                        category: str = "overall",
                        max_items: int = 1000) -> Iterator[dict]:
        return self.paginate("biggest-winners", max_items=max_items,
                             time_period=time_period, category=category)

    def holders(self, condition_ids: list[str], max_items: int = 1000,
                min_balance: float = 0) -> Iterator[dict]:
        return self.paginate("holders", max_items=max_items,
                             condition=",".join(condition_ids[:20]),
                             min_balance=min_balance)

    def trades(self, max_items: int | None = None, **params: Any) -> Iterator[dict]:
        return self.paginate("trades", max_items=max_items, **params)

    def wallet_trades(self, wallet: str, start: int | None = None,
                      max_items: int | None = None) -> Iterator[dict]:
        """All fills for a wallet (maker and taker), oldest first."""
        return self.paginate("activity", max_items=max_items, user=wallet,
                             type="TRADE", start=start,
                             sort_direction="ASC")

    def resolutions(self, condition_ids: list[str]) -> list[dict]:
        page = self.get("resolutions", condition=",".join(condition_ids[:20]))
        return page.get("data") or []

    def price_history(self, token_id: str, start: int | None = None,
                      end: int | None = None,
                      bucket_seconds: int = 60) -> list[dict]:
        return list(self.paginate("prices-history", token_id=token_id,
                                  start=start, end=end,
                                  bucket_seconds=bucket_seconds,
                                  limit=10000))


def _fmt(v: Any) -> Any:
    if isinstance(v, bool):
        return "true" if v else "false"
    return v
