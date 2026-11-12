"""RGY-037: the forecast ledger - a hash chain over the live cards, the verify script (mutation tests), the site routes, the daily anchor post."""
import ast
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi import ledger as LG
from rugyomi import publisher as P
from rugyomi.store import open_store
from test_publisher import FakeTg, Clock, make

GOLDEN_FIRST = "ede62aef17ffe3a381c2b8fa93a5eb8832736fd0119005acb8115bd1afd87ec6"      # pinned: a change of the canonical form breaks every published anchor
GOLDEN_HEAD = "b6c055da43d9277e9b7211106e5b6ee0bc67fd2e48d42da5bddecf0a42f6e7ec"
PKG = Path(LG.__file__).resolve().parent
ROOT = PKG.parent


def add_card(s, token, issued_at, score=10, band="Low", reasons=None, signals=None, dev="dev.near", cfg="v0-default-1", h="abc123", rules="0.1", ms=2, height=7):
    s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, config_version, config_hash, "
                  "rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (token, issued_at, height, 1_000, score, band, json.dumps(reasons if reasons is not None else [], separators=(",", ":")),
                   json.dumps(signals if signals is not None else {"results": [], "notes": []}, separators=(",", ":")), dev, cfg, h, rules, ms))


@pytest.fixture
def db(tmp_path):
    s = open_store(tmp_path / "c.db")
    add_card(s, "a.nearlytrade.near", "2026-11-08T10:00:00.000001Z", 35, "Elevated",
             [{"id": "fingerprint", "fired": True, "contribution": 35, "value": 0.35, "reason": "creator account 9 min old", "evidence": [{"kind": "tx", "id": "T1"}]}])
    add_card(s, "b.nearlytrade.near", "2026-11-09T10:00:00.000001Z", 0)
    add_card(s, "c.nearlytrade.near", "2026-11-09T11:30:00.000001Z", 60, "High", dev=None, signals={"results": [], "notes": ["Ünïcode ✓ note"]})
    add_card(s, "d.nearlytrade.near", "2026-11-10T09:00:00.000001Z", 5)
    s.con.commit()
    yield s
    s.close()


def chain(s):
    return LG.build(s.con)


def lines(s):
    return LG.text(chain(s)).splitlines(keepends=True)


# ---------------------------------------------------------------------------------------------- the chain and its canonical form
@pytest.mark.core
def test_the_chain_is_pinned_a_golden_head_and_stable_across_runs(db):
    e1, e2 = chain(db), chain(db)
    assert LG.text(e1) == LG.text(e2) and [x["hash"] for x in e1] == [x["hash"] for x in e2]
    first = json.loads(e1[0]["line"])
    assert first["prev_hash"] == LG.GENESIS and first["token"] == "a.nearlytrade.near"
    assert list(first) == sorted(first)                                                       # keys sorted
    assert " " not in e1[0]["line"].replace("creator account 9 min old", "")                  # no spaces outside strings
    assert e1[0]["hash"] == GOLDEN_FIRST and e1[-1]["hash"] == GOLDEN_HEAD
    assert LG.head(e1)["head"] == e1[-1]["hash"] and LG.head(e1)["count"] == 4


@pytest.mark.core
def test_the_golden_line_and_hash_never_change(db):
    e = chain(db)
    assert e[0]["line"] == ('{"band":"Elevated","computed_ms":2,"config_hash":"abc123","config_version":"v0-default-1","dev_account":"dev.near","hash":"%s",'
                            '"issued_at":"2026-11-08T10:00:00.000001Z","issued_height":7,"launch_ts_ns":1000,"prev_hash":"%s","reasons":[{"contribution":35,'
                            '"evidence":[{"id":"T1","kind":"tx"}],"fired":true,"id":"fingerprint","reason":"creator account 9 min old","value":0.35}],'
                            '"rules_version":"0.1","score":35,"signals":{"notes":[],"results":[]},"token":"a.nearlytrade.near"}' % (e[0]["hash"], LG.GENESIS))
    import hashlib
    body = e[0]["line"].replace('"hash":"%s",' % e[0]["hash"], "")
    assert hashlib.sha256(body.encode()).hexdigest() == e[0]["hash"]                          # the hash is the sha256 of the line without its hash key
    assert "\\u00dc" in e[2]["line"] and "Ü" not in e[2]["line"] and e[2]["line"].isascii()   # non-ASCII is escaped: the same bytes everywhere
    assert LG.canonical({"b": 1, "a": 2.5, "c": [1, 1.0]}) == '{"a":2.5,"b":1,"c":[1,1.0]}'
    with pytest.raises(ValueError):
        LG.canonical({"x": float("nan")})


