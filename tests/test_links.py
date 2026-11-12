"""RGY-019: links between launch participants, linked accounts, funding clusters, rankings; the tables, the collector hook,
the migration and the pages. Synthetic databases built from facts (the real replay gives the 29.10 pair)."""
import json
import sqlite3
import threading

import pytest

import rugyomi_testkit  # noqa: F401  (puts the package on sys.path)
from rugyomi import links as K
from rugyomi.parse import Fact
from rugyomi.rules import load_config
from rugyomi.store import open_store
from rugyomi.store.db import hash_str

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi.web import app as webapp  # noqa: E402

NEAR = 10**24
T0 = 1_793_300_000 * 10**9
MIN = 60 * 10**9
DAY = 24 * 60 * MIN
tx_counter = [0]


def txid(n=None):
    tx_counter[0] += 1
    return rugyomi_testkit.hid("tx", tx_counter[0])


class World:
    """A database with launches, funding facts and history rows written directly."""

    def __init__(self, path):
        self.s = open_store(path)
        self.path = path
        self.n = 0

    def launch(self, token, creator, owner=None, at=T0, coverage=None, pool=None, launchpad="nearly"):
        self.s.con.execute(
            "INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, extra, coverage, pool) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (token, launchpad, creator, owner or creator, "0", at // 10**9 % 10**9, at, "2026-11-05T00:00:00.000000Z", None,
             coverage, pool))

    def fund(self, src, dst, near, at, kind="near_transfer", tx=None):
        self.n += 1
        tx = tx or txid()
        self.s.insert_facts([Fact(kind, at // 10**9 % 10**9, at, tx, rugyomi_testkit.hid("r", self.n), "a", src, dst, None,
                                  str(int(near * NEAR)), None)])
        return tx

    def create(self, parent, child, at, signer=None):
        self.n += 1
        tx = txid()
        self.s.insert_facts([Fact("create_account", at // 10**9 % 10**9, at, tx, rugyomi_testkit.hid("r", self.n), "a", child,
                                  parent, None, None, {"signer": signer or parent})])
        return tx

    def ft(self, token, src, dst, amount, at):
        self.n += 1
        tx = txid()
        self.s.insert_facts([Fact("ft_transfer", at // 10**9 % 10**9, at, tx, rugyomi_testkit.hid("r", self.n), "a", src, dst,
                                  token, str(amount), None)])
        return tx

    def hist_funding(self, account, funder, near, at, tx="HISTTX", token="near"):
        self.s.con.execute("INSERT INTO hist_fundings(account, ts_ns, kind, funder, intents_account, token, amount, tx_hash, "
                           "dup_count, source, imported_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (account, at, "near", funder, None, token, str(int(near * NEAR)), tx, 1, "legacy_import", "x"))

    def hist_launch(self, token, creator, at):
        self.s.con.execute("INSERT INTO hist_launches(token, launchpad, creator, height, ts_ns, source, imported_at) "
                           "VALUES(?,?,?,?,?,?,?)", (token, "nearly", creator, 1, at, "legacy_import", "x"))

    def build(self, **over):
        cfg = load_config()
        for k, v in over.items():
            sec, key = k.split("__")
            cfg[sec][key] = v
        return K.rebuild_links(self.s, cfg)

    def rows(self, table, cols="*", where=""):
        return self.s.con.execute(f"SELECT {cols} FROM {table} {where}").fetchall()


@pytest.fixture
def w(tmp_path):
    x = World(tmp_path / "l.db")
    yield x
    x.s.close()


def chain(w, names, start=T0):
    """names[i] funds names[i+1] 41 NEAR 2 minutes before the latter's launch; every name has one launch."""
    for i, a in enumerate(names):
        at = start + i * 10 * MIN
        w.launch(f"t{i}-{a.split('.')[0]}.nearlytrade.near", a, at=at)
        if i:
            w.fund(names[i - 1], a, 41, at - 2 * MIN)


# ------------------------------------------------------------------------------------------- linked accounts
@pytest.mark.core
def test_a_chain_is_one_group_without_the_launch_bot(w):
    names = [f"acc{i:02d}.near" for i in range(40)]
    # every launch goes through the bot: creator = launchbot, the real owner is the participant
    for i, a in enumerate(names):
        at = T0 + i * 10 * MIN
        w.launch(f"t{i}.nearlytrade.near", "launchbot.nearlytrade.near", owner=a, at=at)
        w.fund("launchbot.nearlytrade.near", a, 5, at - 3 * MIN)              # the bot paying out: a service, never a link
        if i:
            w.fund(names[i - 1], a, 41, at - 2 * MIN)
    meta = w.build()
    assert meta["participants"] == 40 and meta["groups"] == 1 and meta["grouped_accounts"] == 40
    g = w.rows("link_groups")[0]
    assert g[0] == "acc00.near" and g[1] == 40 and g[2] == 0 and g[3] == 40
    members = {r[0] for r in w.rows("link_members", "account")}
    assert members == set(names) and "launchbot.nearlytrade.near" not in members
    edges = w.rows("link_edges", "type, src, dst, tx, amount, source, group_id")
    assert len(edges) == 39 and all(e[0] == "funded" and e[3] and e[5] == "facts" and e[6] == "acc00.near" for e in edges)
    assert all("launchbot" not in e[1] + e[2] for e in edges)
    assert w.rows("link_clusters") == []


@pytest.mark.core
def test_service_accounts_are_never_endpoints(w):
    w.launch("a.nearlytrade.near", "a.near", at=T0 + 10 * MIN)
    w.launch("b.nearlytrade.near", "b.near", at=T0 + 20 * MIN)
    for svc in ("dclv2.ref-labs.near", "wrap.near", "intents.near", "near", "launchbot.nearlytrade.near"):
        w.fund(svc, "a.near", 10, T0)
        w.fund(svc, "b.near", 10, T0)
    w.build()
    assert w.rows("link_edges") == [] and w.rows("link_clusters") == [] and w.rows("link_members") == []


@pytest.mark.core
def test_created_and_token_transfer_and_history_edges(w):
    w.launch("x.nearlytrade.near", "dev.near", at=T0 + 30 * MIN, pool="pool.near")
    w.launch("y.nearlytrade.near", "friend.near", at=T0 + 40 * MIN)
    w.launch("z.nearlytrade.near", "kid.near", at=T0 + 50 * MIN)
    w.launch("h.nearlytrade.near", "old.near", at=T0 + 60 * MIN, coverage="launch_only")
    tx_tt = w.ft("x.nearlytrade.near", "dev.near", "friend.near", 1000, T0 + 35 * MIN)
    w.ft("x.nearlytrade.near", "dev.near", "pool.near", 500, T0 + 36 * MIN)              # not a participant: no edge
    w.ft("x.nearlytrade.near", "dev.near", "dclv2.ref-labs.near", 500, T0 + 36 * MIN)    # service: no edge
    tx_cr = w.create("friend.near", "kid.near", T0 + 5 * MIN, signer="friend.near")
    w.hist_funding("old.near", "kid.near", 3, T0 + 55 * MIN, tx="HIST1")
    w.hist_launch("hh.nearlytrade.near", "old.near", T0 + 56 * MIN)
    w.build()
    es = {(e[0], e[1], e[2]): e for e in w.rows("link_edges", "type, src, dst, tx, amount, source, token")}
    assert set(es) == {("token_transfer", "dev.near", "friend.near"), ("created", "friend.near", "kid.near"),
                       ("funded", "kid.near", "old.near")}
    assert es[("token_transfer", "dev.near", "friend.near")][3] == tx_tt and es[("token_transfer", "dev.near", "friend.near")][6] == "x.nearlytrade.near"
    assert es[("created", "friend.near", "kid.near")][3] == tx_cr
    h = es[("funded", "kid.near", "old.near")]
    assert h[3] == "HIST1" and h[5] == "history" and int(h[4]) == 3 * NEAR
    g = w.rows("link_groups")
    assert len(g) == 1 and g[0][1] == 4                          # dev, friend, kid, old — one chain of four


@pytest.mark.core
def test_a_service_account_recorded_as_creator_never_becomes_an_edge_end(w):
    """Server crash 05.11: a launch whose recorded creator is a service (nearlytrade.near) and whose owner differs; the
    service account sent tokens to a participant."""
    w.launch("s.nearlytrade.near", "nearlytrade.near", owner="real.near", at=T0 + 10 * MIN)
    w.launch("p.nearlytrade.near", "friend.near", at=T0 + 20 * MIN)
    w.ft("s.nearlytrade.near", "nearlytrade.near", "friend.near", 5, T0 + 12 * MIN)
    w.ft("s.nearlytrade.near", "real.near", "friend.near", 7, T0 + 13 * MIN)
    w.build()
    es = w.rows("link_edges", "type, src, dst")
    assert es == [("token_transfer", "real.near", "friend.near")]
    assert "nearlytrade.near" not in {r[0] for r in w.rows("link_members", "account")}


@pytest.mark.core
def test_the_same_transaction_in_facts_and_history_is_one_edge(w):
    w.launch("a.nearlytrade.near", "a.near", at=T0)
    w.launch("b.nearlytrade.near", "b.near", at=T0 + 10 * MIN)
    tx = w.fund("a.near", "b.near", 5, T0 + 5 * MIN)
    w.hist_funding("b.near", "a.near", 5, T0 + 5 * MIN, tx=tx)
    w.build()
    es = w.rows("link_edges", "source")
    assert es == [("facts",)]


@pytest.mark.core
def test_a_funding_below_the_minimum_is_not_a_link(w):
    w.launch("a.nearlytrade.near", "a.near", at=T0)
    w.launch("b.nearlytrade.near", "b.near", at=T0 + 10 * MIN)
    w.fund("a.near", "b.near", 0.2, T0 + 5 * MIN)
    assert w.build()["edges"] == 0
    assert w.build(links__min_near=0.1)["edges"] == 1


@pytest.mark.core
def test_the_size_cap_marks_a_group_truncated(w):
    chain(w, [f"c{i}.near" for i in range(6)])
    meta = w.build(links__group_max=3)
    assert meta["groups"] == 1 and meta["truncated_groups"] == 1 and meta["grouped_accounts"] == 3
    g = w.rows("link_groups")[0]
    assert g[1] == 6 and g[2] == 1
    assert {r[0] for r in w.rows("link_members", "account")} == {"c0.near", "c1.near", "c2.near"}   # the earliest launches
    assert len(w.rows("link_edges")) == 5                                                         # no edge is lost


@pytest.mark.core
def test_a_creator_of_a_launch_only_history_row_is_a_participant(w):
    w.hist_launch("p.nearlytrade.near", "p.near", T0)
    w.hist_launch("q.nearlytrade.near", "q.near", T0 + 30 * MIN)
    w.fund("p.near", "q.near", 10, T0 + 10 * MIN)
    meta = w.build()
    assert meta["participants"] == 2 and meta["groups"] == 1


# ------------------------------------------------------------------------------------------- clusters
def fanout(w, src, creators, extra_recipients=0, at=T0, funded_before=2 * MIN, near=10):
    for i, c in enumerate(creators):
        t = at + i * 30 * MIN
        w.launch(f"{c.split('.')[0]}.nearlytrade.near", c, at=t)
        w.fund(src, c, near, t - funded_before)
    for i in range(extra_recipients):
        w.fund(src, f"stranger{i}.near", near, at - 5 * MIN)


@pytest.mark.core
def test_a_narrow_source_is_a_cluster(w):
    fanout(w, "0a51b81e.near", ["poohoot.near", "ponzinear.near", "dragonslayer.near"])
    w.build()
    c = w.rows("link_clusters")
    assert len(c) == 1 and c[0][0] == "0a51b81e.near" and c[0][1] == 3 and c[0][2] == 3
    m = w.rows("link_cluster_members", "account, amount, seconds_before, launch_token, edge_source, tx", "ORDER BY account")
    assert [x[0] for x in m] == ["dragonslayer.near", "ponzinear.near", "poohoot.near"]
    assert all(x[2] == 120 and x[4] == "facts" and x[5] for x in m) and int(m[0][1]) == 10 * NEAR
    assert w.rows("link_groups") == []                                       # a cluster is not a linked group


@pytest.mark.core
def test_a_wide_source_is_not_a_cluster(w):
    fanout(w, "exchange.near", ["a.near", "b.near", "c.near"], extra_recipients=12)
    w.build()
    assert w.rows("link_clusters") == []
    w.build(clusters__narrow_max=100)
    assert len(w.rows("link_clusters")) == 1


@pytest.mark.core
def test_a_source_that_is_itself_a_creator_gives_linked_accounts_not_a_cluster(w):
    fanout(w, "boss.near", ["a.near", "b.near"])
    w.launch("boss.nearlytrade.near", "boss.near", at=T0 - 10 * MIN)
    w.build()
    assert w.rows("link_clusters") == []
    g = w.rows("link_groups")
    assert len(g) == 1 and g[0][1] == 3


@pytest.mark.core
def test_the_window_and_the_order_matter(w):
    # funded 8 days before the launch: outside the 7-day window; funded AFTER the launch: not "before its launch"
    w.launch("a.nearlytrade.near", "a.near", at=T0 + 9 * DAY)
    w.fund("src.near", "a.near", 10, T0)
    w.launch("b.nearlytrade.near", "b.near", at=T0)
    w.fund("src.near", "b.near", 10, T0 + MIN)
    w.launch("c.nearlytrade.near", "c.near", at=T0 + DAY)
    w.fund("src.near", "c.near", 10, T0 + DAY - MIN)
    w.build()
    assert w.rows("link_clusters") == []                                   # only c qualifies: one creator
    w.build(clusters__window_days=10)
    m = w.rows("link_cluster_members", "account", "ORDER BY account")
    assert m == [("a.near",), ("c.near",)]                                 # a qualifies with the wider window; b never does


@pytest.mark.core
def test_two_creators_within_a_wider_window_make_a_cluster(w):
    w.launch("a.nearlytrade.near", "a.near", at=T0 + 9 * DAY)
    w.fund("src.near", "a.near", 10, T0)
    w.launch("c.nearlytrade.near", "c.near", at=T0 + DAY)
    w.fund("src.near", "c.near", 10, T0 + DAY - MIN)
    w.build()
    assert w.rows("link_clusters") == []
    w.build(clusters__window_days=10)
    assert len(w.rows("link_clusters")) == 1


@pytest.mark.core
def test_one_creator_is_not_a_cluster_and_a_service_source_never_is(w):
    fanout(w, "alone.near", ["a.near"])
    fanout(w, "dclv2.ref-labs.near", ["b.near", "c.near"], at=T0 + 3 * DAY)
    fanout(w, "intents.near", ["d.near", "e.near"], at=T0 + 4 * DAY)
    w.build()
    assert w.rows("link_clusters") == []


@pytest.mark.core
def test_history_fundings_make_a_cluster_too(w):
    for i, c in enumerate(["nearlick.near", "dragons_market.near"]):
        t = T0 + i * 20 * MIN
        w.hist_launch(f"{c.split('.')[0]}.nearlytrade.near", c, t)
        w.hist_funding(c, "d312.near", 3, t - 4 * MIN, tx=f"H{i}")
    w.build()
    c = w.rows("link_clusters", "source, creators")
    assert c == [("d312.near", 2)]
    m = w.rows("link_cluster_members", "edge_source, tx", "ORDER BY account")
    assert m == [("history", "H1"), ("history", "H0")] or sorted(m) == [("history", "H0"), ("history", "H1")]


@pytest.mark.core
def test_cluster_members_keep_the_funding_closest_before_the_launch(w):
    fanout(w, "src.near", ["a.near", "b.near"])
    w.fund("src.near", "a.near", 7, T0 - 30 * MIN)                          # an earlier, second funding of a
    w.build()
    m = w.rows("link_cluster_members", "account, fundings, amount, seconds_before", "ORDER BY account")
    assert m[0][0] == "a.near" and m[0][1] == 2 and int(m[0][2]) == 10 * NEAR and m[0][3] == 120


# ------------------------------------------------------------------------------------------- build properties
@pytest.mark.core
def test_rebuild_is_idempotent_and_never_touches_facts(w):
    chain(w, ["a.near", "b.near", "c.near"])
    fanout(w, "s.near", ["x.near", "y.near"], at=T0 + 2 * DAY)
    facts = w.rows("facts", "COUNT(*)")
    snap = lambda: {t: sorted(w.rows(t)) for t in ("link_edges", "link_groups", "link_members", "link_clusters", "link_cluster_members")}  # noqa: E731
    w.build()
    one = snap()
    w.build()
    two = snap()
    assert one == two and one["link_groups"] and one["link_clusters"]
    assert w.rows("facts", "COUNT(*)") == facts
    assert w.s.get_status()["links_built"]["groups"] == 1


@pytest.mark.core
def test_a_new_edge_changes_the_tables_on_the_next_build(w):
    chain(w, ["a.near", "b.near"])
    w.build()
    assert len(w.rows("link_groups")) == 1 and w.rows("link_groups")[0][1] == 2
    w.launch("t9.nearlytrade.near", "z.near", at=T0 + 99 * MIN)
    w.fund("b.near", "z.near", 9, T0 + 95 * MIN)
    w.build()
    assert w.rows("link_groups")[0][1] == 3


@pytest.mark.core
def test_the_card_config_hash_ignores_the_link_sections():
    from rugyomi.rules import config_hash, links_config_hash
    a = load_config()
    b = load_config()
    b["links"]["min_near"] = 7
    b["clusters"]["narrow_max"] = 99
    assert config_hash(a) == config_hash(b) and links_config_hash(a) != links_config_hash(b)
    c = load_config()
    c["signals"]["creator_tax"]["points"] += 1
    assert config_hash(c) != config_hash(a)


@pytest.mark.core
def test_migration_v10_adds_the_tables_and_keeps_every_old_row(tmp_path):
    from rugyomi.store.schema import MIGRATIONS, SCHEMA_VERSION
    assert SCHEMA_VERSION >= 10
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    for stmt in MIGRATIONS[:9]:
        con.executescript(stmt)
    con.execute("PRAGMA user_version=9")
    con.execute("INSERT INTO launches(token, launchpad, height, ts_ns, detected_at) VALUES('old.near','nearly',1,2,'x')")
    con.execute("INSERT INTO launch_outcomes(token, evidence, config_version, config_hash, computed_at) VALUES('old.near','{}','v','h','x')")
    con.commit()
    con.close()
    s = open_store(p)
    try:
        assert s.user_version() == SCHEMA_VERSION
        assert s.con.execute("SELECT token FROM launches").fetchall() == [("old.near",)]
        assert s.con.execute("SELECT token FROM launch_outcomes").fetchall() == [("old.near",)]
        for t in ("link_edges", "link_groups", "link_members", "link_clusters", "link_cluster_members"):
            assert s.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
        assert K.rebuild_links(s)["participants"] == 0
    finally:
        s.close()


@pytest.mark.core
def test_rebuild_refuses_an_old_schema(tmp_path):
    p = tmp_path / "v9.db"
    s = open_store(p)
    s.con.execute("PRAGMA user_version=9")
    with pytest.raises(RuntimeError):
        K.rebuild_links(s)
    assert K.refresh(s, lambda m: None) is None


@pytest.mark.core
def test_only_the_link_code_writes_the_link_tables():
    from pathlib import Path
    pkg = Path(K.__file__).parent
    writers = set()
    for p in pkg.rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        if "INTO link_" in t or "DELETE FROM {t}" in t:
            writers.add(p.relative_to(pkg).as_posix())
    assert writers == {"links.py"}


# ------------------------------------------------------------------------------------------- collector hook
@pytest.mark.core
def test_refresh_builds_when_due_and_remembers_failures(w, monkeypatch):
    log = []
    chain(w, ["a.near", "b.near"])
    meta = K.refresh(w.s, log.append)
    assert meta and meta["groups"] == 1 and any("links:" in m for m in log)
    assert K.refresh(w.s, log.append) is None                                # nothing new, the config is the same
    w.launch("n.nearlytrade.near", "n.near", at=T0 + 5 * DAY)
    w.s.con.execute("UPDATE launches SET detected_at = '2099-01-01T00:00:00.000000Z' WHERE token = 'n.nearlytrade.near'")
    assert K.refresh(w.s, log.append, now=_after(w.s, 10)) is None            # too soon after the last build (gap)
    again = K.refresh(w.s, log.append, now=_after(w.s, K.REFRESH_MIN_GAP_S + 5))
    assert again and again["participants"] == 3
    monkeypatch.setattr(K, "rebuild_links", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert K.refresh(w.s, log.append, force=True) is None
    assert "boom" in json.dumps(w.s.get_status().get("links_last_error")) and any("boom" in m for m in log)


def _after(store, seconds):
    import datetime as dt
    at = store.get_status()["links_built"]["at"]
    return dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() + seconds


def test_collector_tick_rebuilds_and_status_reports_it(tmp_path):
    from rugyomi.collector import Collector, status
    from rugyomi.keys import EnvKeySource
    from rugyomi.neardata import Neardata
    s = open_store(tmp_path / "c.db")
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in rugyomi_testkit.refrax_replay_blocks():
        c.process(h, b)
    assert s.get_status().get("links_built") is None            # process() never builds: the loop does, between blocks
    c._links_tick(force=True)
    st = status(s)
    assert st["links"]["built"]["groups"] == 1 and st["links"]["last_error"] is None
    # the 29.10 pair: coinisland.near funded thecoins.near 40.97 NEAR, one linked group, with the transaction
    e = s.con.execute("SELECT type, src, dst, amount, tx, source FROM link_edges").fetchall()
    assert len(e) == 1 and e[0][:3] == ("funded", "coinisland.near", "thecoins.near") and e[0][4] and e[0][5] == "facts"
    assert abs(int(e[0][3]) / NEAR - 40.97) < 0.01
    s.close()


@pytest.mark.core
def test_cli_rebuild_links(tmp_path, capsys):
    from rugyomi.collector import main
    p = tmp_path / "cli.db"
    open_store(p).close()
    assert main(["rebuild-links", "--db", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["participants"] == 0 and out["groups"] == 0 and "compute_s" in out


# ------------------------------------------------------------------------------------------- pages
def page_world(tmp_path):
    w = World(tmp_path / "p.db")
    chain(w, ["a.near", "b.near", "c.near"])
    fanout(w, "0a51b81e.near", ["poohoot.near", "ponzinear.near"], at=T0 + 3 * DAY)
    # outcomes: b's launch is a rug with buyers' losses; a card exists for it
    for tok, pnl, rug, sev, fin in (("t1-b.nearlytrade.near", -3 * NEAR, 1, 3.0, 1), ("t0-a.nearlytrade.near", 1 * NEAR, 0, None, 1),
                                    ("poohoot.nearlytrade.near", -2 * NEAR, 1, 2.0, 1)):
        w.s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, buyers_pnl) "
                        "VALUES(?,60,1,2,'x',?)", (tok, str(pnl)))
        w.s.con.execute("INSERT INTO launch_outcomes(token, rug, severity_near, evidence, config_version, config_hash, computed_at, final) "
                        "VALUES(?,?,?,'{}','v0','h','x',?)", (tok, rug, sev, fin))
    w.s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, "
                    "config_version, config_hash, rules_version, computed_ms) VALUES('t1-b.nearlytrade.near','x',1,1,65,'High','[]','{}',"
                    "NULL,'v0','h','0.1',1)")
    w.build()
    return w


def test_creator_page_shows_linked_accounts_with_transactions_and_launches(tmp_path):
    w = page_world(tmp_path)
    tx = w.rows("link_edges", "tx", "WHERE src = 'a.near' AND dst = 'b.near'")[0][0]
    with TestClient(webapp.create_app(w.path)) as c:
        r = c.get("/creator/b.near")
        assert r.status_code == 200
        t = " ".join(r.text.split())
        assert "Linked accounts" in t and "/creator/a.near" in t and "/creator/c.near" in t
        assert "sent 41.00 NEAR to" in t and f"nearblocks.io/txns/{tx}" in r.text
        assert "not that one person runs them" in t
        assert "65 High" in t and "<strong>rug</strong>" in r.text                # b's own launch: its card and its stored outcome
        assert "Funding clusters" not in t                                    # b belongs to no cluster
        r = c.get("/creator/poohoot.near")
        assert "Funding clusters" in r.text and "/cluster/0a51b81e.near" in r.text
        assert "No linked accounts found" in r.text
        r = c.get("/creator/0a51b81e.near")
        assert r.status_code == 200 and "itself the funding source" in r.text


def test_cluster_page_and_rankings(tmp_path):
    w = page_world(tmp_path)
    with TestClient(webapp.create_app(w.path)) as c:
        r = c.get("/cluster/0a51b81e.near")
        assert r.status_code == 200
        t = " ".join(r.text.split())
        assert "poohoot.near" in t and "ponzinear.near" in t and "2.0 min" in t
        assert "they do not say who controls the accounts" in t
        assert "nearblocks.io/txns/" in r.text and "-2.00 NEAR" in t
        assert c.get("/cluster/nobody.near").status_code == 404
        assert c.get("/cluster/A%20B").status_code in (404, 422)
        r = c.get("/rankings")
        assert r.status_code == 200
        t = " ".join(r.text.split())
        assert "Outcomes exist only for launches we watched live" in t
        assert "3.00 NEAR" in t and "2.00 NEAR" in t                     # the group a-b-c lost 3, the cluster lost 2 (rug severity 2)
        assert "No creator has three launches" in t


@pytest.mark.core
def test_clean_creators_need_three_final_launches_without_a_rug(tmp_path):
    w = World(tmp_path / "cl.db")
    for i in range(3):
        tok = f"k{i}.nearlytrade.near"
        w.launch(tok, "clean.near", at=T0 + i * MIN)
        w.s.con.execute("INSERT INTO launch_outcomes(token, rug, evidence, config_version, config_hash, computed_at, final) "
                        "VALUES(?,0,'{}','v','h','x',1)", (tok,))
    for i in range(3):
        tok = f"m{i}.nearlytrade.near"
        w.launch(tok, "mixed.near", at=T0 + i * MIN)
        w.s.con.execute("INSERT INTO launch_outcomes(token, rug, evidence, config_version, config_hash, computed_at, final) "
                        "VALUES(?,?,'{}','v','h','x',1)", (tok, 1 if i == 0 else 0))
    for i in range(2):
        tok = f"f{i}.nearlytrade.near"
        w.launch(tok, "few.near", at=T0 + i * MIN)
        w.s.con.execute("INSERT INTO launch_outcomes(token, rug, evidence, config_version, config_hash, computed_at, final) "
                        "VALUES(?,0,'{}','v','h','x',1)", (tok,))
    w.build()
    r = K.rankings(w.s)
    assert [c["account"] for c in r["clean"]] == ["clean.near"] and r["clean"][0]["launches"] == 3


def test_pages_on_a_database_before_v10_and_with_empty_tables(tmp_path):
    p = tmp_path / "v9.db"
    from rugyomi.store.schema import MIGRATIONS
    con = sqlite3.connect(p)
    for stmt in MIGRATIONS[:9]:
        con.executescript(stmt)
    con.execute("PRAGMA user_version=9")
    con.execute("INSERT INTO launches(token, launchpad, creator, owner, height, ts_ns, detected_at) "
                "VALUES('x.nearlytrade.near','nearly','dev.near','dev.near',1,2,'x')")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        r = c.get("/creator/dev.near")
        assert r.status_code == 200 and "Links are not computed on this database yet" in r.text
        assert c.get("/rankings").status_code == 200 and "Not computed yet" in c.get("/rankings").text
        assert c.get("/cluster/x.near").status_code == 404
    w = World(tmp_path / "e.db")
    w.launch("x.nearlytrade.near", "dev.near")
    w.build()
    with TestClient(webapp.create_app(w.path)) as c:
        r = c.get("/creator/dev.near")
        assert "No linked accounts found" in r.text
        assert "Nothing to rank yet" in c.get("/rankings").text


def test_methodology_describes_links_from_the_config(tmp_path):
    w = World(tmp_path / "m.db")
    with TestClient(webapp.create_app(w.path)) as c:
        t = " ".join(c.get("/methodology").text.split())
        assert "Links between accounts" in t and "we never say that two accounts are one person" in t
        assert "less than 7 days before each of them launched" in t and "at most 10 accounts" in t
        assert "never by a name pattern" in t and "Linked accounts are not a cluster" in t
