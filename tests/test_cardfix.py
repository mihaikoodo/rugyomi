"""RGY-056 (F-058): cards issued before the RGY-054 fix that the fixed code scores higher on the creator's links are LISTED (computed, never typed) and noted on the launch page;
the issued card, the accuracy numbers and the channel are not touched. The list is derived: issued before the fix's deploy time, only the fix's signals compared, only N > 0."""
import json
from pathlib import Path

import pytest

from test_rules import HOUR, MIN, T0, Db
from test_rules_v1 import outcome
from rugyomi import cardfix as F
from rugyomi import links as K
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.store import open_store

V1 = C.effective(None, "v1")
NOW = "new.nearlytrade.near"
FIX = F.FIXES["creator_links"]
BEFORE = "2026-11-11T20:00:00.000000Z"
AFTER = "2026-11-11T22:00:00.000000Z"
PKG = Path(F.__file__).resolve().parent


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


def network_and_new_creator(db, token=NOW, creator="new.near", t=T0):
    """A rugging network whose link tables were built before the new creator existed; the new creator is funded by a member before his launch."""
    if not db.s.con.execute("SELECT 1 FROM launches WHERE token = 'old0.nearlytrade.near'").fetchone():
        for i, who in enumerate(("g1.near", "g2.near")):
            lt = T0 - (30 - i) * HOUR
            db.launch(f"old{i}.nearlytrade.near", who, lt, coverage="launch_only")
            outcome(db, f"old{i}.nearlytrade.near", lt, rug=True, decided=("fast", "late"))
        db.fund("g1.near", "g2.near", 41, T0 - 40 * HOUR)
        K.rebuild_links(db.s, V1)
    db.fund("g2.near", creator, 5, t - 5 * MIN)
    db.launch(token, creator, t)


def issue(db, token, issued_at, attach=False, cfg=V1, tamper=None):
    """Store a card as the collector does (`issue_card`), with the old path (attach False = before RGY-054) and a chosen issue time."""
    c = rules.compute_card(db.s, token, cfg, attach=attach)
    if tamper:
        tamper(c)
    db.s.con.execute(
        "INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, config_version, config_hash, rules_version, computed_ms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (token, issued_at, 1, c["launch_ts_ns"], c["score"], c["band"], json.dumps(c["reasons"]), json.dumps(c["signals"]), c["dev_account"],
         c["config_version"], c["config_hash"], c["rules_version"], 0))
    return c


def track(c):
    return next(r for r in c["signals"]["results"] if r["id"] == "creator_track_record")


@pytest.mark.core
def test_a_card_issued_before_the_fix_without_the_creators_links_is_listed_with_the_missing_points(db):
    network_and_new_creator(db)
    old = issue(db, NOW, BEFORE)
    assert not track(old)["fired"]                                                        # the card as issued before RGY-054: no group
    K.rebuild_links(db.s, V1)                                                              # the tables now know the new creator
    want = track(rules.compute_card(db.s, NOW, V1))["contribution"]
    assert want > 0
    res = F.rebuild(db.s)
    assert [r["token"] for r in res["rows"]] == [NOW] and res["checked"] == 1 and not res["unmatched_config"]
    row = F.card_fix(db.s.con, NOW)
    assert row["points"] == want and row["score"] == min(100, old["score"] + want) and row["issued_score"] == old["score"]
    assert row["band"] == rules.band_of(row["score"], V1) and row["config_match"] == 1 and row["profile"] == "v1"
    assert [s["id"] for s in row["signals"]] == ["creator_track_record"] and row["signals"][0]["issued"] == 0
    assert f"would have added {want} points (score {row['score']}, band {row['band']})" in row["text"] and "2026-11-11" in row["text"]
    stored = rules.card_row(db.s.con, NOW)                                                 # the issued card is unchanged
    assert stored["score"] == old["score"] and not track(stored)["fired"]


@pytest.mark.core
def test_a_card_issued_after_the_fix_is_never_listed_even_if_it_differs(db):
    network_and_new_creator(db)
    issue(db, NOW, AFTER)                                                                  # the same stale card, issued after the deploy time: not this fix
    K.rebuild_links(db.s, V1)
    res = F.rebuild(db.s)
    assert res["rows"] == [] and res["checked"] == 0 and F.card_fix(db.s.con, NOW) is None


@pytest.mark.core
def test_only_the_fixs_signals_count_and_only_upward(db):
    network_and_new_creator(db)
    K.rebuild_links(db.s, V1)                                                               # the issued card already had the group (= as of now): nothing missing

    def other_signal_lower(c):                                                             # a signal outside the fix differs (data loaded later): not this fix
        for r in c["signals"]["results"]:
            if r["id"] not in FIX["signals"] and r["fired"]:
                r["contribution"] += 7
    issue(db, NOW, BEFORE, tamper=other_signal_lower)
    network_and_new_creator(db, "two.nearlytrade.near", "two.near", T0 + HOUR)
    K.rebuild_links(db.s, V1)

    def higher_than_now(c):                                                                # the issued card had MORE on a fix signal than today: reported, never listed
        track(c)["contribution"] += 5
    issue(db, "two.nearlytrade.near", BEFORE, tamper=higher_than_now)
    res = F.rebuild(db.s)
    assert res["rows"] == [] and res["lower"] == [("two.nearlytrade.near", -5)] and res["checked"] == 2


