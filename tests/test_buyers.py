"""RGY-028: who buys a launch - the first buyers of a launch (stage 1) and the links between them (stage 2: dev side, bundle, co-buyers).

The REFRAX / ISLAND / COIN-2 replay holds the F-005 pair: the same two wallets (`b4adf54a...0486`, `976b2ad5...3fe8`) bought ISLAND and REFRAX within
half a second of each other. Detection rules are pinned on pure inputs (positive and negative, one test per rule), the tables and pages on the replay."""
import json
import re
import shutil
import threading
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import links as K  # noqa: E402
from rugyomi.collector import Collector, main as collector_main  # noqa: E402
from rugyomi.keys import EnvKeySource  # noqa: E402
from rugyomi.neardata import Neardata  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.store.schema import MIGRATIONS  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi_testkit import refrax_replay_blocks, refrax_world_db  # noqa: E402

NS, HOUR, DAY = 10**9, 3600 * 10**9, 86400 * 10**9
NEAR = 10**24
ISLAND, COIN, REFRAX = "island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near"
A = "b4adf54a4ccc38cba50dc818140c8568baddd5b8e072d7a44992520ffdc70486"
B = "976b2ad5b28f7665d2890ee05b331a8996e37b548f7072ed9d4bbf0069493fe8"
CFG = C.load_config(profile="v1")


def cfg_with(**over):
    cfg = json.loads(json.dumps(CFG))
    for path, v in over.items():
        sect, key = path.split("__")
        node = cfg["buyers"]
        for part in sect.split("_x_"):
            node = node[part] if part != "top" else node
        node[key] = v
    return cfg


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("buyers") / "r.db"
    refrax_world_db(path)
    s = open_store(path)
    s.close()
    return path


@pytest.fixture
def db(replay, tmp_path):
    p = tmp_path / "b.db"
    shutil.copy(replay, p)
    s = open_store(p)
    yield s
    s.close()


def owner_of(s, token):
    return s.con.execute("SELECT COALESCE(owner, creator) FROM launches WHERE token = ?", (token,)).fetchone()[0]


def first_buy(s, token, buyer):
    return s.con.execute("SELECT first_buy_ns FROM launch_buyers WHERE token = ? AND buyer = ?", (token, buyer)).fetchone()[0]


def fund(s, account, sender, ts, near=5, signer=None, pred=None, tx=None, n=[0]):
    n[0] += 1
    s.con.execute("INSERT INTO transfers_api(account, dir, height, ts_ns, tx, receipt, tindex, other, asset, amount, ttype, signer, pred) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (account, "in", 1, ts, tx or f"fund-{n[0]}", f"rc-{n[0]}", 0, sender, "native:near", str(int(near * NEAR)), "NativeTransfer",
                   signer or sender, pred or sender))
    s.con.commit()


def links_of(s, typ=None):
    q = "SELECT token, buyer, type, group_key, ref, tx, relation, detail FROM buyer_links" + (" WHERE type = ?" if typ else "") + " ORDER BY token, buyer"
    return s.con.execute(q, (typ,) if typ else ()).fetchall()


# ------------------------------------------------------------------------------------------------ stage 1: the buyers of a launch
def test_stage1_lists_the_first_buyers_and_their_numbers_agree_with_the_snapshots(db):
    res = BU.build_launch_buyers(db, CFG)
    assert res["computed"] == 3 and res["failed"] == 0 and res["no_trades"] == 0 and res["buyer_rows"] == 5
    rows = db.con.execute("SELECT token, buyer, rank, first_buy_ns, spent, pnl, role FROM launch_buyers ORDER BY token, rank").fetchall()
    assert {(r[0], r[1]) for r in rows} == {(ISLAND, A), (ISLAND, B), (COIN, B), (REFRAX, A), (REFRAX, B)}
    for tok in (ISLAND, COIN, REFRAX):                                    # the sums equal the T+60 snapshot (no insiders in the replay)
        spent, pnl, n = db.con.execute("SELECT buyers_spent, buyers_pnl, buyers_count FROM launch_snapshots WHERE token = ? AND minute = 60", (tok,)).fetchone()
        mine = [r for r in rows if r[0] == tok]
        assert sum(int(r[4]) for r in mine) == int(spent) and sum(int(r[5]) for r in mine) == int(pnl) and len(mine) == n
        assert [r[2] for r in mine] == list(range(1, len(mine) + 1)) and [r[3] for r in mine] == sorted(r[3] for r in mine)
        dev = {x for x in db.con.execute("SELECT owner, creator FROM launches WHERE token = ?", (tok,)).fetchone() if x}
        assert not ({r[1] for r in mine} & dev) and {r[6] for r in mine} == {"buyer"}      # the dev is no buyer; nobody got tokens from the dev here
    sells = db.con.execute("SELECT first_sell_ns, first_buy_ns FROM launch_buyers WHERE first_sell_ns IS NOT NULL").fetchall()
    assert sells and all(a >= b for a, b in sells)


def test_stage1_keeps_only_top_n_and_is_resumable(db):
    cfg = cfg_with(top__top_n=1)
    first = BU.build_launch_buyers(db, cfg, limit=1)
    assert first["computed"] == 1 and first["to_compute"] == 3
    rest = BU.build_launch_buyers(db, cfg)
    assert rest["computed"] == 2 and rest["to_compute"] == 2
    assert BU.build_launch_buyers(db, cfg)["to_compute"] == 0                       # nothing left
    assert db.con.execute("SELECT MAX(c) FROM (SELECT COUNT(*) c FROM launch_buyers GROUP BY token)").fetchone()[0] == 1
    assert db.con.execute("SELECT buyers_total, kept FROM launch_buyers_done WHERE token = ?", (ISLAND,)).fetchone() == (2, 1)
    other = BU.build_launch_buyers(db, CFG)                                          # other parameters: recomputed
    assert other["to_compute"] == 3 and db.con.execute("SELECT COUNT(*) FROM launch_buyers").fetchone()[0] == 5


