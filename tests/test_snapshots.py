"""RGY-006: launch snapshots — attribution by token legs, dev / buyers / price / holders, hook, rebuild, migration v2.

Real data: fixtures/snapshots.jsonl.gz (trimmed real mainnet blocks of ISLAND, COIN, REFRAX; make_snapshot_fixture.py).
Checks against an INDEPENDENT recount from raw facts (plain SQL over the token legs), not hard-coded totals.
"""
import gzip
import json
import shutil
import sqlite3
import threading

import pytest

from rugyomi_testkit import FIXTURES, block, hid, reo
from rugyomi import snapshots as S
from rugyomi.collector import Collector
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.db import hash_str
from rugyomi.store.schema import MIGRATIONS, SCHEMA_VERSION as S_VERSION

DCL, NEARLY = "dclv2.ref-labs.near", "nearlytrade.near"
REFRAX, ISLAND, COIN = "refrax.nearlytrade.near", "island.nearlytrade.near", "coin-2.nearlytrade.near"
NEAR = 10**24
MIN = 60 * 10**9
FIXTURE_MINUTES = (5, 15, 30, 60)       # the real-block fixture covers 95 minutes: no T+24 h row exists in it


def collector(store):
    return Collector(store, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()),
                     log=lambda m: None)


def fixture_heights():
    for line in gzip.decompress((FIXTURES / "snapshots.jsonl.gz").read_bytes()).splitlines():
        if line.strip():
            d = json.loads(line)
            yield d["height"], d["block"]


def snap(store, token, minute):
    cols = ("token", "minute", "at_ts_ns") + S.COLUMNS + ("metric_version", "computed_at", "error")
    r = store.con.execute(f"SELECT {','.join(cols)} FROM launch_snapshots WHERE token=? AND minute=?",
                          (token, minute)).fetchone()
    return dict(zip(cols, r)) if r else None


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    db = tmp_path_factory.mktemp("snap") / "r.db"
    s = open_store(db)
    c = collector(s)
    for h, b in fixture_heights():
        c.process(h, b)
    yield s, c, db
    s.close()


# ---------------------------------------------------------------- independent recount (plain SQL on raw facts)

def legs(store, token, cutoff, a=None, b=None):
    q = ("SELECT f.amount, f.height, f.tx FROM facts f JOIN kinds k ON k.id=f.kind JOIN accounts t ON t.id=f.token "
         "LEFT JOIN accounts aa ON aa.id=f.a LEFT JOIN accounts bb ON bb.id=f.b "
         "WHERE k.name='ft_transfer' AND t.name=? AND f.ts_ns<=?")
    args = [token, cutoff]
    if a:
        q += " AND aa.name=?"
        args.append(a)
    if b:
        q += " AND bb.name=?"
        args.append(b)
    return [(int(x), h, hash_str(tx)) for x, h, tx in store.con.execute(q, args)]


def launch(store, token):
    return dict(zip(("owner", "ts", "tx"), store.con.execute(
        "SELECT owner, ts_ns, tx FROM launches WHERE token=?", (token,)).fetchone()))


def test_all_reference_snapshots_exist_without_errors(real):
    s = real[0]
    for tok in (REFRAX, ISLAND, COIN):
        L = launch(s, tok)
        covered = s.con.execute("SELECT MAX(block_ts_ns) FROM blocks").fetchone()[0]
        for m in S.MINUTES:
            row = snap(s, tok, m)
            if L["ts"] + m * MIN <= covered:
                assert row is not None and row["error"] is None, (tok, m)
                assert row["at_ts_ns"] == L["ts"] + m * MIN and row["metric_version"] == S.METRIC_VERSION
            else:
                assert row is None, (tok, m)      # not due yet: never computed from an incomplete window
    # the fixture covers 29.10 14:00-15:35 UTC: all four minutes exist for the three reference launches
    assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots WHERE token IN (?,?,?) AND error IS NULL",
                         (REFRAX, ISLAND, COIN)).fetchone()[0] == 12


