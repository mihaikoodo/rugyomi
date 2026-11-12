"""RGY-033: profile v2 — smooth points for the creator's tax (the effective take per trade) and for a copied name (uses x gap).
v1 stays as it is: same hash, same output."""
import itertools

import pytest

from test_rules import DAY, HOUR, MIN, T0, Db, result
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import signals as SG

FITTED = C.effective(None, "v2")           # the shipped profile: v2-fit-1, fitted on train (not live)
# the shape tests need every tier to carry points: a fixed fixture, the fitted name tiers are 0 / 0 / 15 by design
V2 = C.effective({"signals": {"creator_tax": {"points": 10, "points_2": 10, "points_3": 10, "take2_bps": 30, "take3_bps": 100},
                              "name_copy": {"points": 5, "points_many": 5, "points_recent": 5, "many_uses": 3, "recent_h": 1}}}, "v2")
TAX = V2["signals"]["creator_tax"]
NAME = V2["signals"]["name_copy"]
V2_UNKNOWN = C.effective({"signals": {"creator_tax": {**V2["signals"]["creator_tax"], "burn_mode": "unknown"}}}, "v2")


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "v2.db")
    yield d
    d.s.close()


class Ctx:
    """The two things the creator_tax signal reads from its context."""
    token = "x.nearlytrade.near"

    def __init__(self, opts):
        self.opts = opts

    def tax_options(self):
        return self.opts


def tax(opts, cfg=TAX):
    return SG._creator_tax(Ctx(opts), cfg)


# ------------------------------------------------------------------------------------------------ the profile and v1
@pytest.mark.core
def test_v1_and_v0_are_untouched_and_v2_is_a_known_profile_that_is_not_live():
    assert C.config_hash(C.effective(None, "v0")) == "2cb6d1e9c0c3"
    assert C.config_hash(C.effective(None, "v1")) == "6fedfdb50583"                            # the v1-fit-1 hash before RGY-033
    assert "shape" not in C.effective(None, "v1")["signals"]["creator_tax"] and "shape" not in C.effective(None, "v1")["signals"]["name_copy"]
    assert FITTED["version"] == "v2-fit-1" and C.config_hash(FITTED) == "3d68b94c6371"      # pinned: what fit_v2.py wrote and the holdout evaluated
    assert C.config_hash(FITTED) not in ("2cb6d1e9c0c3", "6fedfdb50583")
    assert TAX["shape"] == "take" and NAME["shape"] == "counted"
    assert C.load_config(profile=None)["version"] == "v0-default-1"                            # nothing switched by shipping the file


@pytest.mark.core
def test_the_shipped_v2_profile_carries_the_fitted_points():
    t, n = FITTED["signals"]["creator_tax"], FITTED["signals"]["name_copy"]
    assert (t["points"], t["points_2"], t["points_3"], t["take2_bps"], t["take3_bps"], t["burn_mode"]) == (25, 10, 10, 36.0, 140.0, "ignore")
    assert (n["points"], n["points_many"], n["points_recent"], n["many_uses"], n["recent_h"]) == (0, 0, 15, 4, 6.0)
    assert [FITTED["signals"][k]["enabled"] for k in ("cluster_rugs", "funded_by_dumper", "prior_fast_dump", "serial_launcher")] == [False] * 4
    assert FITTED["signals"]["funded_by_rugger"]["points"] == 15 and FITTED["signals"]["creator_track_record"]["points_high"] == 50
    assert C.load_config(profile=None)["version"] == "v0-default-1"                      # still not live


@pytest.mark.core
def test_the_v1_signals_give_the_same_output_as_before():
    v1 = C.effective(None, "v1")["signals"]
    r = SG._creator_tax(Ctx({"creator_bps": 3300, "buy_bps": 100, "sell_bps": 100}), v1["creator_tax"])
    assert r["version"] == 1 and r["contribution"] == 20 and r["value"] == {"creator_bps": 3300}
    assert r["reason"] == ("Trade tax 1.00% buy / 1.00% sell; the creator's share of that tax is 33.00% "
                           "(nearly tax options: buy_bps 100, sell_bps 100, creator_bps 3300).")
    full = {"creator_bps": 5000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 0, "holders_bps": 5000, "platform_bps": 4000}
    assert SG._creator_tax(Ctx(full), v1["creator_tax"])["contribution"] == 20                  # flat, whatever the take is