@pytest.mark.core
def test_the_rebuild_replaces_the_list_and_writes_only_its_table(db):
    network_and_new_creator(db)
    issue(db, NOW, BEFORE)
    K.rebuild_links(db.s, V1)
    cards = db.s.con.execute("SELECT * FROM cards").fetchall()
    F.rebuild(db.s)
    F.rebuild(db.s)
    assert db.s.con.execute("SELECT COUNT(*) FROM card_fixes").fetchone()[0] == 1
    assert db.s.con.execute("SELECT * FROM cards").fetchall() == cards


@pytest.mark.core
def test_a_card_with_a_config_the_code_does_not_know_is_reported(db):
    network_and_new_creator(db)
    issue(db, NOW, BEFORE, cfg=C.effective({"signals": {"creator_track_record": {"points_high": 41}}}, "v1"))
    K.rebuild_links(db.s, V1)
    res = F.compute(db.s)
    assert res["unmatched_config"] == [NOW] and res["rows"][0]["config_match"] == 0


@pytest.mark.core
def test_the_v21_migration_is_additive_and_an_existing_v20_database_opens(tmp_path):
    from rugyomi.store.schema import MIGRATIONS
    mig = MIGRATIONS[20]
    assert "card_fixes" in mig and not any(w in mig.upper() for w in ("DROP ", "ALTER ", "DELETE ", "UPDATE ", "INSERT "))
    p = tmp_path / "v20.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    s.con.execute("DROP TABLE card_fixes")
    s.con.execute("PRAGMA user_version=20")
    s.con.commit()
    s.close()
    s = open_store(p)
    assert s.user_version() >= 21 and s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
    assert F.card_fix(s.con, "a.nearlytrade.near") is None and F.summary(s.con) is None and F.rebuild(s)["rows"] == []
    s.close()


@pytest.mark.core
def test_an_existing_database_without_the_table_reads_none(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    open_store(p).close()
    con = sqlite3.connect(p)
    con.execute("DROP TABLE card_fixes")
    assert F.card_fix(con, NOW) is None and F.summary(con) is None
    with pytest.raises(RuntimeError):
        F.rebuild(type("S", (), {"con": con})())


@pytest.mark.core
def test_the_list_is_read_by_the_site_only_never_by_the_channel_the_accuracy_numbers_or_the_rules():
    for rel in ("publisher.py", "rules/accuracy.py", "rules/context.py", "rules/card.py", "rules/signals.py", "rules/features.py", "ledger.py"):
        src = (PKG / rel).read_text(encoding="utf-8")
        assert "card_fixes" not in src and "cardfix" not in src, rel
    assert "card_fixes" in (PKG / "web" / "queries.py").read_text(encoding="utf-8")


def test_the_launch_page_shows_the_note_and_accuracy_keeps_the_issued_cards(db, tmp_path):
    from fastapi.testclient import TestClient
    from rugyomi.web import app as webapp
    from rugyomi.web import queries as Q
    network_and_new_creator(db)
    issue(db, NOW, BEFORE)
    K.rebuild_links(db.s, V1)
    db.s.con.commit()
    acc_before = {k: v for k, v in Q.accuracy(db.s.con).items() if k not in ("card_fixes", "market")}       # market: as of now
    F.rebuild(db.s)
    acc = Q.accuracy(db.s.con)
    assert {k: v for k, v in acc.items() if k not in ("card_fixes", "market")} == acc_before                 # the accuracy numbers are the issued cards
    assert acc["card_fixes"]["n"] == 1 and acc["card_fixes"]["rows"][0]["token"] == NOW
    db.s.con.commit()
    with TestClient(webapp.create_app(db.s.path if hasattr(db.s, "path") else tmp_path / "x.db")) as c:
        r = c.get(f"/launch/{NOW}")
        assert r.status_code == 200 and 'id="card-fix"' in r.text and "Issued before a fix of creator links" in r.text
        assert r.text.index("Risk score:") < r.text.index('id="card-fix"')
        a = c.get("/accuracy")
        assert a.status_code == 200 and acc["card_fixes"]["text"] in a.text                                  # DSN-002: the line of RGY-056 is on the page, with its list
        assert f'<a href="/launch/{NOW}">{NOW}</a> (' in a.text and "→" in a.text
        assert c.get("/launch/old0.nearlytrade.near").status_code == 200