@pytest.mark.core
def test_the_order_is_the_issue_order_and_a_clock_step_cannot_move_a_card(db):
    add_card(db, "z.nearlytrade.near", "2026-11-01T00:00:00.000001Z", 1)                      # issued "earlier" by a wrong clock, but issued last
    db.con.commit()
    e = chain(db)
    assert [x["token"] for x in e][-1] == "z.nearlytrade.near" and [x["token"] for x in e][:4] == [f"{c}.nearlytrade.near" for c in "abcd"]
    assert e[3]["hash"] == chain(db)[3]["hash"]                                               # the chain only grew at its end: the old hashes are the same


@pytest.mark.core
def test_a_database_without_cards_gives_an_empty_ledger(tmp_path):
    import sqlite3
    con = sqlite3.connect(tmp_path / "x.db")
    assert LG.build(con) == [] and LG.head([])["count"] == 0 and LG.head([])["head"] == LG.GENESIS and LG.verify_lines([])["count"] == 0


@pytest.mark.core
def test_the_ledger_is_read_only_stdlib_code():
    tree = ast.parse((PKG / "ledger.py").read_text(encoding="utf-8"))
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert all(not m.startswith("rugyomi") and m != "requests" for m in mods if m), mods
    code = "\n".join(l for l in (PKG / "ledger.py").read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER)\b", code.replace("OUTCOME_SQL", ""))


# ---------------------------------------------------------------------------------------------- verification and mutations
@pytest.mark.core
def test_an_intact_chain_verifies(db):
    r = LG.verify_lines(lines(db))
    assert r["count"] == 4 and r["head"] == chain(db)[-1]["hash"]


def broken_at(ls):
    with pytest.raises(LG.Broken) as e:
        LG.verify_lines(ls)
    return e.value.line


@pytest.mark.core
def test_a_changed_line_is_found_at_that_line(db):
    ls = lines(db)
    assert broken_at([ls[0], ls[1].replace('"score":0', '"score":9'), *ls[2:]]) == 2                       # the content changed, the hash did not
    assert broken_at([ls[0].replace("creator account 9 min old", "creator account 99 min old"), *ls[1:]]) == 1
    assert broken_at([ls[0], ls[1], ls[2].replace('"band":"High"', '"band":"Low"'), ls[3]]) == 3


@pytest.mark.core
def test_a_removed_line_is_found_at_the_next_line(db):
    ls = lines(db)
    assert broken_at([ls[0], ls[2], ls[3]]) == 2
    assert broken_at(ls[1:]) == 1                                                                       # the first card removed: the new first line is not from the genesis
    assert LG.verify_lines(ls[:-1])["count"] == 3                                                       # the LAST card removed: only an anchor can tell (below)


@pytest.mark.core
def test_an_inserted_or_swapped_or_duplicated_line_is_found(db):
    ls = lines(db)
    forged = json.loads(ls[1])
    forged.update(token="evil.nearlytrade.near", score=99)
    forged["hash"] = "0" * 64
    assert broken_at([ls[0], LG.canonical(forged) + "\n", *ls[1:]]) == 2                                # a wrong hash
    forged.pop("hash")
    forged["prev_hash"] = json.loads(ls[0])["hash"]
    line, h = LG.link(forged["prev_hash"], {k: v for k, v in forged.items() if k != "prev_hash"})
    assert broken_at([ls[0], line + "\n", *ls[1:]]) == 3                                                # a valid new line: the next old line no longer links to it
    assert broken_at([ls[1], ls[0], *ls[2:]]) == 1                                                      # swapped
    assert broken_at([*ls, ls[3]]) == 5                                                                 # appended again
    assert broken_at([ls[0], ls[1], ls[1], ls[2], ls[3]]) == 3                                          # repeated


