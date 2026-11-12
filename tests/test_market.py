"""RGY-044: the market's rug rate as of a moment (no lookahead: the 25 h rule, mutation tests), the one function behind three readers, the plain-words paragraph made from the stored
walk-forward summary (and the neutral line when there is none), the calibration caveat, the summary builder, and the pages / the daily summary that render them."""
import datetime as dt
import json
import re
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from rugyomi.rules import market as MK  # noqa: E402
from rugyomi.rules import walkforward as WF  # noqa: E402
from rugyomi.store import open_store  # noqa: E402

NS = 10**9
H = 3600 * NS
DAY = 24 * H
ASOF = 1_792_000_000 * NS


def make(tmp_path, name="m.db"):
    s = open_store(tmp_path / name)
    return s


def add(con, tok, ts, rug, final=1):
    con.execute("INSERT INTO launches(token, launchpad, height, ts_ns, detected_at) VALUES(?,?,?,?,?)", (tok, "nearly", 1, ts, "x"))
    if rug is not None or final is not None:
        con.execute("INSERT INTO launch_outcomes(token, rug, rug_fast, rug_late, evidence, config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,?,?,?)",
                    (tok, rug, rug or 0, 0, "{}", "v", "h", "x", final))


def fill(con, n=40, rugs=10):
    """n launches spread over days 2..13 before ASOF (all well inside the window), `rugs` of them rugs."""
    for i in range(n):
        add(con, f"w{i}.nearlytrade.near", ASOF - 2 * DAY - i * (11 * DAY // n), 1 if i < rugs else 0)


# ------------------------------------------------------------------------------------------------ the rate and its as-of rule
@pytest.mark.core
def test_the_rate_counts_launches_of_the_last_14_days_with_a_known_final_label(tmp_path):
    s = make(tmp_path)
    fill(s.con)
    m = MK.market_rate(s.con, ASOF)
    assert (m["n"], m["rugs"], m["days"]) == (40, 10, 14) and m["rate"] == pytest.approx(0.25) and not m["too_few"] and m["lo"] < 0.25 < m["hi"]
    assert MK.market_rate(s.con, ASOF + 100 * DAY)["n"] == 0                      # a moment long after: the window holds nothing
    assert MK.market_text(m) == "Rug rate, last 14 days: 25 % (40 launches)"
    assert MK.market_text(m, as_of="as of the card's issue time").endswith("(as of the card's issue time)")


@pytest.mark.core
def test_window_boundaries_25_hours_and_14_days(tmp_path):
    s = make(tmp_path)
    add(s.con, "edge_new.nearlytrade.near", ASOF - 25 * H, 1)                     # exactly 25 h old: its label is known -> counted
    add(s.con, "too_new.nearlytrade.near", ASOF - 25 * H + 1, 1)                  # one ns younger: not known yet
    add(s.con, "edge_old.nearlytrade.near", ASOF - 14 * DAY, 1)                   # exactly 14 days old: out (the window is after it)
    add(s.con, "in_old.nearlytrade.near", ASOF - 14 * DAY + 1, 0)
    m = MK.market_rate(s.con, ASOF)
    assert (m["n"], m["rugs"]) == (2, 1)
    assert m["to_ns"] == ASOF - 25 * H and m["from_ns"] == ASOF - 14 * DAY


@pytest.mark.core
def test_only_final_definite_outcomes_count(tmp_path):
    s = make(tmp_path)
    fill(s.con, 40, 10)
    add(s.con, "nf.nearlytrade.near", ASOF - 3 * DAY, 1, final=0)                 # not final
    add(s.con, "und.nearlytrade.near", ASOF - 3 * DAY, None, final=1)             # final but undefined
    add(s.con, "none.nearlytrade.near", ASOF - 3 * DAY, None, final=None)
    s.con.execute("INSERT INTO launches(token, launchpad, height, ts_ns, detected_at) VALUES('noout.nearlytrade.near','nearly',1,?,'x')", (ASOF - 3 * DAY,))
    assert (MK.market_rate(s.con, ASOF)["n"], MK.market_rate(s.con, ASOF)["rugs"]) == (40, 10)


@pytest.mark.core
def test_nothing_later_than_the_moment_can_change_the_answer(tmp_path):
    """The mutation test: launches after the moment, labels of launches younger than 25 h, outcomes older than the window - none moves the rate; a label inside the window does."""
    s = make(tmp_path)
    fill(s.con)
    add(s.con, "young.nearlytrade.near", ASOF - 5 * H, 0)
    add(s.con, "old.nearlytrade.near", ASOF - 20 * DAY, 0)
    base = MK.market_rate(s.con, ASOF)
    for i in range(30):                                                           # a burst of rugs after the moment
        add(s.con, f"later{i}.nearlytrade.near", ASOF + i * H, 1)
    s.con.execute("UPDATE launch_outcomes SET rug = 1, rug_fast = 1 WHERE token = 'young.nearlytrade.near'")       # a label that is not known yet
    s.con.execute("UPDATE launch_outcomes SET rug = 1, rug_fast = 1 WHERE token = 'old.nearlytrade.near'")          # outside the window
    assert MK.market_rate(s.con, ASOF) == base
    s.con.execute("UPDATE launch_outcomes SET rug = 1, rug_fast = 1 WHERE token = 'w39.nearlytrade.near'")          # inside: counts
    assert MK.market_rate(s.con, ASOF)["rugs"] == base["rugs"] + 1
    assert MK.market_rate(s.con, ASOF + 40 * H)["n"] > base["n"]                  # a later moment sees more (the same function, another moment)


@pytest.mark.core
def test_a_database_without_the_tables_gives_no_launches_and_a_plain_line(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    m = MK.market_rate(con, ASOF)
    assert m["n"] == 0 and m["rate"] is None and m["lo"] is None
    assert MK.market_text(m) == "Rug rate, last 14 days: no decided launches yet"
    assert MK.market_short(m) == "market 14 d: no data"
    assert MK.read_summary(con) is None


@pytest.mark.core
def test_few_launches_are_marked_and_the_short_form_matches(tmp_path):
    s = make(tmp_path)
    fill(s.con, 12, 6)
    m = MK.market_rate(s.con, ASOF)
    assert m["too_few"] and MK.market_text(m) == "Rug rate, last 14 days: 50 % (12 launches) — too few to judge"
    assert MK.market_short(m) == "market 14 d: 50 % (12) *"


@pytest.mark.core
def test_issued_at_and_day_end_parse():
    utc = dt.timezone.utc
    want = int(dt.datetime(2026, 10, 29, 14, 31, 53, tzinfo=utc).timestamp()) * NS
    assert MK.issued_ns("2026-10-29T14:31:53.921000Z") == want + 921000000 and MK.issued_ns("2026-10-29T14:31:53+00:00") == want and MK.issued_ns("2026-10-29T14:31:53") == want
    assert MK.issued_ns(None) is None and MK.issued_ns("garbage") is None
    assert MK.day_end_ns("2026-10-29") == int(dt.datetime(2026, 10, 30, tzinfo=utc).timestamp()) * NS and MK.day_end_ns("2026-10-29") - MK.day_end_ns("2026-10-28") == DAY


# ------------------------------------------------------------------------------------------------ the paragraph and the caveat
SUMMARY = {"version": 1, "computed_at": "2026-11-11T09:00:00Z", "weeks": ["2026-W41", "2026-W42", "2026-W43"], "n_weeks": 3, "first_week": "2026-W41", "last_week": "2026-W43",
           "launches": 1200, "rate_min": 0.19, "rate_max": 0.47,
           "best": {"cell": "2w/v1", "mean_gain": -0.0123, "won": 1, "of": 3, "passes": False, "mean_auc": 0.6, "baseline": "2w"}, "beats_recent": False,
           "auc": {"min": 0.58, "max": 0.62},
           "worst_week": {"week": "2026-W43", "cell": "all/v1", "gap": 0.2, "n": 424,
                          "bands": [{"band": "Low", "n": 219, "predicted": 0.11, "observed": 0.45}, {"band": "Elevated", "n": 64, "predicted": 0.14, "observed": 0.38},
                                    {"band": "High", "n": 141, "predicted": 0.35, "observed": 0.56}]}}


@pytest.mark.core
def test_the_paragraph_is_made_from_the_stored_numbers_and_says_the_score_does_not_beat_the_market():
    t = MK.walk_forward_text(SUMMARY)
    p = t["paragraph"]
    assert "2026-11-11" in p and "3 weeks (2026-W41 to 2026-W43, 1200 launches)" in p and "2w/v1" in p and "beat that rate in 1 of 3 weeks." in p
    assert "plain rug rate of the previous 2 weeks" in p and "more than half of the compared weeks and on average" in p
    assert "log-loss" not in p and "mean gain" not in p and "-0.0123" not in p and "4 weeks" not in p.replace("each of 3 weeks", "")
    assert "from 0.58 to 0.62" in p and "from 19 % to 47 %" in p
    assert "does not yet beat the recent rug rate of the market" in p and t["beats"] is False
    assert t["caveat"] == ("Calibration on history understates risk when the market shifts: in 2026-W43 a model fitted on all earlier launches predicted rug rates of "
                           "11 / 14 / 35 % for Low / Elevated / High, and 45 / 38 / 56 % happened.")
    changed = MK.walk_forward_text({**SUMMARY, "n_weeks": 4, "launches": 1500, "auc": {"min": 0.5, "max": 0.7}})
    assert "4 weeks" in changed["paragraph"] and "1500 launches" in changed["paragraph"] and "from 0.50 to 0.70" in changed["paragraph"]


@pytest.mark.core
def test_the_sentence_follows_the_stored_verdict_it_is_not_a_fixed_claim():
    t = MK.walk_forward_text({**SUMMARY, "beats_recent": True})
    assert "did beat the recent rug rate of the market by that rule" in t["paragraph"] and "does not yet beat" not in t["paragraph"] and t["beats"] is True


@pytest.mark.core
def test_a_majority_of_weeks_with_a_negative_average_is_named_and_not_counted():
    won_but_lost = {**SUMMARY, "best": {**SUMMARY["best"], "won": 3, "of": 5}}
    p = MK.walk_forward_text(won_but_lost)["paragraph"]
    assert "beat that rate in 3 of 5 weeks but lost on average; we do not count that as better." in p and "does not yet beat the recent rug rate of the market" in p
    passes = {**SUMMARY, "beats_recent": True, "best": {**SUMMARY["best"], "won": 3, "of": 5, "mean_gain": 0.004, "passes": True}}
    q = MK.walk_forward_text(passes)["paragraph"]
    assert "beat that rate in 3 of 5 weeks and on average." in q and "lost on average" not in q


@pytest.mark.core
def test_a_win_against_the_4_week_rate_alone_neither_changes_the_verdict_nor_reaches_the_site():
    """the first server run: the best model lost to the 2-week constant (-0.0058) while one cell won against the 4-week constant (+0.0060, 2 of 3 weeks): one yardstick (2 weeks), the verdict does not move"""
    res = _result([(100, 0.2), (120, 0.45), (90, 0.3)], None)
    summ = _summ(gain4=0.006, won4=2)
    assert summ["gate"]["any_model_beats_other_recent_base_rate"] is True and summ["gate"]["any_model_beats_recent_base_rate"] is False
    s = WF.site_summary(res, summ, "t")
    assert s["beats_recent"] is False and "best_other" not in s and s["settings"]["baseline"] == "2w"
    p = MK.walk_forward_text(s)["paragraph"]
    assert "does not yet beat the recent rug rate of the market" in p and "4w/v2" not in p and "previous 4 weeks" not in p


@pytest.mark.core
def test_the_summary_verdict_is_the_gate_verdict_and_the_best_cell_is_one_that_passes_when_any_does():
    res = _result([(100, 0.2), (120, 0.45), (90, 0.3)], None)
    for beats in (False, True):
        summ = _summ(beats=beats, gain=0.01 if beats else -0.01, won=2 if beats else 1)
        s = WF.site_summary(res, summ, "t")
        assert s["beats_recent"] is summ["gate"]["any_model_beats_recent_base_rate"] is beats
        assert s["best"]["passes"] is beats and s["best"]["cell"] == "2w/v1"
    # a cell that wins most weeks with a tiny positive gain outranks a cell with a bigger gain over a minority of weeks
    summ = _summ(gain=0.002, won=2)
    summ["cells"]["all/v1"].update({"gain": {"2w": 0.05, "4w": None}, "wins": {"2w": {"won": 1, "of": 3}, "4w": {"won": 0, "of": 0}}})
    assert WF.site_summary(res, summ, "t")["best"]["cell"] == "2w/v1"


@pytest.mark.core
def test_no_summary_gives_a_neutral_line_and_a_week_that_was_not_understated_gives_no_caveat():
    t = MK.walk_forward_text(None)
    assert t["paragraph"] == MK.NEUTRAL and t["caveat"] is None and t["beats"] is None
    assert "does not" not in t["paragraph"]
    ok = {**SUMMARY, "worst_week": {**SUMMARY["worst_week"], "gap": -0.02}}
    assert MK.walk_forward_text(ok)["caveat"] is None and MK.walk_forward_text({**SUMMARY, "worst_week": None})["caveat"] is None


@pytest.mark.core
def test_reading_the_stored_summary(tmp_path):
    s = make(tmp_path)
    assert MK.read_summary(s.con) is None
    s.set_status(MK.SUMMARY_KEY, SUMMARY)
    s.con.commit()
    assert MK.read_summary(s.con)["n_weeks"] == 3
    for bad in ("not json", json.dumps({"version": 2, "weeks": [1]}), json.dumps([1, 2]), json.dumps({"version": 1, "weeks": []})):
        s.con.execute("UPDATE collector_status SET value = ? WHERE key = ?", (bad, MK.SUMMARY_KEY))
        assert MK.read_summary(s.con) is None


# ------------------------------------------------------------------------------------------------ the summary builder (pure)
def _result(rate_by_week, rugs_by_week, auc=0.6, understated=None):
    weeks = []
    for i, (n, rate) in enumerate(rate_by_week):
        bands = [{"band": b, "n": 50, "mean_p": 0.2, "rate": rate} for b in ("Low", "Elevated", "High")]
        weeks.append({"week": f"2026-W{40 + i}", "n": n, "rugs": int(n * rate), "rate": rate, "reference_auc": {"v1": auc + i * 0.01},
                      "baselines": {}, "cells": {"all/v1": {"bands": bands}, "2w/v1": {"skipped": "x"}}})
    return {"weeks": weeks, "settings": {"lag_h": 25, "min_week_n": 30}}


def _summ(gain=-0.01, won=1, of=3, beats=False, gain4=-0.02, won4=1):
    cells = {"all/v1": {"gain": {"2w": -0.02}, "wins": {"2w": {"won": 0, "of": 3}}, "mean_logloss": 0.7, "mean_auc": 0.58},
             "2w/v1": {"gain": {"2w": gain}, "wins": {"2w": {"won": won, "of": of}}, "mean_logloss": 0.65, "mean_auc": 0.6},
             "4w/v1": {"gain": {"2w": None}, "wins": {"2w": {"won": 0, "of": 0}}, "mean_logloss": None, "mean_auc": None}}
    for c in cells.values():
        c["gain"]["4w"], c["wins"]["4w"] = None, {"won": 0, "of": 0}
    cells["4w/v2"] = {"gain": {"2w": -0.03, "4w": gain4}, "wins": {"2w": {"won": 1, "of": 3}, "4w": {"won": won4, "of": 3}}, "mean_logloss": 0.66, "mean_auc": 0.61}
    q2 = {k: {n: WF.beats_rule(c["wins"][n]["won"], c["wins"][n]["of"], c["gain"][n]) for n in ("2w", "4w")} for k, c in cells.items()}
    return {"cells": cells, "gate": {"model_beats_recent_base_rate": q2, "any_model_beats_recent_base_rate": any(d["2w"] for d in q2.values()),
                                     "any_model_beats_other_recent_base_rate": any(d["4w"] for d in q2.values())}}


@pytest.mark.core
def test_site_summary_picks_the_best_cell_the_auc_range_and_the_worst_understated_week():
    res = _result([(100, 0.2), (120, 0.45), (90, 0.3)], None)
    s = WF.site_summary(res, _summ(), "2026-11-11T09:00:00Z")
    assert s["version"] == 1 and s["weeks"] == ["2026-W40", "2026-W41", "2026-W42"] and s["n_weeks"] == 3 and s["launches"] == 310
    assert (s["first_week"], s["last_week"], s["rate_min"], s["rate_max"]) == ("2026-W40", "2026-W42", 0.2, 0.45)
    assert s["best"]["cell"] == "2w/v1" and s["best"]["won"] == 1 and s["best"]["of"] == 3 and s["beats_recent"] is False and s["best"]["passes"] is False
    assert s["auc"] == {"min": 0.6, "max": 0.62}
    assert s["worst_week"]["week"] == "2026-W41" and s["worst_week"]["gap"] == pytest.approx(0.25) and [b["predicted"] for b in s["worst_week"]["bands"]] == [0.2, 0.2, 0.2]
    assert WF.site_summary(res, _summ(gain=0.01, won=2, beats=True), "t")["beats_recent"] is True
    json.dumps(s)                                                                  # storable


@pytest.mark.core
def test_site_summary_of_no_tested_week_is_still_storable_and_reads_as_no_summary():
    s = WF.site_summary({"weeks": [], "settings": {"lag_h": 25, "min_week_n": 30}}, {"cells": {}, "gate": {"model_beats_recent_base_rate": {}, "any_model_beats_recent_base_rate": False, "any_model_beats_other_recent_base_rate": False}}, "t")
    assert s["n_weeks"] == 0 and s["best"] is None and s["worst_week"] is None and s["auc"] == {"min": None, "max": None}
    assert MK.walk_forward_text(s)["paragraph"] == MK.NEUTRAL


# ------------------------------------------------------------------------------------------------ one function, three readers
@pytest.mark.core
def test_one_function_computes_the_rate_for_every_reader():
    root = Path(__file__).resolve().parents[1] / "rugyomi"
    sql = [str(p.relative_to(root)) for p in root.rglob("*.py") if "COALESCE(SUM(o.rug), 0)" in p.read_text(encoding="utf-8")]
    assert sql == ["rules/market.py"]
    for rel in ("web/queries.py", "publisher.py"):
        assert "market_mod.market_rate(" in (root / rel).read_text(encoding="utf-8"), rel
    for tpl in (root / "web" / "templates").glob("*.html"):
        text = tpl.read_text(encoding="utf-8")
        assert "walk-forward" not in text.lower() and "log-loss" not in text.lower()            # the sentence is data, not template text


# ------------------------------------------------------------------------------------------------ the pages and the daily summary
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import publisher as P  # noqa: E402
from rugyomi.collector import Collector  # noqa: E402
from rugyomi.keys import EnvKeySource  # noqa: E402
from rugyomi.neardata import Neardata  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi_testkit import refrax_replay_blocks, refrax_world_db  # noqa: E402

REFRAX = "refrax.nearlytrade.near"


@pytest.fixture()
def site(tmp_path):
    path = tmp_path / "w.db"
    refrax_world_db(path)
    s = open_store(path)
    row = s.con.execute("SELECT issued_at, launch_ts_ns FROM cards WHERE token = ?", (REFRAX,)).fetchone()
    issued = MK.issued_ns(row[0])
    for i in range(40):                                                             # 40 launches before the card, 10 rugs: 25 %
        add(s.con, f"p{i}.nearlytrade.near", issued - 3 * DAY - i * H, 1 if i < 10 else 0)
    for i in range(25):                                                             # after the card and inside 25 h: must be invisible on the page
        add(s.con, f"q{i}.nearlytrade.near", issued + (i + 1) * H, 1)
    for i in range(10):
        add(s.con, f"r{i}.nearlytrade.near", issued - 10 * H - i * H, 1)
    s.con.commit()
    s.close()
    return path, issued


def test_the_launch_page_shows_the_market_as_of_the_card_not_after(site):
    path, issued = site
    with TestClient(webapp.create_app(path)) as c:
        r = c.get(f"/launch/{REFRAX}")
    assert r.status_code == 200
    assert "Market context: Rug rate, last 14 days: 25 % (40 launches) (as of the card&#39;s issue time, outcomes known by then)" in r.text
    s = open_store(path)
    s.con.execute("UPDATE launch_outcomes SET rug = 0 WHERE token LIKE 'q%'")      # later labels change: the page must not move
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(path)) as c:
        assert "25 % (40 launches)" in c.get(f"/launch/{REFRAX}").text


def test_accuracy_and_methodology_render_the_stored_summary_and_a_neutral_line_without_it(site):
    path, issued = site
    with TestClient(webapp.create_app(path)) as c:
        a, m = c.get("/accuracy"), c.get("/methodology")
    for r in (a, m):
        assert r.status_code == 200 and MK.NEUTRAL in r.text and "Calibration caveat" not in r.text
        assert re.search(r"Rug rate, last 14 days:", r.text)
    s = open_store(path)
    s.set_status(MK.SUMMARY_KEY, SUMMARY)
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(path)) as c:
        a, m = c.get("/accuracy"), c.get("/methodology")
    for r in (a, m):
        assert "does not yet beat the recent rug rate of the market" in r.text and "3 weeks (2026-W41 to 2026-W43, 1200 launches)" in r.text and MK.NEUTRAL not in r.text
    assert "Calibration caveat" in a.text and "predicted rug rates of 11 / 14 / 35 %" in a.text and "What the score is worth" in m.text and "What the score is worth" in a.text


def test_the_daily_summary_carries_the_market_as_of_the_end_of_its_day(site):
    path, issued = site
    con = sqlite3.connect(path)
    day = dt.datetime.fromtimestamp(issued / 1e9, dt.timezone.utc).strftime("%Y-%m-%d")
    st = P.daily_stats(con, day)
    assert st["market"] == MK.market_rate(con, MK.day_end_ns(day))
    cfg = P.load_config()
    classic = P.format_daily(st, cfg)
    assert MK.market_text(st["market"], as_of="to the end of the day") in classic
    st2 = {**st, "worst": ("a.nearlytrade.near", 4.1), "group": ("grp_1", 5.2, 2), "rugs": 4, "loss_near": 12.3}
    compact = P.format_daily_compact(st2, {**cfg, "site": {**cfg["site"], "base_url": "https://rugyomi.example"}})
    assert len(compact.splitlines()) <= 8 and MK.market_short(st["market"]) in compact
    assert P.format_daily_compact({k: v for k, v in st2.items() if k != "market"}, cfg).count("market") == 0       # an old stats dict still renders
