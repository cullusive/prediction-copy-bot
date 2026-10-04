"""Paper trading: watch the top wallets live and copy their trades with fake money.

No orders are ever sent anywhere. Fills are simulated against the live order
book, so the results include real spreads, depth and the delay between the
leader's trade and ours.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .clob import Book, simulate_buy, simulate_sell
from .collector import normalize_fill, parse_resolution
from .db import Store
from .notify import Notifier
from .signals import SignalConfig, SignalContext, Verdict, score_signal

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    leader        TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    outcome_index INTEGER,
    title         TEXT,
    leader_price  REAL NOT NULL,
    leader_usdc   REAL NOT NULL,
    our_price     REAL,
    score         REAL NOT NULL,
    decision      TEXT NOT NULL,          -- auto | flag | skip
    status        TEXT NOT NULL,          -- executed | pending | skipped | rejected | expired | blocked
    reasons       TEXT NOT NULL,          -- JSON {"reasons": [...], "red_flags": [...]}
    note          TEXT
);
CREATE TABLE IF NOT EXISTS paper_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL,
    leader        TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    outcome_index INTEGER,
    title         TEXT,
    shares        REAL NOT NULL,
    cost          REAL NOT NULL,          -- dollars still at risk
    proceeds      REAL NOT NULL DEFAULT 0,-- dollars received from sells / settlement
    opened_ts     INTEGER NOT NULL,
    closed_ts     INTEGER,
    status        TEXT NOT NULL           -- open | closed
);
"""


@dataclass
class PaperConfig:
    mode: str = "auto"                # "auto" or "approval"
    bankroll: float = 1000.0
    stake_pct: float = 0.02
    min_stake: float = 5.0
    max_stake: float = 30.0
    per_market_pct: float = 0.10
    per_leader_pct: float = 0.15
    total_exposure_pct: float = 0.60
    daily_loss_stop_pct: float = 0.05
    approval_ttl: int = 300           # seconds before a pending approval expires
    poll_seconds: float = 15.0
    settle_every: int = 600


@dataclass
class Leader:
    wallet: str
    copy_roi: float
    n_markets: int
    median_usdc: float
    flags: list[str] = field(default_factory=list)


def load_leaders(store: Store, wallets: Iterable[str]) -> dict[str, Leader]:
    """Profiles for the watched wallets, from the last `score` run and their fills."""
    scores = {r["wallet"]: json.loads(r["metrics"])
              for r in store.conn.execute("SELECT wallet, metrics FROM wallet_scores")}
    out = {}
    for w in wallets:
        m = scores.get(w, {})
        buys = [r["usdc_size"] for r in store.fills_for(w) if r["side"] == "BUY"]
        flags = []
        if not m.get("eligible", False):
            flags.append("leader not fully proven in the backtest")
        out[w] = Leader(
            wallet=w,
            copy_roi=float((m.get("copy_roi") or {}).get("60", 0.0)),
            n_markets=int(m.get("n_markets", 0)),
            median_usdc=statistics.median(buys) if buys else 0.0,
            flags=flags,
        )
    return out