def test_stage1_history_launch_reads_api_facts_and_a_not_backfilled_one_is_retried(db):
    where = ("(f.token = (SELECT id FROM accounts WHERE name = :t) OR (f.kind = (SELECT id FROM kinds WHERE name = 'dcl_swap') "
             "AND f.b = (SELECT id FROM accounts WHERE name = (SELECT pool FROM launches WHERE token = :t))))")
    BU.build_launch_buyers(db, CFG)
    live = db.con.execute("SELECT buyer, rank, first_buy_ns, spent, pnl FROM launch_buyers WHERE token = ? ORDER BY rank", (ISLAND,)).fetchall()
    db.con.execute("INSERT INTO api_facts(scope,height,ord,seq,kind,ts_ns,tx,receipt,origin,a,b,token,amount,extra,in_blocks) "
                   "SELECT :t, f.height, 0, f.id, f.kind, f.ts_ns, f.tx, f.receipt, f.origin, f.a, f.b, f.token, f.amount, f.extra, 1 "
                   f"FROM facts f WHERE {where}", {"t": ISLAND})
    db.con.execute(f"DELETE FROM facts AS f WHERE {where}", {"t": ISLAND})
    db.con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = ?", (ISLAND,))
    db.con.commit()
    res = BU.build_launch_buyers(db, CFG)                                            # the source changed live -> history, not backfilled yet
    assert res["no_trades"] == 1 and res["computed"] == 0
    assert db.con.execute("SELECT COUNT(*) FROM launch_buyers WHERE token = ?", (ISLAND,)).fetchone()[0] == 0
    db.con.execute("INSERT INTO backfill_queue(kind,key,status,queued_at) VALUES('launch', ?, 'done', 'x')", (ISLAND,))
    db.con.commit()
    res = BU.build_launch_buyers(db, CFG)
    assert res["computed"] == 1 and db.con.execute("SELECT source FROM launch_buyers_done WHERE token = ?", (ISLAND,)).fetchone()[0] == "history"
    got = db.con.execute("SELECT buyer, rank, first_buy_ns, spent, pnl FROM launch_buyers WHERE token = ? ORDER BY rank", (ISLAND,)).fetchall()
    assert got == live                                                               # the same buyers from api_facts as from the live facts


def test_a_young_live_launch_is_not_final_and_is_computed_again(db):
    BU.build_launch_buyers(db, CFG)
    last = db.con.execute("SELECT MAX(ts_ns) FROM facts").fetchone()[0]
    db.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('new.nearlytrade.near','nearly','n','n','1',1,?,'x')", (last - 60 * 10**9,))
    db.con.commit()
    BU.build_launch_buyers(db, CFG)
    assert db.con.execute("SELECT final, kept FROM launch_buyers_done WHERE token = 'new.nearlytrade.near'").fetchone() == (0, 0)
    assert [p[0] for p in BU.pending(db, CFG)] == ["new.nearlytrade.near"]


@pytest.mark.core
def test_service_accounts_and_the_dev_are_never_buyers():
    launch = {"token": "t.nearlytrade.near", "owner": "dev.near", "creator": "launchbot.nearlytrade.near", "ts_ns": 1000 * NS}
    legs = [{"id": 1, "kind": "ft_transfer", "ts": 1001 * NS, "tx": "tx1", "a": "dclv2.ref-labs.near", "b": "dev.near", "amount": 100},
            {"id": 2, "kind": "ft_transfer", "ts": 1002 * NS, "tx": "tx2", "a": "dclv2.ref-labs.near", "b": "wrap.near", "amount": 100},
            {"id": 3, "kind": "ft_transfer", "ts": 1003 * NS, "tx": "tx3", "a": "dclv2.ref-labs.near", "b": "real.near", "amount": 100}]
    swaps = [{"id": i, "ts": (1000 + i) * NS, "tx": f"tx{i}", "swapper": "x", "token_in": "wrap.near", "token_out": "t.nearlytrade.near",
              "amount_in": 10**24, "amount_out": 100} for i in (1, 2, 3)]
    rows, total = BU.compute_launch(launch, legs, swaps, CFG)
    assert [r["buyer"] for r in rows] == ["real.near"] and total == 1


# ------------------------------------------------------------------------------------------------ stage 2: the rules, pure
def inputs(rows, fundings=None, launches=None, group_of=None, members_of=None, sources_of=None, recipients=None, cover=None):
    """rows: {token: [(buyer, seconds after launch)]}; launches: {token: (ts_ns, owner)}."""
    launches = launches or {t: (1000 * DAY + i * HOUR, "dev.near") for i, t in enumerate(rows)}
    L = {t: {"token": t, "launchpad": "nearly", "creator": own, "owner": own, "ts_ns": ts, "live": True} for t, (ts, own) in launches.items()}
    R = {}
    for t, bs in rows.items():
        R[t] = [{"token": t, "buyer": b, "rank": i + 1, "first_buy_ns": L[t]["ts_ns"] + int(sec * NS), "first_buy_tx": f"buy-{t}-{b}", "spent": str(NEAR), "role": "buyer"}
                for i, (b, sec) in enumerate(bs)]
    F = {}
    for b, items in (fundings or {}).items():
        F[b] = sorted(({"type": "funded", "src": src, "dst": b, "ts_ns": ts, "tx": f"f-{src}-{b}-{ts}", "amount": str(int(near * NEAR)), "asset": "NEAR", "source": "transfers_api"}
                       for src, ts, near in items), key=lambda e: e["ts_ns"])
    for rs in R.values():
        for r in rs:
            F.setdefault(r["buyer"], [])
    return {"launches": L, "rows": R, "group_of": group_of or {}, "members_of": members_of or {}, "sources_of": sources_of or {}, "fundings": F,
            "cover": cover or {}, "first_fact_ns": None, "recipients": recipients or (lambda src, lo, hi, limit: set())}


T = lambda i: 1000 * DAY + i * HOUR                    # the launch time of the i-th launch of `inputs`


def kinds(built, typ):
    return [(x["token"], x["buyer"]) for x in built["links"] if x["type"] == typ]


@pytest.mark.core
def test_dev_side_buyer_positive_via_the_dev_a_group_member_and_a_cluster_source():
    rows = {"a.t": [("w1", 5), ("w2", 8), ("w3", 9), ("w4", 12)]}
    f = {"w1": [("dev.near", T(0) - HOUR, 3)], "w2": [("friend.near", T(0) - 2 * HOUR, 3)], "w3": [("src.near", T(0) + 2 * NS, 3)],
         "w4": [("other.near", T(0) - HOUR, 3)]}
    inp = inputs(rows, f, group_of={"dev.near": "g"}, members_of={"g": ["dev.near", "friend.near"]}, sources_of={"dev.near": ["src.near"]})
    b = BU.detect(inp, CFG)
    assert sorted(kinds(b, "dev_side_buyer")) == [("a.t", "w1"), ("a.t", "w2"), ("a.t", "w3")]
    rel = {x["buyer"]: (x["relation"], x["ref"], x["tx"]) for x in b["links"] if x["type"] == "dev_side_buyer"}
    assert rel["w1"][0] == "the dev" and rel["w2"][0] == "a member of the dev's linked group" and rel["w3"][0] == "the funding source of the dev's cluster"
    assert rel["w1"][1] == "dev.near" and rel["w1"][2].startswith("f-dev.near-w1")           # the evidence is the transfer
    assert b["stats"]["dev_side_buyer"]["wallets"] == 3 and b["groups"] == []


