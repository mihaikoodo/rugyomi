"""RGY-018: the accuracy table — Wilson bounds, rates, lift, coverage, per-day and per-config numbers on a fixture with
known cards / outcomes; the /accuracy page on an empty database, on a database whose outcomes are all pending, and on a
fixture database; the creator page shows score and outcome."""
import datetime as dt
import sqlite3
import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

import rugyomi_testkit  # noqa: F401,E402  (puts the package on sys.path)
from rugyomi.rules import accuracy_summary, card_outcome_rows  # noqa: E402
from rugyomi.rules.accuracy import counted, outcome_state, wilson  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402

DAY1 = int(dt.datetime(2026, 11, 5, 18, tzinfo=dt.timezone.utc).timestamp() * 1e9)
DAY2 = DAY1 + 24 * 3600 * 10**9


def row(i, score, band, rug, final=1, day=DAY1, cfg="v0-default-1", sev=None, dev_exit=None, buyers_loss=None):
    return {"token": f"t{i}.nearlytrade.near", "launch_ts_ns": day + i * 10**9, "issued_at": "x", "score": score,
            "band": band, "config_version": cfg, "rug": rug, "final": final, "severity_near": sev,
            "dev_exit": dev_exit, "buyers_loss": buyers_loss}


def fixture_rows():
    rows, i = [], 0

    def add(n, *a, **k):
        nonlocal i
        for _ in range(n):
            rows.append(row(i, *a, **k))
            i += 1
    add(11, 5, "Low", 0, dev_exit=0, buyers_loss=0)                     # Low: 11 final no-rug
    add(1, 25, "Low", 1, sev=1.0, dev_exit=1, buyers_loss=1)            # Low: 1 rug (missed)
    add(4, 45, "Elevated", 1, sev=2.0, dev_exit=1, buyers_loss=1)       # Elevated: 4 of 6
    add(2, 45, "Elevated", 0, dev_exit=0, buyers_loss=0)
    add(3, 75, "High", 1, day=DAY2, sev=3.0, dev_exit=1, buyers_loss=1, cfg="v0-default-2")
    add(1, 85, "High", 0, day=DAY2, cfg="v0-default-2", dev_exit=0, buyers_loss=1)
    add(2, 70, "High", None, final=0, day=DAY2, cfg="v0-default-2")       # waiting
    add(1, 65, "High", 1, final=0, day=DAY2, cfg="v0-default-2", sev=0.5)  # a rug found at T+60, not final yet
    add(1, 10, "Low", None, final=1, day=DAY2)                            # final but no definite answer
    return rows


def test_wilson_matches_hand_values():
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.2693, abs=5e-4) and hi == pytest.approx(0.7307, abs=5e-4)
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2129, abs=5e-4)
    lo, hi = wilson(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.7871, abs=5e-4)
    assert wilson(0, 0) is None
    lo, hi = wilson(1, 12)
    assert lo == pytest.approx(0.0188, abs=5e-4) and hi == pytest.approx(0.3012, abs=5e-4)


def test_counted_needs_final_and_a_definite_answer():
    assert counted(row(0, 1, "Low", 0)) and counted(row(0, 1, "Low", 1))
    assert not counted(row(0, 1, "Low", 1, final=0)) and not counted(row(0, 1, "Low", None))
    assert outcome_state(row(0, 1, "Low", 1, final=0)) == "rug"        # a T+60 rug is already a rug
    assert outcome_state(row(0, 1, "Low", 0, final=0)) == "waiting"
    assert outcome_state(row(0, 1, "Low", 0)) == "no rug"


