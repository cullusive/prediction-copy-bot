from copybot.clob import Book, parse_book, simulate_buy, simulate_sell
from copybot.db import Store
from copybot.paper import Leader, PaperConfig, PaperEngine
from copybot.signals import SignalConfig, SignalContext, score_signal

W = "0x" + "a" * 40
W2 = "0x" + "b" * 40


def ctx(**kw):
    base = dict(leader=W, leader_price=0.40, leader_usdc=500, leader_median_usdc=250,
                leader_copy_roi=0.25, leader_markets=100, leader_flags=[],
                our_price=0.41, spread=0.01, fill_ratio=1.0)
    base.update(kw)
    return SignalContext(**base)


def test_strong_trade_is_auto():
    v = score_signal(ctx(same_side_leaders=1))
    assert v.decision == "auto", v


def test_price_moved_too_far_is_skipped():
    v = score_signal(ctx(our_price=0.50))
    assert v.decision == "skip" and "moved" in v.reasons[0]


def test_red_flag_never_auto():
    v = score_signal(ctx(same_side_leaders=2, leader_flags=["leader not fully proven"]))
    assert v.decision == "flag"


def test_opposition_lowers_score():
    assert score_signal(ctx(opposite_side_leaders=1)).score < score_signal(ctx()).score


def test_weak_leader_is_skipped():
    v = score_signal(ctx(leader_copy_roi=0.0, leader_markets=5, leader_usdc=100,
                         leader_median_usdc=400, our_price=0.43, spread=0.05, fill_ratio=0.3))
    assert v.decision == "skip"


def test_book_parse_and_fills():
    b = parse_book("t", {"bids": [{"price": "0.38", "size": "50"}, {"price": "0.39", "size": "10"}],
                         "asks": [{"price": "0.42", "size": "10"}, {"price": "0.41", "size": "20"}]})
    assert b.best_bid == 0.39 and b.best_ask == 0.41
    f = simulate_buy(b, dollars=10, max_price=0.45)
    assert abs(f.cost - 10) < 1e-9 and f.avg_price > 0.41
    capped = simulate_buy(b, dollars=100, max_price=0.41)
    assert abs(capped.shares - 20) < 1e-9          # never pays above the cap
    s = simulate_sell(b, 30)
    assert abs(s.shares - 30) < 1e-9 and abs(s.cost - (10 * 0.39 + 20 * 0.38)) < 1e-9


class FakeApi:
    def __init__(self):
        self.trades = {}
        self.res = []

    def wallet_trades(self, wallet, start=None, max_items=None):
        return [t for t in self.trades.get(wallet, []) if t["timestamp"] >= (start or 0)]

    def resolutions(self, conds):
        return [r for r in self.res if r["condition_id"] in conds]


def trade(wallet, ts, side, price, size, tx, token="tok", cond="0x" + "c" * 64, outcome=0):
    return {"proxy_wallet": wallet, "timestamp": ts, "condition_id": cond, "type": "TRADE",
            "size": size, "usdc_size": price * size, "transaction_hash": tx, "price": price,
            "token_id": token, "side": side, "outcome_index": outcome, "title": "Will X?"}


def engine(tmp_path, mode="auto", **cfg):
    clock = {"t": 1_000_000}
    api = FakeApi()
    book = Book("tok", bids=[(0.44, 1000)], asks=[(0.41, 1000)])
    leaders = {W: Leader(W, copy_roi=0.25, n_markets=100, median_usdc=200),
               W2: Leader(W2, copy_roi=0.25, n_markets=100, median_usdc=200)}
    sent = []

    class N:
        def send(self, text):
            sent.append(text)

    e = PaperEngine(Store(tmp_path / "p.db"), api, lambda t: book, leaders,
                    PaperConfig(mode=mode, **cfg), SignalConfig(), N(), lambda: clock["t"])
    e.poll_once()                      # first poll only sets the starting point
    return e, api, clock, sent


def test_end_to_end_copy_mirror_and_settle(tmp_path):
    e, api, clock, sent = engine(tmp_path)
    t0 = clock["t"]
    # two fills of one leader order, plus a second leader agreeing
    api.trades[W2] = [trade(W2, t0 + 1, "BUY", 0.40, 600, "0x2")]
    api.trades[W] = [trade(W, t0 + 2, "BUY", 0.40, 600, "0x1"),
                     trade(W, t0 + 2, "BUY", 0.40, 400, "0x1")]
    clock["t"] = t0 + 10
    e.poll_once()
    pos = e.conn.execute("SELECT * FROM paper_positions WHERE status='open'").fetchall()
    assert len(pos) == 2                           # both leaders copied once each
    assert all(abs(p["cost"] - 20) < 1e-6 for p in pos)   # 2% of $1000

    # leader W sells half: we sell half of our W position at the bid
    api.trades[W].append(trade(W, t0 + 20, "SELL", 0.44, 500, "0x3"))
    clock["t"] = t0 + 30
    e.poll_once()
    assert e.realized_pnl() > 0

    # market resolves YES: everything left settles at $1
    api.res = [{"condition_id": "0x" + "c" * 64, "payouts": [1, 0], "status": "resolved"}]
    assert e.settle() == 2
    st = e.status()
    assert st["open_positions"] == 0 and st["realized_pnl"] > 0 and st["win_rate"] == 1.0


def test_approval_mode_waits_and_expires(tmp_path):
    e, api, clock, sent = engine(tmp_path, mode="approval")
    t0 = clock["t"]
    api.trades[W] = [trade(W, t0 + 1, "BUY", 0.40, 1000, "0x1")]
    clock["t"] = t0 + 5
    e.poll_once()
    sid = e.conn.execute("SELECT id FROM paper_signals").fetchone()[0]
    assert "Approval needed" in sent[-1]
    assert e.approve(sid) == "executed"
    api.trades[W].append(trade(W, t0 + 10, "BUY", 0.40, 1000, "0x9", token="tok"))
    clock["t"] = t0 + 12
    e.poll_once()
    sid2 = e.conn.execute("SELECT MAX(id) FROM paper_signals").fetchone()[0]
    clock["t"] = t0 + 12 + 301
    assert e.approve(sid2) == "signal %d expired" % sid2


def test_risk_caps_block(tmp_path):
    e, api, clock, sent = engine(tmp_path, per_market_pct=0.03)
    t0 = clock["t"]
    api.trades[W] = [trade(W, t0 + 1, "BUY", 0.40, 1000, "0x1"),
                     trade(W, t0 + 2, "BUY", 0.40, 1000, "0x2")]
    clock["t"] = t0 + 5
    e.poll_once()
    statuses = [r[0] for r in e.conn.execute("SELECT status FROM paper_signals ORDER BY id")]
    assert statuses == ["executed", "blocked"]


def test_daily_loss_stop_pauses(tmp_path):
    e, api, clock, sent = engine(tmp_path)
    e.conn.execute("""INSERT INTO paper_positions(signal_id, leader, token_id, condition_id,
        shares, cost, proceeds, opened_ts, closed_ts, status)
        VALUES (0, ?, 'x', 'y', 0, 60, 0, ?, ?, 'closed')""", (W, clock["t"], clock["t"]))
    assert "daily loss stop" in e.paused_reason()