@pytest.mark.core
def test_dev_side_buyer_negatives_too_early_after_the_buy_not_direct_small_service_and_not_the_dev():
    win = int(7 * DAY)
    rows = {"a.t": [("w1", 5), ("w2", 5), ("w3", 5), ("w4", 5), ("w5", 5)]}
    f = {"w1": [("dev.near", T(0) + 5 * NS - win - NS, 3)],            # 1 s outside the 7-day window
         "w2": [("dev.near", T(0) + 6 * NS, 3)],                      # after its first buy
         "w3": [("dev.near", T(0) - HOUR, 0.1)],                     # below buyers.min_funding_near is applied when loading; here: still in
         "w5": [("dev.near", T(0) - HOUR, 3)]}
    inp = inputs(rows, {k: v for k, v in f.items() if k != "w3"}, launches={"a.t": (T(0), "dev.near")})
    inp["rows"]["a.t"][4]["buyer"] = "wrap.near"                       # a service account is no buyer: no link even with a funding row
    inp["fundings"]["wrap.near"] = f["w5"]
    b = BU.detect(inp, CFG)
    assert kinds(b, "dev_side_buyer") == []


def test_dev_side_buyer_a_non_direct_transfer_and_a_small_one_are_not_loaded(db):
    BU.build_launch_buyers(db, CFG)
    dev = owner_of(db, ISLAND)
    t = first_buy(db, ISLAND, B)
    fund(db, B, dev, t - HOUR, 5, signer="someone.near", pred="router.near")            # a payout / refund through a contract: no funding
    fund(db, B, dev, t - 2 * HOUR, 0.3)                                                # below 0.5 NEAR
    fund(db, A, dev, t - HOUR, 5, signer=dev, pred="router.near")
    res = BU.build_links(db, CFG)
    assert res["stats"]["dev_side_buyer"]["wallets"] == 0
    fund(db, B, dev, t - 3 * HOUR, 5)                                                    # a direct one: found, with its transaction
    res = BU.build_links(db, CFG)
    assert res["stats"]["dev_side_buyer"]["wallets"] == 1 and res["stats"]["dev_side_buyer"]["groups"] == 0
    row = next(r for r in links_of(db, "dev_side_buyer") if r[0] == ISLAND)                  # (the same dev launched other replayed tokens too)
    assert row[:3] == (ISLAND, B, "dev_side_buyer") and row[4] == dev and row[5] and row[6] == "the dev"


@pytest.mark.core
def test_bundle_positive_three_wallets_one_source_within_the_seconds():
    rows = {"a.t": [("w1", 3), ("w2", 10), ("w3", 29), ("w4", 2), ("w5", 100)]}
    f = {w: [("src.near", T(0) - 2 * HOUR, 2)] for w in ("w1", "w2", "w3", "w5")}
    f["w4"] = [("elsewhere.near", T(0) - 2 * HOUR, 2)]
    b = BU.detect(inputs(rows, f), CFG)
    assert sorted(kinds(b, "bundle")) == [("a.t", "w1"), ("a.t", "w2"), ("a.t", "w3")]                    # w5 bought too late, w4 has another source
    g = next(x for x in b["groups"] if x["type"] == "bundle")
    assert g["group_key"] == "src.near" and g["size"] == 3 and g["launches"] == 1
    link = next(x for x in b["links"] if x["buyer"] == "w2")
    assert link["ref"] == "src.near" and link["tx"].startswith("f-src.near-w2") and link["detail"]["wallets"] == 3 and link["detail"]["seconds_after_launch"] == 10


@pytest.mark.core
def test_bundle_negatives_too_few_too_late_funded_too_early_funded_after_a_wide_source_a_service_source():
    base = {"a.t": [("w1", 3), ("w2", 10), ("w3", 20)]}
    ok = {w: [("src.near", T(0) - HOUR, 2)] for w in ("w1", "w2", "w3")}
    assert len(kinds(BU.detect(inputs(base, ok), CFG), "bundle")) == 3
    # two wallets only
    assert kinds(BU.detect(inputs({"a.t": base["a.t"][:2]}, ok), CFG), "bundle") == []
    # the buys came after bundle.seconds
    late = {"a.t": [("w1", 40), ("w2", 50), ("w3", 60)]}
    assert kinds(BU.detect(inputs(late, ok), CFG), "bundle") == []
    # funded more than bundle.funding_window (24 h) before the launch, or after it
    early = {w: [("src.near", T(0) - 25 * HOUR, 2)] for w in ("w1", "w2", "w3")}
    after = {w: [("src.near", T(0) + NS, 2)] for w in ("w1", "w2", "w3")}
    assert kinds(BU.detect(inputs(base, early), CFG), "bundle") == [] and kinds(BU.detect(inputs(base, after), CFG), "bundle") == []
    # a wide source (it funded more accounts than bundle.narrow_max in the window) is not a bundle source
    wide = inputs(base, ok, recipients=lambda src, lo, hi, limit: {f"x{i}" for i in range(limit + 1)})
    assert kinds(BU.detect(wide, CFG), "bundle") == []
    assert len(kinds(BU.detect(wide, cfg_with(bundle__narrow_max=0)), "bundle")) == 3             # narrowness switched off
    # a service account is never a source
    for svc in ("intents.near", "wrap.near", "v2.ref-finance.near", "lock_7.nearlytrade.near"):
        svcf = {w: [(svc, T(0) - HOUR, 2)] for w in ("w1", "w2", "w3")}
        b = BU.detect(inputs(base, svcf), CFG)
        assert kinds(b, "bundle") == [] and b["groups"] == [], svc


def co(built):
    return built["stats"]["co_buyers"]


def groups_of(built):
    return {g["group_key"]: sorted(m["account"] for m in built["members"] if m["type"] == "trader_group" and m["group_key"] == g["group_key"])
            for g in built["groups"] if g["type"] == "trader_group"}


def pair_set(built):
    return {(p["a"], p["b"]): p for p in built["pairs"]}


