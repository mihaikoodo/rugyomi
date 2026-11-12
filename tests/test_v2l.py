"""RGY-053: profile v2l = v2r with the fee points proportional to the creator's effective take (8 per 1 %, at most 40; an expert rule, not fitted).
The profiles v0 / v1 / v2 / v1r / v2r keep their hashes; the stored measures of v2r serve v2l (the points are a score, not a measure)."""
import json

import pytest

from test_justhoot_fee import TOKEN, justhoot
from test_rules import T0, Db, result
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import features as FT
from rugyomi.rules import signals as SG

V2R, V2L = C.effective(None, "v2r"), C.effective(None, "v2l")
LIN = V2L["signals"]["creator_tax"]


class Ctx:
    token = "x.nearlytrade.near"

    def __init__(self, opts):
        self.opts = opts

    def tax_options(self, justhoot=False):
        return self.opts


def tax(opts, cfg=LIN):
    return SG._creator_tax(Ctx(opts), cfg)


def nearly(creator_bps, buy=100, sell=100, plat=0, burn=0):
    return {"creator_bps": creator_bps, "buy_bps": buy, "sell_bps": sell, "platform_bps": plat, "burn_bps": burn, "holders_bps": 10000 - creator_bps - burn}


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


@pytest.mark.core
def test_the_hashes_of_the_older_profiles_are_unchanged_and_v2l_is_pinned():
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2", "v1r", "v2r", "v2l")] == \
        ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371", "576d19235a70", "410c8c947216", "6fe0b20cacc0"]
    assert V2L["version"] == "v2l-1" and V2L["rules_version"] == "1.3" and V2L["rules_version"] not in (V2R["rules_version"], "1.1", "1.0")


@pytest.mark.core
def test_v2l_is_v2r_with_only_the_fee_shape_changed():
    a, b = json.loads(json.dumps(V2R)), json.loads(json.dumps(V2L))
    for x in (a, b):
        for k in ("version", "rules_version"):
            x.pop(k)
        x["display"].pop("note")
    assert a["signals"].pop("creator_tax") != b["signals"].pop("creator_tax")
    assert a == b                                                                      # bands 45 / 70, every other weight and the expert rules (40 / 10) as in v2r
    assert V2L["bands"] == {"elevated": 45, "high": 70}
    assert LIN["shape"] == "linear" and (LIN["points_per_pct"], LIN["max_points"], LIN["min_take_bps"]) == (8, 40, 1) and LIN["burn_mode"] == "ignore" and LIN["justhoot_platform_bps"] == 0
    assert {k: v["points"] for k, v in V2L["signals"].items() if k in ("name_imitation", "fresh_obfuscated_funding")} == {"name_imitation": 40, "fresh_obfuscated_funding": 10}


@pytest.mark.core
@pytest.mark.parametrize("take_bps,points", [(0, 0), (1, 0), (6.25, 1), (30, 2), (50, 4), (56.24, 4), (56.25, 5), (72, 6), (100, 8), (300, 24), (499.99, 40), (500, 40), (900, 40), (10000, 40)])
def test_points_are_8_per_percent_to_the_nearest_point_capped_at_40(take_bps, points):
    assert SG.linear_points(take_bps, 8, 40) == points


@pytest.mark.core
def test_otterly_imam_and_bagflip_through_the_signal():
    # OTTERLY: tax 1 % buy / 2 % sell, the creator gets 48 % of it (platform 40 % off the top, creator 80 % of the rest) -> 0.72 % per trade
    r = tax(nearly(8000, buy=100, sell=200, plat=4000))
    assert r["fired"] and r["version"] == 3 and r["value"]["take_bps"] == 72.0 and r["contribution"] == 6 and r["value"]["points"] == 6
    assert r["reason"].startswith("Trade tax 1.00% buy / 2.00% sell; after the platform's 40.00% cut the creator gets 48.00% of it") and len(r["reason"]) <= 170
    assert r["short"] and r["evidence"] == [{"kind": "launch", "id": "x.nearlytrade.near"}]
    assert tax(nearly(5000, buy=100, sell=100, plat=0))["contribution"] == 4                    # 0.5 % (the imam fee)
    assert tax(nearly(10000, buy=500, sell=500, plat=0))["contribution"] == 40                  # 5 % (the bagflip fee)
    assert tax(nearly(10000, buy=1000, sell=1000, plat=0))["contribution"] == 40                # above 5 %: the cap


@pytest.mark.core
def test_no_flat_base_for_any_tax():
    r = tax(nearly(1, buy=100, sell=100))                                                         # 0.01 bps: below min_take_bps, nothing fires
    assert not r["fired"] and r["contribution"] == 0 and r["reason"] == "" and r["evidence"] == [] and r["value"]["take_bps"] == 0.01
    r = tax(nearly(10000, buy=100, sell=100), {**LIN, "min_take_bps": 1, "points_per_pct": 0.5})       # 1 % x 0.5 = 0.5 -> 1 point; 0.4 points would be 0: fired with 0 points keeps no text (F-043)
    assert r["contribution"] == 1
    r = tax(nearly(10000, buy=100, sell=100), {**LIN, "points_per_pct": 0.4})
    assert r["fired"] and r["contribution"] == 0 and r["reason"] == "" and r["evidence"] == []
    assert tax(nearly(0))["contribution"] == 0 and not tax(nearly(0))["fired"]
    assert tax({})["contribution"] == 0 and tax(None)["contribution"] == 0


