"""RGY-060: the wallets of one trading bot (every sub-account of `sendler-b.near`) carry a derived label, the parent is a service account, the developer label type exists."""
import json

import pytest

import rugyomi_testkit  # noqa: F401  (puts the package on sys.path)
from rugyomi import buyers as B
from rugyomi import labels as L
from rugyomi import links as K
from rugyomi import snapshots as S
from rugyomi.parse import Fact
from rugyomi.rules import load_config
from rugyomi.store import open_store

PARENT = "sendler-b.near"
NEAR = 10**24
T0 = 1_793_300_000 * 10**9
MIN = 60 * 10**9


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "b.db")
    yield s
    s.close()


@pytest.mark.core
def test_every_sub_account_at_any_depth_gets_the_label_and_nothing_else_does():
    for acc in ("x.sendler-b.near", "a.b.sendler-b.near", "9f3c.sendler-b.near"):
        (lab,) = L.derived_labels(acc)
        assert lab["label"] == "bot_wallet" and lab["text"] == "trading bot wallet" and lab["source"] == "derived" and lab["id"] is None
        assert lab["reason"] == "sub-account of sendler-b.near (@sendler_trade_bot)" and lab["account"] == acc
    for acc in ("sendler-b.near",                      # the parent itself is a service account, not a "wallet"
                "xsendler-b.near",                     # a look-alike that is not a sub-account
                "sendler-b.near.evil.near", "sendler-b.nearx", "sendler-b.near.", ".sendler-b.near",
                "sendler-b.neat", "sendler-bnear", "sendler.near", "evil.near", "", None,
                "x.sendler-b.near.evil.near", "X.SENDLER-B.NEAR"):
        assert L.derived_labels(acc) == [], acc


@pytest.mark.core
def test_the_list_is_one_explicit_file_with_the_wording_in_one_place():
    d = L.bot_wallets()
    assert [b["parent"] for b in d["bots"]] == [PARENT] and d["bots"][0]["bot"] == "@sendler_trade_bot"
    raw = json.loads(L.BOT_WALLETS_PATH.read_text(encoding="utf-8"))
    assert set(raw["bots"][0]) == {"parent", "label", "text", "bot"}


@pytest.mark.core
def test_active_labels_gives_manual_rows_then_the_derived_one_with_text(store):
    acc = "x.sendler-b.near"
    store.begin()
    store.con.execute("INSERT INTO manual_labels(account,label,reason,source,set_by,set_at) VALUES(?,?,?,?,?,?)",
                      (acc, "independent_sniper", "manual: test", "manual", "cli", "2026-11-13T00:00:00Z"))
    store.commit()
    labs = L.active_labels(store, acc)
    assert [x["label"] for x in labs] == ["independent_sniper", "bot_wallet"]            # a manual label on the same account still shows
    assert [x["text"] for x in labs] == ["independent sniper", "trading bot wallet"]
    assert L.active_labels(store, "plain.near") == []
    assert L.has_label(store, acc, "bot_wallet") and L.has_label(store, acc, "independent_sniper") and not L.has_label(store, "plain.near", "bot_wallet")
    assert L.all_labels(store)[0]["text"] == "independent sniper" and len(L.all_labels(store)) == 1      # the derived label is not a row


@pytest.mark.core
def test_the_developer_label_type_exists_and_is_a_manual_row(store):
    assert "bot_developer" in L.LABELS
    dev = "ab" * 32                     # a made-up 64-hex account
    store.begin()
    store.con.execute("INSERT INTO manual_labels(account,label,reason,source,set_by,set_at) VALUES(?,?,?,?,?,?)",
                      (dev, "bot_developer", "developer of the trading bot (test reason)", "manual", "cli", "2026-11-13T00:00:00Z"))
    store.commit()
    (lab,) = L.active_labels(store, dev)
    assert lab["label"] == "bot_developer" and lab["text"] == "trading bot developer" and lab["source"] == "manual"