@pytest.mark.core
def test_co_buyers_a_triangle_is_one_group_and_every_pair_is_stored_with_its_evidence():
    rows = {f"l{i}.t": [("x", 5), ("y", 20), ("z", 40)] for i in range(3)}
    rows["l3.t"] = [("q", 5), ("r", 400)]                 # 400 s apart: no pair
    b = BU.detect(inputs(rows), CFG)
    assert groups_of(b) == {"x": ["x", "y", "z"]}
    g = next(x for x in b["groups"] if x["type"] == "trader_group")
    assert g["size"] == 3 and g["launches"] == 3 and g["truncated"] == 0
    assert set(pair_set(b)) == {("x", "y"), ("x", "z"), ("y", "z")} and all(p["group_key"] == "x" and p["launches"] == 3 for p in b["pairs"])
    ev = [e for e in b["pair_launches"] if (e["a"], e["b"]) == ("x", "y")]
    assert sorted(e["token"] for e in ev) == ["l0.t", "l1.t", "l2.t"] and {e["gap_ms"] for e in ev} == {15000}
    assert {(e["a_tx"], e["b_tx"]) for e in ev} == {(f"buy-l{i}.t-x", f"buy-l{i}.t-y") for i in range(3)}         # both transactions
    c = {(x["token"], x["buyer"]): x for x in b["links"] if x["type"] == "co_buyers"}
    assert ("l0.t", "x") in c and ("l3.t", "q") not in c and c[("l0.t", "y")]["group_key"] == "x" and c[("l0.t", "y")]["detail"]["group_size"] == 3
    assert {p["account"] for p in c[("l0.t", "y")]["detail"]["partners"]} == {"x", "z"}
    assert co(b)["groups"] == 1 and co(b)["largest_group"] == 3 and co(b)["pairs"] == 3 and co(b)["clique_search_truncated"] is False


@pytest.mark.core
def test_co_buyers_no_chaining_x_y_and_y_z_without_x_z_is_no_group_but_both_pairs_stay():
    """RGY-031: replaces the RGY-028 expectation that x-y and y-z chain into one group of three."""
    rows = {f"l{i}.t": [("x", 5), ("y", 20)] for i in range(3)}
    rows.update({f"m{i}.t": [("y", 5), ("z", 20)] for i in range(3)})
    b = BU.detect(inputs(rows), CFG)
    assert groups_of(b) == {} and co(b)["groups"] == 0 and kinds(b, "co_buyers") == []
    assert set(pair_set(b)) == {("x", "y"), ("y", "z")} and all(p["group_key"] == "" for p in b["pairs"])        # the facts stay as pairs
    assert len(b["pair_launches"]) == 6


@pytest.mark.core
def test_co_buyers_a_longer_chain_of_pairs_and_two_triangles_joined_by_one_edge_are_not_merged():
    chain = {}
    for k, (u, v) in enumerate((("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"))):
        chain.update({f"c{k}{i}.t": [(u, 5), (v, 20)] for i in range(3)})
    assert groups_of(BU.detect(inputs(chain), CFG)) == {}
    tri = {}
    for k, ws in enumerate((("a", "b", "c"), ("d", "e", "f"), ("c", "d"))):
        tri.update({f"t{k}{i}.t": [(w, 5 + n) for n, w in enumerate(ws)] for i in range(3)})
    got = groups_of(BU.detect(inputs(tri), CFG))
    assert got == {"a": ["a", "b", "c"], "d": ["d", "e", "f"]}                          # the c-d edge joins nothing


@pytest.mark.core
def test_co_buyers_overlapping_cliques_give_disjoint_groups_largest_first():
    # a-b-c and b-c-d are triangles, a-d is no pair: one group {a,b,c}; d keeps its pairs with b and c but is in no group
    rows = {}
    for k, ws in enumerate((("a", "b", "c"), ("b", "c", "d"))):
        rows.update({f"o{k}{i}.t": [(w, 5 + n) for n, w in enumerate(ws)] for i in range(3)})
    b = BU.detect(inputs(rows), CFG)
    assert groups_of(b) == {"a": ["a", "b", "c"]}
    assert len({m["account"] for m in b["members"]}) == len(b["members"])             # a wallet is in at most one group
    assert {k: p["group_key"] for k, p in pair_set(b).items()} == {("a", "b"): "a", ("a", "c"): "a", ("b", "c"): "a", ("b", "d"): "", ("c", "d"): ""}
    four = {f"q{i}.t": [(w, 5 + n) for n, w in enumerate("abcd")] for i in range(3)}  # a K4 is one group of four, not two triangles
    assert groups_of(BU.detect(inputs(four), CFG)) == {"a": ["a", "b", "c", "d"]}


FREQ = dict(co__frequent_min_launches=4, co__frequent_share=0.5)


@pytest.mark.core
def test_a_frequent_wallet_joins_no_group_connects_nobody_but_its_pairs_stay():
    rows = {f"l{i}.t": [("f", 1), ("x", 5), ("y", 20), ("z", 40)] for i in range(4)}          # f buys everything; x, y, z are always with it
    plain = BU.detect(inputs(rows), CFG)                                                       # defaults need 20 launches: nobody is frequent
    assert groups_of(plain) == {"f": ["f", "x", "y", "z"]} and co(plain)["frequent_wallets"] == 0
    b = BU.detect(inputs(rows), cfg_with(**FREQ))
    w = {x["account"]: x for x in b["wallets"]}
    assert w["f"]["frequent"] == 1 and w["x"]["frequent"] == 1 and co(b)["groups"] == 0         # all four are in all 4 launches: all frequent
    # only f is frequent: x, y, z are early buyers in 4 launches spread over a span of 13, f buys all 16
    rows2 = {}
    for i in range(4):
        rows2[f"l{i}.t"] = [("f", 1), ("x", 5), ("y", 20), ("z", 40)]
        rows2.update({f"n{i}{k}.t": [("f", 1), (f"u{i}{k}", 5)] for k in range(3)})
    b2 = BU.detect(inputs(rows2), cfg_with(**FREQ))
    w2 = {x["account"]: x for x in b2["wallets"]}
    assert w2["f"]["frequent"] == 1 and w2["f"]["launches"] == 16 and w2["f"]["span_launches"] == 16 and w2["f"]["share_bps"] == 10000
    assert w2["x"]["frequent"] == 0 and w2["x"]["launches"] == 4 and w2["x"]["span_launches"] == 13 and w2["x"]["share_bps"] == 3077
    assert groups_of(b2) == {"x": ["x", "y", "z"]}                                              # the group without f
    ps = pair_set(b2)
    assert {k for k in ps if "f" in k} == {("f", "x"), ("f", "y"), ("f", "z")} and all(ps[k]["group_key"] == "" for k in ps if "f" in k)
    assert co(b2)["pairs_with_a_frequent_wallet"] == 3 and co(b2)["frequent_wallets"] == 1
    assert "f" not in {m["account"] for m in b2["members"]} and "f" not in {x["buyer"] for x in b2["links"] if x["type"] == "co_buyers"}
    # the pair of a frequent wallet is still evidence on the launch: both transactions
    assert len([e for e in b2["pair_launches"] if (e["a"], e["b"]) == ("f", "x")]) == 4


