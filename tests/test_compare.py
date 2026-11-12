"""RGY-051: the paired bootstrap says 'better' / 'worse' / 'no detectable difference' (a tie is a tie), is reproducible with a seed and resamples inside the weeks; the per-launchpad
baseline and the fixed-points probabilities use the past only (mutation tests)."""
import copy
import random

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi.rules import compare as CP
from rugyomi.rules import walkforward as WF
from test_walkforward import DAY, MON0, world

B = 300                                    # enough for the tests; the server diagnostic uses 2000


def data(n=600, seed=3, weeks=6):
    rnd = random.Random(seed)
    ys = [int(rnd.random() < 0.3) for _ in range(n)]
    wk = [f"W{i * weeks // n}" for i in range(n)]
    return ys, wk, rnd


# ------------------------------------------------------------------------------------------------ the verdict
@pytest.mark.core
def test_the_verdict_names_a_tie_a_win_and_a_loss():
    assert CP.verdict_of(0.001, 0.02) == "better" and CP.verdict_of(-0.02, -0.001) == "worse"
    assert CP.verdict_of(-0.01, 0.02) == "no detectable difference" and CP.verdict_of(0.0, 0.0) == "no detectable difference"
    assert CP.verdict_of(None, 0.1) is None


def test_two_identical_scores_are_a_tie_and_so_are_two_equally_good_noisy_ones():
    ys, wk, rnd = data()
    s = [y * 0.7 + rnd.random() for y in ys]
    same = CP.compare_auc(s, s, ys, wk, B=B)
    assert same["delta"] == 0 and same["verdict"] == "no detectable difference" and same["lo"] == same["hi"] == 0
    a = [y * 0.7 + rnd.random() for y in ys]               # the same strength, other noise
    b = [y * 0.7 + rnd.random() for y in ys]
    for mode in ("pooled", "weekly"):
        got = CP.compare_auc(a, b, ys, wk, mode=mode, B=B)
        assert got["verdict"] == "no detectable difference" and got["lo"] < 0 < got["hi"], (mode, got)


def test_a_clear_win_is_better_and_the_other_way_round_is_worse():
    ys, wk, rnd = data()
    weak = [y * 0.1 + rnd.random() for y in ys]
    strong = [y * 1.5 + rnd.random() for y in ys]
    win = CP.compare_auc(weak, strong, ys, wk, B=B)
    assert win["verdict"] == "better" and win["lo"] > 0 and win["auc_b"] > win["auc_a"] + 0.1
    lose = CP.compare_auc(strong, weak, ys, wk, B=B)
    assert lose["verdict"] == "worse" and lose["hi"] < 0
    assert CP.compare_auc(weak, strong, ys, wk, mode="weekly", B=B)["verdict"] == "better"


def test_the_loss_gain_has_the_sign_of_the_rule_and_a_tie_in_loss_is_a_tie():
    ys, wk, rnd = data()
    la = [rnd.random() for _ in ys]
    lb = [x - 0.3 for x in la]                              # B has the lower loss by 0.3 everywhere
    g = CP.compare_loss(la, lb, wk, B=B)
    assert g["gain"] == pytest.approx(0.3) and g["verdict"] == "better" and g["loss_a"] > g["loss_b"]
    assert CP.compare_loss(lb, la, wk, B=B)["verdict"] == "worse"
    noise = [rnd.gauss(0, 1) for _ in ys]
    tie = CP.compare_loss([x + e for x, e in zip(la, noise)], [x - e for x, e in zip(la, noise)], wk, B=B)
    assert tie["verdict"] == "no detectable difference"
    assert CP.compare_loss(la, lb, wk, mode="pooled", B=B)["verdict"] == "better"
    with pytest.raises(ValueError):
        CP.compare_loss(la, lb, wk, mode="x")


