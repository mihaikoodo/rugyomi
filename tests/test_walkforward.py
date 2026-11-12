"""RGY-038: walk-forward evaluation by week - no lookahead (mutation tests), the windows, the metrics, the summary and the gate; the by-week block of the accuracy table."""
import copy
import datetime as dt
import json
import random

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi.rules import accuracy_summary
from rugyomi.rules import walkforward as WF
from rugyomi.rules.accuracy import week_of

UTC = dt.timezone.utc
DAY = WF.DAY_NS


def ns(y, m, d, h=0, mi=0):
    return int(dt.datetime(y, m, d, h, mi, tzinfo=UTC).timestamp()) * 10**9


MON0 = ns(2026, 10, 19)               # Monday, ISO week 43


def row(i, ts, rug, a, take=None, pad="nearly", final=1):
    f1, f2 = {}, {}
    if a:
        f1["funded_by_rugger"] = f2["funded_by_rugger"] = 30
    vals = {}
    if take is not None:
        f1["creator_tax"] = f2["creator_tax"] = 15
        vals["creator_tax"] = {"take_bps": take}
    score = (30 if a else 0) + (15 if take is not None else 0)
    band = "High" if score >= 45 else "Elevated" if score >= 30 else "Low"
    card = {"score": score, "band": band, "config_version": "x", "reasons": list(f1), "values": vals}
    return {"token": f"t{i}.{pad}.near", "launch_ts_ns": ts, "launchpad": pad, "source": "live", "rug": rug, "final": final,
            "cards": {"v1": {**card, "fired": f1}, "v2": {**card, "fired": f2}}}


def world(weeks=6, n=60, p_a=0.2, p_rug_a=0.85, p_rug_no=0.1, seed=1, flip_after=None, start=MON0):
    """`n` launches per week from `start`; signal A fires in `p_a` of them. After week `flip_after` the signal means nothing (the market moved)."""
    rnd = random.Random(seed)
    rows, i = [], 0
    for w in range(weeks):
        for k in range(n):
            a = rnd.random() < p_a
            ra, rn = (p_rug_a, p_rug_no) if flip_after is None or w < flip_after else (0.3, 0.3)
            rug = int(rnd.random() < (ra if a else rn))
            ts = start + w * 7 * DAY + int((k + 0.5) / n * 7 * DAY)
            rows.append(row(i, ts, rug, a, take=rnd.choice([None, 4.0, 9.0, 20.0]) if a else None, pad="nearly" if k % 3 else "justhoot"))
            i += 1
    return rows


def cell(res, week, key):
    return next(w for w in res["weeks"] if w["week"] == week)["cells"][key]


# ------------------------------------------------------------------------------------------------ the weeks and the lookahead rule
@pytest.mark.core
def test_weeks_are_iso_weeks_monday_to_sunday_utc():
    assert WF.week_label(WF.week_start_ns(ns(2026, 11, 8, 23, 59))) == "2026-W45"          # Sunday night
    assert WF.week_label(WF.week_start_ns(ns(2026, 11, 9, 0, 0))) == "2026-W46"            # Monday 00:00
    assert WF.week_start_ns(ns(2026, 11, 5, 12)) == ns(2026, 11, 2)
    assert week_of(ns(2026, 11, 8, 23, 59)) == ("2026-W45", "2026-11-02") and week_of(ns(2026, 11, 9)) == ("2026-W46", "2026-11-09")
    assert week_of(ns(2026, 12, 31, 12))[0] == "2026-W53" and week_of(ns(2027, 1, 1, 12))[0] == "2026-W53"      # the ISO year is not the calendar year


@pytest.mark.core
def test_a_launch_enters_a_fit_only_when_its_outcome_was_known_at_the_start_of_the_week():
    start = ns(2026, 11, 2)
    lag = int(WF.LAG_H * 3600 * 10**9)
    on_time = row(1, start - lag, 0, False)                                                 # known exactly at the start: in
    late = row(2, start - lag + 1, 0, False)                                                # known one ns after: out
    just_before = row(3, start - 1, 1, True)                                                # launched before the week, outcome only a day later: out
    in_week = row(4, start + 5, 1, True)
    later = row(5, start + 9 * DAY, 1, True)
    got = {r["token"] for r in WF.train_rows([on_time, late, just_before, in_week, later], start, None)}
    assert got == {on_time["token"]}
    held = row(6, start - 10 * DAY, 0, False)
    held["final_ns"] = start + 1                                                            # launched long ago, but the outcome became final after the start
    assert held["token"] not in {r["token"] for r in WF.train_rows([held], start, None)}
    held["final_ns"] = start
    assert held["token"] in {r["token"] for r in WF.train_rows([held], start, None)}


