"""RGY-023: the creator dossier and the rankings — the derived tables (group totals, money in / out, 1 h / 24 h shares, clean creators)
on a synthetic database, and the pages on an empty and a populated one."""
import sqlite3

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi import dossier as DS
from rugyomi.store.schema import MIGRATIONS
from test_links import DAY, MIN, NEAR, T0, World, chain

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi.web import app as webapp  # noqa: E402

HOUR = 60 * MIN


def snap(w, token, pnl, rug, sev=None, final=1, insiders=None):
    w.s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, buyers_pnl) VALUES(?,60,1,2,'x',?)",
                    (token, str(int(pnl * NEAR))))
    w.s.con.execute("INSERT INTO launch_outcomes(token, rug, severity_near, evidence, config_version, config_hash, computed_at, final, insiders_pnl) "
                    "VALUES(?,?,?,'{}','v0','h','x',?,?)", (token, rug, sev, final, None if insiders is None else str(int(insiders * NEAR))))


def xfer(w, account, d, other, near, at, signer=None):
    w.n += 1
    s = signer if signer is not None else (other if d == "in" else account)
    w.s.con.execute(
        "INSERT INTO transfers_api(account, dir, height, ts_ns, tx, receipt, tindex, other, asset, amount, method, ttype, signer, pred) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (account, d, 1, at, f"TX{w.n}", f"R{w.n}", 0, other, "near", str(int(near * NEAR)), None, None, s, s))


def loaded(w, kind, key, status="done", truncated=0):
    w.s.con.execute("INSERT OR REPLACE INTO backfill_queue(kind, key, status, truncated, queued_at) VALUES(?,?,?,?,'x')", (kind, key, status, truncated))


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path / "d.db")
    chain(w, ["a.near", "b.near", "c.near"])               # one group a-b-c (group key a.near); launches at T0, +10, +20 min
    snap(w, "t0-a.nearlytrade.near", 1, 0, insiders=-1)
    snap(w, "t1-b.nearlytrade.near", -3, 1, sev=3.0, insiders=5)
    w.s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, "
                    "config_version, config_hash, rules_version, computed_ms) VALUES('t1-b.nearlytrade.near','x',1,1,65,'High','[]','{}',"
                    "NULL,'v0','h','0.1',1)")
    # c: the launch has no outcome yet. money in: a funded by a stranger; money out of b after its launch
    xfer(w, "a.near", "in", "funder.near", 20, T0 - HOUR)
    xfer(w, "b.near", "in", "a.near", 41, T0 + 8 * MIN)
    loaded(w, "transfers_in", "a.near")
    loaded(w, "transfers_in", "b.near")
    b_launch = T0 + 10 * MIN
    xfer(w, "b.near", "out", "wallet1.near", 3, b_launch + 30 * MIN)        # within 1 h
    xfer(w, "b.near", "out", "wallet2.near", 1, b_launch + 5 * HOUR)        # within 24 h
    xfer(w, "b.near", "out", "c.near", 2, b_launch + 20 * MIN)             # to a linked account: does not count as leaving
    xfer(w, "b.near", "out", "wallet3.near", 9, b_launch + 30 * HOUR)       # after 24 h
    loaded(w, "transfers_out", "b.near")
    # a rugger with 3 rugs of 4 launches (one is old), a clean creator with 3 final launches
    for i in range(4):
        at = T0 + 2 * DAY + i * HOUR if i else T0 - 20 * DAY
        tok = f"r{i}.nearlytrade.near"
        w.launch(tok, "rugger.near", at=at, launchpad="justhoot_v2" if i == 3 else "nearly")
        snap(w, tok, -2 if i < 3 else 4, 1 if i < 3 else 0, sev=2.0 if i < 3 else None)
    for i in range(3):
        tok = f"k{i}.nearlytrade.near"
        w.launch(tok, "clean.near", at=T0 + 3 * DAY + i * HOUR)
        snap(w, tok, 1, 0)
    w.build()
    DS.rebuild(w.s)
    return w


