"""RGY-027: retroactive as-of cards (`rugyomi.retro`, table `retro_cards`, schema v15).

What is pinned: the in-sample flag comes from `fit.train_until` of the v1 config; a retro card is the SAME `compute_card` as the backtest's as-of card
(difference 0) and does not see the future; empty / populated / resumable / re-versioned rebuilds; a migration is additive; retro rows are never
read by the publisher, the live accuracy, the rules context or the live cards (source scans + behaviour); the pages show a retro card only with its mark
and banner."""
import json
import re
import sqlite3
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import retro as RC  # noqa: E402
from rugyomi import rules  # noqa: E402
from rugyomi.rules import backtest as BT  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.rules import context as CX  # noqa: E402
from rugyomi.rules import stored  # noqa: E402
from rugyomi.rules.accuracy import card_outcome_rows  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.store.schema import MIGRATIONS  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from test_rules import DAY, HOUR, MIN, T0, Db  # noqa: E402
from test_rules_v1 import DRAFT, drop_future, future_world  # noqa: E402

import rugyomi  # noqa: E402

PKG = Path(rugyomi.__file__).resolve().parent
V1 = C.load_config(profile="v1")
MECH = C.effective(DRAFT, "v1")       # the mechanics tests of RGY-021 use the draft weights (the fitted ones may switch a signal off)
SMALL = {"signals": {k: v for k, v in DRAFT["signals"].items() if k in ("creator_tax", "creator_track_record", "funded_by_rugger", "name_copy")}}


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "retro.db")
    yield d
    d.s.close()


def rows(s):
    return s.con.execute("SELECT token, score, band, in_sample, config_version FROM retro_cards ORDER BY token").fetchall()


def world(db):
    future_world(db)
    db.s.con.commit() if db.s.con.in_transaction else None
    return db.s


# ------------------------------------------------------------------------------------------------ config: train_until
@pytest.mark.core
def test_v1_ships_the_end_of_its_fit_and_it_is_not_in_the_hash():
    assert V1["fit"]["train_until"] == "2026-10-20"
    assert C.train_until_label() == "2026-10-20"
    assert C.train_until_ns(V1) == 1792454400 * 10**9                       # 2026-10-20 00:00 UTC
    other = json.loads(json.dumps(V1))
    other["fit"]["train_until"] = "2026-11-01"
    assert C.config_hash(other) == C.config_hash(V1)                        # a label of the rows, not a number of the rules
    assert C.train_until_ns(C.load_config(profile="v0")) is None            # v0 has no fit
    other["fit"]["train_until"] = "yesterday"
    with pytest.raises(C.ConfigError):
        C.train_until_ns(other)


def test_the_diagnostics_take_their_default_split_from_the_config():
    root = PKG.parent.parent / "private" / "deploy" / "diag"
    if not root.exists():
        pytest.skip("private diagnostics are not in this checkout")
    for name in ("backtest", "publish_backtest", "fit_v1"):
        src = (root / f"{name}.py").read_text()
        assert "train_until_label()" in src and 'default="2026-10-20"' not in src, name


@pytest.mark.core
def test_in_sample_comes_from_the_config_not_from_a_constant(db):
    s = world(db)
    cut = C.train_until_ns(V1)
    db.s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, coverage) "
                     "VALUES('old.nearlytrade.near','nearly','o.near','o.near','1',1,?, 'x','launch_only')", (cut - HOUR,))
    db.s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, coverage) "
                     "VALUES('edge.nearlytrade.near','nearly','e.near','e.near','1',2,?, 'x','launch_only')", (cut,))
    db.s.con.commit()
    RC.rebuild_retro_cards(s, V1)
    got = {t: ins for t, _, _, ins, _ in rows(s)}
    assert got["old.nearlytrade.near"] == 1 and got["edge.nearlytrade.near"] == 0       # the boundary belongs to the holdout, like the backtest split
    moved = json.loads(json.dumps(V1))
    moved["fit"]["train_until"] = "2099-01-01"
    RC.rebuild_retro_cards(s, moved, force=True)
    assert {ins for _, _, _, ins, _ in rows(s)} == {1}
    nofit = json.loads(json.dumps(V1))
    del nofit["fit"]
    RC.rebuild_retro_cards(s, nofit, force=True)
    assert {ins for _, _, _, ins, _ in rows(s)} == {0}


