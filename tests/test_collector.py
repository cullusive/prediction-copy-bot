from copybot.collector import normalize_fill, parse_resolution
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