def test_group_totals_and_headline(world):
    d = DS.dossier(world.s.con, "b.near")
    assert d["built"] and d["gk"] == "a.near" and d["grouped"]
    g, own = d["headline_group"], d["headline_own"]
    assert g["launches"] == 3 and g["decided"] == 2 and g["rugs"] == 1 and g["rug_share"] == 0.5
    assert g["loss_near"] == pytest.approx(3.0)                       # only the negative part of the buyers' result
    assert g["insiders_pnl_near"] == pytest.approx(4.0)               # -1 + 5
    assert own["launches"] == 1 and own["rugs"] == 1                  # b's own numbers next to the group's
    assert [t["token"] for t in d["timeline"]][0] == "t2-c.nearlytrade.near"          # newest first
    c = next(t for t in d["timeline"] if t["account"] == "c.near")
    assert c["final"] == 0 and c["rug"] is None and c["live"] == 1
    b = next(t for t in d["timeline"] if t["account"] == "b.near")
    assert b["score"] == 65 and b["band"] == "High" and b["funded_before_s"] == 120 and b["funder"] == "a.near"


def test_money_in_and_out_with_classes_and_shares(world):
    d = DS.dossier(world.s.con, "a.near")
    ins = {f["peer"]: f for f in d["flows_in"]}
    assert ins["funder.near"]["near"] == pytest.approx(20) and ins["funder.near"]["kind"] == "unknown" and ins["funder.near"]["tx"]
    assert ins["a.near"]["kind"] == "linked" and ins["a.near"]["near"] == pytest.approx(41)
    assert d["group"]["first_funder"] == "funder.near" and d["group"]["in_total_near"] == pytest.approx(61)
    outs = {f["peer"]: f for f in d["flows_out"]}
    assert outs["c.near"]["kind"] == "linked" and outs["wallet1.near"]["kind"] == "unknown"
    assert d["group"]["out_total_near"] == pytest.approx(13)          # the linked 2 NEAR is not counted as leaving
    b = next(t for t in d["timeline"] if t["account"] == "b.near")
    assert b["out_1h_near"] == pytest.approx(3) and b["out_24h_near"] == pytest.approx(4)
    a = next(t for t in d["timeline"] if t["account"] == "a.near")
    assert a["out_1h_near"] is None                                  # outgoing not loaded: unknown, not zero
    g = d["headline_group"]
    assert g["share_1h"] == pytest.approx(3 / 5) and g["share_24h"] == pytest.approx(4 / 5)


def test_bridges_count_as_leaving_dexes_and_the_launchpad_do_not(world):
    b_launch = T0 + 10 * MIN
    xfer(world, "b.near", "out", "intents.near", 1, b_launch + 10 * MIN)
    xfer(world, "b.near", "out", "wrap.near", 50, b_launch + 11 * MIN)
    xfer(world, "b.near", "out", "nearlytrade.near", 60, b_launch + 12 * MIN)
    DS.rebuild(world.s)
    d = DS.dossier(world.s.con, "b.near")
    outs = {f["peer"]: f["kind"] for f in d["flows_out"]}
    assert outs["intents.near"] == "exit" and outs["wrap.near"] == "service" and outs["nearlytrade.near"] == "service"
    b = next(t for t in d["timeline"] if t["account"] == "b.near")
    assert b["out_1h_near"] == pytest.approx(4)                        # 3 unknown + 1 to Intents
    g = d["headline_group"]
    assert g["share_24h"] <= 1.0 and g["share_1h"] <= 1.0              # capped per launch at the insiders' result


def test_a_cut_off_outgoing_load_does_not_invent_zeros(world):
    loaded(world, "transfers_out", "b.near", status="partial", truncated=1)
    world.s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, height, ts_ns, detected_at) "
                        "VALUES('old.nearlytrade.near','nearly','b.near','b.near',1,?,'x')", (T0 - 3 * DAY,))
    world.s.con.commit()
    DS.rebuild(world.s)
    d = DS.dossier(world.s.con, "b.near")
    old = next(t for t in d["timeline"] if t["token"] == "old.nearlytrade.near")
    assert old["out_1h_near"] is None                                # before the oldest loaded row


