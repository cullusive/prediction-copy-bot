"""Local SQLite store. Everything the backtest needs lives in one file."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address        TEXT PRIMARY KEY,
    sources        TEXT NOT NULL DEFAULT '',   -- comma list: leaderboard, winners, holders, scan
    name           TEXT,
    first_seen     INTEGER NOT NULL,
    backfilled_to  INTEGER                     -- last fill timestamp fetched
);

CREATE TABLE IF NOT EXISTS fills (
    wallet         TEXT NOT NULL,
    tx_hash        TEXT NOT NULL,
    token_id       TEXT NOT NULL,
    condition_id   TEXT NOT NULL,
    outcome_index  INTEGER,
    side           TEXT NOT NULL,              -- BUY / SELL
    price          REAL NOT NULL,
    size           REAL NOT NULL,              -- shares
    usdc_size      REAL NOT NULL,
    ts             INTEGER NOT NULL,
    title          TEXT,
    slug           TEXT,
    PRIMARY KEY (wallet, tx_hash, token_id, side, size, price)
);
CREATE INDEX IF NOT EXISTS fills_wallet_ts ON fills(wallet, ts);
CREATE INDEX IF NOT EXISTS fills_condition ON fills(condition_id);

CREATE TABLE IF NOT EXISTS resolutions (
    condition_id   TEXT PRIMARY KEY,
    payouts        TEXT,                       -- JSON list, payout per outcome index (0..1); NULL = unresolved
    resolved_at    INTEGER,
    checked_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS prices (
    token_id       TEXT NOT NULL,
    ts             INTEGER NOT NULL,
    price          REAL NOT NULL,
    PRIMARY KEY (token_id, ts)
);

CREATE TABLE IF NOT EXISTS wallet_scores (
    wallet         TEXT PRIMARY KEY,
    computed_at    INTEGER NOT NULL,
    score          REAL NOT NULL,
    eligible       INTEGER NOT NULL,
    metrics        TEXT NOT NULL               -- JSON
);
"""


class Store:
    def __init__(self, path: str | Path = "copybot.db"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # wallets -------------------------------------------------------------

    def add_wallet(self, address: str, source: str, now: int,
                   name: str | None = None) -> None:
        address = address.lower()
        row = self.conn.execute("SELECT sources FROM wallets WHERE address=?",
                                (address,)).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO wallets(address, sources, name, first_seen) VALUES (?,?,?,?)",
                (address, source, name, now))
        else:
            sources = set(filter(None, row["sources"].split(",")))
            sources.add(source)
            self.conn.execute(
                "UPDATE wallets SET sources=?, name=COALESCE(name, ?) WHERE address=?",
                (",".join(sorted(sources)), name, address))
        self.conn.commit()

    def wallets(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM wallets ORDER BY address").fetchall()

    def set_backfilled(self, address: str, ts: int) -> None:
        self.conn.execute("UPDATE wallets SET backfilled_to=? WHERE address=?",
                          (ts, address.lower()))
        self.conn.commit()

    # fills ---------------------------------------------------------------

    def add_fills(self, rows: Iterable[dict]) -> int:
        cur = self.conn.executemany(
            """INSERT OR IGNORE INTO fills
               (wallet, tx_hash, token_id, condition_id, outcome_index, side,
                price, size, usdc_size, ts, title, slug)
               VALUES (:wallet, :tx_hash, :token_id, :condition_id, :outcome_index,
                       :side, :price, :size, :usdc_size, :ts, :title, :slug)""",
            list(rows))
        self.conn.commit()
        return cur.rowcount

    def fills_for(self, wallet: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM fills WHERE wallet=? ORDER BY ts, rowid",
            (wallet.lower(),)).fetchall()

    def condition_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT condition_id FROM fills")]

    def token_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT token_id FROM fills")]

    # resolutions ---------------------------------------------------------

    def set_resolution(self, condition_id: str, payouts: list[float] | None,
                       resolved_at: int | None, now: int) -> None:
        self.conn.execute(
            """INSERT INTO resolutions(condition_id, payouts, resolved_at, checked_at)
               VALUES (?,?,?,?)
               ON CONFLICT(condition_id) DO UPDATE SET
                 payouts=excluded.payouts, resolved_at=excluded.resolved_at,
                 checked_at=excluded.checked_at""",
            (condition_id, json.dumps(payouts) if payouts is not None else None,
             resolved_at, now))
        self.conn.commit()

    def resolutions(self) -> dict[str, tuple[list[float], int | None]]:
        out = {}
        for r in self.conn.execute(
                "SELECT * FROM resolutions WHERE payouts IS NOT NULL"):
            out[r["condition_id"]] = (json.loads(r["payouts"]), r["resolved_at"])
        return out

    def unresolved_condition_ids(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            """SELECT DISTINCT f.condition_id FROM fills f
               LEFT JOIN resolutions r ON r.condition_id = f.condition_id
               WHERE r.payouts IS NULL""")]

    # prices --------------------------------------------------------------

    def add_prices(self, token_id: str, points: Iterable[tuple[int, float]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO prices(token_id, ts, price) VALUES (?,?,?)",
            [(token_id, ts, p) for ts, p in points])
        self.conn.commit()

    def prices_for(self, token_id: str) -> list[tuple[int, float]]:
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT ts, price FROM prices WHERE token_id=? ORDER BY ts", (token_id,))]

    def has_prices(self, token_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM prices WHERE token_id=? LIMIT 1",
                                 (token_id,)).fetchone() is not None

    # scores --------------------------------------------------------------

    def save_score(self, wallet: str, score: float, eligible: bool,
                   metrics: dict, now: int) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO wallet_scores(wallet, computed_at, score, eligible, metrics)
               VALUES (?,?,?,?,?)""",
            (wallet, now, score, int(eligible), json.dumps(metrics)))
        self.conn.commit()

    def top_scores(self, limit: int = 50, eligible_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM wallet_scores"
        if eligible_only:
            q += " WHERE eligible=1"
        q += " ORDER BY score DESC LIMIT ?"
        return self.conn.execute(q, (limit,)).fetchall()
