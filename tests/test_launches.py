"""Launch detection: REFRAX reference case through the full pipeline, launchbot owner, justhoot v2, rebuild."""
import json
import threading

import pytest

from rugyomi_testkit import block, fixture_blocks, hid, reo
from rugyomi import launches as L
from rugyomi.collector import Collector, status
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.schema import SCHEMA_VERSION
from rugyomi.store.db import hash_bytes, hash_str


def collector(store):
    return Collector(store, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()),
                     log=lambda m: None)


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("facts", "accounts", "kinds", "blocks", "parse_errors", "launches")}


@pytest.fixture(scope="module")
def refrax(tmp_path_factory):
    db = tmp_path_factory.mktemp("refrax") / "r.db"
    s = open_store(db)
    c = collector(s)
    blocks = fixture_blocks("refrax")
    assert len(blocks) == 32
    for h, b in blocks:
        c.process(h, b)
    yield s, c, blocks, db
    s.close()


def test_refrax_funding_fact(refrax):
    s = refrax[0]
    co, th = (s.lookup_account(x) for x in ("coinisland.near", "thecoins.near"))
    rows = s.con.execute("SELECT amount, tx, height FROM facts WHERE kind=? AND a=? AND b=?",
                         (s.lookup_kind("near_transfer"), co, th)).fetchall()
    assert len(rows) == 1
    amount, tx, height = rows[0]
    assert amount == "40968801074051790053810260" and height == 222136374
    assert hash_str(tx) == "3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG"


def test_refrax_launch_rows(refrax):
    s = refrax[0]
    rows = {r[0]: r for r in s.con.execute("SELECT token, launchpad, creator, owner, dev_buy, tx, owner_evidence "
                                           "FROM launches")}
    r = rows["refrax.nearlytrade.near"]
    assert r[1:6] == ("nearly", "thecoins.near", "thecoins.near", "40000000000000000000000000",
                      "CDKRkF5Xk8ExTYzdV9hNu6fYhygqzBxpecysbhnz9K2B")
    assert r[6] is None
    for tok in ("island.nearlytrade.near", "coin-2.nearlytrade.near"):
        assert rows[tok][2] == "coinisland.near" and rows[tok][3] == "coinisland.near"
    assert all(not t.startswith("lock_") for t in rows)


def test_refrax_rerun_adds_nothing_and_db_reopens(refrax):
    s, c, blocks, db = refrax
    before = counts(s.con)
    for h, b in blocks:
        c.process(h, b)
    assert counts(s.con) == before
    s2 = open_store(db)
    assert s2.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s2.close()


@pytest.mark.core
def test_rebuild_launches_gives_the_same_table(refrax):
    s = refrax[0]
    q = "SELECT token, launchpad, creator, owner, owner_evidence, pool, dev_buy, height, ts_ns, tx FROM launches ORDER BY token"
    before = s.con.execute(q).fetchall()
    n = L.rebuild_launches(s)
    assert n == len(before) and s.con.execute(q).fetchall() == before