@pytest.mark.core
def test_a_frequent_wallet_does_not_merge_two_groups():
    rows = {}
    for i in range(20):                                   # f buys all 20 launches; the two triples are together in 4 launches each, spread out
        ws = ("a", "b", "c") if i % 5 == 0 else ("d", "e", "g") if i % 5 == 2 else (f"u{i}",)
        rows[f"l{i}.t"] = [("f", 1)] + [(w, 5 + n) for n, w in enumerate(ws)]
    b = BU.detect(inputs(rows), cfg_with(**FREQ))
    assert groups_of(b) == {"a": ["a", "b", "c"], "d": ["d", "e", "g"]}
    assert {x["account"] for x in b["wallets"] if x["frequent"]} == {"f"}
    dflt = BU.detect(inputs(rows), CFG)                                                        # defaults: f has 20 launches and 100 % -> frequent as well
    assert co(dflt)["frequent_wallets"] == 1 and groups_of(dflt) == groups_of(b)
    nofreq = BU.detect(inputs(rows), cfg_with(co__frequent_min_launches=99))                    # frequency off: f is a member of one triple's clique (a wallet is in one group)
    assert co(nofreq)["frequent_wallets"] == 0 and groups_of(nofreq) == {"a": ["a", "b", "c", "f"], "d": ["d", "e", "g"]}


@pytest.mark.core
def test_wallet_frequency_is_measured_over_the_active_span():
    rows = {f"l{i}.t": [(f"u{i}", 5)] for i in range(10)}
    rows["l2.t"].append(("late", 9))
    rows["l3.t"].append(("late", 9))
    rows["l8.t"].append(("many", 9))
    for i in range(10):
        rows[f"l{i}.t"].append(("all", 11))
    w = {x["account"]: x for x in BU.detect(inputs(rows), cfg_with(co__frequent_min_launches=2, co__frequent_share=0.5))["wallets"]}
    assert (w["late"]["launches"], w["late"]["span_launches"], w["late"]["share_bps"], w["late"]["frequent"]) == (2, 2, 10000, 1)      # busy in its span
    assert (w["all"]["launches"], w["all"]["span_launches"], w["all"]["frequent"]) == (10, 10, 1) and w["u4"]["frequent"] == 0
    w = {x["account"]: x for x in BU.detect(inputs(rows), CFG)["wallets"]}
    assert w["late"]["frequent"] == 0 and w["all"]["frequent"] == 0                             # the defaults want enough launches too


@pytest.mark.core
def test_co_buyers_negatives_too_few_launches_too_far_apart_service_accounts_and_no_data_cap():
    two = {f"l{i}.t": [("x", 5), ("y", 20)] for i in range(2)}
    assert kinds(BU.detect(inputs(two), CFG), "co_buyers") == [] and BU.detect(inputs(two), CFG)["pairs"] == []        # 2 < co.min_launches (3)
    assert len(BU.detect(inputs(two), cfg_with(co__min_launches=2))["pairs"]) == 1
    far = {f"l{i}.t": [("x", 5), ("y", 200)] for i in range(4)}
    assert BU.detect(inputs(far), CFG)["pairs"] == []                                          # 195 s > co.seconds (60)
    assert len(BU.detect(inputs(far), cfg_with(co__seconds=300))["pairs"]) == 1
    svc = {f"l{i}.t": [("x", 5), ("wrap.near", 6), ("y", 7), ("z", 8)] for i in range(4)}
    b = BU.detect(inputs(svc), CFG)
    assert all("wrap.near" not in (x["buyer"], x["ref"]) for x in b["links"]) and "wrap.near" not in {m["account"] for m in b["members"]}
    assert all("wrap.near" not in (p["a"], p["b"]) for p in b["pairs"]) and "wrap.near" not in {w["account"] for w in b["wallets"]}
    assert groups_of(b) == {"x": ["x", "y", "z"]}
    big = {f"l{i}.t": [(f"w{j}", j) for j in range(60)] for i in range(3)}                     # 60 wallets that always buy together: one clique, no cap
    b = BU.detect(inputs(big), CFG)
    g = next(x for x in b["groups"] if x["type"] == "trader_group")
    assert g["size"] == 60 and g["truncated"] == 0 and len([m for m in b["members"] if m["type"] == "trader_group"]) == 60
    assert "group_max" not in CFG["buyers"]["co"]


@pytest.mark.core
def test_the_clique_search_has_a_visible_limit(monkeypatch):
    rows = {f"l{i}.t": [(f"w{j}", j) for j in range(12)] for i in range(3)}
    monkeypatch.setattr(BU, "CLIQUE_MAX_STEPS", 1)
    assert co(BU.detect(inputs(rows), CFG))["clique_search_truncated"] is True
    monkeypatch.undo()
    assert co(BU.detect(inputs(rows), CFG))["clique_search_truncated"] is False


@pytest.mark.core
def test_no_account_is_included_or_excluded_by_its_name():
    """A wallet named like a service is a buyer like any other (master §8.8): only the explicit list excludes."""
    rows = {f"l{i}.t": [("lock_9.fake.near", 5), ("usdt-pool.near.fake.near", 6), ("plain.near", 7)] for i in range(3)}
    assert not K.service("usdt-pool.near.fake.near") and not K.service("lock_9.fake.near")
    b = BU.detect(inputs(rows), CFG)
    assert groups_of(b) == {"lock_9.fake.near": ["lock_9.fake.near", "plain.near", "usdt-pool.near.fake.near"]} and len(b["pairs"]) == 3
    assert len(kinds(b, "co_buyers")) == 9


# ------------------------------------------------------------------------------------------------ stage 2 on the replay
def test_the_f005_pair_is_stored_as_a_pair_with_both_transactions_on_the_replay(db):
    """RGY-031: two wallets are a PAIR, not a group (a group is a clique of three). The pair bought ISLAND and REFRAX within a second or so of each
    other; COIN-2 has one early buyer of the two (only B bought it in the first hour), which is why the pair is not found there."""
    res = BU.rebuild(db, cfg_with(co__min_launches=2))
    assert res["stage1"]["computed"] == 3 and res["stage2"]["stats"]["co_buyers"]["groups"] == 0 and res["stage2"]["stats"]["co_buyers"]["pairs"] == 1
    a, b_ = sorted((A, B))
    assert db.con.execute("SELECT a, b, launches, group_key FROM buyer_pairs").fetchall() == [(a, b_, 2, "")]
    ev = db.con.execute("SELECT token, gap_ms, a_tx, b_tx FROM buyer_pair_launches ORDER BY token").fetchall()
    assert [e[0] for e in ev] == [ISLAND, REFRAX] and all(0 <= e[1] < 3000 and e[2] and e[3] and e[2] != e[3] for e in ev)
    txs = dict(db.con.execute("SELECT buyer || token, first_buy_tx FROM launch_buyers"))
    assert {(e[2], e[3]) for e in ev} == {(txs[a + t], txs[b_ + t]) for t in (ISLAND, REFRAX)}            # the transactions of both first buys
    assert db.con.execute("SELECT COUNT(*) FROM launch_buyers WHERE token = ? AND buyer = ?", (COIN, A)).fetchone()[0] == 0
    assert db.con.execute("SELECT COUNT(*) FROM launch_buyers WHERE token = ? AND buyer = ?", (COIN, B)).fetchone()[0] == 1
    assert db.con.execute("SELECT COUNT(*) FROM buyer_groups").fetchone()[0] == 0 and db.con.execute("SELECT COUNT(*) FROM buyer_links").fetchone()[0] == 0
    assert {r[0] for r in db.con.execute("SELECT account FROM buyer_wallets")} == {A, B}
    assert BU.rebuild(db, CFG)["stage2"]["stats"]["co_buyers"]["pairs"] == 0                       # the default needs 3 launches: the replay has 2
    assert db.con.execute("SELECT COUNT(*) FROM buyer_pairs").fetchone()[0] == 0 and db.con.execute("SELECT COUNT(*) FROM buyer_pair_launches").fetchone()[0] == 0


