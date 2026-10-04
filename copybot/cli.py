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


def watchlist(store: Store, top: int, cfg: ScoringConfig) -> list[str]:
    """Wallets worth recording going forward: everyone currently eligible plus
    the top pre-screened wallets by their own return."""
    eligible = [r["wallet"] for r in store.top_scores(10_000)]
    fills = {w["address"]: [dict(r) for r in store.fills_for(w["address"])]
             for w in store.wallets()}
    screened = prescreen(fills, store.resolutions(), int(time.time()), top, cfg)
    return list(dict.fromkeys(eligible + screened))


def cmd_collect(args, store: Store) -> None:
    """Daily forward collection. Fine-grained price history expires after
    about 9 days, so run this at least weekly (daily is safer)."""
    cfg = ScoringConfig(min_markets=args.min_markets)
    wallets = watchlist(store, args.top, cfg)
    since = int(time.time()) - args.days * 86400
    c = Collector(DataApi(), store)
    print(f"collecting for {len(wallets)} wallets, fills since {args.days} days ago")
    c.backfill_all(max_fills=args.max_fills, since=since, only=wallets)
    print(f"resolved markets found: {c.refresh_resolutions()}")
    c.fetch_prices(since=since, wallets=wallets)
    print("collect done")


def cmd_paper(args, store: Store) -> None:
    from .clob import fetch_book
    from .notify import from_env
    from .paper import PaperConfig, PaperEngine, load_leaders

    cfg = PaperConfig(mode=args.mode, bankroll=args.bankroll)
    if args.action == "run":
        wallets = watchlist(store, args.top, ScoringConfig(min_markets=args.min_markets))
        leaders = load_leaders(store, wallets)
    else:
        leaders = {}
    engine = PaperEngine(store, DataApi(), fetch_book, leaders, cfg, notifier=from_env())
    if args.action == "run":
        print(f"watching {len(leaders)} wallets in {args.mode} mode; Ctrl+C to stop")
        try:
            engine.run()
        except KeyboardInterrupt:
            print("stopped")
    elif args.action == "status":
        print(json.dumps(engine.status(), indent=2))
    elif args.action in ("approve", "reject"):
        if args.id is None:
            raise SystemExit(f"usage: copybot paper {args.action} ID")
        print(getattr(engine, args.action)(args.id))
    elif args.action in ("pause", "resume"):
        engine._set_meta("paused", "1" if args.action == "pause" else "0")
        print(f"{args.action}d")


def cmd_dashboard(args, store: Store) -> None:
    from .clob import fetch_book
    from .dashboard import serve
    from .notify import from_env
    from .paper import SCHEMA as PAPER_SCHEMA, PaperConfig, PaperEngine

    def engine_factory(st: Store) -> PaperEngine:
        meta = dict(st.conn.execute("SELECT key, value FROM paper_meta").fetchall())
        cfg = PaperConfig(mode=meta.get("mode", "auto"),
                          bankroll=float(meta.get("bankroll", 1000)))
        return PaperEngine(st, DataApi(), fetch_book, {}, cfg, notifier=from_env())

    store.conn.executescript(PAPER_SCHEMA)
    serve(args.db, engine_factory, args.port)


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
    print("copy return after delay + slippage:")
    print(f"  same stake per trade (how the bot sizes): {res.fixed_stake_roi:.2%}")
    print(f"  each wallet weighted equally:             {res.wallet_equal_roi:.2%}")
    print(f"  at the leaders' own size:                 {res.copy_roi:.2%}")
    for w in res.wallets:
        wf = res.per_wallet[w]
        print(f"  {w}  trades={wf.n_lots:5d}  stake={wf.mean_r:7.2%}  leader-size={wf.roi:7.2%}")


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

    co = sub.add_parser("collect", help="daily: new fills, resolutions and 60s prices for the watchlist")
    co.add_argument("--top", type=int, default=50, help="pre-screened wallets to watch")
    co.add_argument("--days", type=int, default=2, help="how far back to (re)collect")
    co.add_argument("--max-fills", type=int, default=20_000)
    co.add_argument("--min-markets", type=int, default=30)

    pp = sub.add_parser("paper", help="paper trading: copy live trades with fake money")
    pp.add_argument("action", choices=["run", "status", "approve", "reject", "pause", "resume"])
    pp.add_argument("id", type=int, nargs="?", help="signal id for approve/reject")
    pp.add_argument("--mode", choices=["auto", "approval"], default="auto")
    pp.add_argument("--bankroll", type=float, default=1000.0)
    pp.add_argument("--top", type=int, default=20,
                    help="also watch this many pre-screened wallets (their trades always ask first)")
    pp.add_argument("--min-markets", type=int, default=30)

    db_ = sub.add_parser("dashboard", help="live dashboard at http://localhost:8765")
    db_.add_argument("--port", type=int, default=8765)

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
         "report": cmd_report, "prices": cmd_prices, "collect": cmd_collect, "paper": cmd_paper, "dashboard": cmd_dashboard, "walkforward": cmd_walkforward}[args.cmd](args, store)
    finally:
        store.close()
    return 0