@pytest.mark.core
def test_the_recent_windows_take_only_launches_within_their_days():
    start = ns(2026, 11, 16)
    rows = [row(i, start - d * DAY - 2 * 3600 * 10**9 * 12, 0, False) for i, d in enumerate((1, 8, 13, 15, 27, 29, 60))]
    n = lambda days: len(WF.train_rows(rows, start, days))                                  # noqa: E731
    assert (n(None), n(14), n(28)) == (7, 3, 5)


def test_changing_an_outcome_known_only_after_the_week_starts_changes_nothing_in_that_weeks_forecast():
    rows = world(weeks=6, n=60, seed=3)
    target = "2026-W46"
    start = WF.week_start_ns(next(r for r in rows if WF.week_label(WF.week_start_ns(r["launch_ts_ns"])) == target)["launch_ts_ns"])
    base = WF.evaluate(rows, only_weeks=[target])
    assert [w["week"] for w in base["weeks"]] == [target]
    mutated = copy.deepcopy(rows)
    flipped = 0
    for r in mutated:
        if WF.known_ns(r) > start and r["launch_ts_ns"] < start + 7 * DAY:                  # the last day before the week: launched before it, outcome not yet known
            r["rug"] = 1 - r["rug"]
            flipped += 1
    for r in mutated:
        if r["launch_ts_ns"] >= start + 7 * DAY:                                            # every later week
            r["rug"] = 1 - r["rug"]
    assert flipped > 0
    for r in mutated:                                                                       # the TEST week's own labels stay: only the rule 'known by the start' is under test
        if start <= r["launch_ts_ns"] < start + 7 * DAY:
            orig = next(o for o in rows if o["token"] == r["token"])
            r["rug"] = orig["rug"]
    again = WF.evaluate(mutated, only_weeks=[target])
    for key in base["weeks"][0]["cells"]:
        a, b = cell(base, target, key), cell(again, target, key)
        assert a == b, key
    assert base["weeks"][0]["baselines"] == again["weeks"][0]["baselines"]
    # sanity: flipping an outcome that WAS known does change the forecast
    known = copy.deepcopy(rows)
    for r in known:
        if WF.known_ns(r) <= start:
            r["rug"] = 1 - r["rug"]
    assert cell(WF.evaluate(known, only_weeks=[target]), target, "all/v1") != cell(base, target, "all/v1")


def test_a_week_is_never_fitted_on_itself_or_the_future():
    rows = world(weeks=5, n=60, seed=5)
    res = WF.evaluate(rows)
    for w in res["weeks"]:
        start = WF.week_start_ns(next(r for r in rows if WF.week_label(WF.week_start_ns(r["launch_ts_ns"])) == w["week"])["launch_ts_ns"])
        for name, days in WF.WINDOWS:
            t = WF.train_rows(rows, start, days)
            assert all(WF.known_ns(r) <= start for r in t)
            b = w["baselines"][name]
            if b:
                assert b["n_train"] == len(t) and b["rate"] == pytest.approx(sum(r["rug"] for r in t) / len(t))


# ------------------------------------------------------------------------------------------------ what is evaluated
def test_small_weeks_and_a_first_week_without_history_are_skipped_with_the_reason():
    rows = world(weeks=4, n=60, seed=2, p_rug_no=0.3)
    rows += [row(1000 + i, ns(2026, 11, 23, 12) + i, 0, False) for i in range(5)]           # a fifth week of 5 launches
    res = WF.evaluate(rows)
    assert [w["week"] for w in res["weeks"]] == ["2026-W44", "2026-W45", "2026-W46"]
    why = {s["week"]: s["why"] for s in res["skipped"]}
    assert "no earlier launches" in why["2026-W43"] and "5 final launches" in why["2026-W48"]


