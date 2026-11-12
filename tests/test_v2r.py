"""RGY-049: the rules profile v2r = the v2 signals + the two expert rules of v1r + the justhoot creator fee read through the same effective-take formula as nearly.
The profiles v0 / v1 / v2 / v1r give the same hashes and cards as before; a fired signal with 0 points has no reason text anywhere (F-043)."""
import json
import sqlite3

import pytest

from test_justhoot_fee import TOKEN, justhoot
from test_rules import MIN, NEAR, T0, Db, result
from rugyomi import publisher as P
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import signals as SG

V1R, V2, V2R = (C.effective(None, p) for p in ("v1r", "v2", "v2r"))
EXPERT = ("name_imitation", "fresh_obfuscated_funding")


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


def with_platform(bps):
    return C.effective({"signals": {"creator_tax": {"justhoot_platform_bps": bps}}}, "v2r")


def without_platform():
    cfg = json.loads(json.dumps(V2R))
    cfg["signals"]["creator_tax"]["justhoot_platform_bps"] = None             # a config that does not know the platform's share
    return cfg


# ---------------------------------------------------------------- the profile
@pytest.mark.core
def test_the_hashes_are_pinned_and_the_older_profiles_are_unchanged():
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2", "v1r", "v2r")] == \
        ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371", "576d19235a70", "410c8c947216"]
    assert V2R["version"] == "v2r-fit-2" and V2R["rules_version"] == "1.2" and V2R["rules_version"] not in (V2["rules_version"], V1R["rules_version"])


@pytest.mark.core
def test_v2r_is_v2_plus_the_expert_rules_plus_the_justhoot_flag():
    a, b = json.loads(json.dumps(V2)), json.loads(json.dumps(V2R))
    for k in ("version", "rules_version"):
        a.pop(k), b.pop(k)
    a["display"].pop("note"), b["display"].pop("note")
    assert b["signals"].pop("name_imitation") == V1R["signals"]["name_imitation"] == {"enabled": True, "points": 40}      # the fixed points of v1r
    assert b["signals"].pop("fresh_obfuscated_funding") == V1R["signals"]["fresh_obfuscated_funding"]
    assert b["signals"]["creator_tax"].pop("justhoot") is True and b["signals"]["creator_tax"].pop("justhoot_platform_bps") == 0
    # the refit on train (fit_v2r.txt, 11.11): every weight comes out as in v2 except the fee: first tier 20 (v2: 25), the second tier from 39.5 bps (v2: 36)
    assert (b["signals"]["creator_tax"].pop("points"), b["signals"]["creator_tax"].pop("take2_bps")) == (20, 39.5)
    assert (a["signals"]["creator_tax"].pop("points"), a["signals"]["creator_tax"].pop("take2_bps")) == (25, 36.0)
    assert (a.pop("bands"), b.pop("bands")) == ({"elevated": 30, "high": 60}, {"elevated": 45, "high": 70})            # the equal-coverage bands (Q-030, C)
    assert a == b


@pytest.mark.core
def test_the_note_of_a_v2r_card_is_its_own():
    n = rules.note_for("1.2")
    assert "expert rules" in n and "justhoot" in n and n != rules.note_for("1.1") != rules.note_for("1.0")


@pytest.mark.core
def test_the_methodology_expert_section_is_live_for_v2r_too(monkeypatch):
    monkeypatch.setenv("RUGYOMI_RULES_PROFILE", "v2r")
    assert rules.methodology_expert()["live"] is True
    monkeypatch.setenv("RUGYOMI_RULES_PROFILE", "v2")
    assert rules.methodology_expert()["live"] is False


