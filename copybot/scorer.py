"""Wallet scoring.

The key idea: rank wallets by how a *copy* of their trades would have done,
entering after a delay at the price available then, not by the wallet's own
PnL. A wallet whose edge vanishes a minute after it trades is useless to us.
"""

from __future__ import annotations

import bisect
import statistics
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

PriceLookup = Callable[[str, int], "float | None"]


@dataclass
class ScoringConfig:
    delays: tuple[int, ...] = (10, 60, 300)   # seconds after the leader's fill
    primary_delay: int = 60
    slippage: float = 0.01                    # extra cost per share on entry and on early exit
    max_entry_price: float = 0.97             # we never copy buys above this
    min_markets: int = 30
    shrink_k: float = 30.0                    # Bayesian shrink toward 0 for small samples
    max_concentration: float = 0.5            # share of profit from the single best market
    max_inactive_days: int = 30
    scalper_median_hold: int = 600            # median hold below this = market maker / scalper
    max_price_lag: int = 900                  # ignore price points further than this past the target time


@dataclass
class Lot:
    """One buy, closed by a later sell or by market resolution."""
    token_id: str
    condition_id: str
    entry_ts: int
    entry_price: float
    size: float
    exit_ts: int | None = None
    exit_price: float | None = None
    exit_kind: str | None = None             # "sell" | "resolution"

    @property
    def cost(self) -> float:
        return self.entry_price * self.size

    @property
    def pnl(self) -> float:
        assert self.exit_price is not None
        return (self.exit_price - self.entry_price) * self.size


@dataclass
class WalletMetrics:
    wallet: str
    n_markets: int = 0
    n_lots: int = 0
    open_lots: int = 0
    cost: float = 0.0
    pnl: float = 0.0
    roi: float = 0.0
    market_win_rate: float = 0.0
    concentration: float = 1.0
    median_hold_s: float = 0.0
    last_active: int = 0
    copy_roi: dict[int, float] = field(default_factory=dict)
    copy_coverage: float = 0.0               # share of lots we had prices for
    score: float = 0.0
    eligible: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["copy_roi"] = {str(k): v for k, v in self.copy_roi.items()}
        return d


def build_lots(fills: Iterable[Mapping],
               resolutions: Mapping[str, tuple[Sequence[float], int | None]]) -> list[Lot]:
    """FIFO-match a wallet's buys to its sells per token, then close what's
    left at the resolution payout. Lots still open are returned unclosed."""
    open_lots: dict[str, deque[Lot]] = defaultdict(deque)
    outcome_of: dict[str, int | None] = {}
    closed: list[Lot] = []

    for f in sorted(fills, key=lambda f: f["ts"]):
        tok = f["token_id"]
        outcome_of.setdefault(tok, f["outcome_index"])
        if f["side"] == "BUY":
            open_lots[tok].append(Lot(tok, f["condition_id"], f["ts"],
                                      f["price"], f["size"]))
            continue
        remaining = f["size"]
        q = open_lots[tok]
        while remaining > 1e-9 and q:
            lot = q[0]
            take = min(lot.size, remaining)
            if take < lot.size - 1e-9:
                # split the lot: close `take` shares, keep the rest open
                part = Lot(tok, lot.condition_id, lot.entry_ts, lot.entry_price, take)
                lot.size -= take
            else:
                part = q.popleft()
            part.exit_ts, part.exit_price, part.exit_kind = f["ts"], f["price"], "sell"
            closed.append(part)
            remaining -= take
        # sells beyond known buys (shares from before our history, splits, ...) are ignored

    still_open: list[Lot] = []
    for tok, q in open_lots.items():
        for lot in q:
            res = resolutions.get(lot.condition_id)
            idx = outcome_of.get(tok)
            if res is not None and idx is not None and idx < len(res[0]):
                lot.exit_price = float(res[0][idx])
                lot.exit_ts = res[1]
                lot.exit_kind = "resolution"
                closed.append(lot)
            else:
                still_open.append(lot)
    return closed + still_open


def make_price_lookup(series: Mapping[str, Sequence[tuple[int, float]]],
                      max_lag: int = 900) -> PriceLookup:
    """Price at or just after `ts` (conservative: we can't trade before we see it)."""
    index = {tok: ([t for t, _ in pts], [p for _, p in pts])
             for tok, pts in series.items()}

    def lookup(token_id: str, ts: int) -> float | None:
        if token_id not in index:
            return None
        times, prices = index[token_id]
        i = bisect.bisect_left(times, ts)
        if i >= len(times) or times[i] - ts > max_lag:
            return None
        return prices[i]

    return lookup


def copy_return(lot: Lot, delay: int, prices: PriceLookup,
                cfg: ScoringConfig) -> float | None:
    """Return per dollar of a copy entering `delay`s after the leader, mirroring
    their exit (also delayed) or holding to resolution."""
    entry = prices(lot.token_id, lot.entry_ts + delay)
    if entry is None:
        return None
    entry += cfg.slippage
    if entry >= cfg.max_entry_price or entry <= 0:
        return None
    if lot.exit_kind == "sell":
        exit_ = prices(lot.token_id, lot.exit_ts + delay)
        if exit_ is None:
            exit_ = lot.exit_price
        exit_ = max(exit_ - cfg.slippage, 0.0)
    else:
        exit_ = lot.exit_price
    return (exit_ - entry) / entry