def test_summary_numbers_on_known_rows():
    a = accuracy_summary(fixture_rows())
    s = a["summary"]
    # counted: 12 Low + 6 Elevated + 4 High = 22; rugs 1 + 4 + 3 = 8
    assert (s["cards"], s["final"], s["waiting"], s["rugs"]) == (26, 22, 4, 8)
    assert s["base_rate"] == pytest.approx(8 / 22)
    assert s["first_launch_ns"] == DAY1 and s["last_launch_ns"] == DAY2 + 25 * 10**9
    low, el, high = a["by_band"]
    assert (low["band"], low["cards"], low["n"], low["rugs"]) == ("Low", 13, 12, 1)
    assert low["rate"] == pytest.approx(1 / 12) and low["lift"] == pytest.approx((1 / 12) / (8 / 22))
    assert (low["lo"], low["hi"]) == pytest.approx(wilson(1, 12))
    assert low["too_few"] is False                                        # n = 12 >= 10
    assert (el["n"], el["rugs"], el["too_few"]) == (6, 4, True)
    assert el["rate"] == pytest.approx(4 / 6) and el["lift"] == pytest.approx((4 / 6) / (8 / 22))
    assert el["mean_score"] == pytest.approx(45.0) and el["dev_exit"] == pytest.approx(4 / 6)
    assert (high["cards"], high["n"], high["rugs"]) == (7, 4, 3)
    assert high["rate"] == pytest.approx(0.75) and high["buyers_loss"] == pytest.approx(1.0)
    assert high["mean_score"] == pytest.approx((75 * 3 + 85 + 70 * 2 + 65) / 7)
    # score ranges
    r = {x["range"]: x for x in a["by_range"]}
    assert list(r) == ["0–19", "20–39", "40–59", "60–79", "80–100"]
    assert (r["0–19"]["n"], r["0–19"]["rugs"]) == (11, 0)                  # the 11 clean Low rows + the unresolved is not counted
    assert (r["20–39"]["n"], r["20–39"]["rugs"]) == (1, 1) and r["20–39"]["too_few"]
    assert (r["40–59"]["n"], r["40–59"]["rugs"]) == (6, 4)
    assert (r["60–79"]["n"], r["60–79"]["rugs"]) == (3, 3)
    assert (r["80–100"]["n"], r["80–100"]["rugs"]) == (1, 0)
    # coverage: rugs with an Elevated / High card: 4 + 3 of 8; severity 1 + 8 + 9 = 18, flagged 17
    c = a["coverage"]
    assert (c["rugs"], c["flagged"]) == (8, 7) and c["share"] == pytest.approx(7 / 8)
    assert c["severity_total"] == pytest.approx(18.0) and c["severity_flagged"] == pytest.approx(17.0)
    assert c["severity_share"] == pytest.approx(17 / 18)
    # by day: newest first; the second day has 8 cards, 4 final, 3 rugs
    d1, d2 = a["by_day"][1], a["by_day"][0]
    assert d2["day"] == "2026-11-06" and (d2["cards"], d2["final"], d2["rugs"]) == (8, 4, 3)
    assert d2["rate"] == pytest.approx(0.75)
    assert d1["day"] == "2026-11-05" and (d1["cards"], d1["final"], d1["rugs"]) == (18, 18, 5)
    # by config
    cfgs = {x["config_version"]: x for x in a["by_config"]}
    assert set(cfgs) == {"v0-default-1", "v0-default-2"}
    hb = {b["band"]: b for b in cfgs["v0-default-2"]["bands"]}
    assert (hb["High"]["n"], hb["High"]["rugs"]) == (4, 3) and hb["Low"]["n"] == 0 and hb["Low"]["rate"] is None
    assert (cfgs["v0-default-1"]["cards"], cfgs["v0-default-2"]["cards"]) == (19, 7)
    # every launch is listed once, with its state
    assert len(a["launches"]) == 26
    assert {x["state"] for x in a["launches"]} == {"rug", "no rug", "waiting"}


def test_summary_of_nothing_and_of_all_pending():
    e = accuracy_summary([])
    assert e["summary"]["cards"] == 0 and e["summary"]["base_rate"] is None
    assert all(b["rate"] is None and b["lift"] is None and b["too_few"] for b in e["by_band"])
    assert e["coverage"]["share"] is None and e["by_day"] == [] and e["by_config"] == []
    p = accuracy_summary([row(i, 70, "High", None, final=0) for i in range(3)])
    assert p["summary"]["cards"] == 3 and p["summary"]["final"] == 0 and p["summary"]["waiting"] == 3
    assert p["summary"]["base_rate"] is None and p["by_band"][2]["rate"] is None


def test_no_rugs_at_all_gives_no_lift():
    a = accuracy_summary([row(i, 5, "Low", 0) for i in range(12)])
    assert a["summary"]["base_rate"] == 0.0 and a["by_band"][0]["rate"] == 0.0 and a["by_band"][0]["lift"] is None


