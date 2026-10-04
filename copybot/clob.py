"""Public order books from the Polymarket CLOB (read only, no auth).

Paper trading fills simulated orders against these books. Polymarket US
books will replace them once that account exists; the interface stays the same.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass

CLOB_URL = "https://clob.polymarket.com"


@dataclass
class Book:
    token_id: str
    bids: list[tuple[float, float]]   # (price, size), best first
    asks: list[tuple[float, float]]   # (price, size), best first

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


def parse_book(token_id: str, raw: dict) -> Book:
    def levels(key: str, reverse: bool) -> list[tuple[float, float]]:
        out = [(float(l["price"]), float(l["size"])) for l in raw.get(key) or []]
        out = [l for l in out if l[1] > 0]
        return sorted(out, key=lambda l: l[0], reverse=reverse)
    return Book(token_id, bids=levels("bids", True), asks=levels("asks", False))


def fetch_book(token_id: str, base_url: str = CLOB_URL, timeout: float = 10.0) -> Book:
    req = urllib.request.Request(f"{base_url}/book?token_id={token_id}",
                                 headers={"Accept": "application/json",
                                          "User-Agent": "prediction-copy-bot/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_book(token_id, json.loads(resp.read().decode("utf-8")))


@dataclass
class Fill:
    shares: float
    cost: float          # dollars paid (buy) or received (sell)

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


def simulate_buy(book: Book, dollars: float, max_price: float) -> Fill:
    """Walk the asks up to `max_price`, spending at most `dollars` (a limit
    order with a hard price cap, filled immediately or not at all)."""
    shares = cost = 0.0
    for price, size in book.asks:
        if price > max_price or cost >= dollars - 1e-9:
            break
        take = min(size, (dollars - cost) / price)
        shares += take
        cost += take * price
    return Fill(shares, cost)


def simulate_sell(book: Book, shares: float, min_price: float = 0.0) -> Fill:
    sold = proceeds = 0.0
    for price, size in book.bids:
        if price < min_price or sold >= shares - 1e-9:
            break
        take = min(size, shares - sold)
        sold += take
        proceeds += take * price
    return Fill(sold, proceeds)