# ---------------------------------------------------------------- the justhoot fee through creator_take
@pytest.mark.core
def test_shipped_v2r_takes_the_platform_share_of_a_justhoot_fee_as_zero(db):
    """Q-029 (justhoot docs): the transfer tax is on top of the pool fee, the platform's 0.8 % of the pool is not part of it: platform_bps 0."""
    assert V2R["signals"]["creator_tax"]["justhoot_platform_bps"] == 0
    justhoot(db)
    r = result(db.card(TOKEN, config=V2R), "creator_tax")
    assert r["fired"] and r["contribution"] == 40 and r["value"]["take_bps"] == 500.0 and r["value"]["basis"] == "known" and r["value"]["source"] == "justhoot"
    assert r["reason"] == ("Token transfer fee 5%; the token owner gets 100% of it, i.e. 5% of every transfer "
                           "(justhoot launch call: transfer_fee_pips 50000, token_owner_bps 10000).") and len(r["reason"]) <= 170
    assert r["short"] == "token fee 5%, the owner gets 100% of it (5% per transfer)"
    justhoot(db, token="split.v2-launchpad.justhoot.near", dev="s.near", tx="Tx9",
             args={"transfer_fee_pips": 30000, "shares": {"token_owner_bps": 4000, "burn_bps": 3000, "holders_bps": 3000}})
    r = result(db.card("split.v2-launchpad.justhoot.near", config=V2R), "creator_tax")
    assert r["value"]["take_bps"] == 120.0 and "i.e. 1.2% of every transfer" in r["reason"]
    worst = {"source": "justhoot", "transfer_fee_pips": 1000000, "creator_bps": 10000, "platform_bps": 0}
    assert len(SG._tax_options_text(worst, 10000)) <= 170


@pytest.mark.core
def test_a_config_that_does_not_know_the_platform_share_reads_the_call_but_gives_the_first_tier_only(db):
    justhoot(db)
    r = result(db.card(TOKEN, config=without_platform()), "creator_tax")
    assert r["fired"] and r["contribution"] == 20                                              # the lowest tier only: the take cannot be computed
    assert r["value"] == {"creator_bps": 10000, "take_bps": None, "basis": "unknown_platform", "source": "justhoot", "transfer_fee_pips": 50000}
    assert r["reason"].startswith("Token transfer fee 5%") and len(r["reason"]) <= 170 and "platform" not in r["reason"].lower() and "i.e." not in r["reason"]


@pytest.mark.core
def test_with_a_platform_share_imam_and_bagflip_differ(db):
    cfg = V2R
    justhoot(db)
    bag = result(db.card(TOKEN, config=cfg), "creator_tax")
    assert bag["value"]["take_bps"] == 500.0 and bag["contribution"] == 40                      # 5 %: all three tiers
    justhoot(db, token="imam.v2-launchpad.justhoot.near", dev="imam.near", tx="Tx2",
             args={"transfer_fee_pips": 5000, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}})
    imam = result(db.card("imam.v2-launchpad.justhoot.near", config=cfg), "creator_tax")
    assert imam["value"]["take_bps"] == 50.0 and imam["contribution"] == 30                     # 0.5 %: two tiers
    cfg30 = with_platform(3000)                                                                    # the platform takes 30 % off the top
    assert result(db.card(TOKEN, config=cfg30), "creator_tax")["value"]["take_bps"] == 350.0


@pytest.mark.core
def test_a_burn_share_does_not_make_the_take_unknown_as_in_v2(db):
    justhoot(db, args={"transfer_fee_pips": 50000, "shares": {"token_owner_bps": 4000, "burn_bps": 3000, "holders_bps": 3000}})
    r = result(db.card(TOKEN, config=with_platform(0)), "creator_tax")
    assert r["value"]["basis"] == "known" and r["value"]["take_bps"] == 200.0 and r["contribution"] == 40


@pytest.mark.core
def test_zero_fee_and_holders_only_launches_stay_silent_in_v2r(db):
    justhoot(db, args={"transfer_fee_pips": 0, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}})
    assert not result(db.card(TOKEN, config=with_platform(0)), "creator_tax")["fired"]
    justhoot(db, token="h.v2-launchpad.justhoot.near", dev="h.near", tx="Tx3",
             args={"transfer_fee_pips": 10000, "shares": {"token_owner_bps": 0, "burn_bps": 0, "holders_bps": 10000}})
    assert not result(db.card("h.v2-launchpad.justhoot.near", config=with_platform(0)), "creator_tax")["fired"]