@pytest.mark.core
def test_head_to_head_is_the_one_rule_of_q025():
    wk = ["a"] * 4 + ["b"] * 4 + ["c"] * 4
    h = CP.head_to_head([1.0] * 12, [0.9] * 4 + [0.9] * 4 + [1.1] * 4, wk)
    assert (h["won"], h["of"]) == (2, 3) and h["gain"] == pytest.approx((0.1 + 0.1 - 0.1) / 3) and h["better"] is True
    h2 = CP.head_to_head([1.0] * 12, [0.99] * 8 + [1.5] * 4, wk)                     # two of three weeks, but a negative mean gain: not better
    assert (h2["won"], h2["of"]) == (2, 3) and h2["better"] is False
    assert h["better"] == WF.beats_rule(h["won"], h["of"], h["gain"])


# ------------------------------------------------------------------------------------------------ the bootstrap itself
def test_the_bootstrap_is_reproducible_with_a_seed_and_moves_with_another():
    ys, wk, rnd = data()
    a = [y * 0.3 + rnd.random() for y in ys]
    b = [y * 0.6 + rnd.random() for y in ys]
    one, two = CP.compare_auc(a, b, ys, wk, B=B, seed=1), CP.compare_auc(a, b, ys, wk, B=B, seed=1)
    assert one == two
    assert CP.compare_auc(a, b, ys, wk, B=B, seed=2)["lo"] != one["lo"]
    assert one["B"] == B and one["lo"] < one["delta"] < one["hi"]


@pytest.mark.core
def test_launches_are_resampled_inside_their_week_and_the_weeks_keep_their_size():
    seen = []

    def stat(groups):
        seen.append([len(g) for g in groups])
        return float(sum(len(g) for g in groups))

    groups = CP.strata(["w2", "w1", "w1", "w3", "w2", "w1"])
    assert groups == [[1, 2, 5], [0, 4], [3]]                                         # by week, in week order
    r = CP.bootstrap(stat, groups, B=50, seed=1)
    assert len(seen) == 51 and all(s == [3, 2, 1] for s in seen) and r["B"] == 50
    members = []
    CP.bootstrap(lambda gs: members.append(gs) or 1.0, groups, B=30, seed=1)
    for gs in members:
        for g, orig in zip(gs, groups):
            assert set(g) <= set(orig)                                               # never a launch of another week


@pytest.mark.core
def test_a_statistic_that_is_sometimes_undefined_still_gives_an_interval_unless_it_is_mostly_undefined():
    ys = [1] * 2 + [0] * 98                                                          # a resample without a rug has no AUC
    s = [float(i) for i in range(100)]
    r = CP.compare_auc(s, s[::-1], ys, ["w"] * 100, B=100)
    assert r["B"] < 100 and r["lo"] is not None
    none = CP.compare_auc(s, s, [0] * 100, ["w"] * 100, B=100)
    assert none["verdict"] is None and none["lo"] is None and none["delta"] is None


# ------------------------------------------------------------------------------------------------ the baselines: the past only
@pytest.mark.core
def test_the_baseline_is_the_launchpads_own_recent_rate_and_the_pooled_rate_where_it_has_too_few():
    rows = world(weeks=4, n=60)
    wk = WF.week_start_ns(MON0 + 3 * 7 * DAY + DAY)
    test = [r for r in rows if WF.week_start_ns(r["launch_ts_ns"]) == wk]
    got = CP.baseline_probs(rows, wk, test)
    recent = WF.train_rows(rows, wk, 14)
    pooled = sum(r["rug"] for r in recent) / len(recent)
    nearly = [r["rug"] for r in recent if r["launchpad"] == "nearly"]
    assert all(g["pooled"] == pooled for g in got)
    for r, g in zip(test, got):
        if r["launchpad"] == "nearly":
            assert g["pad"] == pytest.approx(sum(nearly) / len(nearly)) and not g["fallback"]
    few = CP.baseline_probs(rows, wk, test, pad_min_n=10**6)                         # nobody has that many: every launchpad falls back
    assert all(g["fallback"] and g["pad"] == g["pooled"] for g in few)


