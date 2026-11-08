"""RGY-021 step 2: the statistics of the as-of backtest (`rugyomi.rules.backtest`) on small synthetic sets."""
import math

import pytest

import rugyomi_testkit  # noqa: F401  (puts the package on sys.path)

from rugyomi.rules import backtest as BT
from rugyomi.rules.accuracy import wilson

NS = 10**9


def row(score, rug, ts=1, lp="nearly", src="history", fired=None, band=None):
    band = band or ("High" if score >= 60 else "Elevated" if score >= 30 else "Low")
    return {"token": f"t{score}-{rug}-{ts}-{id(fired)}", "launch_ts_ns": ts * NS, "launchpad": lp, "source": src, "issued_at": None,
            "rug": rug, "final": 1, "severity_near": 2.0 if rug else None, "dev_exit": None, "buyers_loss": None,
            "cards": {"p": {"score": score, "band": band, "config_version": "x", "fired": fired or {}, "reasons": list(fired or {})}}}


def test_auc_is_the_probability_a_rug_outscores_a_non_rug_ties_half():
    rows = [row(80, 1), row(10, 0), row(10, 0), row(50, 1)]
    assert BT.auc(BT.view(rows, "p")) == 1.0
    assert BT.auc(BT.view([row(10, 1), row(10, 0)], "p")) == 0.5            # a tie
    assert BT.auc(BT.view([row(10, 0), row(80, 1), row(10, 1)], "p")) == pytest.approx((1 + 0.5) / 2)
    assert BT.auc(BT.view([row(10, 0)], "p")) is None


