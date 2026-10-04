"""Command line entry point: `python -m copybot <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import datetime, timezone

from .collector import Collector
from .data_api import DataApi
from .db import Store
from .scorer import ScoringConfig, make_price_lookup, score_wallet, walk_forward


def _load(store: Store):
    resolutions = store.resolutions()
    series = {t: store.prices_for(t) for t in store.token_ids()}
    fills = {w["address"]: [dict(r) for r in store.fills_for(w["address"])]
             for w in store.wallets()}
    return fills, resolutions, series


def cmd_discover(args, store: Store) -> None:
    c = Collector(DataApi(), store)
    n = c.discover_from_boards(per_board=args.per_board)
    print(f"leaderboards/winners: {n} entries")
    if args.scan_days:
        n = c.discover_from_trade_scan(days=args.scan_days, min_cash=args.min_cash)
        print(f"trade scan: {n} entries")
    print(f"{len(store.wallets())} candidate wallets in database")


def cmd_backfill(args, store: Store) -> None:
    c = Collector(DataApi(), store)
    since = int(time.time()) - args.since_days * 86400 if args.since_days else None
    c.backfill_all(max_fills=args.max_fills, max_wallets=args.max_wallets, since=since)
    print(f"resolved markets found: {c.refresh_resolutions()}")
    if not args.skip_prices:
        c.fetch_prices()
    print("backfill done")


def prescreen(fills: dict, resolutions: dict, now: int, top: int,
              cfg: ScoringConfig) -> list[str]:
    """Wallets worth fetching prices for, ranked by their own (not copy) ROI.
    Same filters as eligibility except the ones that need price history."""
    skip = ("not enough price history", "copy edge gone")
    ranked = []
    for wallet, fs in fills.items():
        m = score_wallet(wallet, fs, resolutions, lambda t, ts: None, now, cfg)
        if not [r for r in m.reasons if not r.startswith(skip)] and m.roi > 0:
            ranked.append((m.roi * m.n_markets / (m.n_markets + cfg.shrink_k), wallet))
    ranked.sort(reverse=True)
    return [w for _, w in ranked[:top]]


def cmd_prices(args, store: Store) -> None:
    """Fetch price history only for the most promising wallets' tokens."""
    cfg = ScoringConfig(min_markets=args.min_markets)
    fills = {w["address"]: [dict(r) for r in store.fills_for(w["address"])]
             for w in store.wallets()}
    resolutions = store.resolutions()
    chosen = set(prescreen(fills, resolutions, int(time.time()), args.top, cfg))
    if args.split:
        split = int(datetime.fromisoformat(args.split).replace(tzinfo=timezone.utc).timestamp())
        past = {w: [f for f in fs if f["ts"] < split] for w, fs in fills.items()}
        chosen |= set(prescreen(past, {c: v for c, v in resolutions.items()
                                       if v[1] is not None and v[1] < split},
                                split, args.top, cfg))
    tokens = {f["token_id"] for w in chosen for f in fills[w]}
    print(f"fetching prices for {len(tokens)} tokens of {len(chosen)} wallets")
    Collector(DataApi(), store).fetch_prices(tokens)


def cmd_score(args, store: Store) -> None:
    cfg = ScoringConfig(min_markets=args.min_markets)
    fills, resolutions, series = _load(store)
    prices = make_price_lookup(series, cfg.max_price_lag)
    now = int(time.time())
    for wallet, fs in fills.items():
        m = score_wallet(wallet, fs, resolutions, prices, now, cfg)
        store.save_score(wallet, m.score, m.eligible, m.as_dict(), now)
    cmd_report(args, store)


def cmd_report(args, store: Store) -> None:
    rows = store.top_scores(args.top, eligible_only=not args.all)
    print(f"{'wallet':44} {'score':>7} {'mkts':>5} {'roi':>7} "
          f"{'copy10s':>8} {'copy60s':>8} {'copy5m':>8}  notes")
    for r in rows:
        m = json.loads(r["metrics"])
        cr = m["copy_roi"]
        print(f"{r['wallet']:44} {r['score']:7.3f} {m['n_markets']:5d} {m['roi']:7.1%} "
              f"{cr.get('10', 0):8.1%} {cr.get('60', 0):8.1%} {cr.get('300', 0):8.1%}  "
              f"{'; '.join(m['reasons'])}")


def cmd_walkforward(args, store: Store) -> None:
    split = int(datetime.fromisoformat(args.split).replace(tzinfo=timezone.utc).timestamp())
    cfg = ScoringConfig(min_markets=args.min_markets)
    fills, resolutions, series = _load(store)
    prices = make_price_lookup(series, cfg.max_price_lag)
    res = walk_forward(fills, resolutions, prices, split, args.top, cfg)
    print(f"picked {len(res.wallets)} wallets using data before {args.split}")
    print(f"copied {res.n_lots} of their later trades, ${res.cost:,.0f} of leader size")
    print(f"copy return after delay + slippage: {res.copy_roi:.2%}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="copybot")
    p.add_argument("--db", default="copybot.db")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="find candidate wallets")
    d.add_argument("--per-board", type=int, default=500)
    d.add_argument("--scan-days", type=int, default=14,
                   help="scan recent big trades for quiet wallets (0 to skip)")
    d.add_argument("--min-cash", type=float, default=250)

    b = sub.add_parser("backfill", help="download fills, resolutions and prices")
    b.add_argument("--max-fills", type=int, default=50_000)
    b.add_argument("--skip-prices", action="store_true")
    b.add_argument("--max-wallets", type=int, default=None,
                   help="backfill only this many candidates, multi-source first")
    b.add_argument("--since-days", type=int, default=None,
                   help="first fetch starts this many days ago instead of the beginning")

    for name, help_ in (("score", "score all wallets"), ("report", "show top wallets")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--top", type=int, default=30)
        s.add_argument("--all", action="store_true", help="include ineligible wallets")
        s.add_argument("--min-markets", type=int, default=30)

    pr = sub.add_parser("prices", help="price history for the top pre-screened wallets")
    pr.add_argument("--top", type=int, default=40)
    pr.add_argument("--split", help="also pre-screen on data before this YYYY-MM-DD")
    pr.add_argument("--min-markets", type=int, default=30)

    w = sub.add_parser("walkforward", help="out-of-sample go/no-go test")
    w.add_argument("--split", required=True, help="YYYY-MM-DD; select before, test after")
    w.add_argument("--top", type=int, default=20)
    w.add_argument("--min-markets", type=int, default=30)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s")
    store = Store(args.db)
    try:
        {"discover": cmd_discover, "backfill": cmd_backfill, "score": cmd_score,
         "report": cmd_report, "prices": cmd_prices, "walkforward": cmd_walkforward}[args.cmd](args, store)
    finally:
        store.close()
    return 0