@pytest.mark.core
def test_the_form_of_the_line_is_checked_too(db):
    ls = lines(db)
    assert broken_at([ls[0], " " + ls[1].rstrip("\n") + "\n", *ls[2:]]) == 2                            # padded
    assert broken_at([ls[0], json.dumps(json.loads(ls[1]), indent=1).replace("\n", " ") + "\n", *ls[2:]]) == 2   # same data, not canonical
    assert broken_at([ls[0], "\n", *ls[1:]]) == 2                                                       # empty line
    assert broken_at([ls[0], "not json\n"]) == 2
    extra = json.loads(ls[1])
    extra["note"] = "x"
    assert broken_at([ls[0], LG.canonical(extra) + "\n"]) == 2                                          # a field the ledger does not have
    twice = [ls[0], LG.link(json.loads(ls[0])["hash"], {k: v for k, v in json.loads(ls[0]).items() if k not in ("hash", "prev_hash")})[0] + "\n"]
    assert broken_at(twice) == 2                                                                        # the same token twice (with a valid hash)


@pytest.mark.core
def test_an_attacker_who_recomputes_the_whole_chain_is_caught_by_the_anchor(db, tmp_path):
    ls = lines(db)
    anchor = f"4:{chain(db)[-1]['hash']}"
    recs = [{k: v for k, v in json.loads(l).items() if k not in ("hash", "prev_hash")} for l in ls]
    recs[1]["score"] = 99                                                                               # the attacker edits a card and re-hashes everything after it
    prev, forged = LG.GENESIS, []
    for r in recs:
        line, prev = LG.link(prev, r)
        forged.append(line + "\n")
    res = LG.verify_lines(forged)                                                                       # the chain itself is consistent ...
    assert res["count"] == 4 and res["head"] != chain(db)[-1]["hash"]
    assert LG.check_anchors(res, [anchor])                                                              # ... but it does not reach the head the channel published
    f = tmp_path / "forged.jsonl"
    f.write_text("".join(forged), encoding="utf-8")
    assert LG.main(["verify", str(f), "--anchor", anchor]) == 1 and LG.main(["verify", str(f)]) == 0


@pytest.mark.core
def test_the_anchor_catches_a_removed_last_card_a_moved_position_and_unknown_hashes(db):
    e = chain(db)
    res = LG.verify_lines(lines(db)[:-1])
    assert LG.check_anchors(res, [f"4:{e[3]['hash']}"]) and LG.check_anchors(res, [e[3]["hash"]])        # the anchored head is not in the shortened chain
    full = LG.verify_lines(lines(db))
    assert LG.check_anchors(full, [f"4:{e[3]['hash']}", e[1]["hash"], f"2:{e[1]['hash']}"]) == []
    assert LG.check_anchors(full, [f"3:{e[3]['hash']}"]) and LG.check_anchors(full, ["f" * 64]) and LG.check_anchors(full, ["x:y"])


@pytest.mark.core
def test_the_cli_prints_ok_or_the_first_broken_line(db, tmp_path, capsys):
    f = tmp_path / "l.jsonl"
    f.write_text(LG.text(chain(db)), encoding="utf-8")
    a = tmp_path / "anchors.txt"
    a.write_text(f"# a comment\n4:{chain(db)[-1]['hash']}\n\n{chain(db)[1]['hash']}  # day 2\n", encoding="utf-8")
    assert LG.main(["verify", str(f), "--anchors-file", str(a)]) == 0 and capsys.readouterr().out.startswith("OK: 4 cards, head ")
    ls = f.read_text(encoding="utf-8").splitlines(keepends=True)
    f.write_text("".join([ls[0], ls[1].replace('"score":0', '"score":1'), *ls[2:]]), encoding="utf-8")
    assert LG.main(["verify", str(f)]) == 1 and "BROKEN at line 2: hash does not match the content of the line" in capsys.readouterr().out
    f.write_text("".join(ls)[:-30], encoding="utf-8")                                                  # a cut-off file
    assert LG.main(["verify", str(f)]) == 1 and "newline" in capsys.readouterr().out
    assert LG.main(["verify", str(tmp_path / "missing.jsonl")]) == 2