def test_end_to_end_bundle_and_dev_side_from_transfers(db):
    BU.build_launch_buyers(db, CFG)
    ts = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (ISLAND,)).fetchone()[0]
    dev = owner_of(db, ISLAND)
    fund(db, A, "bsrc.near", ts - HOUR, 2, tx="txA")
    fund(db, B, "bsrc.near", ts - 2 * HOUR, 22, tx="txB")
    fund(db, B, dev, ts - 3 * HOUR, 1, tx="txdev")
    cfg = cfg_with(bundle__min_wallets=2, bundle__narrow_max=0)
    res = BU.build_links(db, cfg)
    # the funding also precedes the REFRAX launch 15 minutes later by less than 24 h, and both wallets bought it at once too: a bundle there as well
    assert res["stats"]["bundle"] == {"launches": 2, "wallets": 2, "groups": 1}
    assert {r[:4] for r in links_of(db, "bundle")} == {(t, w, "bundle", "bsrc.near") for t in (ISLAND, REFRAX) for w in (A, B)}
    assert {r[5] for r in links_of(db, "bundle")} == {"txA", "txB"}
    ds = [r for r in links_of(db, "dev_side_buyer") if r[0] == ISLAND]
    assert [r[:2] for r in ds] == [(ISLAND, B)] and ds[0][5] == "txdev"
    assert db.con.execute("SELECT type, group_key, size, launches FROM buyer_groups").fetchall() == [("bundle", "bsrc.near", 2, 2)]
    assert BU.status(db)["stats"]["bundle"]["groups"] == 1
    # the same data twice gives the same rows
    before = db.con.execute("SELECT * FROM buyer_links ORDER BY 1,2,3,4").fetchall()
    BU.build_links(db, cfg)
    assert db.con.execute("SELECT * FROM buyer_links ORDER BY 1,2,3,4").fetchall() == before


@pytest.mark.core
def test_funding_coverage_tells_what_our_data_can_see():
    inp = inputs({"a.t": [("w1", 5)]}, cover={"w1": T(0) + 4 * NS, "w2": None, "w3": T(0) - HOUR})
    win = int(7 * DAY)
    assert not BU.funding_covered(inp, "w1", T(0) + 5 * NS, win)              # loaded up to 4 s after the launch only
    assert BU.funding_covered(inp, "w1", T(0) + 4 * NS, win) and BU.funding_covered(inp, "w2", T(0) + 99 * NS, win)
    assert not BU.funding_covered(inp, "w3", T(0), win) and not BU.funding_covered(inp, "nobody", T(0), win)
    inp["first_fact_ns"] = T(0) - 8 * DAY                                         # the live facts reach back past the window: covered
    assert BU.funding_covered(inp, "nobody", T(0), win)


# ------------------------------------------------------------------------------------------------ the tables are derived; the rest of the system ignores them
@pytest.mark.core
def test_the_migration_is_additive_and_an_old_database_opens(tmp_path):
    assert "buyer_links" in MIGRATIONS[15] and "launch_buyers" in MIGRATIONS[15] and len(MIGRATIONS) >= 16
    assert not any(w in MIGRATIONS[15].upper() for w in ("DROP ", "ALTER ", "DELETE ", "UPDATE "))
    p = tmp_path / "old.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    for t in ("launch_buyers", "launch_buyers_done", "buyer_links", "buyer_groups", "buyer_group_members", "buyer_wallets", "buyer_pairs", "buyer_pair_launches",
              "launch_holdings", "launch_holdings_done", "signal_features", "launch_live_state", "launch_live_state_at", "card_fixes"):                                                  # the later migration (v18) is replayed too
        s.con.execute(f"DROP TABLE {t}")
    s.con.execute("PRAGMA user_version=15")
    s.con.commit()
    s.close()
    s = open_store(p)
    assert s.user_version() >= 16 and s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
    assert BU.early_buyers(s, "a.nearlytrade.near") == [] and BU.launch_summary(s, ["a.nearlytrade.near"]) == {}
    s.close()


@pytest.mark.core
def test_the_v17_migration_is_additive_keeps_the_old_group_rows_and_the_pages_open_on_it(tmp_path):
    """RGY-031: a v16 database (with the old chained group rows) migrates by adding three tables; nothing is dropped; the old rows stay until the rebuild."""
    assert all(t in MIGRATIONS[16] for t in ("buyer_wallets", "buyer_pairs", "buyer_pair_launches")) and len(MIGRATIONS) >= 17
    assert not any(w in MIGRATIONS[16].upper() for w in ("DROP ", "ALTER ", "DELETE ", "UPDATE ", "INSERT "))
    p = tmp_path / "v16.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    s.con.execute("INSERT INTO buyer_groups(type, group_key, size, launches, first_ns, last_ns, truncated) VALUES('trader_group','k',394,3,1,2,1)")
    s.con.execute("INSERT INTO buyer_group_members(type, group_key, account, launches) VALUES('trader_group','k','m.near',3)")
    s.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, buys, spent, bought, sold, received, pnl) VALUES('a.nearlytrade.near','m.near',1,5,1,'1','1','0','0','0')")
    for t in ("buyer_wallets", "buyer_pairs", "buyer_pair_launches", "launch_holdings", "launch_holdings_done", "signal_features", "launch_live_state", "launch_live_state_at", "card_fixes"):
        s.con.execute(f"DROP TABLE {t}")
    s.con.execute("PRAGMA user_version=16")
    s.con.commit()
    s.close()
    s = open_store(p)
    assert s.user_version() >= 17 and s.con.execute("SELECT size FROM buyer_groups").fetchone()[0] == 394        # the old row is still there
    assert BU.trader_page(s, "m.near")["pairs"] == [] and BU.trader_page(s, "nobody.near") is None              # in the old group, in no pair: a page, not an error
    assert BU.early_buyers(s, "a.nearlytrade.near")[0]["pairs"] == []
    BU.build_links(s, CFG)
    assert s.con.execute("SELECT COUNT(*) FROM buyer_groups").fetchone()[0] == 0                                   # the rebuild replaces the old rows
    s.close()