@pytest.mark.core
def test_v2_and_v1r_do_not_change_for_the_same_launch(db):
    justhoot(db)
    assert not result(db.card(TOKEN, config=V2), "creator_tax")["fired"]                          # v2 does not read the call
    r = result(db.card(TOKEN, config=V1R), "creator_tax")
    assert r["fired"] and r["contribution"] == 20 and r["value"] == {"creator_bps": 10000, "source": "justhoot", "transfer_fee_pips": 50000}


@pytest.mark.core
def test_the_expert_signals_are_in_v2r_and_the_fit_leaves_them_out():
    from rugyomi.rules import fit as FIT
    cards = {s["id"] for s in rules._signal_list(V2R)}
    assert set(EXPERT) <= cards and not set(EXPERT) & {s["id"] for s in rules._signal_list(V2)}
    assert set(EXPERT) <= set(FIT.EXPERT)


# ---------------------------------------------------------------- F-043: no reason text for a fired signal with 0 points
@pytest.mark.core
def test_a_fired_signal_with_zero_points_has_no_text_but_keeps_its_value():
    r = SG._res("x", 1, True, {"n": 3}, 0, "long text", [{"kind": "launch", "id": "t"}], "short text")
    assert r["fired"] and r["value"] == {"n": 3} and r["contribution"] == 0
    assert r["reason"] == "" and r["evidence"] == [] and "short" not in r
    r = SG._res("x", 1, True, {"n": 3}, 5, "long text", [{"kind": "launch", "id": "t"}], "short text")
    assert r["reason"] == "long text" and r["short"] == "short text" and r["evidence"]


@pytest.mark.core
def test_the_v2_card_of_a_zero_point_signal_adds_no_reason_line(db):
    db.launch("a.nearlytrade.near", "dev.near", T0)
    cfg = C.effective({"signals": {"creator_tax": {"points": 0, "points_2": 0, "points_3": 0}}}, "v2r")
    c = db.card("a.nearlytrade.near", config=cfg)
    assert all(r["reason"] for r in c["reasons"] if r["contribution"])
    assert all(not r["reason"] for r in c["reasons"] if not r["contribution"])


def test_the_posts_skip_a_zero_point_reason():
    zero = {"id": "name_copy", "fired": True, "contribution": 0, "reason": "ZEROTEXT", "short": "ZEROSHORT", "evidence": []}
    card = {"score": 25, "band": "Low", "reasons": [{"id": "creator_tax", "fired": True, "contribution": 25, "reason": "REALTEXT", "short": "REALSHORT", "evidence": []}, zero],
            "rules_version": "1.2", "config_version": "v2r-fit-1", "issued_at": "2026-11-06T12:05:00.000000Z", "dev_account": "dev.near"}
    launch = {"token": "a.nearlytrade.near", "launchpad": "nearly", "creator": "dev.near", "symbol": "A"}
    cfg = P.load_config()
    for text in (P.format_card(launch, card, cfg), P.format_card_compact(launch, card, cfg)):
        assert "ZERO" not in text and "REAL" in text
    only = {**card, "reasons": [zero]}
    assert "ZERO" not in P.format_card(launch, only, cfg) and "no risk signal fired" in P.format_card_compact(launch, only, cfg)


# ---------------------------------------------------------------- bands come from the config
@pytest.mark.core
def test_the_bands_of_a_v2r_card_come_from_the_config(db):
    justhoot(db)
    assert db.card(TOKEN, config=with_platform(0))["score"] == 40 and db.card(TOKEN, config=with_platform(0))["band"] == "Low"                  # 40 is under the shipped 45 / 70
    cfg = C.effective({"bands": {"elevated": 30, "high": 60}, "signals": {"creator_tax": {"justhoot_platform_bps": 0}}}, "v2r")
    assert db.card(TOKEN, config=cfg)["band"] == "Elevated"                                                                                       # the first proposal, 30 / 60
    cfg = C.effective({"bands": {"elevated": 20, "high": 40}, "signals": {"creator_tax": {"justhoot_platform_bps": 0}}}, "v2r")
    assert db.card(TOKEN, config=cfg)["band"] == "High"
    assert C.config_hash(cfg) != C.config_hash(V2R)                                       # the bands are part of the hash: a card says which bands it was issued with


