"""RGY-017: the stored outcome labels (`launch_outcomes`): the collector writes them, everything else reads them."""
import ast
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from rugyomi_testkit import block, refrax_replay_blocks
from rugyomi import rules
from rugyomi import snapshots as S
from rugyomi.collector import Collector, main as collector_main
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.rules import stored
from rugyomi.store import open_store
from test_rug import DEV, NEAR, REFRAX, ISLAND, COIN, at, launch_blocks, rows, sell, world
from test_snapshots import TOK

PKG = Path(rules.__file__).resolve().parents[1]


def collector_for(s):
    return Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)


def table(s):
    return s.con.execute("SELECT token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, "
                         "evidence, config_version, config_hash, final FROM launch_outcomes ORDER BY token").fetchall()


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    s = open_store(tmp_path_factory.mktemp("out") / "r.db")
    c = collector_for(s)
    for h, b in refrax_replay_blocks():
        c.process(h, b)
    yield s
    s.close()


def test_collector_writes_a_row_when_the_t60_snapshot_appears(real):
    got = {r[0]: r for r in table(real)}
    assert set(got) == {REFRAX, ISLAND, COIN}
    r = got[REFRAX]
    assert (r[1], r[2], r[3]) == (1, 0, 1) and r[4] == pytest.approx(2.531, abs=0.01) and r[11] == 0     # not final: no T+24 h
    assert r[9] == "v0-default-1" and len(r[10]) == 12
    assert r[6] == 0 and r[7] == 1                               # the RGY-015 facts: dev_exit is judged at T+30 (REFRAX's dev sold the rest later), buyers_loss at T+60
    assert int(r[5]) / 1e24 == pytest.approx(-0.071, abs=0.001)  # insiders' result at T+60 (F-013)


def test_the_stored_row_equals_the_label_function_on_the_snapshots(real):
    snaps = rows(real, REFRAX)
    want = rules.rug_labels(snaps)
    got = rules.outcome_row(real, REFRAX)
    for k in ("rug_fast", "rug_late", "rug"):
        assert got[k] == want[k]
    assert got["severity_near"] == want["severity_near"] and got["final"] is False
    want_facts = rules.outcome_labels(snaps)
    assert got["outcomes"] == want_facts and want_facts["buyers_loss"]["value"] is True
    assert rules.outcome_row(real, "nope.near") is None