@pytest.mark.core
def test_buyer_rows_and_group_members_show_the_derived_label(store):
    store.begin()
    token = "t.nearlytrade.near"
    store.con.execute("INSERT INTO launches(token, launchpad, creator, owner, height, ts_ns, detected_at) VALUES(?,?,?,?,?,?,?)",
                      (token, "nearly", "c.near", "c.near", 1, T0, "2026-11-13T00:00:00Z"))
    for rank, b in enumerate(("w1.sendler-b.near", "human.near"), 1):
        store.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (token, b, rank, T0 + rank * MIN, f"tx{rank}", 1, "1", "1", "0", "0", "0"))
    store.con.execute("INSERT INTO buyer_groups(type, group_key, size, launches) VALUES('trader_group', 'g', 2, 1)")
    for b in ("w1.sendler-b.near", "human.near"):
        store.con.execute("INSERT INTO buyer_group_members(type, group_key, account, launches) VALUES('trader_group', 'g', ?, 1)", (b,))
    store.commit()
    rows = {r["buyer"]: r for r in B.early_buyers(store, token)}
    assert [x["label"] for x in rows["w1.sendler-b.near"]["labels"]] == ["bot_wallet"] and rows["human.near"]["labels"] == []
    g = B.group_page(store, "trader_group", "g")
    labs = {m["account"]: [x["label"] for x in m["labels"]] for m in g["members"]}
    assert labs == {"w1.sendler-b.near": ["bot_wallet"], "human.near": []}


@pytest.mark.core
def test_known_account_for_a_bot_wallet_with_no_other_trace(store):
    from rugyomi.web import queries
    assert queries.known_account(store.con, "w9.sendler-b.near") and not queries.known_account(store.con, "w9.other.near")


# ------------------------------------------------------------------------------------------ the parent is a service account
@pytest.mark.core
def test_the_parent_is_a_service_account_and_its_sub_accounts_are_not():
    assert K.service(PARENT) and PARENT in K.SERVICE_ACCOUNTS
    assert not K.service("x.sendler-b.near") and not K.service("xsendler-b.near") and not K.service("sendler-b.near.evil.near")
    assert not S.is_service(PARENT, "t")                      # the swap attribution list (snapshots.SERVICE) is a different list and is not changed here


class World:
    def __init__(self, path):
        self.s = open_store(path)
        self.n = 0

    def launch(self, token, creator, at):
        self.s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, extra, coverage, pool) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (token, "nearly", creator, creator, "0", at // 10**9 % 10**9, at, "2026-11-13T00:00:00.000000Z", None, None, None))

    def fund(self, src, dst, near, at):
        self.n += 1
        self.s.insert_facts([Fact("near_transfer", at // 10**9 % 10**9, at, rugyomi_testkit.hid("tx", self.n), rugyomi_testkit.hid("r", self.n), "a", src, dst, None, str(int(near * NEAR)), None)])


def scenario(tmp_path, funder):
    w = World(tmp_path / f"{funder}.db")
    w.launch("f.nearlytrade.near", funder, T0)                       # the funder launched a token itself: it is a participant (a creator)
    w.launch("a.nearlytrade.near", "a.near", T0 + 20 * MIN)
    w.launch("b.nearlytrade.near", "b.near", T0 + 30 * MIN)
    w.fund(funder, "a.near", 12, T0 + 10 * MIN)
    w.fund(funder, "b.near", 12, T0 + 15 * MIN)
    meta = K.rebuild_links(w.s, load_config())
    groups = w.s.con.execute("SELECT group_id, size FROM link_groups").fetchall()
    edges = w.s.con.execute("SELECT src, dst FROM link_edges").fetchall()
    w.s.close()
    return meta, groups, edges


@pytest.mark.core
def test_two_accounts_funded_only_by_the_bot_parent_are_not_linked_but_a_normal_funder_still_links(tmp_path):
    _, groups, edges = scenario(tmp_path, PARENT)
    assert groups == [] and edges == []                              # a.near and b.near share only the bot's parent: no network
    _, groups2, edges2 = scenario(tmp_path, "friend.near")           # the same shape with an ordinary funder: one group of three
    assert [g[1] for g in groups2] == [3] and len(edges2) == 2


@pytest.mark.core
def test_a_sub_account_of_the_parent_is_still_a_participant_and_its_own_transfers_still_link(tmp_path):
    w = World(tmp_path / "s.db")
    w.launch("a.nearlytrade.near", "w1.sendler-b.near", T0 + 20 * MIN)
    w.launch("b.nearlytrade.near", "w2.sendler-b.near", T0 + 30 * MIN)
    w.fund("w1.sendler-b.near", "w2.sendler-b.near", 12, T0 + 25 * MIN)          # a user's own transfer between two wallets is a fact, not the parent's funding
    K.rebuild_links(w.s, load_config())
    groups = w.s.con.execute("SELECT size FROM link_groups").fetchall()
    w.s.close()
    assert groups == [(2,)]
