"""RGY-021 step 3: the non-negative logistic fit (`rugyomi.rules.fit`) on synthetic rows."""
import random

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi.rules import fit as F


def make(n=3000, seed=3):
    rnd = random.Random(seed)
    rows = []
    for i in range(n):
        a, b, c = rnd.random() < 0.2, rnd.random() < 0.1, rnd.random() < 0.3
        p = 0.08 + 0.45 * a + 0.3 * b - 0.05 * c          # c LOWERS the risk: a non-negative fit must give it 0
        fired = {}
        if a: fired["a"] = 30
        if b: fired["b"] = 40
        if c: fired["c"] = 10
        rows.append({"token": f"t{i}", "final": 1, "rug": int(rnd.random() < p), "cards": {"v": {"fired": fired}}})
    return rows


def test_coefficients_are_never_negative_and_follow_the_effects():
    rows = make()
    names = ["a:30", "b:40", "c:10"]
    b, w = F.fit_nonneg(F.patterns(rows, "v", names), 3, 1.0)
    assert all(x >= 0 for x in w) and w[0] > w[1] > 0 and w[2] < 0.1
    pts = F.to_points(w, 50, 5)
    assert pts[0] == 50 and 0 < pts[1] < 50 and pts[2] in (0, 5)


def test_the_fit_is_deterministic_and_cv_prefers_a_sane_regularisation():
    rows = make()
    names = ["a:30", "b:40", "c:10"]
    assert F.fit_nonneg(F.patterns(rows, "v", names), 3, 1.0) == F.fit_nonneg(F.patterns(rows, "v", names), 3, 1.0)
    assert F.cv_logloss(rows, "v", names, 1.0) < F.cv_logloss(rows, "v", [], 1.0) + 1e-9      # beats "no signal at all"


def test_no_rows_and_a_single_class_do_not_crash():
    assert F.fit_nonneg({}, 2) == (0.0, [0.0, 0.0])
    b, w = F.fit_nonneg({(): [10, 0]}, 1, 1.0)
    assert w == [0.0] and b < -3


def test_tier_keys_maps_points_to_the_config_key_and_flags_ambiguity():
    from rugyomi.rules.config import effective
    cfg = effective(None, "v1")
    assert F.tier_keys(cfg, "creator_track_record", 50) == ["points_high"]
    assert F.tier_keys(cfg, "funded_by_rugger", 10) == ["points", "points_multi"]      # equal tiers: ambiguous, a fit cannot tell them apart
    assert F.tier_keys(cfg, "nothing", 5) == []
    for sid, c in cfg["signals"].items():                     # no two tiers of a signal share a value, or a fit could not tell them apart
        vals = [v for k, v in c.items() if k.startswith("points")]
        if sid != "funded_by_rugger":                         # the fit merged the two funding tiers into one value (see the config notes)
            assert len(vals) == len(set(vals)), sid