class PaperEngine:
    def __init__(self, store: Store, api, book_fn: Callable[[str], Book],
                 leaders: dict[str, Leader], cfg: PaperConfig | None = None,
                 sig_cfg: SignalConfig | None = None,
                 notifier: Notifier | None = None,
                 now_fn: Callable[[], float] = time.time):
        self.store = store
        self.conn = store.conn
        self.conn.executescript(SCHEMA)
        self.api = api
        self.book_fn = book_fn
        self.leaders = leaders
        self.cfg = cfg or PaperConfig()
        self.sig_cfg = sig_cfg or SignalConfig()
        self.notify = notifier or Notifier()
        self.now = lambda: int(now_fn())
        # recent leader buys for consensus: (ts, wallet, condition_id, outcome_index)
        self.recent: list[tuple[int, str, str, int | None]] = []
        # leader share counts we've seen them buy, for mirroring partial sells
        self.leader_shares: dict[tuple[str, str], float] = defaultdict(float)

    # state -----------------------------------------------------------------

    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key: str, value) -> None:
        self.conn.execute("INSERT OR REPLACE INTO paper_meta(key, value) VALUES (?,?)",
                          (key, str(value)))
        self.conn.commit()

    def realized_pnl(self, since: int = 0) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(proceeds - cost), 0) FROM paper_positions "
            "WHERE status='closed' AND closed_ts >= ?", (since,)).fetchone()
        return float(row[0])

    def equity(self) -> float:
        return self.cfg.bankroll + self.realized_pnl()

    def exposure(self, where: str = "1=1", args: tuple = ()) -> float:
        row = self.conn.execute(
            f"SELECT COALESCE(SUM(cost), 0) FROM paper_positions WHERE status='open' AND {where}",
            args).fetchone()
        return float(row[0])

    def paused_reason(self) -> str | None:
        day_start = self.now() - self.now() % 86400
        lost = -self.realized_pnl(since=day_start)
        if lost >= self.cfg.daily_loss_stop_pct * self.cfg.bankroll:
            return f"daily loss stop hit (${lost:.2f} lost today)"
        if self._meta("paused") == "1":
            return "paused by you"
        return None

    # polling -----------------------------------------------------------------

    def poll_once(self) -> None:
        for wallet in self.leaders:
            key = f"last_ts:{wallet}"
            last = self._meta(key)
            if last is None:  # first run: start from now, don't replay history
                self._set_meta(key, self.now())
                continue
            try:
                items = list(self.api.wallet_trades(wallet, start=int(last) + 1, max_items=500))
            except Exception as exc:
                log.warning("poll %s failed: %s", wallet, exc)
                continue
            fills = [f for f in (normalize_fill(i, wallet) for i in items) if f]
            if not fills:
                continue
            self.store.add_fills(fills)
            self._set_meta(key, max(f["ts"] for f in fills))
            for group in _group_orders(fills):
                if group["side"] == "BUY":
                    self.on_leader_buy(group)
                else:
                    self.on_leader_sell(group)

    # buys ------------------------------------------------------------------

    def stake(self) -> float:
        return min(max(self.equity() * self.cfg.stake_pct, self.cfg.min_stake), self.cfg.max_stake)

    def risk_block(self, leader: str, condition_id: str, dollars: float) -> str | None:
        eq = self.equity()
        if (why := self.paused_reason()):
            return why
        if self.exposure() + dollars > self.cfg.total_exposure_pct * eq:
            return "total exposure cap"
        if self.exposure("condition_id=?", (condition_id,)) + dollars > self.cfg.per_market_pct * eq:
            return "per-market cap"
        if self.exposure("leader=?", (leader,)) + dollars > self.cfg.per_leader_pct * eq:
            return "per-leader cap"
        return None

    def on_leader_buy(self, g: dict) -> Verdict:
        now = self.now()
        leader = self.leaders[g["wallet"]]
        self.leader_shares[(g["wallet"], g["token_id"])] += g["size"]
        self.recent = [r for r in self.recent if now - r[0] <= self.sig_cfg.consensus_window]
        same = len({r[1] for r in self.recent if r[1] != g["wallet"]
                    and r[2] == g["condition_id"] and r[3] == g["outcome_index"]})
        opposite = len({r[1] for r in self.recent if r[1] != g["wallet"]
                        and r[2] == g["condition_id"] and r[3] != g["outcome_index"]})
        self.recent.append((now, g["wallet"], g["condition_id"], g["outcome_index"]))

        stake = self.stake()
        book = self._book(g["token_id"])
        cap = g["price"] + self.sig_cfg.max_drift
        fill = simulate_buy(book, stake, cap) if book else None
        our_price = (fill.avg_price if fill and fill.shares else
                     (book.best_ask if book else None))
        ctx = SignalContext(
            leader=g["wallet"], leader_price=g["price"], leader_usdc=g["usdc"],
            leader_median_usdc=leader.median_usdc, leader_copy_roi=leader.copy_roi,
            leader_markets=leader.n_markets, leader_flags=list(leader.flags),
            our_price=our_price, spread=book.spread if book else None,
            fill_ratio=(fill.cost / stake) if fill and stake else 0.0,
            same_side_leaders=same, opposite_side_leaders=opposite)
        v = score_signal(ctx, self.sig_cfg)

        if v.decision == "skip":
            self._record(g, v, our_price, "skipped")
            return v
        if v.decision == "auto" and self.cfg.mode == "auto":
            sid = self._record(g, v, our_price, "pending")
            self._execute(sid)
            return v
        sid = self._record(g, v, our_price, "pending")
        self.notify.send(self._approval_text(sid, g, v, our_price))
        return v

    def _book(self, token_id: str) -> Book | None:
        try:
            return self.book_fn(token_id)
        except Exception as exc:
            log.warning("book %s failed: %s", token_id, exc)
            return None

    def _record(self, g: dict, v: Verdict, our_price, status: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO paper_signals(ts, leader, token_id, condition_id, outcome_index,
                 title, leader_price, leader_usdc, our_price, score, decision, status, reasons)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (self.now(), g["wallet"], g["token_id"], g["condition_id"], g["outcome_index"],
             g.get("title"), g["price"], g["usdc"], our_price, v.score, v.decision, status,
             json.dumps({"reasons": v.reasons, "red_flags": v.red_flags})))
        self.conn.commit()
        return cur.lastrowid

    def _set_status(self, sid: int, status: str, note: str | None = None) -> None:
        self.conn.execute("UPDATE paper_signals SET status=?, note=? WHERE id=?",
                          (status, note, sid))
        self.conn.commit()

    def _execute(self, sid: int) -> bool:
        s = self.conn.execute("SELECT * FROM paper_signals WHERE id=?", (sid,)).fetchone()
        stake = self.stake()
        if (why := self.risk_block(s["leader"], s["condition_id"], stake)):
            self._set_status(sid, "blocked", why)
            self.notify.send(f"Blocked copy of **{s['title']}**: {why}.")
            return False
        book = self._book(s["token_id"])
        fill = simulate_buy(book, stake, s["leader_price"] + self.sig_cfg.max_drift) if book else None
        if not fill or fill.shares <= 0:
            self._set_status(sid, "blocked", "no liquidity under price cap")
            return False
        self.conn.execute(
            """INSERT INTO paper_positions(signal_id, leader, token_id, condition_id, outcome_index,
                 title, shares, cost, opened_ts, status) VALUES (?,?,?,?,?,?,?,?,?, 'open')""",
            (sid, s["leader"], s["token_id"], s["condition_id"], s["outcome_index"],
             s["title"], fill.shares, fill.cost, self.now()))
        self._set_status(sid, "executed", f"{fill.shares:.2f} shares @ {fill.avg_price:.3f}")
        self.notify.send(f"Paper buy: **{s['title']}** ${fill.cost:.2f} @ {fill.avg_price:.3f} "
                         f"(score {s['score']:.0f}, copying {s['leader'][:10]}…)")
        return True

    def _approval_text(self, sid: int, g: dict, v: Verdict, our_price) -> str:
        lines = [f"Approval needed #{sid}: **{g.get('title') or g['token_id']}**",
                 f"Leader {g['wallet'][:10]}… bought ${g['usdc']:.0f} @ {g['price']:.3f}; "
                 f"we'd pay {our_price:.3f}. Score {v.score:.0f}."]
        lines += [f"• {r}" for r in v.reasons[:4]]
        lines += [f"⚠ {f}" for f in v.red_flags]
        lines.append(f"Reply `copybot paper approve {sid}` or `reject {sid}` "
                     f"within {self.cfg.approval_ttl // 60} min.")
        return "\n".join(lines)

    def approve(self, sid: int) -> str:
        s = self.conn.execute("SELECT * FROM paper_signals WHERE id=?", (sid,)).fetchone()
        if s is None or s["status"] != "pending":
            return f"signal {sid} is not pending"
        if self.now() - s["ts"] > self.cfg.approval_ttl:
            self._set_status(sid, "expired")
            return f"signal {sid} expired"
        return "executed" if self._execute(sid) else "blocked"

    def reject(self, sid: int) -> str:
        self._set_status(sid, "rejected")
        return "rejected"

    def expire_pending(self) -> None:
        self.conn.execute("UPDATE paper_signals SET status='expired' "
                          "WHERE status='pending' AND ts < ?",
                          (self.now() - self.cfg.approval_ttl,))
        self.conn.commit()

    # sells and settlement ----------------------------------------------------

    def on_leader_sell(self, g: dict) -> None:
        key = (g["wallet"], g["token_id"])
        held = self.leader_shares.get(key, 0.0)
        frac = 1.0 if held <= 0 else min(1.0, g["size"] / held)
        self.leader_shares[key] = max(0.0, held - g["size"])
        positions = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status='open' AND leader=? AND token_id=?",
            key).fetchall()
        if not positions:
            return
        book = self._book(g["token_id"])
        for p in positions:
            shares = p["shares"] * frac
            fill = simulate_sell(book, shares) if book else None
            if not fill or fill.shares <= 0:
                log.warning("could not mirror sell on %s: no bids", g["token_id"])
                continue
            closed = fill.shares >= p["shares"] - 1e-9
            if closed:
                self.conn.execute(
                    """UPDATE paper_positions SET shares=0, proceeds=proceeds+?,
                         status='closed', closed_ts=? WHERE id=?""",
                    (fill.cost, self.now(), p["id"]))
            else:  # keep the rest open; book the part sold as its own closed row
                cost_part = p["cost"] * fill.shares / p["shares"]
                self.conn.execute(
                    "UPDATE paper_positions SET shares=shares-?, cost=cost-? WHERE id=?",
                    (fill.shares, cost_part, p["id"]))
                self.conn.execute(
                    """INSERT INTO paper_positions(signal_id, leader, token_id, condition_id,
                         outcome_index, title, shares, cost, proceeds, opened_ts, closed_ts, status)
                       VALUES (?,?,?,?,?,?,0,?,?,?,?, 'closed')""",
                    (p["signal_id"], p["leader"], p["token_id"], p["condition_id"],
                     p["outcome_index"], p["title"], cost_part, fill.cost,
                     p["opened_ts"], self.now()))
            self.conn.commit()
            self.notify.send(f"Paper sell (mirroring leader): **{p['title']}** "
                             f"{fill.shares:.2f} shares @ {fill.avg_price:.3f}")

    def settle(self) -> int:
        open_ = self.conn.execute(
            "SELECT * FROM paper_positions WHERE status='open'").fetchall()
        conds = sorted({p["condition_id"] for p in open_})
        payouts = {}
        for i in range(0, len(conds), 20):
            try:
                for item in self.api.resolutions(conds[i:i + 20]):
                    cond, pay, _ = parse_resolution(item)
                    if cond and pay is not None:
                        payouts[cond] = pay
            except Exception as exc:
                log.warning("settle lookup failed: %s", exc)
        settled = 0
        for p in open_:
            pay = payouts.get(p["condition_id"])
            idx = p["outcome_index"]
            if pay is None or idx is None or idx >= len(pay):
                continue
            value = p["shares"] * pay[idx]
            self.conn.execute(
                "UPDATE paper_positions SET proceeds=proceeds+?, status='closed', closed_ts=? WHERE id=?",
                (value, self.now(), p["id"]))
            settled += 1
            self.notify.send(f"Settled **{p['title']}**: paid ${value:.2f} on ${p['cost']:.2f} "
                             f"({value - p['cost']:+.2f})")
        self.conn.commit()
        return settled

    # reporting -------------------------------------------------------------

    def status(self) -> dict:
        closed = self.conn.execute(
            "SELECT proceeds - cost AS pnl FROM paper_positions WHERE status='closed'").fetchall()
        counts = dict(self.conn.execute(
            "SELECT status, COUNT(*) FROM paper_signals GROUP BY status").fetchall())
        return {
            "mode": self.cfg.mode,
            "equity": round(self.equity(), 2),
            "realized_pnl": round(self.realized_pnl(), 2),
            "open_positions": self.conn.execute(
                "SELECT COUNT(*) FROM paper_positions WHERE status='open'").fetchone()[0],
            "open_exposure": round(self.exposure(), 2),
            "closed_trades": len(closed),
            "win_rate": round(sum(1 for r in closed if r[0] > 0) / len(closed), 3) if closed else None,
            "signals": counts,
            "paused": self.paused_reason(),
        }

    def run(self, stop: Callable[[], bool] = lambda: False) -> None:
        last_settle = 0
        self._set_meta("mode", self.cfg.mode)
        self._set_meta("bankroll", self.cfg.bankroll)
        self._set_meta("watching", json.dumps(sorted(self.leaders)))
        self._set_meta("started", self.now())
        self.notify.send(f"Paper trading started in {self.cfg.mode} mode, "
                         f"watching {len(self.leaders)} wallets.")
        while not stop():
            started = time.monotonic()
            self.poll_once()
            self._set_meta("last_poll", self.now())
            self.expire_pending()
            if self.now() - last_settle >= self.cfg.settle_every:
                self.settle()
                last_settle = self.now()
            time.sleep(max(0.0, self.cfg.poll_seconds - (time.monotonic() - started)))


def _group_orders(fills: list[dict]) -> list[dict]:
    """One leader order can produce several fills; copy it once."""
    groups: dict[tuple, dict] = {}
    for f in fills:
        key = (f["wallet"], f["tx_hash"], f["token_id"], f["side"])
        g = groups.setdefault(key, {**f, "size": 0.0, "usdc": 0.0})
        g["size"] += f["size"]
        g["usdc"] += f["usdc_size"]
    for g in groups.values():
        g["price"] = g["usdc"] / g["size"] if g["size"] else g["price"]
    return sorted(groups.values(), key=lambda g: g["ts"])