def test_undecided_and_not_final_launches_are_not_counted_anywhere():
    rows = world(weeks=4, n=60, seed=2)
    extra = [dict(row(2000 + i, ns(2026, 11, 10, 12) + i, 1, True), final=0) for i in range(40)] + [dict(row(3000 + i, ns(2026, 11, 10, 13) + i, None, False)) for i in range(40)]
    a, b = WF.evaluate(rows), WF.evaluate(rows + extra)
    assert [(w["n"], w["rugs"]) for w in a["weeks"]] == [(w["n"], w["rugs"]) for w in b["weeks"]]
    assert a["weeks"][-1]["cells"] == b["weeks"][-1]["cells"]


def test_every_cell_has_the_metrics_and_the_baselines_are_the_constants_of_their_window():
    rows = world(weeks=5, n=60, seed=4)
    res = WF.evaluate(rows)
    w = res["weeks"][-1]
    assert set(w["cells"]) == {f"{n}/{fs}" for n in ("all", "2w", "4w") for fs in ("v1", "v2")}
    for c in w["cells"].values():
        assert 0 <= c["auc"] <= 1 and c["logloss"] > 0 and 0 <= c["brier"] <= 1 and c["n"] == w["n"]
        assert [b["band"] for b in c["bands"]] == ["Low", "Elevated", "High"] and sum(b["n"] for b in c["bands"]) == w["n"]
        assert c["train_n"] >= WF.MIN_TRAIN and c["l2"] in WF.L2S
    assert w["reference_auc"]["v1"] is not None
    b = w["baselines"]["all"]
    assert b["logloss"] == pytest.approx(WF.logloss([(b["rate"], r["rug"]) for r in rows if week_of(r["launch_ts_ns"])[0] == w["week"]]))


@pytest.mark.core
def test_the_logloss_is_clipped_and_the_constants_are_exact():
    assert WF.logloss([(0.5, 1), (0.5, 0)]) == pytest.approx(0.6931, abs=1e-4)
    assert WF.logloss([(0.0, 1)]) == pytest.approx(-__import__("math").log(0.01)) and WF.logloss([(1.0, 0)]) == pytest.approx(-__import__("math").log(0.01))
    assert WF.logloss([]) is None


def test_two_runs_give_the_same_numbers():
    rows = world(weeks=5, n=60, seed=7)
    assert json.dumps(WF.evaluate(rows), sort_keys=True) == json.dumps(WF.evaluate(copy.deepcopy(rows)), sort_keys=True)
    res = WF.evaluate(rows)
    assert json.dumps(WF.summarize(res), sort_keys=True) == json.dumps(WF.summarize(res), sort_keys=True)


def test_a_signal_that_means_something_beats_the_base_rate_and_noise_does_not():
    good = WF.summarize(WF.evaluate(world(weeks=7, n=80, seed=1)))
    c = good["cells"]["all/v1"]
    assert c["weeks"] == 6 and c["mean_auc"] > 0.7 and c["gain"]["all"] > 0.02 and c["wins"]["all"]["won"] >= 5
    assert good["gate"]["model_beats_recent_base_rate"]["all/v1"]["2w"] is True and good["gate"]["any_model_beats_recent_base_rate"] is True
    noise = WF.summarize(WF.evaluate(world(weeks=7, n=80, p_rug_a=0.25, p_rug_no=0.25, seed=2)))
    assert all(abs(c["gain"][n]) < 0.01 for c in noise["cells"].values() for n in ("all", "2w", "4w") if c["gain"][n] is not None)      # no real gain, whatever the coin says
    assert all(c["gain"]["all"] < 0.01 for c in noise["cells"].values()) and good["cells"]["all/v1"]["gain"]["all"] > 5 * max(c["gain"]["all"] for c in noise["cells"].values())


def test_after_the_market_moves_a_recent_window_wins_over_all_history():
    s = WF.summarize(WF.evaluate(world(weeks=14, n=80, seed=1, flip_after=3)))           # the signal worked for 3 weeks, then meant nothing
    r2 = s["recent_vs_all"]["v1"]["2w"]
    assert r2["of"] >= 8 and r2["won"] * 2 > r2["of"]                                      # the two-week window follows the move; the four-week one lags it
    assert s["gate"]["recent_window_beats_all_history"]["v1"]["2w"] is True and s["gate"]["any_recent_window_beats_all"] is True
    assert sum(s["window_wins"]["v1"]["best_window"].values()) == s["window_wins"]["v1"]["weeks"]
    assert s["baseline_recent_vs_all"]["4w"]["of"] > 0