def test_refrax_dev_acquired_includes_both_buys_by_independent_recount(real):
    s = real[0]
    L = launch(s, REFRAX)
    row = snap(s, REFRAX, 5)
    got = legs(s, REFRAX, row["at_ts_ns"], a=DCL, b="thecoins.near")
    heights = {h for _x, h, _t in got}
    assert 222136935 in heights                                   # the 40 NEAR launch buy's token leg
    assert 222137220 in heights                                   # the later 5 NEAR buy (swap 14:34:44 UTC)
    assert int(row["dev_acquired"]) == sum(x for x, _h, _t in got)
    assert L["owner"] == "thecoins.near"


@pytest.mark.parametrize("minute", FIXTURE_MINUTES)
def test_refrax_first_sell_and_dev_sold_by_independent_recount(real, minute):
    s = real[0]
    row = snap(s, REFRAX, minute)
    assert 33 <= row["dev_first_sell_after_s"] <= 37
    pool_legs = legs(s, REFRAX, row["at_ts_ns"], a="thecoins.near", b=DCL)
    tax_legs = legs(s, REFRAX, row["at_ts_ns"], a="thecoins.near", b=REFRAX)
    assert int(row["dev_sold"]) == sum(x for x, _h, _t in pool_legs + tax_legs)
    # the first sell alone: gross 36 993 654.04 tokens = 36 623 717.50 to the pool + 369 936.54 tax
    first = [x for x, h, _t in pool_legs + tax_legs if h == 222136988]
    assert sum(first) == 36993654035676629802967303
    assert int(row["dev_transferred_out"]) == 0


@pytest.mark.parametrize("minute", FIXTURE_MINUTES)
@pytest.mark.parametrize("token", [ISLAND, COIN])
def test_island_and_coin_dev_pool_sells(real, token, minute):
    s = real[0]
    row = snap(s, token, minute)
    pool = legs(s, token, row["at_ts_ns"], a="coinisland.near", b=DCL)
    tax = legs(s, token, row["at_ts_ns"], a="coinisland.near", b=token)
    assert [x for x, _h, _t in pool] == [36623717495319863504937630]
    assert len(tax) == 1 and tax[0][2] == pool[0][2]               # the 1% tax leg in the same tx
    assert int(row["dev_sold"]) == pool[0][0] + tax[0][0]
    assert row["dev_sold_share_bps"] == int(row["dev_sold"]) * 10000 // int(row["dev_acquired"])


@pytest.mark.parametrize("minute", FIXTURE_MINUTES)
@pytest.mark.parametrize("token", [REFRAX, ISLAND, COIN])
def test_buyers_pnl_by_independent_recount(real, token, minute):  # noqa: PLR0914
    """Buyers recounted from raw legs, without the swap matching: tokens from dclv2 = bought, paid by the
    receiver's wNEAR to dclv2 in that tx; tokens to dclv2 (+ the tax leg in that tx) = sold, NEAR back from dclv2
    in that tx. Dev and service accounts excluded. DCL may leave up to 1 yocto of the input unused in the buyer's
    internal DCL deposit (real case: ISLAND, 22.568 NEAR sent, 22.567999…9 swapped), so spent may differ from the
    swap amounts by at most 1 yocto per buy."""
    from decimal import Decimal
    from rugyomi.store.db import hash_bytes
    s = real[0]
    row = snap(s, token, minute)
    cut = row["at_ts_ns"]
    dev = {launch(s, token)["owner"]}

    def near(kind, a, b, tx):
        return sum(int(x) for (x,) in s.con.execute(
            "SELECT f.amount FROM facts f JOIN kinds k ON k.id=f.kind LEFT JOIN accounts aa ON aa.id=f.a "
            "LEFT JOIN accounts bb ON bb.id=f.b WHERE k.name=? AND aa.name=? AND bb.name=? AND f.tx=? AND f.ts_ns<=?",
            (kind, a, b, hash_bytes(tx), cut)))

    receivers = {r[0] for r in s.con.execute(
        "SELECT DISTINCT bb.name FROM facts f JOIN kinds k ON k.id=f.kind JOIN accounts aa ON aa.id=f.a "
        "JOIN accounts bb ON bb.id=f.b JOIN accounts t ON t.id=f.token "
        "WHERE k.name='ft_transfer' AND aa.name=? AND t.name=? AND f.ts_ns<=?", (DCL, token, cut))}
    buyers = {w for w in receivers if w not in dev and not S.is_service(w, token)}
    bought, sold, spent, received = {}, {}, 0, 0
    for w in buyers:
        for x, _h, tx in legs(s, token, cut, a=DCL, b=w):
            bought[w] = bought.get(w, 0) + x
            spent += near("wnear_transfer", w, DCL, tx)
        for x, _h, tx in legs(s, token, cut, a=w, b=DCL):
            tax = sum(y for y, _h2, t2 in legs(s, token, cut, a=w, b=token) if t2 == tx)
            sold[w] = sold.get(w, 0) + x + tax
            received += near("near_transfer", DCL, w, tx) + near("wnear_transfer", DCL, w, tx)
    unsold = sum(max(0, bought[w] - sold.get(w, 0)) for w in buyers)
    unsold_value = int(unsold * Decimal(row["price_last"])) if row["price_last"] else 0
    assert row["buyers_count"] == len(buyers) > 0
    n_buys = sum(len(legs(s, token, cut, a=DCL, b=w)) for w in buyers)
    assert 0 <= spent - int(row["buyers_spent"]) <= n_buys
    assert int(row["buyers_received"]) == received
    assert int(row["buyers_unsold_value"]) == unsold_value
    assert 0 <= int(row["buyers_pnl"]) - (received + unsold_value - spent) <= n_buys