# ------------------------------------------------------------------------------------------------ rebuild
@pytest.mark.core
def test_an_empty_database_gives_an_empty_result(tmp_path):
    s = open_store(tmp_path / "e.db")
    res = RC.rebuild_retro_cards(s, V1)
    assert res["launches_without_live_card"] == 0 and res["written"] == 0 and res["failed"] == 0 and res["total_rows"] == 0
    assert RC.retro_row(s, "nope") is None and RC.retro_scores(s, ["nope"]) == {}
    s.close()


@pytest.mark.core
def test_every_launch_without_a_live_card_gets_one_and_a_launch_with_a_card_does_not(db):
    s = world(db)
    n = s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    rules.issue_card(s, "now.nearlytrade.near", 1, V1) if hasattr(rules, "issue_card") else None
    from rugyomi.rules.card import issue_card
    s.begin()
    issue_card(s, "now.nearlytrade.near", 1, V1)
    s.commit()
    res = RC.rebuild_retro_cards(s, V1)
    cards = s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    assert cards == 1 and res["launches_without_live_card"] == n - cards and res["written"] == n - cards and res["failed"] == 0
    got = {r[0] for r in rows(s)}
    assert "now.nearlytrade.near" not in got and len(got) == n - 1
    assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1          # `cards` is untouched


@pytest.mark.core
def test_a_retro_card_is_the_same_computation_as_the_backtest_as_of_card(db):
    s = world(db)
    stored.rebuild_outcomes(s, cfg=V1)
    s.con.commit() if s.con.in_transaction else None
    RC.rebuild_retro_cards(s, V1)
    final = BT.asof_rows(s, {"v1": V1})
    assert len(final) >= 1
    mine = {r[0]: (r[1], r[2]) for r in rows(s)}
    diff = [r["token"] for r in final if mine[r["token"]] != (r["cards"]["v1"]["score"], r["cards"]["v1"]["band"])]
    assert diff == []
    for t in mine:                                                                     # and the full card, reasons and signals, equals compute_card
        c = rules.compute_card(s, t, V1)
        r = RC.retro_row(s, t)
        assert (r["score"], r["band"], [(x["id"], x["contribution"]) for x in r["reasons"]]) == (c["score"], c["band"], [(x["id"], x["contribution"]) for x in c["reasons"]])
        assert r["signals"] == json.loads(json.dumps(c["signals"])) and r["retro"] is True


@pytest.mark.core
def test_no_lookahead_the_retro_card_equals_the_one_on_a_database_cut_at_the_launch(db, tmp_path):
    s = world(db)
    RC.rebuild_retro_cards(s, V1, only=["now.nearlytrade.near"])
    full = RC.retro_row(s, "now.nearlytrade.near")
    assert full["score"] > 0
    s2 = drop_future(s.con, tmp_path / "cut.db", T0)
    try:
        RC.rebuild_retro_cards(s2, V1, only=["now.nearlytrade.near"], force=True)
        cut = RC.retro_row(s2, "now.nearlytrade.near")
    finally:
        s2.close()
    assert (cut["score"], cut["band"], cut["reasons"], cut["signals"]) == (full["score"], full["band"], full["reasons"], full["signals"])