def test_the_launchpad_split_pools_the_forecasts_and_appears_only_where_n_allows():
    s = WF.summarize(WF.evaluate(world(weeks=7, n=80, seed=1)))
    pads = {(x["cell"], x["launchpad"]) for x in s["by_launchpad"]}
    assert ("all/v1", "nearly") in pads and ("all/v1", "justhoot") in pads
    x = next(x for x in s["by_launchpad"] if x["cell"] == "all/v1" and x["launchpad"] == "justhoot")
    assert x["n"] >= WF.MIN_LAUNCHPAD_N and set(x["baselines"]) == {"all", "2w", "4w"}
    small = WF.summarize(WF.evaluate(world(weeks=3, n=35, seed=1)))
    assert small["by_launchpad"] == [] or all(x["n"] >= WF.MIN_LAUNCHPAD_N for x in small["by_launchpad"])


def test_the_v2_features_take_their_edges_from_the_training_rows_only():
    rows = world(weeks=5, n=80, seed=1)
    start = WF.week_start_ns(rows[-1]["launch_ts_ns"])
    train = WF.train_rows(rows, start, None)
    res = WF.evaluate(rows, only_weeks=[WF.week_label(start)])
    assert cell(res, WF.week_label(start), "all/v2")["features"] >= cell(res, WF.week_label(start), "all/v1")["features"] - 1
    cell_ = WF._fit_cell(train, [r for r in rows if r not in train], "v2", (1.0,), 15)
    from rugyomi.rules import fit as FIT
    assert cell_["edges"] == FIT.choose_edges(train, "v2")


def test_a_training_set_with_one_class_or_too_few_launches_makes_no_model_and_says_why():
    rows = world(weeks=4, n=60, seed=1, p_rug_a=0.0, p_rug_no=0.0)
    res = WF.evaluate(rows)
    assert res["weeks"] == [] and all("enough launches of both classes" in k["why"] for k in res["skipped"] if "W43" not in k["week"])
    s = WF.summarize(res)
    assert s["cells"]["all/v1"]["weeks"] == 0 and s["cells"]["all/v1"]["mean_logloss"] is None
    few = world(weeks=4, n=60, seed=1, p_rug_a=0.0, p_rug_no=0.0)
    for k, r in enumerate(few[:60]):                                                        # the first week has 3 rugs: more than none, fewer than 10
        r["rug"] = int(k < 3)
    res = WF.evaluate(few)
    why = {k["week"]: k["why"] for k in res["skipped"]}
    assert res["weeks"] == [] and "3 rugs and" in why["2026-W44"] and "(each needs >= 10)" in why["2026-W44"]
    res = WF.evaluate(world(weeks=8, n=60, seed=1), min_train=130)                         # two weeks of history hold ~120 launches
    last = res["weeks"][-1]
    assert last["cells"]["2w/v1"]["skipped"].endswith("training launches (< 130)") and last["baselines"]["2w"] is None and "skipped" not in last["cells"]["all/v1"]
    assert WF.summarize(res)["cells"]["2w/v1"]["weeks"] < len(res["weeks"])


@pytest.mark.core
def test_the_module_is_pure():
    import ast
    from pathlib import Path
    src = Path(WF.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = {n.module if isinstance(n, ast.ImportFrom) else a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])}
    assert not any(m and (m.startswith("sqlite3") or "store" in m or m == "time" or "requests" in m) for m in mods), mods
    assert "open(" not in src and "datetime.now" not in src and "utcnow" not in src


# ------------------------------------------------------------------------------------------------ the by-week block of the accuracy table
def card_row(i, ts, band, rug, final=1, score=None):
    return {"token": f"t{i}.nearlytrade.near", "launch_ts_ns": ts, "issued_at": "x", "score": score if score is not None else {"Low": 5, "Elevated": 45, "High": 75}[band],
            "band": band, "config_version": "v0-default-1", "rug": rug, "final": final, "severity_near": None, "dev_exit": None, "buyers_loss": None}