@pytest.mark.core
def test_status_offline(refrax):
    s = refrax[0]
    st = status(s, now_ns=1793333000 * 10**9)
    assert st["last_height"] == 222137219 and st["parse_errors"] == 0
    assert st["facts_total"] > 0 and st["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------- launchbot real owner

NEARLY, LB = "nearlytrade.near", "launchbot.nearlytrade.near"
NEAR = 10**24


def launch_block(h, token, dep, tx):
    ev = "EVENT_JSON:" + json.dumps({"standard": "nearly", "event": "launch_started", "data": [
        {"creator": LB, "token": token, "pool_id": f"{token}|wrap.near|10000", "dev_buy": str(dep), "symbol": "X"}]})
    return block(h, [reo(hid("rl", h), tx, LB, NEARLY,
                         [{"FunctionCall": {"method_name": "launch", "deposit": str(dep), "args": ""}}],
                         logs=[ev], executor=NEARLY)])


def pay_block(h, owner, amount):
    return block(h, [reo(hid("rp", h), hid("tp", h), owner, LB, [{"Transfer": {"deposit": str(amount)}}])])


@pytest.mark.core
def test_launchbot_owner_is_the_payer_and_evidence_is_not_reused(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    dep = 3 * NEAR
    c.process(1000, pay_block(1000, "early.near", dep + 2 * 10**22))          # older payment, same amount
    c.process(1010, pay_block(1010, "owner.near", dep + 2 * 10**22 + 5 * 10**20))  # within ±0.001 NEAR
    c.process(1020, launch_block(1020, "aaa.nearlytrade.near", dep, hid("tl", 1)))
    c.process(1030, launch_block(1030, "bbb.nearlytrade.near", dep, hid("tl", 2)))
    c.process(1040, launch_block(1040, "ccc.nearlytrade.near", dep, hid("tl", 3)))
    rows = {r[0]: r[1:] for r in s.con.execute("SELECT token, creator, owner, owner_evidence, extra FROM launches")}
    assert rows["aaa.nearlytrade.near"][:3] == (LB, "owner.near", hid("rp", 1010))     # the newest matching payment
    assert rows["bbb.nearlytrade.near"][:3] == (LB, "early.near", hid("rp", 1000))     # the next unused one
    ccc = rows["ccc.nearlytrade.near"]
    assert ccc[1] is None and json.loads(ccc[3])["owner_unresolved"] is True           # never launchbot itself
    # outside the 150-block window
    c.process(1500, pay_block(1500, "late.near", dep + 2 * 10**22))
    c.process(1700, launch_block(1700, "ddd.nearlytrade.near", dep, hid("tl", 4)))
    assert s.con.execute("SELECT owner FROM launches WHERE token='ddd.nearlytrade.near'").fetchone()[0] is None


@pytest.mark.core
def test_justhoot_v2_creator_from_meta_events(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    h, b = next((h, b) for h, b in fixture_blocks("cache") if h == 220657479)
    c.process(h, b)
    (tok, pad, creator, owner, pool, dev_buy) = s.con.execute(
        "SELECT token, launchpad, creator, owner, pool, dev_buy FROM launches").fetchone()
    assert pad == "justhoot_v2" and tok.endswith(".v2-launchpad.justhoot.near")
    assert creator and owner == creator and dev_buy is not None


@pytest.mark.core
def test_justhoot_meta_after_the_plan_fills_the_creator(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    tok = "zzz.v2-launchpad.justhoot.near"
    plan = "EVENT_JSON:" + json.dumps({"standard": "jh", "event": "launch_planned",
                                       "data": [{"token_id": tok, "plan": {"pool_id": "p", "dev_quote": "7"}}]})
    meta = "EVENT_JSON:" + json.dumps({"standard": "jh", "event": "launch_priced",
                                       "data": [{"token_id": tok, "creator_id": "maker.near"}]})
    j = "v2-launchpad.justhoot.near"
    c.process(1, block(1, [reo(hid("a"), hid("ta"), "maker.near", j, [], logs=[plan], executor=j)]))
    assert s.con.execute("SELECT creator FROM launches").fetchone()[0] is None
    c.process(2, block(2, [reo(hid("b"), hid("ta"), "maker.near", j, [], logs=[meta], executor=j)]))
    assert s.con.execute("SELECT creator, owner FROM launches").fetchone() == ("maker.near", "maker.near")


@pytest.mark.core
def test_a_launchbot_payment_after_the_launch_in_the_same_block_is_not_evidence(tmp_path):
    """RGY-005 #6: inside the launch block only payments stored BEFORE the launch event count."""
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    dep = 3 * NEAR
    tok = "eee.nearlytrade.near"
    launch = launch_block(2000, tok, dep, hid("tl", 9))["shards"][0]["receipt_execution_outcomes"]
    pay_after = pay_block(2000, "after.near", dep + 2 * 10**22)["shards"][0]["receipt_execution_outcomes"]
    c.process(2000, block(2000, launch + pay_after))
    row = s.con.execute("SELECT owner, extra FROM launches WHERE token=?", (tok,)).fetchone()
    assert row[0] is None and json.loads(row[1])["owner_unresolved"] is True
    tok2 = "fff.nearlytrade.near"
    pay_before = pay_block(2010, "before.near", dep + 2 * 10**22)["shards"][0]["receipt_execution_outcomes"]
    launch2 = launch_block(2010, tok2, dep, hid("tl", 10))["shards"][0]["receipt_execution_outcomes"]
    c.process(2010, block(2010, pay_before + launch2))
    assert s.con.execute("SELECT owner FROM launches WHERE token=?", (tok2,)).fetchone()[0] == "before.near"
    # and the rebuild gives the same answer
    L.rebuild_launches(s)
    assert s.con.execute("SELECT owner FROM launches WHERE token=?", (tok,)).fetchone()[0] is None
    assert s.con.execute("SELECT owner FROM launches WHERE token=?", (tok2,)).fetchone()[0] == "before.near"
