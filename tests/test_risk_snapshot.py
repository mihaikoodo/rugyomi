"""RGY-069: the score file for a developer who does not want to call the API for the past."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi import risk_snapshot as RS

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def demo_db():
    from rugyomi.web import demo
    return demo.unpack()


@pytest.fixture(scope="module")
def demo_rows(demo_db):
    return RS.rows_from_db(demo_db)


@pytest.mark.core
def test_the_scores_are_the_ones_the_api_shows(demo_db, demo_rows):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from rugyomi.web.app import create_app
    api = TestClient(create_app(demo_db)).get("/api/v1/launches", params={"limit": 200}).json()["launches"]
    by_token = {r["token"]: r for r in demo_rows}
    assert len(api) == 200 and len(demo_rows) > 200
    for a in api:
        assert a["risk"] is not None, a["token"]
        r = by_token[a["token"]]
        assert (r["score"], r["band"], r["launchpad"], r["launched_at"]) == (a["risk"]["score"], a["risk"]["band"], a["launchpad"], a["launched_at"])
        assert r["site_url"].endswith("/launch/" + a["token"])
    assert [r["launched_at"] for r in demo_rows] == sorted((r["launched_at"] for r in demo_rows), reverse=True)


@pytest.mark.core
def test_no_row_tells_how_the_score_was_made_and_the_database_is_not_touched(demo_db, demo_rows):
    before = Path(demo_db).read_bytes()
    text = json.dumps(RS.document(demo_rows)) + RS.to_csv(demo_rows)
    for word in ("kind", "retro", "as_of", "in_sample", "live"):
        assert word not in text.lower(), word
    assert set(demo_rows[0]) == set(RS.FIELDS)
    assert Path(demo_db).read_bytes() == before


@pytest.mark.core
def test_a_stored_card_wins_over_a_later_one_and_the_newest_later_card_is_used(tmp_path):
    db = tmp_path / "t.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE launches(token TEXT PRIMARY KEY, launchpad TEXT, ts_ns INTEGER);
        CREATE TABLE cards(token TEXT PRIMARY KEY, issued_at TEXT, score INTEGER, band TEXT, rules_version TEXT, config_version TEXT);
        CREATE TABLE retro_cards(token TEXT, rules_version TEXT, config_version TEXT, config_hash TEXT, launch_ts_ns INTEGER, score INTEGER, band TEXT,
            reasons TEXT, signals TEXT, notes TEXT, in_sample INTEGER, computed_at TEXT, PRIMARY KEY (token, config_version));
    """)
    t0 = 1_700_000_000 * 10**9
    for i, tok in enumerate(("a.nearlytrade.near", "b.nearlytrade.near", "c.nearlytrade.near", "d.nearlytrade.near")):
        con.execute("INSERT INTO launches VALUES(?,?,?)", (tok, "nearly", t0 + i * 10**9))
    con.execute("INSERT INTO cards VALUES('a.nearlytrade.near','2023-11-14T22:13:21.123456Z',70,'High','1.3','v2l-1')")
    for tok, cv, sc, b, at in (("a.nearlytrade.near", "x", 5, "Low", "2026-01-01T00:00:00Z"),
                               ("b.nearlytrade.near", "old", 10, "Low", "2026-01-01T00:00:00Z"), ("b.nearlytrade.near", "new", 45, "Elevated", "2026-02-01T00:00:00Z"),
                               ("c.nearlytrade.near", "bad", 140, "Low", "2026-01-01T00:00:00Z")):
        con.execute("INSERT INTO retro_cards VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (tok, "1.0", cv, "h", t0, sc, b, "[]", "{}", "[]", 0, at))
    con.commit()
    con.close()
    rows = {r["token"]: r for r in RS.rows_from_db(db, "http://localhost:8000/")}
    assert set(rows) == {"a.nearlytrade.near", "b.nearlytrade.near"}                     # c: score out of range; d: no card at all
    assert rows["a.nearlytrade.near"]["score"] == 70 and rows["a.nearlytrade.near"]["scored_at"] == "2023-11-14T22:13:21Z"
    assert rows["b.nearlytrade.near"]["score"] == 45 and rows["b.nearlytrade.near"]["config_version"] == "new"
    assert rows["b.nearlytrade.near"]["site_url"] == "http://localhost:8000/launch/b.nearlytrade.near"


@pytest.mark.core
def test_a_database_without_the_tables_gives_no_rows_and_does_not_create_one(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    assert RS.rows_from_db(db) == []
    with pytest.raises(sqlite3.Error):
        RS.rows_from_db(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_build_split_and_the_three_files(demo_db, demo_rows, tmp_path, capsys):
    assert RS.main(["build", "--db", str(demo_db), "--out", str(tmp_path / "a")]) == 0
    full = json.loads((tmp_path / "a" / RS.JSON_NAME).read_text(encoding="utf-8"))
    assert full["count"] == len(demo_rows) == len(full["rows"]) and full["disclaimer"] == "Analysis, not advice." and full["fields"] == list(RS.FIELDS)
    csv_lines = (tmp_path / "a" / RS.CSV_NAME).read_text(encoding="utf-8").splitlines()
    assert csv_lines[0] == ",".join(RS.FIELDS) and len(csv_lines) == len(demo_rows) + 1
    small = json.loads((tmp_path / "a" / RS.SCORES_NAME).read_text(encoding="utf-8"))
    assert small["count"] == len(demo_rows) and small["bands"] == {"L": "Low", "E": "Elevated", "H": "High"}
    r = demo_rows[0]
    assert small["scores"][r["token"]] == [r["score"], RS.BAND_CODE[r["band"]]]
    assert (tmp_path / "a" / RS.SCORES_NAME).stat().st_size < (tmp_path / "a" / RS.JSON_NAME).stat().st_size / 4
    capsys.readouterr()
    assert RS.main(["build", "--db", str(demo_db)]) == 0                                # stdout, then split from that text
    (tmp_path / "s.txt").write_text(capsys.readouterr().out, encoding="utf-8")
    assert RS.main(["split", str(tmp_path / "s.txt"), str(tmp_path / "b")]) == 0
    for name in (RS.JSON_NAME, RS.CSV_NAME, RS.SCORES_NAME):
        assert (tmp_path / "b" / name).read_bytes() == (tmp_path / "a" / name).read_bytes() or name == RS.JSON_NAME
    assert json.loads((tmp_path / "b" / RS.JSON_NAME).read_text(encoding="utf-8"))["rows"] == full["rows"]


@pytest.mark.core
@pytest.mark.parametrize("bad", [{"token": "../x", "score": 5, "band": "Low"}, {"token": "a.near", "score": 101, "band": "Low"},
                                 {"token": "a.near", "score": 5, "band": "Medium"}, {"token": "a.near", "score": "5", "band": "Low"}])
def test_split_refuses_a_bad_row_and_writes_nothing(tmp_path, bad, capsys):
    row = {k: "x" for k in RS.FIELDS} | bad
    src = tmp_path / "in.json"
    src.write_text(json.dumps({"generated_at": "2026-11-14T00:00:00Z", "source": "https://x", "rows": [row]}), encoding="utf-8")
    assert RS.main(["split", str(src), str(tmp_path / "out")]) == 1
    assert "not a valid snapshot row" in capsys.readouterr().err and not (tmp_path / "out").exists()
    src.write_text("[]", encoding="utf-8")
    assert RS.main(["split", str(src), str(tmp_path / "out")]) == 1


@pytest.mark.core
def test_the_file_runs_on_its_own_with_the_standard_library_only(demo_rows, tmp_path):
    src = tmp_path / "in.json"
    src.write_text(json.dumps(RS.document(demo_rows)), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(ROOT / "rugyomi" / "risk_snapshot.py"), "split", str(src), str(tmp_path / "o")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "o" / RS.CSV_NAME).exists() and f"{len(demo_rows)} launches" in r.stdout
