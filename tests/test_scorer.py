from copybot.scorer import (ScoringConfig, build_lots, copy_return,
                            make_price_lookup, score_wallet, walk_forward)

DAY = 86400


def fill(ts, side, price, size, token="t1", cond="c1", outcome=0):
    return {"ts": ts, "side": side, "price": price, "size": size,
            "token_id": token, "condition_id": cond, "outcome_index": outcome}


def test_fifo_split_and_resolution():
    fills = [fill(0, "BUY", 0.40, 100), fill(10, "BUY", 0.50, 100),
             fill(20, "SELL", 0.60, 150)]
    lots = build_lots(fills, {"c1": ([1.0, 0.0], 100)})
    sold = [l for l in lots if l.exit_kind == "sell"]
    held = [l for l in lots if l.exit_kind == "resolution"]
    assert [(l.entry_price, l.size) for l in sold] == [(0.40, 100), (0.50, 50)]
    assert len(held) == 1 and held[0].size == 50 and held[0].exit_price == 1.0
    assert abs(sum(l.pnl for l in lots) - (20 + 5 + 25)) < 1e-9


def test_unresolved_lots_stay_open():
    lots = build_lots([fill(0, "BUY", 0.3, 10)], {})
    assert lots[0].exit_price is None


def test_price_lookup_is_conservative():
    look = make_price_lookup({"t1": [(0, 0.4), (60, 0.5), (120, 0.6)]}, max_lag=100)
    assert look("t1", 1) == 0.5          # next point at/after, never before
    assert look("t1", 60) == 0.5
    assert look("t1", 500) is None       # too stale
    assert look("nope", 0) is None


def test_copy_return_pays_for_delay():
    cfg = ScoringConfig(slippage=0.01)
    lots = build_lots([fill(0, "BUY", 0.40, 100)], {"c1": ([1.0, 0.0], 100)})
    look = make_price_lookup({"t1": [(0, 0.40), (60, 0.59)]})
    r = copy_return(lots[0], 60, look, cfg)
    assert abs(r - (1.0 - 0.60) / 0.60) < 1e-9


def _profitable_wallet(n_markets, start=0, edge_survives=True):
    fills, res, series = [], {}, {}
    for i in range(n_markets):
        t0 = start + i * DAY
        tok, cond = f"t{i}", f"c{i}"
        fills.append(fill(t0, "BUY", 0.40, 100, tok, cond))
        res[cond] = ([1.0, 0.0] if i % 3 else [0.0, 1.0], t0 + DAY // 2)
        later = 0.45 if edge_survives else 0.95
        series[tok] = [(t0, 0.40), (t0 + 60, later), (t0 + 300, later)]
    return fills, res, series


def test_good_wallet_is_eligible():
    fills, res, series = _profitable_wallet(40)
    now = 40 * DAY
    m = score_wallet("0xgood", fills, res, make_price_lookup(series), now)
    assert m.n_markets == 40
    assert m.copy_roi[60] > 0
    assert m.eligible, m.reasons


def test_edge_that_vanishes_is_rejected():
    fills, res, series = _profitable_wallet(40, edge_survives=False)
    m = score_wallet("0xfast", fills, res, make_price_lookup(series), 40 * DAY)
    assert m.roi > 0                      # the wallet itself made money...
    assert not m.eligible                 # ...but we couldn't have copied it
    assert "copy edge gone after delay" in m.reasons


def test_one_lucky_bet_is_rejected():
    fills, res, series = [], {}, {}
    for i in range(35):
        tok, cond = f"t{i}", f"c{i}"
        fills.append(fill(i * DAY, "BUY", 0.5, 10 if i else 10_000, tok, cond))
        res[cond] = ([1.0, 0.0] if i == 0 or i % 2 else [0.0, 1.0], i * DAY + 10)
        series[tok] = [(i * DAY, 0.5), (i * DAY + 60, 0.5)]
    m = score_wallet("0xlucky", fills, res, make_price_lookup(series), 35 * DAY)
    assert any("one market" in r for r in m.reasons)


def test_walk_forward_uses_only_past_data():
    good, res_g, ser_g = _profitable_wallet(60)
    bad, res_b, ser_b = _profitable_wallet(60, edge_survives=False)
    bad = [dict(f, token_id="b" + f["token_id"], condition_id="b" + f["condition_id"]) for f in bad]
    res_b = {"b" + k: v for k, v in res_b.items()}
    ser_b = {"b" + k: v for k, v in ser_b.items()}
    look = make_price_lookup({**ser_g, **ser_b})
    out = walk_forward({"0xgood": good, "0xbad": bad}, {**res_g, **res_b}, look,
                       split_ts=45 * DAY, top_k=5)
    assert out.wallets == ["0xgood"]
    assert out.n_lots == 15
    assert out.copy_roi > 0
