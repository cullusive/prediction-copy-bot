from copybot.collector import (_price_point, _wallet_of, iter_holders, normalize_fill,
                               parse_resolution, price_windows)
from copybot.db import Store


def test_normalize_activity_item():
    item = {"proxy_wallet": "0xABC", "timestamp": 1700000000, "condition_id": "0xc",
            "type": "TRADE", "size": 10, "usdc_size": 4.2, "transaction_hash": "0xh",
            "price": 0.42, "token_id": "123", "side": "BUY", "outcome_index": 1,
            "title": "Will X?", "slug": "will-x"}
    row = normalize_fill(item)
    assert row["wallet"] == "0xabc" and row["side"] == "BUY" and row["usdc_size"] == 4.2


def test_normalize_rejects_bad_rows():
    assert normalize_fill({"side": "BUY"}) is None


def test_parse_resolution_shapes():
    assert parse_resolution({"condition_id": "c", "payouts": [0, 1], "status": "resolved"})[1] == [0.0, 1.0]
    assert parse_resolution({"condition_id": "c", "winning_outcome_index": 0})[1] == [1.0, 0.0]
    assert parse_resolution({"condition_id": "c", "payouts": [1, 0], "status": "proposed"})[1] is None


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_wallet("0xA", "leaderboard", 1)
    s.add_wallet("0xa", "scan", 2)
    assert s.wallets()[0]["sources"] == "leaderboard,scan"
    row = normalize_fill({"proxy_wallet": "0xa", "timestamp": 5, "condition_id": "c",
                          "size": 1, "price": 0.5, "token_id": "t", "side": "BUY",
                          "transaction_hash": "h", "outcome_index": 0})
    assert s.add_fills([row, row]) == 1
    s.set_resolution("c", [1.0, 0.0], 9, 10)
    assert s.resolutions() == {"c": ([1.0, 0.0], 9)}
    assert s.unresolved_condition_ids() == []


def test_parse_resolution_live_uma_shape():
    # shapes seen on data-api.polymarket.com/v2/resolutions, 2026-10-04
    base = {"condition_id": "0xc", "status": "resolved", "last_update_timestamp": "1789594780"}
    assert parse_resolution({**base, "price": "1000000000000000000"}) == ("0xc", [1.0, 0.0], 1789594780)
    assert parse_resolution({**base, "price": "0"})[1] == [0.0, 1.0]
    assert parse_resolution({**base, "price": "500000000000000000"})[1] == [0.5, 0.5]
    assert parse_resolution({**base, "status": "posed", "price": "69"})[1] is None
    assert parse_resolution({**base, "price": "69"})[1] is None


def test_iter_holders_flattens_token_groups():
    groups = [{"token_id": "1", "holders": [{"proxy_wallet": "0xa"}, {"proxy_wallet": "0xb"}]},
              {"token_id": "2", "holders": [{"proxy_wallet": "0xc"}]}]
    assert [_wallet_of(h) for h in iter_holders(groups)] == ["0xa", "0xb", "0xc"]


def test_price_windows_merge_and_cap():
    assert price_windows([100], pad=10) == [(100, 110)]
    # close fills share a window; a far one gets its own
    assert price_windows([100, 150, 10_000_000], pad=10, max_gap=100) == [(100, 160), (10_000_000, 10_000_010)]
    # windows never exceed the API's 15-day span
    day = 86400
    ws = price_windows(range(0, 40 * day, day // 2), pad=1800)
    assert all(b - a <= 15 * day for a, b in ws) and len(ws) == 3


def test_price_point_live_shape():
    assert _price_point({"timestamp": 1791045480, "price": 0.485, "resolution_seconds": 60}) == (1791045480, 0.485)


def test_wallets_limit_prefers_multi_source(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_wallet("0x1", "scan", 1)
    s.add_wallet("0x2", "leaderboard", 1)
    s.add_wallet("0x2", "scan", 1)
    s.add_wallet("0x3", "winners", 1)
    assert [w["address"] for w in s.wallets(2)] == ["0x2", "0x3"]
    assert len(s.wallets()) == 3


def test_buckets_for_age():
    from copybot.collector import buckets_for
    day = 86400
    assert buckets_for(100 * day, 101 * day) == [60, 300, 1800]
    assert buckets_for(100 * day, 130 * day) == [300, 1800]
    assert buckets_for(0, 200 * day) == [1800]


def test_parse_resolution_payouts_shape_with_iso_time():
    # second live shape (older markets): raw payout numerators and ISO times
    item = {"condition_id": "0xc", "status": "resolved", "payouts": [1000000, 0],
            "last_update_timestamp": "2026-07-13T19:43:11Z",
            "resolved_at": "2026-07-13T19:43:11Z"}
    assert parse_resolution(item) == ("0xc", [1.0, 0.0], 1783971791)


def test_combo_condition_ids_are_not_standard():
    from copybot.collector import is_standard_condition
    assert is_standard_condition("0x876506d8b2bd7a0d3fa4fe18c024eee6e1dd81ee24c26795dadd6cfe4a7b5d0d")
    assert not is_standard_condition("0x0300001d85ab16a84211d5157f1ad293110000000000000000000000000000")
