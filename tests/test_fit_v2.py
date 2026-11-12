"""RGY-033: the tier helpers of `rules/fit.py` (edges from the train inputs, cumulative tier features)."""
import pytest
import json

from rugyomi.rules import fit as FIT


def row(token, take=None, uses=None, gap=None, fired=(), rug=0, ts=1):
    f = {k: 10 for k in fired}
    vals = {}
    if take is not None or "creator_tax" in fired:
        f["creator_tax"] = 10
        vals["creator_tax"] = {"creator_bps": 5000, "take_bps": take, "basis": "known" if take is not None else "unknown_platform"}
    if uses:
        f["name_copy"] = 5
        vals["name_copy"] = {"uses": uses, "gap_h": gap}
    return {"token": token, "launch_ts_ns": ts, "rug": rug, "final": 1, "cards": {"v2": {"fired": f, "values": vals}}}


@pytest.mark.core
def test_edges_are_quantiles_of_the_inputs_and_ignore_the_outcome():
    takes = [1, 2, 3, 4, 5, 6, 7, 8, 9]
    a = [row(f"a{i}", take=t, rug=0) for i, t in enumerate(takes)]
    b = [row(f"b{i}", take=t, rug=1) for i, t in enumerate(takes)]
    e = FIT.choose_edges(a, "v2")
    assert FIT.choose_edges(b, "v2") == e                       # the labels do not move an edge
    assert e["n_take"] == 9 and e["take2_bps"] < e["take3_bps"]
    assert e["take2_bps"] == 3.0 and e["take3_bps"] == 7.0


def test_edges_skip_unknown_takes_and_stay_strictly_increasing():
    rows = [row(f"u{i}", fired=("creator_tax",)) for i in range(5)] + [row(f"k{i}", take=2.0) for i in range(5)]
    e = FIT.choose_edges(rows, "v2")
    assert e["n_take"] == 5 and e["take3_bps"] > e["take2_bps"] == 2.0       # all takes equal: take3 is pushed up by 0.5
    assert FIT.choose_edges([], "v2") == {"n_take": 0, "n_copies": 0}


@pytest.mark.core
def test_name_edges_have_a_floor():
    rows = [row(f"n{i}", uses=1, gap=0.01) for i in range(6)]
    e = FIT.choose_edges(rows, "v2")
    assert e["many_uses"] == 2 and e["recent_h"] == 0.25 and e["n_copies"] == 6
    rows = [row(f"m{i}", uses=u, gap=g) for i, (u, g) in enumerate([(1, 0.5), (1, 2.0), (4, 3.0), (6, 9.0), (6, 30.0), (8, 40.0)])]
    e = FIT.choose_edges(rows, "v2")
    assert e["many_uses"] == 6 and e["recent_h"] == 2.0


@pytest.mark.core
def test_the_tier_view_is_cumulative_and_leaves_the_input_alone():
    edges = {"take2_bps": 3.0, "take3_bps": 7.0, "many_uses": 3, "recent_h": 1.0}
    src = [row("lo", take=1.0), row("mid", take=3.0), row("hi", take=7.0), row("unk", fired=("creator_tax",)),
           row("n1", uses=1, gap=5.0), row("n3", uses=3, gap=5.0), row("n3r", uses=3, gap=1.0), row("plain", fired=("serial_launcher",))]
    before = json.dumps(src, sort_keys=True)
    view = {r["token"]: r["cards"][FIT.TIERS]["fired"] for r in FIT.tier_view(src, "v2", edges)}
    assert json.dumps(src, sort_keys=True) == before
    assert view["lo"] == {"creator_tax": 1}
    assert view["mid"] == {"creator_tax": 1, "creator_tax@2": 1}
    assert view["hi"] == {"creator_tax": 1, "creator_tax@2": 1, "creator_tax@3": 1}
    assert view["unk"] == {"creator_tax": 1}                                    # an unknown take reaches the first tier only
    assert view["n1"] == {"name_copy": 1} and view["n3"] == {"name_copy": 1, "name_copy@many": 1}
    assert view["n3r"] == {"name_copy": 1, "name_copy@many": 1, "name_copy@recent": 1}
    assert view["plain"] == {"serial_launcher": 10}                             # other signals as stored


def test_flat_keeps_the_first_tier_only_and_a_row_without_the_profile_is_skipped():
    edges = {"take2_bps": 3.0, "take3_bps": 7.0, "many_uses": 3, "recent_h": 1.0}
    src = [row("hi", take=9.0, uses=5, gap=0.5), {"token": "x", "cards": {"v1": {"fired": {}}}}]
    out = FIT.tier_view(src, "v2", edges, flat=True)
    assert len(out) == 1 and out[0]["cards"][FIT.TIERS]["fired"] == {"creator_tax": 1, "name_copy": 1}


@pytest.mark.core
def test_tier_features_fit_into_monotone_points():
    # a higher take rugs more: the fitted points of the cumulative tiers cannot decrease with the take
    import random
    rnd = random.Random(7)
    rows = []
    for i in range(900):
        take = rnd.choice([1.0, 4.0, 9.0])
        rows.append(row(f"t{i}", take=take, rug=int(rnd.random() < {1.0: 0.2, 4.0: 0.35, 9.0: 0.6}[take])))
    edges = {"take2_bps": 3.0, "take3_bps": 7.0}
    view = FIT.tier_view(rows, "v2", edges)
    names = ["creator_tax:1", "creator_tax@2:1", "creator_tax@3:1"]
    b, w = FIT.fit_nonneg(FIT.patterns(view, FIT.TIERS, names), 3, 1.0)
    assert all(c >= 0 for c in w)
    pts = FIT.to_points(w, 50, 5)
    assert sum(pts) > 0
    ll_flat = FIT.cv_logloss(FIT.tier_view(rows, "v2", edges, flat=True), FIT.TIERS, names[:1], 1.0)
    ll_tier = FIT.cv_logloss(view, FIT.TIERS, names, 1.0)
    assert ll_tier < ll_flat                                                      # the tiers describe this data better than one flat step