def test_the_standalone_script_runs_without_the_package_installed(db, tmp_path):
    f = tmp_path / "l.jsonl"
    f.write_text(LG.text(chain(db)), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(ROOT / "verify_ledger.py"), str(f), "--anchor", f"4:{chain(db)[-1]['hash']}"], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0 and r.stdout.startswith("OK: 4 cards") and "1 anchor(s) match" in r.stdout, r.stderr
    f.write_text(f.read_text(encoding="utf-8").replace("Elevated", "Low"), encoding="utf-8")
    r = subprocess.run([sys.executable, "-I", str(ROOT / "verify_ledger.py"), str(f)], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 1 and "BROKEN at line 1" in r.stdout


# ---------------------------------------------------------------------------------------------- anchors and outcomes
@pytest.mark.core
def test_the_anchor_of_a_day_is_the_head_at_the_end_of_that_utc_day(db):
    e = chain(db)
    assert LG.anchor_for_day(e, "2026-11-07") is None                                         # no card yet
    assert LG.anchor_for_day(e, "2026-11-08") == {"day": "2026-11-08", "count": 1, "head": e[0]["hash"], "new": 1}
    assert LG.anchor_for_day(e, "2026-11-09") == {"day": "2026-11-09", "count": 3, "head": e[2]["hash"], "new": 2}
    assert LG.anchor_for_day(e, "2026-11-10")["count"] == 4
    quiet = LG.anchor_for_day(e, "2026-11-12")                                                # a day without cards repeats the head: nothing changed
    assert quiet == {"day": "2026-11-12", "count": 4, "head": e[3]["hash"], "new": 0}


@pytest.mark.core
def test_outcomes_are_next_to_the_chain_not_in_it(db):
    ev = {"label_version": 4, "rug": {"value": True, "reason": "r", "evidence": {}}}
    db.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, config_version, "
                   "config_hash, computed_at, final) VALUES('b.nearlytrade.near',1,0,1,2.5,NULL,NULL,NULL,?,'v1-fit-1','h','2026-11-10T10:00:00Z',1)", (json.dumps(ev),))
    db.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, config_version, "
                   "config_hash, computed_at, final) VALUES('c.nearlytrade.near',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'{}','v1-fit-1','h','x',0)")
    db.con.commit()
    before = LG.text(chain(db))
    rows = [json.loads(l) for l in LG.outcome_lines(db.con, chain(db)).splitlines()]
    assert [r["token"] for r in rows] == [f"{c}.nearlytrade.near" for c in "abcd"]
    assert rows[1] == {"token": "b.nearlytrade.near", "card_hash": chain(db)[1]["hash"], "final": True, "rug": True, "rug_fast": True, "rug_late": False,
                       "label_version": 4, "config_version": "v1-fit-1", "config_hash": "h", "computed_at": "2026-11-10T10:00:00Z"}
    assert rows[2]["final"] is False and rows[2]["rug"] is None and rows[0]["final"] is False and rows[0]["label_version"] is None
    db.con.execute("UPDATE launch_outcomes SET final = 1 WHERE token = 'c.nearlytrade.near'")                 # an outcome changes: the chain does not
    db.con.commit()
    assert LG.text(chain(db)) == before