@pytest.mark.core
def test_the_no_lookahead_check_is_not_blind_to_a_leak_in_the_retro_path(db, tmp_path, monkeypatch):
    s = world(db)

    def leaky(self, token):                                                            # the mutation of test_rules_v1: read the answers whatever their cutoff
        from rugyomi.rules.rug import rug_labels
        from rugyomi.rules.stored import SNAP_COLS
        snaps = [dict(zip(SNAP_COLS, r)) for r in self._store.con.execute(
            f"SELECT {','.join(SNAP_COLS)} FROM launch_snapshots WHERE token = ? AND minute IN (60, 1440) ORDER BY minute", (token,))]
        if not snaps:
            return {"rug": None, "fast": None, "late": None, "known_ns": None}
        lab = rug_labels(snaps, self.config)
        return {"rug": lab["rug"]["value"], "fast": lab["rug_fast"]["value"], "late": lab["rug_late"]["value"], "known_ns": 0}

    monkeypatch.setattr(CX.LaunchContext, "outcome", leaky)
    RC.rebuild_retro_cards(s, MECH, only=["now.nearlytrade.near"])
    full = RC.retro_row(s, "now.nearlytrade.near")
    s2 = drop_future(s.con, tmp_path / "cut2.db", T0)
    try:
        RC.rebuild_retro_cards(s2, MECH, only=["now.nearlytrade.near"], force=True)
        cut = RC.retro_row(s2, "now.nearlytrade.near")
    finally:
        s2.close()
    assert (cut["signals"]["results"], cut["reasons"]) != (full["signals"]["results"], full["reasons"]), "the leak must change the card, or the check is blind"


@pytest.mark.core
def test_rebuild_is_resumable_and_follows_the_config_version(db):
    s = world(db)
    n = len(RC.eligible(s))
    first = RC.rebuild_retro_cards(s, V1, limit=2)
    assert first["written"] == 2 and first["kept"] == 0
    second = RC.rebuild_retro_cards(s, V1)
    assert second["kept"] == 2 and second["written"] == n - 2 and second["total_rows"] == n
    third = RC.rebuild_retro_cards(s, V1)
    assert third["written"] == 0 and third["kept"] == n
    # a new config version replaces the rows: one row per token, never two versions side by side
    newer = json.loads(json.dumps(V1))
    newer["version"] = "v1-fit-2"
    newer["signals"]["name_copy"]["points"] = 7
    again = RC.rebuild_retro_cards(s, newer)
    assert again["written"] == n and again["total_rows"] == n
    assert {r[4] for r in rows(s)} == {"v1-fit-2"}


@pytest.mark.core
def test_a_failing_card_is_listed_and_does_not_stop_the_rest(db, monkeypatch):
    s = world(db)
    real = RC.compute_card

    def boom(store, token, cfg=None):
        if token == "p0.nearlytrade.near":
            raise RuntimeError("boom")
        return real(store, token, cfg)

    monkeypatch.setattr(RC, "compute_card", boom)
    res = RC.rebuild_retro_cards(s, V1)
    assert res["failed"] == 1 and res["failed_tokens"][0].startswith("p0.nearlytrade.near: RuntimeError") and res["written"] == len(RC.eligible(s)) - 1
    assert "p0.nearlytrade.near" not in {r[0] for r in rows(s)}


@pytest.mark.core
def test_a_launch_that_gets_a_live_card_loses_its_retro_row(db):
    s = world(db)
    RC.rebuild_retro_cards(s, V1)
    assert "now.nearlytrade.near" in {r[0] for r in rows(s)}
    from rugyomi.rules.card import issue_card
    s.begin()
    issue_card(s, "now.nearlytrade.near", 1, V1)
    s.commit()
    res = RC.rebuild_retro_cards(s, V1)
    assert res["pruned"] == 1 and "now.nearlytrade.near" not in {r[0] for r in rows(s)}


