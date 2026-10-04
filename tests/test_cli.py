from copybot.cli import drop_wallets
from copybot.db import Store


def test_drop_wallets_by_address_or_name(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    store.add_wallet("0xAAA", "leaderboard", 0, name="BrotherObama")
    store.add_wallet("0xbbb", "leaderboard", 0, name="Seeking-Alpha01")
    wallets = ["0xAAA", "0xbbb", "0xccc"]
    assert drop_wallets(store, wallets, []) == wallets
    assert drop_wallets(store, wallets, ["brotherobama"]) == ["0xbbb", "0xccc"]
    assert drop_wallets(store, wallets, ["0xaaa", "0xCCC"]) == ["0xbbb"]