# ------------------------------------------------------------------------------------------------ database + page
def make_db(path, rows, with_outcomes=True, orphan_launch=True):
    """A real schema database with cards (and outcomes) for `rows`; plus a launch without a card that has a final rug
    outcome — it must never be counted."""
    s = open_store(path)
    s.con.execute("BEGIN")
    for r in rows:
        s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, "
                      "dev_account, config_version, config_hash, rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["token"], "2026-11-05T18:00:00Z", 1, r["launch_ts_ns"], r["score"], r["band"], "[]", "{}", None,
                       r["config_version"], "h", "0.1", 1))
        if with_outcomes and r["final"] is not None and not (r["rug"] is None and not r["final"] and False):
            s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, "
                          "buyers_loss, evidence, config_version, config_hash, computed_at, final) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (r["token"], None, None, r["rug"], r["severity_near"], None, r["dev_exit"], r["buyers_loss"], "{}",
                           r["config_version"], "h", "2026-11-06T00:00:00Z", r["final"]))
    if orphan_launch and with_outcomes:
        s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, "
                      "buyers_loss, evidence, config_version, config_hash, computed_at, final) "
                      "VALUES('nocard.nearlytrade.near',1,NULL,1,9.0,NULL,1,1,'{}','v0-default-1','h','x',1)")
    s.con.execute("COMMIT")
    s.close()


@pytest.fixture
def make_client(tmp_path):
    clients = []

    def make(rows, **kw):
        db = tmp_path / f"a{len(clients)}.db"
        make_db(db, rows, **kw)
        c = TestClient(webapp.create_app(db))
        c.__enter__()
        clients.append(c)
        return c, db
    yield make
    for c in clients:
        c.__exit__(None, None, None)


def test_page_on_an_empty_database(make_client):
    c, _ = make_client([])
    r = c.get("/accuracy")
    assert r.status_code == 200 and "No cards yet" in r.text and "not calibrated" in r.text.lower()
    assert 'href="/accuracy"' in r.text                                     # navigation


def test_page_when_every_outcome_is_pending(make_client):
    c, _ = make_client([row(i, 70, "High", None, final=0) for i in range(4)])
    r = c.get("/accuracy")
    assert r.status_code == 200
    assert "4 cards waiting for their 24 h outcome" in " ".join(r.text.split())
    assert "Base rate" in r.text and "No rugs with a final outcome yet" in r.text
    assert ">no rug<" not in r.text                                           # a pending launch is never shown as "no rug"


def test_page_numbers_and_a_launch_without_a_card_never_counts(make_client):
    c, db = make_client(fixture_rows())
    rows = card_outcome_rows(sqlite3.connect(db))
    assert len(rows) == 26 and all(r["token"] != "nocard.nearlytrade.near" for r in rows)
    r = c.get("/accuracy")
    assert r.status_code == 200
    t = r.text
    assert "nocard.nearlytrade.near" not in t
    assert "36%" in t                                                      # base rate 8 / 22
    assert "too few to judge" in t and "Elevated" in t and "High" in t
    assert "7 of 8 rugs (88%) had an Elevated or High card" in " ".join(t.split())
    assert "17.00 of 18.00 NEAR" in t
    assert "2026-11-05" in t and "2026-11-06" in t and "v0-default-2" in t
    assert "/launch/t0.nearlytrade.near" in t
    assert "Cache-Control" in r.headers and r.headers["content-security-policy"]


def test_accuracy_never_writes_and_reads_only_cards_and_outcomes(make_client):
    c, db = make_client(fixture_rows())
    before = db.stat().st_mtime_ns
    c.get("/accuracy")
    assert db.stat().st_mtime_ns == before


def test_page_on_a_database_without_the_new_tables(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=6")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(db)) as c:
        r = c.get("/accuracy")
        assert r.status_code == 200 and "No cards yet" in r.text


def test_creator_page_shows_score_and_outcome(tmp_path):
    """On the REFRAX replay: the creator page lists the launch with its card and its stored outcome."""
    from rugyomi_testkit import refrax_replay_blocks
    from rugyomi.collector import Collector
    from rugyomi.keys import EnvKeySource
    from rugyomi.neardata import Neardata
    db = tmp_path / "w.db"
    s = open_store(db)
    col = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in refrax_replay_blocks():
        col.process(h, b)
    s.close()
    with TestClient(webapp.create_app(db)) as c:
        r = c.get("/creator/thecoins.near")
        assert r.status_code == 200
        text = " ".join(r.text.split())
        assert "70 High" in text and "<strong>rug</strong>" in r.text and "<th>Risk</th>" in r.text
        r2 = c.get("/launch/refrax.nearlytrade.near")
        assert 'href="/accuracy"' in r2.text
        a = c.get("/accuracy")
        assert a.status_code == 200 and "3 cards waiting for their 24 h outcome" in " ".join(a.text.split())