@pytest.mark.core
def test_only_the_buyers_module_writes_the_buyer_tables_and_nothing_in_the_rules_reads_them():
    import rugyomi
    pkg = Path(rugyomi.__file__).parent
    src = {p.relative_to(pkg).as_posix(): p.read_text(encoding="utf-8") for p in pkg.rglob("*.py")}
    writers = {k for k, v in src.items() if re.search(r"INTO (launch_buyers|buyer_links|buyer_groups|buyer_group_members|buyer_wallets|buyer_pairs|buyer_pair_launches)|DELETE FROM (launch_buyers|buyer_)", v)}
    assert writers == {"buyers.py"}
    readers = {k for k, v in src.items() if re.search(r"(launch_buyers|buyer_links|buyer_groups|buyer_group_members|buyer_wallets|buyer_pairs|buyer_pair_launches)", v)}
    assert readers <= {"buyers.py", "store/schema.py", "holdings.py", "livestate.py"}      # RGY-036: holdings reads the stored early buyers and links; RGY-043: so does the live state (facts only)
    for forbidden in ("publisher.py", "rules/card.py", "rules/context.py", "rules/signals.py", "rules/rug.py", "rules/stored.py", "rules/accuracy.py",
                      "rules/backtest.py", "retro.py", "links.py"):
        assert not re.search(r"import buyers|from \. import buyers|launch_buyers|buyer_links|buyer_groups|rugyomi\.buyers", src[forbidden]), forbidden       # the score and the label never read them
    # the card hash does not depend on the buyers section
    other = json.loads(json.dumps(CFG))
    other["buyers"]["bundle"]["min_wallets"] = 9
    assert C.config_hash(other) == C.config_hash(CFG) and C.buyers_config_hash(other) != C.buyers_config_hash(CFG)


# ------------------------------------------------------------------------------------------------ pages
@pytest.fixture
def linked(db):
    ts = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (ISLAND,)).fetchone()[0]
    dev = owner_of(db, ISLAND)
    BU.build_launch_buyers(db, CFG)
    fund(db, A, "bsrc.near", ts - HOUR, 2, tx="txA")
    fund(db, B, "bsrc.near", ts - 2 * HOUR, 22, tx="txB")
    fund(db, B, dev, ts - 3 * HOUR, 1, tx="txdev")
    db.con.execute("INSERT INTO manual_labels(account, label, reason, source, set_by, set_at) VALUES(?,?,?,?,?,?)", (A, "independent_sniper", "user", "manual", "user", "2026-10-31T00:00:00Z"))
    db.con.commit()
    BU.build_links(db, cfg_with(bundle__min_wallets=2, bundle__narrow_max=0, co__min_launches=2))
    return db


def test_the_launch_page_shows_the_early_buyers_with_their_links_and_labels(linked):
    path = linked.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        r = c.get(f"/launch/{ISLAND}")
        t = " ".join(r.text.split())
        assert r.status_code == 200 and 'id="early-buyers"' in r.text and "Early buyers" in t
        assert ">independent sniper</span>" in r.text                                    # the manual label as it is
        assert 'href="/bundle/bsrc.near"' in r.text and f'href="/traders/{A}"' in r.text and "dev side" in t and "received 1.00 NEAR from" in t
        assert "they do not say who controls the wallets" in t.lower() or "do not say who controls the wallets" in t
        assert "the dev</" not in t.replace("the dev (", "")                                  # a group / bundle is never called "the dev"
        assert re.search(r"/tx|nearblocks\.io/txns/", r.text)
        assert '<span class="tag">pair</span>' in r.text and "bought within" in t and "launches together" in t and "their buy</a>" in t     # RGY-031: a pair, shown as a pair
        assert f'href="/traders/{B}#pair-{A}"' in r.text or f'href="/traders/{A}#pair-{B}"' in r.text                                      # the evidence link
        assert 'class="tag" href="/traders/' not in r.text                                                                                 # no group tag: two wallets are no group
        t2 = " ".join(c.get(f"/launch/{COIN}").text.split())                             # one buyer, no link of any kind: listed, "none found" (not "clean")
        assert 'id="early-buyers"' in c.get(f"/launch/{COIN}").text and "none found" in t2 and "/bundle/" not in t2


def test_cluster_pages_render_per_type_and_unknown_ones_are_404(linked):
    path = linked.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        r = c.get("/bundle/bsrc.near")
        t = " ".join(r.text.split())
        assert r.status_code == 200 and "Bundle" in t and A[:10] in t and "txns" in r.text and "do not say who controls the wallets" in t
        assert f"/launch/{ISLAND}#early-buyers" in r.text
        for key, other in ((A, B), (B, A)):                                                   # a wallet in no group: a page with its pair, not a 404 / 500
            r = c.get(f"/traders/{key}")
            t = " ".join(r.text.split())
            assert r.status_code == 200 and "not in a trader group" in t and "do not say that one person runs any of these wallets" in t
            assert ISLAND in r.text and REFRAX in r.text and other[:10] in t and "2 launches together" in t
            assert "this wallet's buy</a>" in t and "the other wallet's buy</a>" in t and "nearblocks.io/txns/" in r.text
        assert c.get("/bundle/nobody.near").status_code == 404 and c.get("/traders/nobody.near").status_code == 404
        assert c.get("/bundle/A%20B").status_code in (404, 422) and c.get("/traders/not valid").status_code in (404, 422)


def make_world(tmp_path):
    """25 launches: F buys all of them (frequent); X, Y, Z buy together in 5 of them (a triangle) and F is paired with each; P1 / P2 are a pair
    in 3 launches (no group, not frequent)."""
    s = open_store(tmp_path / "w.db")
    base = 1000 * DAY
    for i in range(25):
        tok = f"w{i}.nearlytrade.near"
        s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, extra) VALUES(?,?,?,?,?,?,?,?,?)",
                      (tok, "nearly", "dev.near", "dev.near", "1", i + 1, base + i * HOUR, "x", json.dumps({"symbol": f"S{i}"})))
        buyers = [("f.near", 1)]
        if i % 5 == 0:
            buyers += [("x.near", 5), ("y.near", 9), ("z.near", 14)]
        if i in (3, 8, 13):
            buyers += [("p1.near", 20), ("p2.near", 21)]
        for rank, (w, sec) in enumerate(buyers, 1):
            s.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (tok, w, rank, base + i * HOUR + sec * NS, f"tx-{i}-{w}", 1, str(NEAR), "1", "0", "0", "0"))
        s.con.execute("INSERT INTO launch_buyers_done(token, buyers_total, kept, source, final, config_hash, computed_at) VALUES(?,?,?,?,?,?,?)",
                      (tok, len(buyers), len(buyers), "live", 1, "h", "x"))
    s.con.commit()
    BU.build_links(s, CFG)
    return s