def score_wallet(wallet: str, fills: Sequence[Mapping],
                 resolutions: Mapping[str, tuple[Sequence[float], int | None]],
                 prices: PriceLookup, now: int,
                 cfg: ScoringConfig | None = None,
                 lot_filter: Callable[[Lot], bool] | None = None) -> WalletMetrics:
    cfg = cfg or ScoringConfig()
    m = WalletMetrics(wallet=wallet)
    if not fills:
        m.reasons.append("no fills")
        return m
    m.last_active = max(f["ts"] for f in fills)

    lots = build_lots(fills, resolutions)
    if lot_filter:
        lots = [l for l in lots if lot_filter(l)]
    closed = [l for l in lots if l.exit_price is not None]
    m.open_lots = len(lots) - len(closed)
    m.n_lots = len(closed)
    if not closed:
        m.reasons.append("no closed positions")
        return m

    m.cost = sum(l.cost for l in closed)
    m.pnl = sum(l.pnl for l in closed)
    m.roi = m.pnl / m.cost if m.cost else 0.0

    by_market: dict[str, float] = defaultdict(float)
    for l in closed:
        by_market[l.condition_id] += l.pnl
    m.n_markets = len(by_market)
    m.market_win_rate = sum(1 for v in by_market.values() if v > 0) / m.n_markets
    gains = [v for v in by_market.values() if v > 0]
    m.concentration = max(gains) / sum(gains) if gains else 1.0

    holds = [l.exit_ts - l.entry_ts for l in closed
             if l.exit_kind == "sell" and l.exit_ts is not None]
    m.median_hold_s = statistics.median(holds) if holds else float("inf")

    for d in cfg.delays:
        num = den = 0.0
        covered = 0
        for l in closed:
            r = copy_return(l, d, prices, cfg)
            if r is None:
                continue
            covered += 1
            w = l.cost                       # weight by leader conviction (dollars)
            num += r * w
            den += w
        m.copy_roi[d] = num / den if den else 0.0
        if d == cfg.primary_delay:
            m.copy_coverage = covered / len(closed)

    shrink = m.n_markets / (m.n_markets + cfg.shrink_k)
    m.score = m.copy_roi.get(cfg.primary_delay, 0.0) * shrink

    if m.n_markets < cfg.min_markets:
        m.reasons.append(f"only {m.n_markets} resolved markets")
    if m.concentration > cfg.max_concentration:
        m.reasons.append(f"{m.concentration:.0%} of profit from one market")
    if m.median_hold_s < cfg.scalper_median_hold and len(holds) > 0.5 * len(closed):
        m.reasons.append("looks like a market maker or scalper")
    if now - m.last_active > cfg.max_inactive_days * 86400:
        m.reasons.append("inactive")
    if m.copy_coverage < 0.5:
        m.reasons.append("not enough price history to replay copies")
    if m.copy_roi.get(cfg.primary_delay, 0.0) <= 0:
        m.reasons.append("copy edge gone after delay")
    m.eligible = not m.reasons
    return m


@dataclass
class WalletForward:
    n_lots: int = 0
    cost: float = 0.0          # leader dollars
    pnl: float = 0.0           # copy PnL at leader size
    sum_r: float = 0.0         # sum of per-trade copy returns

    @property
    def roi(self) -> float:
        return self.pnl / self.cost if self.cost else 0.0

    @property
    def mean_r(self) -> float:
        return self.sum_r / self.n_lots if self.n_lots else 0.0


@dataclass
class WalkForwardResult:
    split_ts: int
    wallets: list[str]
    per_wallet: dict[str, WalletForward] = field(default_factory=dict)

    @property
    def n_lots(self) -> int:
        return sum(w.n_lots for w in self.per_wallet.values())

    @property
    def cost(self) -> float:
        return sum(w.cost for w in self.per_wallet.values())

    @property
    def copy_roi(self) -> float:
        """Copying at the leader's own size. One whale can dominate this."""
        return sum(w.pnl for w in self.per_wallet.values()) / self.cost if self.cost else 0.0

    @property
    def fixed_stake_roi(self) -> float:
        """Same stake on every copied trade, which is how the bot sizes."""
        n = self.n_lots
        return sum(w.sum_r for w in self.per_wallet.values()) / n if n else 0.0

    @property
    def wallet_equal_roi(self) -> float:
        """Average of each wallet's fixed-stake return, so no wallet dominates."""
        ws = [w for w in self.per_wallet.values() if w.n_lots]
        return sum(w.mean_r for w in ws) / len(ws) if ws else 0.0


def walk_forward(fills_by_wallet: Mapping[str, Sequence[Mapping]],
                 resolutions: Mapping[str, tuple[Sequence[float], int | None]],
                 prices: PriceLookup, split_ts: int, top_k: int = 20,
                 cfg: ScoringConfig | None = None) -> WalkForwardResult:
    """The go/no-go test: pick wallets using only data before `split_ts`,
    then measure how copying them would have done on trades after it."""
    cfg = cfg or ScoringConfig()
    ranked = []
    for wallet, fills in fills_by_wallet.items():
        past = [f for f in fills if f["ts"] < split_ts]
        # only positions that had closed before the split count for selection
        m = score_wallet(wallet, past, _resolved_before(resolutions, split_ts),
                         prices, split_ts, cfg)
        if m.eligible:
            ranked.append((m.score, wallet))
    ranked.sort(reverse=True)
    chosen = [w for _, w in ranked[:top_k]]

    result = WalkForwardResult(split_ts, chosen)
    for wallet in chosen:
        wf = result.per_wallet.setdefault(wallet, WalletForward())
        for l in build_lots(fills_by_wallet[wallet], resolutions):
            if l.entry_ts < split_ts or l.exit_price is None:
                continue
            r = copy_return(l, cfg.primary_delay, prices, cfg)
            if r is None:
                continue
            wf.n_lots += 1
            wf.cost += l.cost
            wf.pnl += r * l.cost
            wf.sum_r += r
    return result


def _resolved_before(resolutions, ts):
    return {c: v for c, v in resolutions.items() if v[1] is not None and v[1] < ts}