# ---------------------------------------------------------------------------------------------- the site
def test_the_site_serves_the_ledger_its_head_and_the_outcomes_read_only(db, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from rugyomi.web import app as webapp
    path = db.con.execute("PRAGMA database_list").fetchone()[2]
    before = db.con.execute("SELECT COUNT(*), group_concat(score) FROM cards").fetchone()
    with TestClient(webapp.create_app(path)) as c:
        r = c.get("/ledger.jsonl")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and r.text == LG.text(chain(db))
        h = c.get("/ledger/head").json()
        assert h == LG.head(chain(db)) and h["count"] == 4 and h["format"] == "rugyomi-ledger-1"
        o = c.get("/ledger/outcomes.jsonl")
        assert o.status_code == 200 and len(o.text.splitlines()) == 4
        assert LG.verify_lines(r.text.splitlines(keepends=True))["head"] == h["head"]
        assert c.post("/ledger.jsonl").status_code == 405
        assert "public ledger" in " ".join(c.get("/accuracy").text.split()) and 'href="/ledger.jsonl"' in c.get("/accuracy").text
    assert db.con.execute("SELECT COUNT(*), group_concat(score) FROM cards").fetchone() == before


def test_the_site_serves_an_empty_ledger_on_a_database_without_cards(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from rugyomi.web import app as webapp
    s = open_store(tmp_path / "e.db")
    s.con.execute("DROP TRIGGER cards_no_update")
    s.con.execute("DROP TABLE cards")
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(tmp_path / "e.db")) as c:
        assert c.get("/ledger.jsonl").text == "" and c.get("/ledger/head").json()["count"] == 0


# ---------------------------------------------------------------------------------------------- the anchor post
def anchor_posts(tg):
    return [p for p in tg.sent if "forecast ledger" in p["text"]]


def seeded_publisher(tmp_path, hour=8, day=10, **pub):
    s = open_store(tmp_path / "c.db")
    for i, at in enumerate(["2026-11-08T10:00:00.000001Z", "2026-11-09T10:00:00.000001Z", "2026-11-09T11:30:00.000001Z", "2026-11-10T09:00:00.000001Z"]):
        add_card(s, f"{'abcd'[i]}.nearlytrade.near", at, i)
    s.con.commit()
    s.close()
    import datetime as dt
    clock = Clock(dt.datetime(2026, 11, day, hour, 0, tzinfo=dt.timezone.utc).timestamp())
    pub.setdefault("since", "2026-11-08T00:00:00Z")
    pub.setdefault("dry_run", False)
    return make(tmp_path, clock=clock, **pub) + (clock,)


@pytest.mark.core
def test_the_anchor_is_posted_once_a_day_after_the_hour_with_the_head_of_the_day_before(tmp_path):
    p, tg, clock = seeded_publisher(tmp_path)
    assert p.tick() == 1
    text = anchor_posts(tg)[0]["text"]
    e = chain_of(tmp_path)
    assert text.splitlines()[0] == "rugyomi forecast ledger · 2026-11-09 (UTC)" and "3 cards in the chain · 2 issued that day" in text
    assert f"anchor 3:{e[2]['hash']}" in text and "parse_mode" not in anchor_posts(tg)[0]
    assert p.tick() == 0 and len(anchor_posts(tg)) == 1                                            # at most once
    clock.t += 86400                                                                              # the next day: the next anchor, a quiet day repeats nothing
    assert p.tick() == 1 and f"anchor 4:{e[3]['hash']}" in anchor_posts(tg)[1]["text"]
    assert p.posts.execute("SELECT token, kind, status FROM channel_posts WHERE kind = 'anchor' ORDER BY token").fetchall() == [("2026-11-09", "anchor", "sent"), ("2026-11-10", "anchor", "sent")]


def chain_of(tmp_path):
    s = open_store(tmp_path / "c.db", readonly=True)
    try:
        return LG.build(s.con)
    finally:
        s.close()


@pytest.mark.core
def test_the_anchor_post_is_checked_by_the_verify_script_end_to_end(tmp_path):
    p, tg, _ = seeded_publisher(tmp_path)
    p.tick()
    m = re.search(r"anchor (\d+:[0-9a-f]{64})", anchor_posts(tg)[0]["text"])
    f = tmp_path / "l.jsonl"
    f.write_text(LG.text(chain_of(tmp_path)), encoding="utf-8")
    assert LG.main(["verify", str(f), "--anchor", m.group(1)]) == 0
    f.write_text(f.read_text(encoding="utf-8").replace('"score":1', '"score":2'), encoding="utf-8")
    assert LG.main(["verify", str(f), "--anchor", m.group(1)]) == 1


def test_no_anchor_before_the_hour_before_the_first_card_before_since_or_when_off(tmp_path):
    p, tg, _ = seeded_publisher(tmp_path, hour=6)
    assert p.tick() == 0 and not anchor_posts(tg)                                                  # before daily_hour_utc
    p, tg, _ = seeded_publisher(tmp_path / "x", hour=8, day=8)                                      # 8 Nov: yesterday (7 Nov) had no card
    assert p.tick() == 0 and not anchor_posts(tg)
    p, tg, _ = seeded_publisher(tmp_path / "y", since="2026-11-10T00:00:00Z")                      # switching on never posts the past
    assert p.tick() == 0 and not anchor_posts(tg)
    p, tg, _ = seeded_publisher(tmp_path / "z", enabled=False)
    assert p.tick() == 0 and not tg.sent


@pytest.mark.core
def test_the_anchor_text_is_plain_in_both_styles_and_links_the_ledger(tmp_path):
    p, tg, _ = seeded_publisher(tmp_path, post_style="compact")
    p.cfg["site"]["base_url"] = "https://example.org/"
    p.tick()
    post = anchor_posts(tg)[0]
    assert "parse_mode" not in post and post["text"].splitlines()[-1] == "https://example.org/ledger.jsonl"
    assert "<" not in post["text"] and len(post["text"].splitlines()) == 4


@pytest.mark.core
def test_a_dry_run_anchor_goes_to_the_admin_chat_marked(tmp_path):
    p, tg, _ = seeded_publisher(tmp_path, dry_run=True)
    p.tick()
    assert anchor_posts(tg)[0]["chat_id"] == "777" and anchor_posts(tg)[0]["text"].startswith("[dry run")


def test_a_publisher_db_made_before_the_anchor_kind_is_rebuilt_keeping_its_rows(tmp_path):
    import sqlite3
    state = tmp_path / "state"
    state.mkdir()
    old = sqlite3.connect(state / "publisher.db")
    old.executescript("""CREATE TABLE channel_posts(token TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('card','outcome','daily')), chat TEXT NOT NULL,
        mode TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','unknown','skipped')), message_id INTEGER, reply_to INTEGER, text TEXT NOT NULL,
        text_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try_at REAL NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT,
        PRIMARY KEY (token, kind, chat));
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO channel_posts(token, kind, chat, mode, status, message_id, text, text_hash, created_at, sent_at) VALUES('t.near','card','-100','channel','sent',5,'hello','h','2026-11-08T00:00:00Z','2026-11-08T00:00:01Z');
        INSERT INTO meta VALUES('since_channel','123');""")
    old.commit()
    old.close()
    open_store(tmp_path / "c.db").close()
    p, tg = make(tmp_path)
    assert p.posts.execute("SELECT token, kind, status, message_id, text, fmt FROM channel_posts").fetchall() == [("t.near", "card", "sent", 5, "hello", None)]
    assert p.posts.execute("SELECT value FROM meta WHERE key = 'since_channel'").fetchone()[0] == "123"
    assert "'anchor'" in p.posts.execute("SELECT sql FROM sqlite_master WHERE name = 'channel_posts'").fetchone()[0]
    p.posts.execute("INSERT INTO channel_posts(token, kind, chat, mode, status, text, text_hash, created_at) VALUES('2026-11-09','anchor','-100','channel','sent','x','h','2026-11-10T00:00:00Z')")
    p2, _ = make(tmp_path)                                                                          # a second start does not rebuild again
    assert p2.posts.execute("SELECT COUNT(*) FROM channel_posts").fetchone()[0] == 2


@pytest.mark.core
def test_the_preview_prints_the_anchor_post(tmp_path, capsys, monkeypatch):
    seeded_publisher(tmp_path)
    monkeypatch.setenv("RUGYOMI_DB", str(tmp_path / "c.db"))
    monkeypatch.delenv(P.CONFIG_ENV, raising=False)
    assert P.main(["preview-anchor", "2026-11-09"]) == 0
    out = capsys.readouterr().out
    assert "forecast ledger · 2026-11-09" in out and "anchor 3:" in out
    assert P.main(["preview-anchor", "2026-11-01"]) == 0 and "nothing would be posted" in capsys.readouterr().out


@pytest.mark.core
def test_nothing_else_reads_the_ledger_and_the_cards_keep_their_hash(db):
    src = {p.relative_to(PKG).as_posix(): p.read_text(encoding="utf-8") for p in PKG.rglob("*.py")}
    users = {k for k, v in src.items() if re.search(r"import ledger|from \. import ledger|from \.\. import ledger|rugyomi\.ledger", v)}
    assert users - {"ledger.py"} == {"publisher.py", "web/app.py"}, users
    for forbidden in ("rules/card.py", "rules/signals.py", "rules/rug.py", "rules/stored.py", "collector.py", "snapshots.py"):
        assert "ledger" not in src[forbidden], forbidden
    from rugyomi.rules import config as C
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2")] == ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371"]