@pytest.mark.core
def test_the_world_has_one_group_one_pair_one_frequent_wallet(tmp_path):
    s = make_world(tmp_path)
    assert s.con.execute("SELECT group_key, size, launches FROM buyer_groups").fetchall() == [("x.near", 3, 5)]
    assert dict(s.con.execute("SELECT account, frequent FROM buyer_wallets")) == {"f.near": 1, "x.near": 0, "y.near": 0, "z.near": 0, "p1.near": 0, "p2.near": 0}
    assert {tuple(r[:2]): r[2] for r in s.con.execute("SELECT a, b, group_key FROM buyer_pairs")} == {
        ("f.near", "x.near"): "", ("f.near", "y.near"): "", ("f.near", "z.near"): "", ("x.near", "y.near"): "x.near", ("x.near", "z.near"): "x.near",
        ("y.near", "z.near"): "x.near", ("p1.near", "p2.near"): "",
        ("f.near", "p1.near"): "", ("f.near", "p2.near"): ""}                                  # f is next to everybody who buys, within seconds
    # every two members of every group are a stored pair; no group holds a frequent wallet
    for key, in s.con.execute("SELECT group_key FROM buyer_groups WHERE type = 'trader_group'").fetchall():
        mem = sorted(r[0] for r in s.con.execute("SELECT account FROM buyer_group_members WHERE group_key = ?", (key,)))
        have = {tuple(r) for r in s.con.execute("SELECT a, b FROM buyer_pairs")}
        assert all((x, y) in have for i, x in enumerate(mem) for y in mem[i + 1:])
        assert not {r[0] for r in s.con.execute("SELECT account FROM buyer_wallets WHERE frequent = 1")} & set(mem)
    s.close()


def test_the_wallet_and_launch_pages_on_the_world(tmp_path, monkeypatch):
    s = make_world(tmp_path)
    path = s.con.execute("PRAGMA database_list").fetchone()[2]
    s.close()
    with TestClient(webapp.create_app(path)) as c:
        r = c.get("/traders/y.near")                                                         # a member opens the group
        t = " ".join(r.text.split())
        assert r.status_code == 200 and "Trader group" in t and "one of 3 wallets" in t and "does not say that one person runs them" in t
        assert "x.near" in t and "z.near" in t and "S0" in t
        members = r.text[r.text.index("<h2>Wallets</h2>"):r.text.index("<h2>Launches</h2>")]
        assert "f.near" not in members                                                       # the frequent wallet is no member
        r = c.get("/traders/f.near")                                                         # the frequent wallet: not in a group, its pairs are shown with the evidence
        t = " ".join(r.text.split())
        assert r.status_code == 200 and "not in a trader group" in t and "early buyer in most launches" in t
        assert "5 launches together" in t and "x.near" in t and "nearblocks.io/txns/" in r.text and "25</strong> of 25 launches" in t
        t = " ".join(c.get("/traders/p1.near").text.split())
        assert "not in a trader group" in t and "p2.near" in t and "3 launches together" in t
        r = c.get("/launch/w0.nearlytrade.near")                                             # the launch page: pairs, the group tag, the frequent wallet
        t = " ".join(r.text.split())
        assert r.status_code == 200 and "one of 3 wallets in which every two bought" in t and 'class="tag" href="/traders/x.near"' in r.text
        assert "early buyer in 25 of 25 launches (not put in a group)" in t
        assert 'href="/traders/f.near#pair-x.near"' in r.text or 'href="/traders/x.near#pair-f.near"' in r.text
        assert "more pairs" not in t                                                        # f has 3 pairs in this launch: all are listed
        t3 = " ".join(c.get("/launch/w3.nearlytrade.near").text.split())
        assert "p1.near" in t3 and "p2.near" in t3 and "more pairs" not in t3
    monkeypatch.setattr(BU, "page_settings", lambda: {"page_n": 20, "sold_within_min": 15, "page_members": 2})
    with TestClient(webapp.create_app(path)) as c:
        t = " ".join(c.get("/traders/x.near").text.split())
        assert "and 1 more wallets in this group" in t                                       # a display cap with its "and N more", never a data cap


def test_the_dossier_shows_the_buyer_links_of_its_launches(linked):
    from rugyomi import dossier as DS
    DS.rebuild(linked)
    linked.con.commit()
    path = linked.con.execute("PRAGMA database_list").fetchone()[2]
    owner = owner_of(linked, ISLAND)
    d = DS.dossier(linked.con, owner)
    row = next(t for t in d["timeline"] if t["token"] == ISLAND)
    assert row["early_buyers"] == {"bundle": 2, "dev_side": 1}                                  # two wallets are a pair, not a group (RGY-031)
    with TestClient(webapp.create_app(path)) as c:
        t = " ".join(c.get(f"/creator/{owner}").text.split())
        assert "Early buyers" in t and "dev side 1" in t and "bundle 2" in t and "group " not in t.split("bundle 2", 1)[1][:20]


def test_a_database_without_the_tables_still_serves_the_pages(tmp_path):
    p = tmp_path / "n.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a.near','a.near','1',1,1,'x')")
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(p)) as c:
        r = c.get("/launch/a.nearlytrade.near")
        assert r.status_code == 200 and "Early buyers" not in r.text


# ------------------------------------------------------------------------------------------------ CLI and the collector hook
def test_the_cli_prints_the_counts_and_the_hook_waits_for_the_first_manual_build(replay, tmp_path):
    p = tmp_path / "c.db"
    shutil.copy(replay, p)
    s = open_store(p)
    log = []
    assert BU.refresh(s, log.append) is None and s.con.execute("SELECT COUNT(*) FROM launch_buyers").fetchone()[0] == 0     # never the initial backlog
    s.close()
    assert collector_main(["rebuild-buyer-links", "--db", str(p), "--no-links"]) == 0
    s = open_store(p)
    assert s.con.execute("SELECT COUNT(*) FROM launch_buyers").fetchone()[0] == 5 and BU.status(s) is None
    s.close()
    assert collector_main(["rebuild-buyer-links", "--db", str(p)]) == 0
    s = open_store(p)
    assert BU.status(s)["launches_with_buyers"] == 3
    # after the first build the hook keeps the tables current: a new launch is picked up when the build is old enough
    last = s.con.execute("SELECT MAX(ts_ns) FROM facts").fetchone()[0]
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('later.nearlytrade.near','nearly','n','n','1',1,?,'x')", (last - 4000 * NS,))
    s.con.commit()
    assert BU.refresh(s, log.append, now=1e12) is not None
    assert s.con.execute("SELECT COUNT(*) FROM launch_buyers_done").fetchone()[0] == 4
    s.close()