@pytest.mark.core
def test_the_accuracy_table_has_a_by_week_block_with_the_band_split():
    rows = [card_row(0, ns(2026, 11, 8, 23, 59), "High", 1), card_row(1, ns(2026, 11, 8, 12), "Low", 0), card_row(2, ns(2026, 11, 9, 0, 0), "High", 0),
            card_row(3, ns(2026, 11, 10, 9), "Elevated", 1), card_row(4, ns(2026, 11, 10, 10), "Low", None, final=0), card_row(5, ns(2026, 11, 15, 22), "Low", 0)]
    a = accuracy_summary(rows)
    assert [w["week"] for w in a["by_week"]] == ["2026-W46", "2026-W45"]                  # newest first
    w46, w45 = a["by_week"]
    assert (w45["monday"], w45["cards"], w45["final"], w45["rugs"], w45["rate"]) == ("2026-11-02", 2, 2, 1, 0.5)
    assert (w46["monday"], w46["cards"], w46["final"], w46["rugs"]) == ("2026-11-09", 4, 3, 1)
    assert [(b["band"], b["n"], b["rugs"]) for b in w45["bands"]] == [("Low", 1, 0), ("Elevated", 0, 0), ("High", 1, 1)]
    assert [(b["band"], b["n"], b["rugs"]) for b in w46["bands"]] == [("Low", 1, 0), ("Elevated", 1, 1), ("High", 1, 0)]
    assert all(b["too_few"] for b in w45["bands"] + w46["bands"])                        # MIN_N = 10
    assert sum(w["cards"] for w in a["by_week"]) == a["summary"]["cards"] == sum(d["cards"] for d in a["by_day"])
    assert accuracy_summary([])["by_week"] == []


def test_a_window_that_holds_the_same_launches_as_all_history_is_not_counted_as_a_win_or_a_loss():
    res = WF.evaluate(world(weeks=6, n=60, seed=1))
    s = WF.summarize(res)
    first, last = res["weeks"][0], res["weeks"][-1]
    assert not WF.distinct(first, "2w") and not WF.distinct(first, "4w") and WF.distinct(first, "all")
    assert WF.distinct(last, "2w") and WF.distinct(last, "4w")
    n_distinct_2w = sum(1 for w in res["weeks"] if WF.distinct(w, "2w"))
    assert s["recent_vs_all"]["v1"]["2w"]["of"] == n_distinct_2w < len(res["weeks"])
    assert s["baseline_recent_vs_all"]["2w"]["of"] == n_distinct_2w and s["cells"]["2w/v1"]["wins"]["2w"]["of"] == n_distinct_2w
    assert s["cells"]["all/v1"]["wins"]["all"]["of"] == len(res["weeks"])
    assert s["window_wins"]["v1"]["weeks"] == n_distinct_2w and sum(s["window_wins"]["v1"]["best_window"].values()) == n_distinct_2w
    assert WF.summarize(WF.evaluate(world(weeks=3, n=60, seed=1)))["recent_vs_all"]["v1"]["2w"] == {"won": 0, "of": 0}


@pytest.mark.core
def test_the_verdict_counts_only_the_two_week_constant_and_one_rule():
    """RGY-044 / Q-025: a cell that beats the 4-week constant on most weeks with a positive gain does not make the verdict; the same cell against 2 weeks does"""
    assert WF.beats_rule(2, 3, 0.006) is True and WF.beats_rule(3, 5, -0.0058) is False and WF.beats_rule(1, 3, 0.0443) is False and WF.beats_rule(0, 0, 0.1) is False
    assert WF.beats_rule(1, 2, 0.1) is False                                       # exactly half is not "more than half"
    def cell(g2, w2, g4, w4):
        return {"gain": {"2w": g2, "4w": g4}, "wins": {"2w": {"won": w2, "of": 5}, "4w": {"won": w4, "of": 3}}}
    base = {"2w": {"won": 0, "of": 0}, "4w": {"won": 0, "of": 0}}
    only4 = WF._gate({"4w/v2": cell(-0.01, 2, 0.006, 2)}, {"v1": base}, base, ["2w", "4w"], {"feature_sets": ["v1"]})
    assert only4["any_model_beats_recent_base_rate"] is False and only4["any_model_beats_other_recent_base_rate"] is True and only4["verdict_baseline"] == "2w"
    in2 = WF._gate({"2w/v1": cell(0.004, 3, -0.01, 1)}, {"v1": base}, base, ["2w", "4w"], {"feature_sets": ["v1"]})
    assert in2["any_model_beats_recent_base_rate"] is True and in2["any_model_beats_other_recent_base_rate"] is False