@pytest.mark.core
def test_the_baseline_does_not_see_the_week_or_anything_after_it():
    rows = world(weeks=4, n=60)
    wk = WF.week_start_ns(MON0 + 3 * 7 * DAY + DAY)
    test = [r for r in rows if WF.week_start_ns(r["launch_ts_ns"]) == wk]
    before = CP.baseline_probs(rows, wk, test)
    mutated = copy.deepcopy(rows)
    for r in mutated:
        if r["launch_ts_ns"] >= wk:
            r["rug"] = 1 - r["rug"]                                                  # the week itself and every later launch flips its outcome
    after = CP.baseline_probs(mutated, wk, [r for r in mutated if WF.week_start_ns(r["launch_ts_ns"]) == wk])
    assert before == after
    recent_known = [r for r in mutated if r["launch_ts_ns"] < wk and WF.known_ns(r) > wk]   # a launch whose outcome is not known yet at the week start is not used either
    for r in recent_known:
        r["rug"] = 1 - r["rug"]
    assert CP.baseline_probs(mutated, wk, [r for r in mutated if WF.week_start_ns(r["launch_ts_ns"]) == wk]) == before


# ------------------------------------------------------------------------------------------------ the fixed points
@pytest.mark.core
def test_fixed_probabilities_follow_the_points_and_come_from_the_past_only():
    rows = world(weeks=6, n=60)
    t = CP.fixed_table(rows, ("v1", "v2"))
    assert t["token"] and len(set(t["week"])) >= 3 and len(t["y"]) == len(t["p"]["v1"]) == len(t["base"]["pad"])
    for w in set(t["week"]):                                                         # the scale is monotone: a higher score never gets a lower probability inside a week
        ps = sorted((s, p) for s, p, wk in zip(t["score"]["v1"], t["p"]["v1"], t["week"]) if wk == w)
        assert all(a[1] <= b[1] + 1e-12 for a, b in zip(ps, ps[1:]))
    mutated = copy.deepcopy(rows)
    cut = WF.week_start_ns(MON0 + 4 * 7 * DAY)                                       # flip every outcome from week 5 on
    for r in mutated:
        if r["launch_ts_ns"] >= cut:
            r["rug"] = 1 - r["rug"]
    m = CP.fixed_table(mutated, ("v1", "v2"))
    early = [i for i, wk in enumerate(t["week"]) if WF.week_label(cut) > wk]
    assert early and [t["p"]["v1"][i] for i in early] == [m["p"]["v1"][i] for i in range(len(early))]
    assert [t["base"]["pad"][i] for i in early] == [m["base"]["pad"][i] for i in range(len(early))]


def test_a_week_without_enough_history_or_launches_is_skipped_and_says_why():
    rows = world(weeks=3, n=60)
    t = CP.fixed_table(rows, ("v1",))
    assert {s["week"] for s in t["skipped"]} and all("too few" in s["why"] or "final launches" in s["why"] for s in t["skipped"])
    assert CP.fixed_table(rows, ("v1",), only_weeks=["2099-W01"])["token"] == []
    assert CP.fixed_table(rows, ("nope",))["token"] == []                            # a profile the rows do not carry


def test_refit_forecasts_are_joined_by_token_and_losses_use_the_launches_both_sides_have():
    rows = world(weeks=8, n=60)
    res = WF.evaluate(rows, feature_sets=("v1",))
    assert all("token" in p for p in res["preds"])
    t = CP.fixed_table(rows, ("v1",))
    CP.add_refit(t, res, ["all/v1", "2w/v1"])
    assert len(t["p"]["refit:all/v1"]) == len(t["token"]) and any(p is not None for p in t["p"]["refit:all/v1"])
    la, lb, wk, idx = CP.losses(t, "v1", "refit:all/v1")
    assert len(la) == len(lb) == len(wk) == len(idx) and len(idx) == sum(p is not None for p in t["p"]["refit:all/v1"])
    la2, lb2, _, idx2 = CP.losses(t, "base:pooled", "base:pad", only=lambda i: t["distinct"][i])
    assert len(idx2) == sum(t["distinct"]) and all(x >= 0 for x in la2 + lb2)
    assert CP.logloss_row(0.5, 1) == pytest.approx(0.6931, abs=1e-3) and CP.logloss_row(0.0, 1) == CP.logloss_row(WF.P_MIN, 1)