# ------------------------------------------------------------------------------------------------ creator_tax: the effective take
@pytest.mark.core
def test_the_effective_take_is_rate_times_the_creators_share_after_the_platforms_cut():
    # F-033: void.nearlytrade.near, tax 1 %, creator_bps 5000, platform 4000 -> 0.30 % of the trade = 30 bps
    assert SG.creator_take({"buy_bps": 100, "sell_bps": 100, "creator_bps": 5000, "platform_bps": 4000, "burn_bps": 0}) == {"take_bps": pytest.approx(30.0), "basis": "known"}
    # creator_bps 10000 at a 4 % tax and the same platform cut = 60 % of the tax = 2.4 % per trade
    assert SG.creator_take({"buy_bps": 400, "sell_bps": 400, "creator_bps": 10000, "platform_bps": 4000, "burn_bps": 0})["take_bps"] == pytest.approx(240.0)
    # buy 1 %, sell 3 %: the mean rate
    assert SG.creator_take({"buy_bps": 100, "sell_bps": 300, "creator_bps": 5000, "platform_bps": 0, "burn_bps": 0})["take_bps"] == pytest.approx(100.0)


@pytest.mark.core
@pytest.mark.parametrize("opts,basis", [
    ({"creator_bps": 3000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 0}, "unknown_platform"),
    ({"creator_bps": 3000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 0, "platform_bps": 12000}, "unknown_platform"),
    ({"creator_bps": 3000, "sell_bps": 100, "burn_bps": 0, "platform_bps": 2000}, "unknown_rate"),
    ({"creator_bps": 3000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 2000, "platform_bps": 2000}, "unknown_burn"),
    ({"creator_bps": 3000, "buy_bps": 100, "sell_bps": 100, "platform_bps": 2000}, "unknown_burn"),
    ({"buy_bps": 100, "sell_bps": 100, "burn_bps": 0, "platform_bps": 2000}, "no_creator_bps"),
])
def test_a_take_that_cannot_be_computed_is_named_and_gets_the_first_tier_only(opts, basis):
    assert SG.creator_take(opts)["take_bps"] is None and SG.creator_take(opts)["basis"] == basis
    assert SG.creator_take(opts, "ignore")["basis"] == ("known" if basis == "unknown_burn" else basis)       # the default rule 'ignore' only changes the burn basis
    r = tax(opts, V2_UNKNOWN["signals"]["creator_tax"])                          # the burn rule 'unknown' (the strict reading), as well as the default 'ignore' for the other bases
    if basis == "no_creator_bps":
        assert not r["fired"]
    else:
        assert r["fired"] and r["contribution"] == TAX["points"] and r["value"]["basis"] == basis and r["value"]["take_bps"] is None


@pytest.mark.core
def test_v2_takes_a_burn_share_as_part_of_what_is_left_and_unknown_mode_does_not():
    # RGY-033, burn_split_check: platform off the top, creator / holders / burn are shares of the rest (3 launches with the burn stream fully observed, 0.3 points)
    assert TAX["burn_mode"] == "ignore"
    opts = {"creator_bps": 3300, "buy_bps": 400, "sell_bps": 400, "burn_bps": 3300, "holders_bps": 3400, "platform_bps": 2000}
    assert SG.creator_take(opts, "ignore")["take_bps"] == pytest.approx(400 * 3300 * 8000 / 1e8)
    assert SG.creator_take(opts, "unknown")["take_bps"] is None
    r = tax(opts)
    assert r["value"]["basis"] == "known" and r["value"]["take_bps"] == pytest.approx(105.6) and r["contribution"] == TAX["points"] + TAX["points_2"] + TAX["points_3"]
    assert "per buy" not in r["reason"] and len(r["reason"]) <= 170        # facts only: no per-trade rate stated where a burn share exists; the post clips at 170
    u = tax(opts, V2_UNKNOWN["signals"]["creator_tax"])
    assert u["value"]["basis"] == "unknown_burn" and u["contribution"] == TAX["points"]


@pytest.mark.core
def test_the_points_never_fall_when_the_take_rises():
    c = TAX
    rows = []
    for buy, sell, plat, cb, burn in itertools.product((100, 200, 400), (100, 300, 400), (2000, 3000, 4000), range(0, 10001, 500), (0, 2000)):
        o = {"buy_bps": buy, "sell_bps": sell, "platform_bps": plat, "creator_bps": cb, "burn_bps": burn}
        r = tax(o, c)
        rows.append((SG.creator_take(o, "ignore")["take_bps"], r["contribution"] if r["fired"] else 0))
    rows = [x for x in rows if x[0] is not None]
    rows.sort()
    assert len(rows) > 500 and all(b[1] >= a[1] for a, b in zip(rows, rows[1:]))
    assert {p for _, p in rows} >= {0, c["points"], c["points"] + c["points_2"], c["points"] + c["points_2"] + c["points_3"]}    # all tiers are reachable