def test_split_is_by_launch_time_and_the_boundary_belongs_to_the_holdout():
    rows = [row(10, 0, ts=5), row(10, 1, ts=10), row(10, 0, ts=15)]
    train, hold = BT.split(rows, 10 * NS)
    assert [r["launch_ts_ns"] // NS for r in train] == [5] and [r["launch_ts_ns"] // NS for r in hold] == [10, 15]


def test_isotonic_pools_adjacent_violators_and_is_monotone():
    rows = ([row(0, 0)] * 8 + [row(0, 1)] * 2          # score 0: 20 %
            + [row(40, 0)] * 9 + [row(40, 1)] * 1      # score 40: 10 %  <- violates, pooled with 0
            + [row(70, 1)] * 3 + [row(70, 0)] * 1)     # score 70: 75 %
    blocks = BT.isotonic(BT.view(rows, "p"), min_n=1)
    assert [(b["lo_score"], b["hi_score"], round(b["p"], 3), b["n"]) for b in blocks] == [(0, 40, 0.15, 20), (70, 70, 0.75, 4)]
    ps = [b["p"] for b in blocks]
    assert ps == sorted(ps)
    assert BT.apply_calibration(blocks, 55) == 0.15 and BT.apply_calibration(blocks, 99) == 0.75 and BT.apply_calibration([], 5) is None


def test_calibration_check_judges_a_scale_fitted_on_train_on_another_set():
    train = [row(0, 0)] * 90 + [row(0, 1)] * 10 + [row(70, 1)] * 6 + [row(70, 0)] * 4
    hold = [row(0, 0)] * 45 + [row(0, 1)] * 5 + [row(70, 1)] * 3 + [row(70, 0)] * 2
    cal = BT.calibration_check(BT.view(train, "p"), BT.view(hold, "p"), min_n=5)
    assert cal["n"] == 55 and cal["base_rate_train"] == pytest.approx(16 / 110)
    b0, b70 = cal["blocks"]
    assert b0["predicted"] == pytest.approx(0.10) and b0["n"] == 50 and b0["observed"] == pytest.approx(0.10) and b0["inside"]
    assert b70["predicted"] == pytest.approx(0.60) and b70["observed"] == pytest.approx(0.60)
    assert cal["brier_scale"] < cal["brier_constant"]                       # the scale beats the constant here


def test_signal_table_gives_fire_rate_and_rug_rate_fired_vs_not():
    rows = [row(40, 1, fired={"a": 40}), row(40, 1, fired={"a": 40}), row(40, 0, fired={"a": 40}),
            row(0, 0), row(0, 0), row(0, 1)]
    t = {(g["signal"], g["points"]): g for g in BT.signal_table(BT.view(rows, "p"))}
    g = t[("a", 40)]
    assert g["fired"] == 3 and g["fire_rate"] == 0.5 and g["rugs_fired"] == 2 and g["rate_fired"] == pytest.approx(2 / 3)
    assert g["rate_not_fired"] == pytest.approx(1 / 3)
    lo, hi = wilson(2, 3)
    assert g["lo"] == pytest.approx(lo) and g["hi"] == pytest.approx(hi)


def test_report_has_bands_ranges_coverage_auc_and_signals_per_launchpad():
    rows = [row(80, 1, lp="nearly", fired={"a": 40}), row(80, 0, lp="justhoot_v2"), row(5, 0, lp="nearly"), row(5, 1, lp="justhoot_v2")]
    rep = BT.report(rows, "p")
    assert rep["summary"]["final"] == 4 and rep["summary"]["base_rate"] == 0.5
    high = next(b for b in rep["by_band"] if b["band"] == "High")
    assert high["n"] == 2 and high["rugs"] == 1 and high["lift"] == pytest.approx(1.0)
    assert rep["coverage"]["rugs"] == 2 and rep["coverage"]["flagged"] == 1 and rep["coverage"]["severity_share"] == pytest.approx(0.5)
    assert rep["auc"] == pytest.approx(0.5)
    per = BT.by_launchpad(rows, "p")
    assert set(per) == {"nearly", "justhoot_v2"} and per["nearly"]["summary"]["final"] == 2
    assert BT.feature_names(rows, "p") == ["a:40"]


def test_history_summary_is_holdout_only_and_plain_data():
    import json
    import random
    from rugyomi.rules import backtest as B
    random.seed(3)
    rows = []
    for i in range(300):
        sc = random.randint(0, 100)
        rows.append({"token": f"t{i}", "launch_ts_ns": (i + 1) * 10**9, "launchpad": "x", "source": "live" if i > 250 else "history",
                     "rug": int(random.random() < sc / 120), "final": 1, "severity_near": None, "dev_exit": 0, "buyers_loss": 0,
                     "cards": {p: {"score": sc, "band": "High" if sc >= 60 else "Low", "config_version": "c", "fired": {}} for p in ("v0", "v1")}})
    h = B.history_summary(rows, 201 * 10**9, "S")
    assert h["train"]["n"] == 200 and h["holdout"]["n"] == 100 and h["holdout"]["live"] == 49
    assert sum(b["n"] for b in h["profiles"]["v1"]["by_band"]) == 100                  # the bands count the holdout only
    assert sum(b["train_n"] for b in h["calibration"]["v1"]["blocks"]) == 200          # the scale is fitted on train
    assert "token" not in json.dumps(h)                                                 # nothing identifying
    json.dumps(h)
    assert B.chance_for_score(h, 55)["lo_score"] <= 55 and B.chance_for_score(None, 5) is None


def test_a_block_of_a_handful_of_launches_is_folded_into_its_neighbour():
    rows = [row(0, 0)] * 60 + [row(0, 1)] * 20 + [row(50, 1)] * 20 + [row(50, 0)] * 30 + [row(100, 1)]    # a single launch at the top
    blocks = BT.isotonic(BT.view(rows, "p"))
    assert [(b["lo_score"], b["hi_score"], b["n"]) for b in blocks] == [(0, 0, 80), (50, 100, 51)]        # score 100 (n 1) joined score 50
    assert all(b["n"] >= 30 for b in blocks) and [b["p"] for b in blocks] == sorted(b["p"] for b in blocks)
    assert BT.isotonic(BT.view([row(0, 1)] * 5, "p"))[0]["n"] == 5                                         # one block is never merged away