@pytest.mark.core
def test_the_cli_prints_the_counts(tmp_path, capsys):
    from rugyomi.collector import main
    p = tmp_path / "cli.db"
    d = Db(p)
    d.launch("h.nearlytrade.near", "h.near", T0, coverage="launch_only")
    d.close()
    assert main(["rebuild-retro-cards", "--db", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["launches_without_live_card"] == 1 and out["written"] == 1 and out["failed"] == 0 and "seconds" in out
    assert main(["rebuild-retro-cards", "--db", str(p)]) == 0
    assert json.loads(capsys.readouterr().out)["kept"] == 1


# ------------------------------------------------------------------------------------------------ migration
@pytest.mark.core
def test_the_migration_is_additive_and_an_old_database_opens(tmp_path):
    assert "retro_cards" in MIGRATIONS[14] and len(MIGRATIONS) >= 15
    assert "DROP" not in MIGRATIONS[14].upper() and "ALTER" not in MIGRATIONS[14].upper()
    p = tmp_path / "old.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    for t in ("retro_cards", "launch_buyers", "launch_buyers_done", "buyer_links", "buyer_groups", "buyer_group_members", "buyer_wallets", "buyer_pairs", "buyer_pair_launches",
              "launch_holdings", "launch_holdings_done", "signal_features", "launch_live_state", "launch_live_state_at", "card_fixes"):     # the later migrations' tables
        s.con.execute(f"DROP TABLE {t}")
    s.con.execute("PRAGMA user_version=14")
    s.con.commit()
    s.close()
    s = open_store(p)                                                                   # migrates 14 -> 18
    assert s.user_version() >= 15
    assert s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1 and s.con.execute("SELECT COUNT(*) FROM retro_cards").fetchone()[0] == 0
    s.close()


# ------------------------------------------------------------------------------------------------ never read as a forecast
@pytest.mark.core
def test_retro_rows_are_read_only_by_the_retro_module_and_the_site():
    src = {p.relative_to(PKG).as_posix(): p.read_text() for p in PKG.rglob("*.py")}
    assert {k for k, v in src.items() if re.search(r"(?<![A-Za-z_])retro_cards", v)} == {"retro.py", "store/schema.py"}
    allowed = {"retro.py", "collector.py", "dossier.py", "web/queries.py", "web/app.py", "store/schema.py",
               "web/api.py", "web/badge.py",
               "risk_snapshot.py"}        # RGY-067: the API and the badge name the kind "retro" (a card computed afterwards); they read it only through the pages' functions. RGY-069: the score file reads it only through retro.retro_row
    assert {k for k, v in src.items() if re.search(r"\bretro\b|retro_mod|import retro|RC\.", v)} <= allowed | {"rules/config.py"}, "a new reader of retro cards"
    for forbidden in ("publisher.py", "rules/accuracy.py", "rules/context.py", "rules/card.py", "rules/signals.py", "rules/rug.py", "rules/stored.py",
                      "rules/backtest.py", "rules/__init__.py", "snapshots.py", "links.py"):
        assert "retro" not in src[forbidden].lower(), forbidden


@pytest.mark.core
def test_the_live_accuracy_and_the_live_context_ignore_retro_rows(db):
    s = world(db)
    stored.rebuild_outcomes(s, cfg=V1)
    s.con.commit() if s.con.in_transaction else None
    before = (card_outcome_rows(s.con), rules.compute_card(s, "now.nearlytrade.near", V1)["score"])
    RC.rebuild_retro_cards(s, V1)
    assert s.con.execute("SELECT COUNT(*) FROM retro_cards").fetchone()[0] > 0
    assert card_outcome_rows(s.con) == before[0] == []                                  # no live card -> no row in the live accuracy
    assert rules.compute_card(s, "now.nearlytrade.near", V1)["score"] == before[1]       # the live rules context does not see them
    assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0


@pytest.mark.core
def test_the_publisher_never_posts_a_retro_row(db):
    from rugyomi import publisher
    s = world(db)
    RC.rebuild_retro_cards(s, V1)
    s.con.commit() if s.con.in_transaction else None
    src = (PKG / "publisher.py").read_text()
    assert "retro" not in src
    sql = re.findall(r"con\.execute\(\s*\"([^\"]*cards[^\"]*)\"", src)
    assert sql and all("retro" not in q for q in sql)
    assert hasattr(publisher, "__name__")


# ------------------------------------------------------------------------------------------------ pages
@pytest.fixture
def pages(tmp_path):
    from test_links import page_world
    w = page_world(tmp_path)
    res = RC.rebuild_retro_cards(w.s, V1, only=["poohoot.nearlytrade.near", "t0-a.nearlytrade.near"])
    assert res["written"] == 2
    w.s.con.execute("DELETE FROM launch_outcomes")                                     # the fixture's outcome rows carry no label evidence (not what these tests are about)
    w.s.con.execute("UPDATE retro_cards SET in_sample = 1 WHERE token = 't0-a.nearlytrade.near'")
    w.s.con.commit()
    return w


def test_the_launch_page_shows_the_retro_card_with_banner_tag_and_no_rug_chance(pages):
    with TestClient(webapp.create_app(pages.path)) as c:
        r = c.get("/launch/poohoot.nearlytrade.near")
        t = " ".join(r.text.split())
        assert r.status_code == 200
        assert "computed retroactively as of the launch time (rules v1)" in t and "not a forecast published at the time" in t
        assert re.search(r"Risk score: <span class=\"band band-\w+\">\w+</span> <strong>\d+</strong> / 100 <span class=\"tag retro\"", " ".join(r.text.split()))
        assert "No risk card" not in t and "ug chance" not in t and "issued" not in t and "never changes" not in t
        assert "In-sample" not in t                                                 # launched after the fit ended
        t2 = " ".join(c.get("/launch/t0-a.nearlytrade.near").text.split())
        assert "In-sample" in t2 and "not an out-of-sample result" in t2
        # a launch with no retro row still says there is no card
        assert "No risk card" in " ".join(c.get("/launch/ponzinear.nearlytrade.near").text.split()) or c.get("/launch/ponzinear.nearlytrade.near").status_code == 404


def test_a_live_card_is_shown_as_before_without_any_retro_mark(pages):
    with TestClient(webapp.create_app(pages.path)) as c:
        t = " ".join(c.get("/launch/t1-b.nearlytrade.near").text.split())
        assert "issued" in t and "retro" not in t.lower() and "retroactively" not in t


def test_lists_dossier_and_cluster_members_mark_the_retro_score(pages):
    with TestClient(webapp.create_app(pages.path)) as c:
        cl = c.get("/cluster/0a51b81e.near").text
        assert 'class="tag retro"' in cl and "forecast" not in re.sub(r"not a forecast published at the time", "", cl)
        assert 'class="tag retro"' in c.get("/creator/poohoot.near").text
        # the score of a live card keeps no mark
        t = c.get("/creator/b.near").text
        assert "65 High" in t and t.count('class="tag retro"') == t.count("retro</span>")
    from rugyomi.web import queries
    con = sqlite3.connect(f"file:{pages.path}?mode=ro", uri=True)
    m = {x["launch_token"]: x for x in queries.cluster(con, "0a51b81e.near")["members"]}
    assert m["poohoot.nearlytrade.near"]["retro"] is True and m["poohoot.nearlytrade.near"]["score"] is not None
    con.close()


def test_the_feed_marks_a_live_launch_that_has_only_a_retro_card(tmp_path):
    from rugyomi.web import queries
    d = Db(tmp_path / "f.db")
    d.launch("live.nearlytrade.near", "l.near", T0)
    d.s.con.commit()
    RC.rebuild_retro_cards(d.s, V1)
    rows_ = queries.feed(d.s.con)
    assert rows_[0]["retro"] is True and rows_[0]["score"] is not None
    d.s.close()
    with TestClient(webapp.create_app(tmp_path / "f.db")) as c:
        assert 'class="tag retro"' in c.get("/").text


@pytest.mark.core
def test_the_retro_rebuild_from_the_stored_measures_writes_the_rows_the_computed_rebuild_writes(tmp_path):
    from rugyomi.rules import features as FT
    results = []
    for use_store in (False, True):
        d = Db(tmp_path / f"r{int(use_store)}.db")
        s = world(d)
        if use_store:
            FT.fill(s, [V1])
        out = RC.rebuild_retro_cards(s, V1, use_store=use_store)
        results.append((out, s.con.execute("SELECT token, score, band, reasons, signals, notes, in_sample FROM retro_cards ORDER BY token").fetchall()))
        s.close()
    (o0, r0), (o1, r1) = results
    assert r0 and r0 == r1
    assert o0["from_store"] == 0 and o1["from_store"] == o1["written"] > 0 and o1["stale_rows"] == 0