@pytest.mark.core
def test_the_methodology_text_names_the_source_of_the_justhoot_platform_share():
    doc = SG._creator_tax_doc(V2R["signals"]["creator_tax"])
    assert "justhoot docs" in doc and "RHEA 0.2 % + platform 0.8 %" in doc and "transfer tax 0-5 % on top" in doc and "platform's cut of the transfer tax is 0 bps" in doc
    assert "justhoot docs" not in SG._creator_tax_doc(V2["signals"]["creator_tax"])


@pytest.mark.core
def test_the_fitted_fee_tiers_never_fall_with_the_take():
    c = V2R["signals"]["creator_tax"]
    assert c["points"] >= 0 and c["points_2"] >= 0 and c["points_3"] >= 0 and 0 < c["take2_bps"] < c["take3_bps"]
    assert (c["points"], c["points_2"], c["points_3"]) == (20, 10, 10) and V2R["bands"] == {"elevated": 45, "high": 70}             # the equal-coverage bands the user chose (Q-030, C)


# ---------------------------------------------------------------- the site reads the live profile from the stored cards (RGY-049e)
@pytest.mark.core
def test_the_methodology_follows_the_live_rules_version():
    m = rules.methodology("1.2")
    assert [b["range"] for b in m["bands"]] == ["0–44", "45–69", "70–100"] and "v2r-fit-2" in m["bands_note"] and "issued earlier keep" in m["bands_note"]
    assert m["expert"]["live"] is True and m["expert"]["config_version"] == "v2r-fit-2" and "since the switch" in m["expert"]["intro"]
    m = rules.methodology("1.1")
    assert [b["range"] for b in m["bands"]] == ["0–29", "30–59", "60–100"] and m["expert"]["live"] is True and m["expert"]["config_version"] == "v1-rules-1"
    m = rules.methodology("0.1")
    assert m["bands_note"] == "" and m["expert"]["live"] is False and "not live" in m["expert"]["intro"].lower()


@pytest.mark.core
def test_profile_of_maps_every_rules_version():
    assert [rules.profile_of(v) for v in ("1.2", "1.1", "1.0", "0.1", None)] == ["v2r", "v1r", "v1", "v0", "v0"]


def _store_with_cards(path, versions):
    from rugyomi.store import open_store
    s = open_store(path)
    s.con.execute("BEGIN")
    for i, v in enumerate(versions):
        s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, config_version, config_hash, "
                      "rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (f"t{i}.nearlytrade.near", "2026-11-11T12:00:00.000000Z", 100 + i, 10**18 + i, 20, "Low", "[]", "{}", None, "x", "h", v, 1))
    s.con.execute("COMMIT")
    return s


@pytest.mark.core
def test_the_live_rules_version_is_the_newest_stored_card(tmp_path):
    from rugyomi.web import queries
    s = _store_with_cards(tmp_path / "a.db", [])
    assert queries.live_rules_version(s.con) is None
    s.close()
    s = _store_with_cards(tmp_path / "b.db", ["1.1", "1.1", "1.2"])
    assert queries.live_rules_version(s.con) == "1.2"
    s.close()
    s = _store_with_cards(tmp_path / "c.db", ["1.2", "1.1"])
    assert queries.live_rules_version(s.con) == "1.1"
    s.close()
    con = sqlite3.connect(":memory:")
    assert queries.live_rules_version(con) is None                      # no table at all
    con.close()


def test_the_methodology_page_shows_the_bands_and_live_flag_of_the_profile_in_the_cards(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from rugyomi.web import app as webapp
    monkeypatch.delenv("RUGYOMI_RULES_PROFILE", raising=False)             # the web service has no profile variable
    p = tmp_path / "w.db"
    _store_with_cards(p, ["1.1", "1.2"]).close()
    with TestClient(webapp.create_app(p)) as c:
        t = c.get("/methodology").text
    assert "45–69" in t and "70–100" in t and "0–44" in t and "not fitted · live · configuration v2r-fit-2" in t.replace("\n", " ") and "issued earlier keep the bands" in t