@pytest.mark.core
def test_the_tier_edges_are_inclusive_and_creator_bps_zero_never_fires():
    base = {"buy_bps": 100, "sell_bps": 100, "platform_bps": 0, "burn_bps": 0}
    def at(take_bps):                                                            # creator_bps that gives exactly this take (platform 0, rate 100 bps)
        return tax({**base, "creator_bps": round(take_bps * 100)})
    t2, t3 = TAX["take2_bps"], TAX["take3_bps"]
    assert at(TAX["min_take_bps"])["contribution"] == TAX["points"] and at(TAX["min_take_bps"] - 0.5)["fired"] is False
    assert at(t2 - 0.01)["contribution"] == TAX["points"] and at(t2)["contribution"] == TAX["points"] + TAX["points_2"]
    assert at(t3 - 0.01)["contribution"] == TAX["points"] + TAX["points_2"] and at(t3)["contribution"] == TAX["points"] + TAX["points_2"] + TAX["points_3"]
    assert not tax({**base, "creator_bps": 0})["fired"] and not SG._creator_tax(Ctx(None), TAX)["fired"]


@pytest.mark.core
def test_the_v2_card_carries_the_take_and_the_old_reason_wording(db):
    db.launch("a.nearlytrade.near", "d.near", T0, height=77, extra={"id": 9})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=T0, height=77, extra={"contract": "nearlytrade.near", "data": {
        "id": 9, "creator_bps": 5000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 0, "holders_bps": 5000, "platform_bps": 4000}})
    r = result(db.card("a.nearlytrade.near", config=V2), "creator_tax")
    assert r["version"] == 2 and r["value"]["take_bps"] == pytest.approx(30.0) and TAX["take2_bps"] <= 30 < TAX["take3_bps"] and r["contribution"] == TAX["points"] + TAX["points_2"]
    assert r["reason"].startswith("Trade tax 1.00% buy / 1.00% sell; after the platform's 40.00% cut the creator gets 30.00% of it")


# ------------------------------------------------------------------------------------------------ name_copy: uses x gap
def card_name(db, token, cfg=V2):
    return result(db.card(token, config=cfg), "name_copy")


@pytest.mark.core
def test_name_copy_points_grow_with_the_uses_and_with_a_recent_latest_use(db):
    L = T0
    # one earlier use, 2 days ago (older than recent_h): the base points only
    db.launch("panda.nearlytrade.near", "a.near", L - 2 * DAY)
    db.launch("panda-2.nearlytrade.near", "me.near", L)
    r = card_name(db, "panda-2.nearlytrade.near")
    assert r["fired"] and r["version"] == 2 and r["value"]["uses"] == 1 and r["contribution"] == NAME["points"] and "once" not in r["reason"]
    assert r["value"]["kind"] == "other_creator" and r["value"]["other_launch"] == "panda.nearlytrade.near"
    # a second earlier use by another creator 3 days ago and a third 4 days ago -> many_uses (3)
    db.launch("panda-3.nearlytrade.near", "b.near", L - 3 * DAY)
    db.launch("panda-4.nearlytrade.near", "c.near", L - 4 * DAY)
    r = card_name(db, "panda-2.nearlytrade.near")
    assert r["value"]["uses"] == 3 and r["contribution"] == NAME["points"] + NAME["points_many"]
    # the latest use 30 minutes before -> the recency bonus; the count is 4
    db.launch("panda-5.nearlytrade.near", "d.near", L - 30 * MIN)
    r = card_name(db, "panda-2.nearlytrade.near")
    assert r["value"]["uses"] == 4 and r["value"]["other_launch"] == "panda-5.nearlytrade.near"
    assert r["contribution"] == NAME["points"] + NAME["points_many"] + NAME["points_recent"] and "4 times" in r["reason"] and "30 min" in r["reason"]


@pytest.mark.core
def test_name_copy_one_recent_use_gets_the_recency_bonus_only_and_the_gap_edge_is_inclusive(db):
    L = T0
    db.launch("bear.nearlytrade.near", "a.near", L - NAME["recent_h"] * HOUR)
    db.launch("bear-2.nearlytrade.near", "me.near", L)
    r = card_name(db, "bear-2.nearlytrade.near")
    assert r["value"]["uses"] == 1 and r["contribution"] == NAME["points"] + NAME["points_recent"]
    db.launch("wolf.nearlytrade.near", "a.near", L - NAME["recent_h"] * HOUR - 1)
    db.launch("wolf-2.nearlytrade.near", "me.near", L)
    assert card_name(db, "wolf-2.nearlytrade.near")["contribution"] == NAME["points"]