def test_refrax_later_dev_sell_shows_at_t60(real):
    """The later 5 NEAR lot is sold before T+60: the whole acquisition is sold by then (independent recount above)."""
    s = real[0]
    r5, r60 = snap(s, REFRAX, 5), snap(s, REFRAX, 60)
    assert int(r60["dev_sold"]) > int(r5["dev_sold"]) and r60["dev_acquired"] == r5["dev_acquired"]


def test_rerun_and_rebuild_are_idempotent(real):
    s, c, _db = real
    q = ("SELECT " + ",".join(r[1] for r in s.con.execute("PRAGMA table_info(launch_snapshots)") if r[1] != "computed_at")
         + " FROM launch_snapshots ORDER BY token, minute")
    before = s.con.execute(q).fetchall()
    for h, b in fixture_heights():
        c.process(h, b)                                        # hook: INSERT OR IGNORE, nothing changes
    assert s.con.execute(q).fetchall() == before
    res = S.rebuild_snapshots(s)
    assert res["error"] == 0 and res["ok"] == len(before)
    assert s.con.execute(q).fetchall() == before               # every column but computed_at
    assert S.rebuild_snapshots(s, token=REFRAX)["launches"] == 1


# ---------------------------------------------------------------- synthetic: attribution rules

TOK = "aaa.nearlytrade.near"
POOL = f"{TOK}|wrap.near|10000"
DEV = "dev.near"
T0 = 1_900_000_000 * 10**9


def ev(std, name, data):
    return "EVENT_JSON:" + json.dumps({"standard": std, "event": name, "data": [data]})


def swap_reo(tx, n, swapper, tin, ain, tout, aout):
    return reo(hid("sw", tx, n), tx, swapper, DCL, [], logs=[ev("dcl", "swap", {
        "swapper": swapper, "pool_id": POOL, "token_in": tin, "token_out": tout,
        "amount_in": str(ain), "amount_out": str(aout)})], executor=DCL)


def ft_reo(tx, n, sender, receiver, amount, contract=TOK):
    return reo(hid("ft", tx, n), tx, sender, contract, [], logs=[ev("nep141", "ft_transfer", {
        "old_owner_id": sender, "new_owner_id": receiver, "amount": str(amount)})], executor=contract)


def at(sec):
    return T0 + int(sec * 10**9)