def test_rankings_views_windows_and_clean(world):
    now = T0 + 4 * DAY
    r = DS.rankings(world.s.con, "all", None, now_ns=now)
    assert r["built"]
    assert r["by_loss"][0]["key"] == "rugger.near" and r["by_loss"][0]["loss"] == pytest.approx(6.0)     # 3 rugs x 2
    assert [x["key"] for x in r["by_loss"]] == ["rugger.near", "a.near"]
    assert r["by_rugs"][0]["key"] == "rugger.near" and r["by_rugs"][0]["rugs"] == 3
    assert [x["key"] for x in r["clean"]] == ["clean.near"] and r["clean"][0]["decided"] == 3     # a-b-c has a rug; clean.near has none
    w7 = DS.rankings(world.s.con, "7d", None, now_ns=now)
    assert next(x for x in w7["by_rugs"] if x["key"] == "rugger.near")["rugs"] == 2                # the old one is out of the window
    pad = DS.rankings(world.s.con, "all", "justhoot_v2", now_ns=now)
    assert [x["key"] for x in pad["by_loss"]] == [] and pad["clean"] == []                          # the justhoot launch is no rug
    assert DS.rankings(world.s.con, "30d", None, now_ns=now + 40 * DAY)["by_loss"] == []


def test_refresh_is_due_when_never_built_and_after_changes(world):
    log = []
    assert DS.refresh(world.s, log.append) is None                     # just built, nothing new
    world.s.con.execute("UPDATE collector_status SET value = replace(value, '\"signature\"', '\"sig_old\"') WHERE key = 'dossier_built'")
    assert DS.refresh(world.s, log.append, force=True)["launches"] >= 10 and any("dossier:" in m for m in log)


def test_pages_on_a_populated_database(world):
    with TestClient(webapp.create_app(world.path)) as c:
        t = " ".join(c.get("/creator/b.near").text.split())
        assert "Dossier" in t and "funder.near" in t and "wallet1.near" in t and "unknown wallet" in t
        assert "not one person" in t or "not that one person" in t
        r = c.get("/rankings")
        assert r.status_code == 200
        t = " ".join(r.text.split())
        assert "rugger.near" in t and "Clean creators" in t and "clean.near" in t and "approximate" in t
        for q in ("?window=7d", "?window=30d&launchpad=nearly&view=rugs", "?window=bogus&launchpad=evil"):
            assert c.get("/rankings" + q).status_code == 200


def test_rankings_page_with_funding_clusters(world):
    # a narrow funding source with two creators who each rugged: the cluster rows have the same columns as the group rows
    for i, name in enumerate(("kc1.near", "kc2.near")):
        at = T0 + 5 * DAY + i * HOUR
        tok = f"kc{i}.nearlytrade.near"
        world.launch(tok, name, at=at)
        world.fund("srcx.near", name, 10, at - 2 * MIN)
        snap(world, tok, -5, 1, sev=5.0)
    world.build()
    DS.rebuild(world.s)
    assert DS.rankings(world.s.con, "all", None)["clusters"]
    with TestClient(webapp.create_app(world.path)) as c:
        r = c.get("/rankings")
        assert r.status_code == 200 and "srcx.near" in r.text
        assert c.get("/rankings?window=7d&view=rugs").status_code == 200


def test_pages_on_an_empty_database(tmp_path):
    w = World(tmp_path / "e.db")
    w.s.close()
    with TestClient(webapp.create_app(w.path)) as c:
        assert c.get("/rankings").status_code == 200
        w2 = World(tmp_path / "e2.db")
        w2.launch("x.nearlytrade.near", "dev.near")
        w2.build()
        DS.rebuild(w2.s)
        w2.s.close()
    with TestClient(webapp.create_app(tmp_path / "e2.db")) as c:
        r = c.get("/creator/dev.near")
        assert r.status_code == 200
        assert c.get("/rankings").status_code == 200


def test_old_databases_and_additive_migration(tmp_path):
    assert MIGRATIONS[13].count("CREATE TABLE") == 3 and "DROP" not in MIGRATIONS[13] and "ALTER" not in MIGRATIONS[13]
    con = sqlite3.connect(tmp_path / "v13.db")
    for stmt in MIGRATIONS[:13]:
        con.executescript(stmt)
    con.execute("PRAGMA user_version=13")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(tmp_path / "v13.db")) as c:
        assert c.get("/rankings").status_code == 200 and c.get("/creator/dev.near").status_code in (200, 404)
    assert DS.dossier(sqlite3.connect(tmp_path / "v13.db"), "x.near") == {"built": False}
