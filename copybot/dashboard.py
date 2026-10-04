"""Local live dashboard: `python -m copybot dashboard`, then open http://localhost:8765.

It reads the same database the paper trader writes, refreshes every few
seconds, and has buttons to approve or reject flagged trades and to pause
or resume trading. It only listens on this computer (127.0.0.1).
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .db import Store
from .paper import SCHEMA, PaperConfig, PaperEngine

PAGE = Path(__file__).with_name("dashboard.html")


def _meta(conn, key, default=None):
    row = conn.execute("SELECT value FROM paper_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def snapshot(store: Store, now: int | None = None) -> dict:
    """Everything the page shows, in one JSON document."""
    conn = store.conn
    conn.executescript(SCHEMA)
    now = now or int(time.time())
    bankroll = float(_meta(conn, "bankroll", 1000))
    cfg = PaperConfig(mode=_meta(conn, "mode", "auto"), bankroll=bankroll)
    engine = PaperEngine(store, None, lambda t: None, {}, cfg, now_fn=lambda: now)

    equity, total = [], bankroll
    started = _meta(conn, "started")
    if started:
        equity.append({"ts": int(started), "equity": bankroll})
    for r in conn.execute("""SELECT closed_ts, proceeds - cost AS pnl, title FROM paper_positions
                             WHERE status='closed' ORDER BY closed_ts, id"""):
        total += r["pnl"]
        equity.append({"ts": r["closed_ts"], "equity": round(total, 2),
                       "pnl": round(r["pnl"], 2), "title": r["title"]})

    def rows(q, args=()):
        return [dict(r) for r in conn.execute(q, args)]

    signals = rows("""SELECT id, ts, leader, title, leader_price, leader_usdc, our_price, score,
                             decision, status, reasons, note
                      FROM paper_signals ORDER BY id DESC LIMIT 100""")
    for s in signals:
        s.update(json.loads(s.pop("reasons")))
    ttl = cfg.approval_ttl
    pending = [dict(s, expires_in=max(0, ttl - (now - s["ts"])))
               for s in signals if s["status"] == "pending" and now - s["ts"] <= ttl]

    scores = {r["wallet"]: json.loads(r["metrics"]) for r in conn.execute(
        "SELECT wallet, metrics FROM wallet_scores")}
    names = {r["address"]: r["name"] for r in conn.execute("SELECT address, name FROM wallets")}
    copied = {r["leader"]: r for r in rows(
        """SELECT leader, COUNT(*) AS trades, ROUND(SUM(proceeds - cost), 2) AS pnl
           FROM paper_positions WHERE status='closed' GROUP BY leader""")}
    leaders = []
    for w in json.loads(_meta(conn, "watching", "[]")):
        m = scores.get(w, {})
        last = _meta(conn, f"last_ts:{w}")
        leaders.append({
            "wallet": w, "name": names.get(w),
            "eligible": bool(m.get("eligible")),
            "copy_roi_60s": (m.get("copy_roi") or {}).get("60"),
            "markets": m.get("n_markets"),
            "last_trade": int(last) if last else None,
            "our_trades": (copied.get(w) or {}).get("trades", 0),
            "our_pnl": (copied.get(w) or {}).get("pnl", 0.0),
        })

    collect = conn.execute("SELECT MAX(ts) FROM fills").fetchone()[0]
    last_poll = _meta(conn, "last_poll")
    return {
        "now": now,
        "status": engine.status(),
        "bankroll": bankroll,
        "running": bool(last_poll) and now - int(last_poll) < 120,
        "last_poll": int(last_poll) if last_poll else None,
        "latest_fill": collect,
        "equity": equity,
        "open_positions": rows("""SELECT id, leader, title, shares, cost, opened_ts,
                                         cost / shares AS entry FROM paper_positions
                                  WHERE status='open' ORDER BY opened_ts DESC"""),
        "closed_positions": rows("""SELECT id, leader, title, cost, proceeds,
                                           proceeds - cost AS pnl, closed_ts FROM paper_positions
                                    WHERE status='closed' ORDER BY closed_ts DESC LIMIT 50"""),
        "pending": pending,
        "signals": signals,
        "leaders": leaders,
    }


def make_handler(db_path: str, engine_factory: Callable[[Store], PaperEngine]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                store = Store(db_path)
                try:
                    self._json(snapshot(store))
                finally:
                    store.close()
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            # A custom header can't be sent cross-site without a CORS preflight,
            # which we never allow, so other web pages can't press these buttons.
            if self.headers.get("X-Copybot") != "1":
                return self._json({"error": "forbidden"}, 403)
            parts = self.path.strip("/").split("/")
            store = Store(db_path)
            try:
                engine = engine_factory(store)
                if parts[:2] == ["api", "approve"] and len(parts) == 3:
                    result = engine.approve(int(parts[2]))
                elif parts[:2] == ["api", "reject"] and len(parts) == 3:
                    result = engine.reject(int(parts[2]))
                elif parts == ["api", "pause"]:
                    engine._set_meta("paused", "1"); result = "paused"
                elif parts == ["api", "resume"]:
                    engine._set_meta("paused", "0"); result = "resumed"
                else:
                    return self._json({"error": "unknown action"}, 404)
                self._json({"result": result})
            except ValueError:
                self._json({"error": "bad id"}, 400)
            finally:
                store.close()

    return Handler


def serve(db_path: str, engine_factory: Callable[[Store], PaperEngine],
          port: int = 8765) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path, engine_factory))
    print(f"dashboard on http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