def scenario(extra_blocks=()):
    """A nearly launch by DEV: launch event, a 0->0 swap and the dev buy through lock_3 (legs to DEV + tax),
    a buyer, the dev's sell with tax, a fee inflow from the token contract, an LP-fee leg from dclv2 (no swap)."""
    L = hid("launch")
    launch = reo(hid("le"), L, DEV, NEARLY, [], logs=[ev("nearly", "launch_started", {
        "creator": DEV, "token": TOK, "pool_id": POOL, "dev_buy": str(10 * NEAR)})], executor=NEARLY)
    lock = "lock_3.nearlytrade.near"
    blocks = [
        (100, block(100, [launch], at(0))),
        (101, block(101, [swap_reo(L, 0, lock, TOK, 0, "wrap.near", 0),
                          swap_reo(L, 1, lock, "wrap.near", 10 * NEAR, TOK, 1000)], at(1))),
        (102, block(102, [ft_reo(L, 0, DCL, DEV, 990), ft_reo(L, 1, DCL, TOK, 10)], at(2))),
        # a buyer: 2 NEAR -> 150 tokens (148 net + 2 tax), swapped by a router account
        (103, block(103, [swap_reo(hid("b1"), 0, "router.near", "wrap.near", 2 * NEAR, TOK, 150)], at(10))),
        (104, block(104, [ft_reo(hid("b1"), 0, DCL, "buyer.near", 148), ft_reo(hid("b1"), 1, DCL, TOK, 2)], at(11))),
        # the dev sells 500 to the pool + 5 tax for 4 NEAR
        (105, block(105, [ft_reo(hid("s1"), 0, DEV, DCL, 500), ft_reo(hid("s1"), 1, DEV, TOK, 5)], at(40))),
        (106, block(106, [swap_reo(hid("s1"), 0, DEV, TOK, 500, "wrap.near", 4 * NEAR)], at(41))),
        # fee / tax inflows to the dev: from the token contract (creator_bps > 0) and an LP-fee withdrawal from dclv2
        (107, block(107, [ft_reo(hid("fee"), 0, TOK, DEV, 77), ft_reo(hid("lp"), 0, DCL, DEV, 33)], at(50))),
        # the dev moves tokens to another wallet
        (108, block(108, [ft_reo(hid("mv"), 0, DEV, "alt.near", 100)], at(60))),
        *extra_blocks,
        (200, block(200, [], at(6 * 60))),                     # past T+5
    ]
    return blocks


def run(blocks, tmp_path):
    """Process synthetic blocks like the collector would: the heights between them are stored as null, so every
    snapshot window is covered (the RGY-012 coverage guard)."""
    s = open_store(tmp_path / "s.db")
    c = collector(s)
    prev = None
    for h, b in blocks:
        if prev is not None:
            for g in range(prev + 1, h):
                c.process(g, None)
        c.process(h, b)
        prev = h
    return s


@pytest.mark.core
def test_attribution_rules(tmp_path):
    s = run(scenario(), tmp_path)
    r = snap(s, TOK, 5)
    assert r["error"] is None
    assert r["n_swaps"] == 3                         # the 0 -> 0 swap is not a trade
    assert r["unattributed_swaps"] == 0
    assert r["dev_acquired"] == "990"                # the lock_3 buy lands on the dev; no tax, no fee, no LP fee
    assert r["dev_sold"] == "505"                    # pool leg + tax leg
    assert r["dev_sold_share_bps"] == 505 * 10000 // 990
    assert r["dev_transferred_out"] == "100"
    assert r["dev_first_sell_after_s"] == 41.0
    assert r["buyers_count"] == 1                    # buyer.near — never lock_3, never router.near, never the dev
    assert r["buyers_spent"] == str(2 * NEAR)
    assert r["price_first_buyer"] is not None
    assert r["max_x"] == 1.0                          # the dev sold below the first buyer's price
    # holders: dev 990-505+77+33-100 = 495, buyer 148, alt 100; service accounts (dclv2, token, lock) excluded
    assert r["holders_count"] == 3 and r["top10_share_bps"] == 10000
    t = S.attribute(TOK, *S._rows(s, S._launch(s, TOK), r["at_ts_ns"]))
    assert {x["who"] for x in t} == {DEV, "buyer.near"}


@pytest.mark.core
def test_max_x_from_the_first_buyer(tmp_path):
    later = [(109, block(109, [swap_reo(hid("b2"), 0, "b2.near", "wrap.near", NEAR, TOK, 50)], at(70))),
             (110, block(110, [ft_reo(hid("b2"), 0, DCL, "b2.near", 50)], at(71)))]
    s = run(scenario(later), tmp_path)
    r = snap(s, TOK, 5)
    assert r["buyers_count"] == 2
    assert r["max_x"] == pytest.approx((NEAR / 50) / (2 * NEAR / 150), rel=1e-6)    # 1.5
    assert r["price_last"].startswith("20000000000000000000000")                     # the last trade: 1 NEAR / 50


