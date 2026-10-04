import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from copybot.dashboard import make_handler, snapshot
from copybot.db import Store
from copybot.paper import PaperConfig, PaperEngine

from test_paper import W, engine, trade


def seeded(tmp_path):
    e, api, clock, sent = engine(tmp_path, mode="approval")
    e._set_meta("watching", json.dumps([W]))
    e._set_meta("started", clock["t"])
    t0 = clock["t"]
    api.trades[W] = [trade(W, t0 + 1, "BUY", 0.40, 1000, "0x1")]
    clock["t"] = t0 + 5
    e.poll_once()
    return e, clock


def test_snapshot_shows_pending_and_leaders(tmp_path):
    e, clock = seeded(tmp_path)
    snap = snapshot(e.store, now=clock["t"])
    assert len(snap["pending"]) == 1 and snap["pending"][0]["expires_in"] == 300
    assert snap["leaders"][0]["wallet"] == W
    assert snap["status"]["equity"] == 1000.0
    json.dumps(snap)  # must be serializable


def test_server_routes_and_csrf_guard(tmp_path):
    e, clock = seeded(tmp_path)
    db = str(tmp_path / "p.db")

    def factory(st):
        return PaperEngine(st, None, lambda t: e.book_fn(t), {}, PaperConfig(mode="approval"),
                           now_fn=lambda: clock["t"])

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db, factory))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Copy Bot" in page
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        sid = state["signals"][0]["id"]

        try:  # no custom header: refused
            urllib.request.urlopen(urllib.request.Request(base + f"/api/approve/{sid}", method="POST"))
            assert False
        except urllib.error.HTTPError as err:
            assert err.code == 403

        req = urllib.request.Request(base + f"/api/approve/{sid}", method="POST",
                                     headers={"X-Copybot": "1"})
        assert json.loads(urllib.request.urlopen(req).read())["result"] == "executed"
        req = urllib.request.Request(base + "/api/pause", method="POST", headers={"X-Copybot": "1"})
        urllib.request.urlopen(req)
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["status"]["open_positions"] == 1
        assert state["status"]["paused"] == "paused by you"
    finally:
        srv.shutdown()
