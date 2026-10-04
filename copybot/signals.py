"""Judging a single leader trade: copy it, ask the user, or skip it.

Each new buy by a watched wallet gets a 0-100 score from several factors.
Hard rules can skip a trade outright, and red flags cap the score so the
trade is sent for approval instead of executed automatically.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class SignalConfig:
    auto_threshold: float = 70.0      # at or above: execute in automatic mode
    flag_threshold: float = 45.0      # between flag and auto: ask the user
    min_price: float = 0.05
    max_price: float = 0.90
    max_drift: float = 0.04           # skip if our price is this much worse than the leader's
    max_spread: float = 0.06
    min_leader_usdc: float = 25.0     # ignore dust trades
    consensus_window: int = 3600      # seconds
    big_bet_multiple: float = 5.0     # this many times the leader's usual size is a red flag


@dataclass
class SignalContext:
    leader: str
    leader_price: float               # leader's average fill price
    leader_usdc: float                # leader's dollars in this trade
    leader_median_usdc: float         # leader's typical buy size
    leader_copy_roi: float            # backtested copy return at 60s delay
    leader_markets: int               # resolved markets behind that number
    leader_flags: list[str]           # e.g. from wallet scoring reasons
    our_price: float | None           # price we'd pay now (best ask, or avg fill for our stake)
    spread: float | None
    fill_ratio: float                 # share of our intended stake the book can fill under the cap
    same_side_leaders: int = 0        # other watched wallets buying this outcome recently
    opposite_side_leaders: int = 0    # watched wallets buying the other outcome recently


@dataclass
class Verdict:
    score: float
    decision: str                     # "auto" | "flag" | "skip"
    reasons: list[str] = field(default_factory=list)
    red_flags: list[str] = field(default_factory=list)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_signal(ctx: SignalContext, cfg: SignalConfig | None = None) -> Verdict:
    cfg = cfg or SignalConfig()
    reasons: list[str] = []
    flags: list[str] = list(ctx.leader_flags)

    # Hard skips --------------------------------------------------------------
    skip = []
    if ctx.leader_usdc < cfg.min_leader_usdc:
        skip.append(f"tiny trade (${ctx.leader_usdc:.0f})")
    if ctx.our_price is None or ctx.fill_ratio <= 0:
        skip.append("no liquidity under our price cap")
    else:
        if not cfg.min_price <= ctx.our_price <= cfg.max_price:
            skip.append(f"price {ctx.our_price:.2f} outside {cfg.min_price:.2f}-{cfg.max_price:.2f}")
        drift = ctx.our_price - ctx.leader_price
        if drift > cfg.max_drift:
            skip.append(f"price already moved {drift * 100:.1f}c past the leader")
    if ctx.spread is not None and ctx.spread > cfg.max_spread:
        skip.append(f"spread {ctx.spread * 100:.1f}c too wide")
    if skip:
        return Verdict(0.0, "skip", skip, flags)

    score = 0.0

    # Who: the leader's copyable edge and how much evidence backs it (max 40)
    quality = _clamp(ctx.leader_copy_roi / 0.30) * 30
    confidence = ctx.leader_markets / (ctx.leader_markets + 30) * 10
    score += quality + confidence
    reasons.append(f"leader copy edge {ctx.leader_copy_roi:+.0%} over {ctx.leader_markets} markets")

    # Conviction: bigger than their usual bet (max 15)
    ratio = ctx.leader_usdc / ctx.leader_median_usdc if ctx.leader_median_usdc > 0 else 1.0
    score += _clamp((math.log2(max(ratio, 1e-9)) + 1) / 2) * 15
    reasons.append(f"{ratio:.1f}x their usual size")

    # Consensus among watched wallets (max 16, opposition subtracts)
    score += min(ctx.same_side_leaders, 2) * 8
    score -= ctx.opposite_side_leaders * 15
    if ctx.same_side_leaders:
        reasons.append(f"{ctx.same_side_leaders} other top wallet(s) agree")
    if ctx.opposite_side_leaders:
        flags.append(f"{ctx.opposite_side_leaders} top wallet(s) bet the other way")

    # Price drift since the leader's fill (max 15)
    drift = ctx.our_price - ctx.leader_price
    score += _clamp(1 - max(drift - 0.01, 0) / max(cfg.max_drift - 0.01, 1e-9)) * 15
    reasons.append(f"we'd pay {ctx.our_price:.3f} vs leader {ctx.leader_price:.3f}")

    # Execution quality (max 14)
    if ctx.spread is not None:
        score += _clamp(1 - ctx.spread / cfg.max_spread) * 7
    score += _clamp(ctx.fill_ratio) * 7

    # Payoff shape: expensive favourites have little upside
    if ctx.our_price > 0.80:
        score -= 5
        reasons.append("expensive entry, small upside")

    # Red flags never auto-execute
    if ratio >= cfg.big_bet_multiple and ctx.leader_markets < 50:
        flags.append(f"unusually large bet ({ratio:.0f}x) from a wallet with a short record")

    score = round(_clamp(score, 0, 100), 1)
    if score >= cfg.auto_threshold and not flags:
        decision = "auto"
    elif score >= cfg.flag_threshold or (flags and score >= cfg.auto_threshold):
        decision = "flag"
    else:
        decision = "skip"
        reasons.append(f"score {score} below {cfg.flag_threshold}")
    return Verdict(score, decision, reasons, flags)