@pytest.mark.core
def test_name_copy_counts_the_same_creator_on_another_launchpad_and_each_launch_once(db):
    L = T0
    db.launch("fox.v2-launchpad.justhoot.near", "me.near", L - 10 * MIN, launchpad="justhoot_v2", extra={"symbol": "FOX"})
    db.launch("fox2.nearlytrade.near", "z.near", L - 2 * DAY, extra={"symbol": "fox"})                  # shares two keys with ours: still one use
    db.launch("fox-3.nearlytrade.near", "me.near", L, extra={"symbol": "fox"})
    r = card_name(db, "fox-3.nearlytrade.near")
    assert r["value"]["uses"] == 2 and r["value"]["kind"] == "same_creator_other_launchpad"
    assert r["contribution"] == NAME["points"] + NAME["points_recent"]                               # 2 < many_uses, latest 10 min ago


@pytest.mark.core
def test_name_copy_never_counts_a_later_launch_or_the_same_instant(db):
    # no lookahead: the card of the first launch is the same with or without the copies that came after it
    L = T0
    db.launch("lynx.nearlytrade.near", "a.near", L - DAY)
    db.launch("lynx-2.nearlytrade.near", "me.near", L)
    before = card_name(db, "lynx-2.nearlytrade.near")
    for i, ts in enumerate((L + 1, L + MIN, L + HOUR, L + 3 * DAY)):
        db.launch(f"lynx-{i + 3}.nearlytrade.near", f"x{i}.near", ts)
    db.launch("lynx-9.nearlytrade.near", "q.near", L)                                           # the very same instant: not strictly before
    after = card_name(db, "lynx-2.nearlytrade.near")
    assert before["value"]["uses"] == after["value"]["uses"] == 1 and before["contribution"] == after["contribution"]
    # mutation: the same card WITH a really earlier copy does change
    db.launch("lynx-10.nearlytrade.near", "w.near", L - HOUR * 5)
    assert card_name(db, "lynx-2.nearlytrade.near")["value"]["uses"] == 2


@pytest.mark.core
def test_name_copy_v2_is_silent_like_v1_where_v1_is_silent(db):
    L = T0
    db.launch("panda.nearlytrade.near", "someone.near", L - 8 * DAY)                           # older than 7 days
    db.launch("panda-2.nearlytrade.near", "me.near", L)
    assert not card_name(db, "panda-2.nearlytrade.near")["fired"]
    db.launch("ab.nearlytrade.near", "someone.near", L - HOUR)                                 # a short name is not compared
    db.launch("ab-2.nearlytrade.near", "me.near", L)
    assert not card_name(db, "ab-2.nearlytrade.near")["fired"]
    db.launch("owl.nearlytrade.near", "me.near", L - HOUR)                                      # same creator, same launchpad
    db.launch("owl-2.nearlytrade.near", "me.near", L)
    assert not card_name(db, "owl-2.nearlytrade.near")["fired"]


@pytest.mark.core
def test_the_v2_reasons_obey_the_publisher_clip(db):
    L = T0
    long_token = "x" * 30 + ".v2-launchpad.justhoot.near"
    db.launch(long_token, "a.near", L - HOUR, launchpad="justhoot_v2", extra={"symbol": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"})
    db.launch("xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-2.nearlytrade.near", "me.near", L)
    assert len(card_name(db, "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx-2.nearlytrade.near")["reason"]) <= 170


@pytest.mark.core
def test_the_asof_rows_keep_the_value_of_a_fired_signal(db, tmp_path):
    from rugyomi.rules import backtest as BT
    L = T0
    db.launch("panda.nearlytrade.near", "a.near", L - 2 * DAY)
    db.launch("panda-2.nearlytrade.near", "me.near", L)
    db.s.con.execute("INSERT INTO launch_outcomes(token, rug, evidence, config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,1)",
                     ("panda-2.nearlytrade.near", 0, "{}", "v", "h", "x"))
    db.s.con.commit()
    rows = BT.asof_rows(db.s, {"v2": V2})
    row = next(r for r in rows if r["token"] == "panda-2.nearlytrade.near")
    assert row["cards"]["v2"]["fired"]["name_copy"] == NAME["points"]
    assert row["cards"]["v2"]["values"]["name_copy"]["uses"] == 1


@pytest.mark.core
def test_the_methodology_text_comes_from_the_config_and_names_only_the_tiers_that_carry_points():
    sig = {x.id: x for x in SG.SIGNALS}
    assert "25 points; a take of 36 bps" in sig["creator_tax"].describe(FITTED["signals"]["creator_tax"])
    fitted = sig["name_copy"].describe(FITTED["signals"]["name_copy"])
    assert "a latest use at most 6 h earlier adds 15" in fitted and "adds 0" not in fitted and "or more earlier uses" not in fitted
    full = sig["name_copy"].describe(V2["signals"]["name_copy"])
    assert "adds 5 points" in full and "3 or more earlier uses add 5" in full and "at most 1 h earlier adds 5" in full
    assert "No points" in sig["name_copy"].describe({**NAME, "points": 0, "points_many": 0, "points_recent": 0})