@pytest.mark.core
def test_an_unknown_take_gets_the_configured_points_and_says_why():
    unknown = {"creator_bps": 5000, "buy_bps": 100, "sell_bps": 100, "burn_bps": 0}            # no platform_bps
    r = tax(unknown)
    assert r["contribution"] == 0 and r["value"]["take_bps"] is None and r["value"]["basis"] == "unknown_platform"
    r = tax(unknown, {**LIN, "points_unknown_take": 5})
    assert r["fired"] and r["contribution"] == 5 and r["value"]["basis"] == "unknown_platform"


@pytest.mark.core
def test_a_burn_share_does_not_change_the_creators_share():
    assert tax(nearly(4000, buy=500, sell=500, plat=0, burn=3000))["value"] == {"creator_bps": 4000, "take_bps": 200.0, "basis": "known", "points": 16}


@pytest.mark.core
def test_v2r_and_the_older_profiles_give_the_same_points_as_before():
    r = SG._creator_tax(Ctx(nearly(8000, buy=100, sell=200, plat=4000)), V2R["signals"]["creator_tax"])
    assert r["version"] == 2 and r["contribution"] == 30                                                  # the +30 for 0.72 % that RGY-053 replaces in v2l
    assert SG._creator_tax(Ctx(nearly(10000, buy=500, sell=500)), V2R["signals"]["creator_tax"])["contribution"] == 40


@pytest.mark.core
def test_justhoot_fees_through_the_card(db):
    justhoot(db)                                                                                  # bagflip: 5 % to the owner
    r = result(db.card(TOKEN, config=V2L), "creator_tax")
    assert r["fired"] and r["contribution"] == 40 and r["value"]["take_bps"] == 500.0 and r["value"]["source"] == "justhoot" and len(r["reason"]) <= 170
    justhoot(db, token="imam.v2-launchpad.justhoot.near", dev="imam.near", tx="Tx2",
             args={"transfer_fee_pips": 5000, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}})
    r = result(db.card("imam.v2-launchpad.justhoot.near", config=V2L), "creator_tax")
    assert r["value"]["take_bps"] == 50.0 and r["contribution"] == 4                              # 0.5 %: 4 points (v2r: 30)
    justhoot(db, token="z.v2-launchpad.justhoot.near", dev="z.near", tx="Tx3", args={"transfer_fee_pips": 0, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}})
    assert not result(db.card("z.v2-launchpad.justhoot.near", config=V2L), "creator_tax")["fired"]


@pytest.mark.core
def test_the_score_is_a_sum_of_signals_and_the_bands_are_45_70(db):
    justhoot(db)
    c = db.card(TOKEN, config=V2L)
    assert c["config_version"] == "v2l-1" and c["rules_version"] == "1.3" and c["band"] == ("Low" if c["score"] < 45 else "Elevated" if c["score"] < 70 else "High")
    assert c["score"] == min(100, sum(r["contribution"] for r in c["signals"]["results"]))


@pytest.mark.core
def test_the_stored_measures_of_v2r_serve_v2l():
    """The points are not part of a stored measure: v2l needs no new feature rows, `fill` for v2r covers it."""
    assert {(s.id, FT.feature_key(s, V2R)) for s, _ in FT.enabled(V2R)} == {(s.id, FT.feature_key(s, V2L)) for s, _ in FT.enabled(V2L)}


@pytest.mark.core
def test_the_profile_is_known_to_the_site_and_is_not_live_by_default(monkeypatch):
    assert rules.profile_of("1.3") == "v2l" and rules.profile_of("1.2") == "v2r"
    assert "8 points per 1 %" in rules.note_for("1.3") and rules.note_for("1.3") != rules.note_for("1.2")
    m = rules.methodology("1.3")
    assert [b["range"] for b in m["bands"]] == ["0–44", "45–69", "70–100"] and m["expert"]["live"] is True and m["expert"]["config_version"] == "v2l-1"
    monkeypatch.setenv("RUGYOMI_RULES_PROFILE", "v2r")
    assert rules.methodology_expert()["config_version"] == "v2r-fit-2"
    doc = SG._creator_tax_doc(LIN)
    assert "8 points per 1 %" in doc and "at most 40" in doc and "not fitted" in doc and "nearest whole point" in doc and "no flat base" in doc.lower()


@pytest.mark.core
def test_an_unknown_shape_key_or_a_typo_in_the_linear_section_is_an_error():
    with pytest.raises(C.ConfigError):
        C.effective({"signals": {"creator_tax": {"points_per_percent": 8}}}, "v2l")
    assert C.effective({"signals": {"creator_tax": {"max_points": 30}}}, "v2l")["signals"]["creator_tax"]["max_points"] == 30