def test_the_row_is_updated_in_place_when_the_t24h_snapshot_appears(tmp_path):
    extra = sell(DEV, 300, 3 * NEAR, 20, "e1") + sell(DEV, 650, NEAR // 100, 3 * 3600, "e2")
    blocks = sorted(launch_blocks() + extra, key=lambda x: x[0])
    last = blocks[-1][0]
    s = open_store(tmp_path / "u.db")
    c = collector_for(s)

    def feed(items):
        for h, b in items:
            c.process(h, b)

    feed(blocks[: len(launch_blocks())])
    # the heights between are null; the hour mark first
    for g in range(blocks[len(launch_blocks()) - 1][0] + 1, blocks[len(launch_blocks())][0]):
        c.process(g, None)
    feed(blocks[len(launch_blocks()):])
    for g in range(last + 1, last + 1):
        c.process(g, None)
    feed([(last + 1, block(last + 1, [], at(61 * 60)))])
    r = table(s)
    assert len(r) == 1 and r[0][11] == 0 and r[0][3] is None and r[0][2] is None       # T+60 only: rug_fast no, rug pending
    assert json.loads(r[0][8])["rug_late"]["reason"] == "pending" or r[0][2] is None
    feed([(last + 2, block(last + 2, [], at(24 * 3600 + 30)))])
    r = table(s)
    assert len(r) == 1 and r[0][11] == 1 and (r[0][1], r[0][2], r[0][3]) == (0, 1, 1)     # one row, now final: rug_late
    assert r[0][4] > 1.5
    s.close()


def test_final_rows_for_a_whole_world(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    r = table(s)
    assert len(r) == 1 and (r[0][1], r[0][2], r[0][3], r[0][11]) == (1, 0, 1, 1)
    s2 = world([], with_buyer=False, tmp_path=tmp_path / "b")
    r2 = table(s2)
    assert (r2[0][3], r2[0][11]) == (0, 1)                                  # no outside buyers: not a rug, final


def test_rebuild_outcomes_is_idempotent_and_matches_the_live_rows(real):
    live = table(real)
    res = stored.rebuild_outcomes(real)
    assert res == {"launches": 3, "rows": 3, "final": 0, "rug_fast": 3, "rug_late": 0, "no_rug": 0, "pending": 0}
    assert table(real) == live                                      # computed_at is not in the compared columns
    assert stored.rebuild_outcomes(real) == res and table(real) == live


def test_rebuild_outcomes_cli_with_a_private_override(tmp_path, monkeypatch, capsys):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    db = s.path
    s.close()
    over = tmp_path / "prod.json"
    over.write_text(json.dumps({"version": "prod-2", "rug": {"buyers_loss_near": 100}}))     # buyers must lose > 100 NEAR
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(over))
    assert collector_main(["rebuild-outcomes", "--db", str(db)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == 1 and out["no_rug"] == 1 and out["rug_fast"] == 0
    s = open_store(db)
    r = table(s)
    assert (r[0][3], r[0][9], r[0][11]) == (0, "prod-2", 1)
    s.close()
    monkeypatch.delenv("RUGYOMI_RULES_CONFIG")
    assert collector_main(["rebuild-outcomes", "--db", str(db), "--token", TOK]) == 0
    s = open_store(db)
    assert (table(s)[0][3], table(s)[0][9]) == (1, "v0-default-1")                       # the config change is undone
    s.close()
    over.write_text(json.dumps({"rug": {"typo": 1}}))
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(over))
    capsys.readouterr()
    assert collector_main(["rebuild-outcomes", "--db", str(db)]) == 2
    assert "ConfigError" in capsys.readouterr().out


def test_a_broken_override_writes_no_row_and_never_breaks_the_block(tmp_path, monkeypatch):
    over = tmp_path / "bad.json"
    over.write_text("{not json")
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(over))
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    assert table(s) == []
    assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 5          # the blocks and snapshots went through
    err = s.get_status()["outcomes_last_error"]
    assert err["token"] == "-" and "ConfigError" in err["error"]


def test_a_failing_label_is_remembered_and_the_block_commits(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(stored, "compute_row", boom)
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    assert table(s) == [] and s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 5
    assert "boom" in s.get_status()["outcomes_last_error"]["error"]


def test_rebuild_snapshots_cli_refreshes_the_stored_labels(tmp_path, capsys):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    s.begin()
    s.con.execute("DELETE FROM launch_outcomes")
    s.commit()
    db = s.path
    s.close()
    assert collector_main(["rebuild-snapshots", "--db", str(db), "--token", TOK]) == 0
    s = open_store(db)
    assert len(table(s)) == 1 and table(s)[0][11] == 1
    s.close()


def test_status_reports_the_outcomes(real):
    from rugyomi.collector import status
    st = status(real)
    assert st["outcomes"]["total"] == 3 and st["outcomes"]["final"] == 0 and st["outcomes"]["last_error"] is None


def test_only_the_collector_side_writes_outcomes_and_the_site_never_computes_labels():
    """The site reads the table: no label function is imported or called anywhere in web/."""
    bad = {"rug_labels", "outcome_labels", "compute_row", "rebuild_outcomes", "default_config", "load_config"}
    for p in (PKG / "web").rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert not (bad & names - {"default_config"} if p.name == "methodology.py" else bad & names), (p.name, bad & names)
    writers = {}
    for p in PKG.rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        if "INTO launch_outcomes" in text or "launch_outcomes SET" in text:
            writers[p.relative_to(PKG).as_posix()] = True
    assert set(writers) == {"rules/stored.py"}


def test_the_site_shows_what_the_table_says(real, tmp_path):
    """Change the stored row and the page follows: the config version and the label come from the table, not from a
    recomputation with the public defaults."""
    import shutil
    from fastapi.testclient import TestClient
    from rugyomi.web import app as webapp
    path = tmp_path / "site.db"
    s = real.con.execute("PRAGMA wal_checkpoint(FULL)")
    shutil.copy(real.path, path)
    con = sqlite3.connect(path)
    ev = json.loads(con.execute("SELECT evidence FROM launch_outcomes WHERE token = ?", (REFRAX,)).fetchone()[0])
    ev["rug"] = {**ev["rug"], "value": False, "reason": "neither rug_fast nor rug_late"}
    ev["rug_fast"] = {**ev["rug_fast"], "value": False, "reason": "outside buyers were not hurt"}
    con.execute("UPDATE launch_outcomes SET evidence = ?, config_version = 'prod-7', rug = 0, rug_fast = 0, final = 1 "
                "WHERE token = ?", (json.dumps(ev), REFRAX))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(path)) as c:
        t = c.get(f"/launch/{REFRAX}").text
    assert "<strong>No rug.</strong>" in t and "with config prod-7" in t and "not final" not in t