@pytest.mark.core
def test_justhoot_mint_in_the_launch_tx_is_the_dev_buy(tmp_path):
    tok = "jjj.v2-launchpad.justhoot.near"
    J = "v2-launchpad.justhoot.near"
    L = hid("jl")
    plan = reo(hid("jp"), L, "maker.near", J, [], logs=[
        ev("jh", "launch_priced", {"token_id": tok, "creator_id": "maker.near"}),
        ev("jh", "launch_planned", {"token_id": tok, "plan": {"pool_id": f"{tok}|wrap.near|10000", "dev_quote": "5"}})],
        executor=J)
    mint = reo(hid("jm"), L, J, tok, [], logs=[ev("nep141", "ft_mint", {"owner_id": "maker.near", "amount": "700"})],
               executor=tok)
    later = reo(hid("jm2"), hid("other"), J, tok, [], logs=[ev("nep141", "ft_mint", {"owner_id": "maker.near",
                                                                                    "amount": "9"})], executor=tok)
    s = run([(1, block(1, [plan, mint], at(0))), (2, block(2, [later], at(1))), (3, block(3, [], at(301)))], tmp_path)
    r = snap(s, tok, 5)
    assert r["error"] is None and r["dev_acquired"] == "700"     # a mint outside the launch tx is not a purchase


@pytest.mark.core
def test_hook_once_per_minute_in_catch_up(tmp_path):
    blocks = scenario() + [(300, block(300, [], at(61 * 60)))]   # one block jumps past T+15, T+30 and T+60
    s = run(blocks, tmp_path)
    rows = s.con.execute("SELECT minute, at_ts_ns, error FROM launch_snapshots WHERE token=? ORDER BY minute",
                         (TOK,)).fetchall()
    assert [m for m, _t, _e in rows] == [5, 15, 30, 60] and all(e is None for *_x, e in rows)
    first = s.con.execute("SELECT computed_at FROM launch_snapshots ORDER BY minute").fetchall()
    c = collector(s)
    c.process(301, block(301, [], at(62 * 60)))
    assert s.con.execute("SELECT computed_at FROM launch_snapshots ORDER BY minute").fetchall() == first


def test_a_failing_snapshot_is_a_row_error_and_the_block_still_commits(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "compute", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    s = run(scenario(), tmp_path)
    r = snap(s, TOK, 5)
    assert "boom" in r["error"] and r["dev_sold"] is None
    assert s.con.execute("SELECT status FROM blocks WHERE height=200").fetchone()[0] == "ok"
    monkeypatch.undo()
    assert S.rebuild_snapshots(s)["ok"] == 1 and snap(s, TOK, 5)["error"] is None


# ---------------------------------------------------------------- migration v2 on an existing v1 database

def test_migration_v2_on_a_copy_of_a_v1_db(tmp_path):
    from rugyomi_testkit import fixture_blocks
    v1 = tmp_path / "v1.db"
    con = sqlite3.connect(v1)
    for stmt in [x for x in MIGRATIONS[0].split(";") if x.strip()]:
        con.execute(stmt)
    con.execute("PRAGMA user_version=1")
    con.commit()
    con.close()
    # fill it like the v1 collector did (v1 tables only)
    import rugyomi.store.schema as schema
    orig = schema.SCHEMA_VERSION
    import rugyomi.store.db as dbmod
    dbmod.SCHEMA_VERSION = 1
    try:
        s = open_store(v1)
        c = collector(s)
        import rugyomi.snapshots as snapmod
        save = snapmod.compute_due
        snapmod.compute_due = lambda *a: 0
        try:
            for h, b in fixture_blocks("refrax"):
                c.process(h, b)
        finally:
            snapmod.compute_due = save
        assert s.user_version() == 1
        s.close()
    finally:
        dbmod.SCHEMA_VERSION = orig
    copy = tmp_path / "copy.db"
    shutil.copy(v1, copy)
    s = open_store(copy)
    assert s.user_version() == S_VERSION
    idx = {r[0] for r in s.con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "facts_token_ts" in idx
    assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 0
    assert s.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert S.rebuild_snapshots(s)["error"] == 0
    s.close()
