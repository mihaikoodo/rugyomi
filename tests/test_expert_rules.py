"""RGY-045: two expert rules (not fitted) in the new profile v1r - a creator name that resembles a notable account, and a fresh account funded through Intents / an exchange.
The live profiles v0 / v1 / v2 must not change: their config hashes and their output are pinned here and in test_holdings."""
import json

import pytest

from test_rules import HOUR, MIN, NEAR, T0, Db, result
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import notable as N

V1R = C.effective(None, "v1r")
V1 = C.effective(None, "v1")


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


# ---------------------------------------------------------------- the similarity rule (pure)
HITS = [
    ("rootnearr.near", "root.near", "decorated"),       # the case that started it: root + near + a doubled r
    ("r00t.near", "root.near", "same"),                # look-alike characters
    ("roott.near", "root.near", "same"),               # repeated letter
    ("r-o-o-t.near", "root.near", "same"),
    ("root.tg", "root.near", "same"),                  # another top-level suffix is another account
    ("rootofficial.near", "root.near", "decorated"),
    ("realroot.near", "root.near", "decorated"),
    ("root.team.near", "root.near", "decorated"),
    ("rootapp.near", "root.near", "decorated"),
    ("omnibridg.near", "omni.bridge.near", "one_off"),     # one character dropped (both names >= 7 characters)
    ("omnibrigde.near", "omni.bridge.near", "one_off"),    # two neighbours swapped
    ("omnibridgx.near", "omni.bridge.near", "one_off"),    # one character changed
    ("reff-inance.near", "v2.ref-finance.near", "same"),   # an alias of the entry
    ("ref-finanse.near", "v2.ref-finance.near", "one_off"),
    ("memecooking-official.near", "meme-cooking.near", "decorated"),
]
MISSES = [
    "root.near",                # the notable account itself
    "x.root.near",              # its own sub-account: created by root.near
    "a.b.root.near",
    "rooted.near",              # not a decoration from the list
    "rut.near",                 # too far
    "sam.near", "hotdog.near", "randomguy.near",
    "wrap.near",                # on the list itself: not an imitation of another entry
    "omft.near",
    "root111.near", "root18.near", "root2.near", "near6900.near",       # digits are not a decoration (11.11)
    "root123.near", "root.team123.near",
    "near.near", "nearly.near", "nearlytrade.near", "intents.near", "launchpad.justhoot.near", "justhoot.near", "near-by.near", "nearapp.near",   # platform words left the list (11.11)
    "burrw.near", "intear1.near",    # one character off, but the name is shorter than 7 characters
    "ab.near",                  # shorter than any rule needs
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",     # an implicit account
    "", None,
]


@pytest.mark.core
@pytest.mark.parametrize("creator,notable_acct,how", HITS)
def test_a_name_that_resembles_a_notable_account_is_found(creator, notable_acct, how):
    h = N.resembles(creator)
    assert h and h["account"] == notable_acct and h["how"] == how, h


@pytest.mark.core
@pytest.mark.parametrize("creator", MISSES)
def test_names_that_do_not_resemble_or_that_are_the_account_or_its_own_sub_accounts_are_not_found(creator):
    assert N.resembles(creator) is None


@pytest.mark.core
def test_short_names_get_no_one_character_hit_and_digits_never_decorate():
    assert N.MIN_EDIT_LEN == 7
    assert N.resembles("roat.near") is None                                                           # `root` is 4 characters: no edit-distance rule
    h = N.resembles("rootnear.near")
    assert h and h["account"] == "root.near" and h["how"] == "decorated"
    assert N.resembles("rootnearr.near")["account"] == "root.near"                                    # the case that started it still fires
    assert N.resembles("root111.near") is None and N.resembles("r00t111.near") is None
    assert N.resembles("memecookin.near")["how"] == "one_off"                                         # 10 characters: one off counts


@pytest.mark.core
def test_the_normalisation_and_the_edit_distance_helpers():
    assert N.normalize("R-0_0.t") == "rot" and N.normalize("rn") == "m" and N.normalize("vvvv") == "w" and N.normalize("LeeT1") == "let" + "l"
    assert N.stem("rootnearr.near") == "rootnearr" and N.stem("x.root.tg") == "x.root" and N.stem("near") == "near"
    for a, b in (("abcde", "abcde"), ("abcde", "abcdx"), ("abcde", "abcd"), ("abcde", "xabcde"), ("abcde", "abdce"), ("abcde", "bcde")):
        assert N.within_one(a, b) and N.within_one(b, a), (a, b)
    for a, b in (("abcde", "abxye"), ("abcde", "ab"), ("abcde", "edcba"), ("abcde", "bacde" + "x")):
        assert not N.within_one(a, b), (a, b)


def lst_note(lst):
    import json
    return json.loads(N.NOTABLE_PATH.read_text(encoding="utf-8"))["note"]


@pytest.mark.core
def test_the_lists_are_explicit_with_a_source_per_entry_and_no_pattern():
    lst = N.notable()
    accts = [e["account"] for e in lst["accounts"]]
    assert len(accts) == len(set(accts)) and "root.near" in accts and all(e.get("source") for e in lst["accounts"])
    assert all(a == a.lower() and "*" not in a and "?" not in a and " " not in a for a in accts)
    assert all(a == a.lower() and "*" not in a for e in lst["accounts"] for a in e.get("aliases", []))
    fs = N.funding_sources()
    assert {e["account"] for e in fs["intents"]} >= {"intents.near", "omft.near", "omni.bridge.near"}
    removed = {"near", "nearlytrade.near", "intents.near", "launchpad.justhoot.near"}
    assert not removed & set(accts) and all(a not in {x for e in lst["accounts"] for x in e.get("aliases", [])} for a in ("nearly", "justhoot", "intents"))
    assert "illia.near" in lst_note(lst) and lst["version"] == "2026-11-11-2"
    assert all(e.get("source") for e in fs["intents"] + fs["exchanges"]) and fs["exchanges"] == []        # no exchange is listed until the user confirms entries
    assert N.funding_source_kind("intents.near") == "intents" and N.funding_source_kind("rich.near") is None and N.funding_source_kind(None) is None


# ---------------------------------------------------------------- the signals on a database
def launch(db, token, dev, at=T0):
    db.launch(token, dev, at, dev_buy=5 * NEAR)
    return db.card(token, config=V1R)


@pytest.mark.core
def test_name_imitation_fires_in_v1r_with_evidence_and_careful_words_and_never_in_v1_or_v0(db):
    c = launch(db, "nulls-2.nearlytrade.near", "rootnearr.near")
    r = result(c, "name_imitation")
    assert r["fired"] and r["contribution"] == 40 and r["value"]["resembles"] == "root.near" and r["value"]["how"] == "decorated"
    assert r["reason"] == "The creator account name resembles root.near (the same name with a decoration added); it is not that account."
    assert r["short"] == "creator name resembles root.near" and [e["id"] for e in r["evidence"]] == ["rootnearr.near", "root.near"]
    low = (r["reason"] + r["short"]).lower()
    assert "impersonat" not in low and "fake" not in low and "scam" not in low
    assert c["score"] >= 40 and c["band"] == "Elevated" and c["config_version"] == "v1-rules-1"          # alone = Elevated
    for prof in ("v0", "v1", "v2"):                                                                       # the live and the older profiles do not know the signal
        ids = {x["id"] for x in db.card("nulls-2.nearlytrade.near", config=C.effective(None, prof))["signals"]["results"]}
        assert "name_imitation" not in ids and "fresh_obfuscated_funding" not in ids


@pytest.mark.core
def test_the_notable_account_and_its_sub_accounts_do_not_fire(db):
    assert not result(launch(db, "a.nearlytrade.near", "root.near"), "name_imitation")["fired"]
    assert not result(launch(db, "b.nearlytrade.near", "x.root.near"), "name_imitation")["fired"]
    assert not result(launch(db, "c.nearlytrade.near", "sam.near"), "name_imitation")["fired"]


def test_name_imitation_is_silent_when_the_dev_is_unknown(db):
    db.launch("u.nearlytrade.near", None, T0, owner=None)
    assert result(db.card("u.nearlytrade.near", config=V1R), "name_imitation")["value"] == {"skipped": "dev account unknown"}


def fund_case(db, token, dev, age_min, sender, near, funded_min_before, at=T0):
    db.launch(token, dev, at, dev_buy=5 * NEAR)
    db.created(dev, at - age_min * MIN)
    db.fund(sender, dev, near, at - funded_min_before * MIN)
    return result(db.card(token, config=V1R), "fresh_obfuscated_funding")


@pytest.mark.core
def test_a_fresh_account_funded_via_intents_fires_ten_points_and_inflows_do_not_drop_intents(db):
    r = fund_case(db, "a.nearlytrade.near", "fresh1.near", 25, "intents.near", 6, 20)
    assert r["fired"] and r["contribution"] == 10 and r["value"]["funder_kind"] == "intents" and r["value"]["largest_funder"] == "intents.near"
    assert r["value"]["account_age_min"] == 25.0
    assert "created 25 min before the launch" in r["reason"] and "intents.near" in r["reason"] and "does not show the original sender" in r["reason"]
    assert r["short"] == "account 25 min old, funded via intents.near" and [e["kind"] for e in r["evidence"]] == ["tx", "tx"]


@pytest.mark.core
@pytest.mark.parametrize("age_min,sender,near,funded_min_before,why", [
    (90, "intents.near", 6, 20, "the account is older than 60 minutes"),
    (25, "rich.near", 6, 20, "an ordinary sender"),
    (25, "intents.near", 0.5, 20, "below the minimum funding"),
    (25, "intents.near", 6, 70, "the funding is older than the window"),
])
def test_the_fresh_obfuscated_rule_stays_silent(db, age_min, sender, near, funded_min_before, why):
    assert not fund_case(db, "z.nearlytrade.near", "fresh2.near", age_min, sender, near, funded_min_before)["fired"], why


@pytest.mark.core
def test_the_largest_transfer_decides_and_later_facts_are_ignored(db):
    db.launch("a.nearlytrade.near", "fresh3.near", T0, dev_buy=5 * NEAR)
    db.created("fresh3.near", T0 - 30 * MIN)
    db.fund("intents.near", "fresh3.near", 2, T0 - 20 * MIN)
    db.fund("rich.near", "fresh3.near", 9, T0 - 10 * MIN)                  # the larger transfer is an ordinary one
    assert not result(db.card("a.nearlytrade.near", config=V1R), "fresh_obfuscated_funding")["fired"]
    db.fund("intents.near", "fresh3.near", 50, T0 + 5 * MIN)               # after the launch: not knowable at the launch time
    assert not result(db.card("a.nearlytrade.near", config=V1R), "fresh_obfuscated_funding")["fired"]
    db.fund("intents.near", "fresh3.near", 40, T0 - 5 * MIN)               # now the largest before the launch
    assert result(db.card("a.nearlytrade.near", config=V1R), "fresh_obfuscated_funding")["fired"]


@pytest.mark.core
def test_an_account_created_after_the_launch_or_unknown_is_silent(db):
    db.launch("a.nearlytrade.near", "late.near", T0, dev_buy=5 * NEAR)
    db.created("late.near", T0 + MIN)
    db.fund("intents.near", "late.near", 6, T0 - 5 * MIN)
    assert result(db.card("a.nearlytrade.near", config=V1R), "fresh_obfuscated_funding")["value"] == {"fresh_account": None}
    db.launch("b.nearlytrade.near", "nodata.near", T0, dev_buy=5 * NEAR)
    db.fund("intents.near", "nodata.near", 6, T0 - 5 * MIN)
    r = result(db.card("b.nearlytrade.near", config=V1R), "fresh_obfuscated_funding")
    assert not r["fired"] and r["value"] == {"fresh_account": None}


@pytest.mark.core
def test_a_listed_exchange_wallet_counts_like_intents(db, monkeypatch):
    base = N.funding_sources()
    monkeypatch.setattr(N, "funding_sources", lambda: {**base, "exchanges": [{"account": "cexwallet.near", "source": "test"}]})
    r = fund_case(db, "a.nearlytrade.near", "fresh4.near", 25, "cexwallet.near", 6, 20)
    assert r["fired"] and r["value"]["funder_kind"] == "exchange" and "an exchange wallet on our list" in r["reason"]


@pytest.mark.core
def test_the_two_signals_add_up_with_the_cap_and_no_text_about_a_cap(db):
    db.launch("a.nearlytrade.near", "rootnearr.near", T0, dev_buy=40 * NEAR)
    db.created("rootnearr.near", T0 - 3 * MIN)
    db.fund("intents.near", "rootnearr.near", 41, T0 - 3 * MIN)
    c = db.card("a.nearlytrade.near", config=V1R)
    pts = {r["id"]: r["contribution"] for r in c["reasons"]}
    assert pts["name_imitation"] == 40 and pts["fresh_obfuscated_funding"] == 10 and pts["fingerprint"] == 35
    assert c["score"] == min(100, sum(pts.values())) == 85
    assert "cap" not in " ".join(r["reason"] for r in c["reasons"]).lower() and "capped" not in str(c["signals"]["notes"]).lower()


# ---------------------------------------------------------------- the profiles
@pytest.mark.core
def test_the_new_profile_is_a_copy_of_v1_plus_two_expert_signals_and_the_others_are_unchanged():
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2")] == ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371"]
    a, b = dict(V1R), dict(V1)
    assert V1R["version"] == "v1-rules-1" and V1R["rules_version"] == "1.1" and V1R["display"]["rug_chance"] is False and "Not live" not in V1R["display"]["note"]
    sig_a, sig_b = a.pop("signals"), b.pop("signals")
    for k in ("version", "rules_version", "display"):
        a.pop(k), b.pop(k)
    assert a == b                                                                       # bands, outcomes, rug, fit: the same
    ct = dict(sig_a["creator_tax"])
    assert ct.pop("justhoot") is True                                                   # RGY-045b: v1r also reads the justhoot creator fee
    sig_a = {**sig_a, "creator_tax": ct}
    assert {k: v for k, v in sig_a.items() if k not in ("name_imitation", "fresh_obfuscated_funding")} == sig_b
    assert sig_a["name_imitation"] == {"enabled": True, "points": 40} and sig_a["fresh_obfuscated_funding"]["points"] == 10
    assert C.config_hash(V1R) not in {"2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371"}


@pytest.mark.core
def test_v1_cards_are_the_same_as_before_the_new_signals_existed(db):
    c = launch(db, "n.nearlytrade.near", "rootnearr.near")
    old = db.card("n.nearlytrade.near", config=V1)
    assert c["config_hash"] != old["config_hash"] and old["config_hash"] == "6fedfdb50583" and old["config_version"] == "v1-fit-1"
    assert {r["id"] for r in old["signals"]["results"]} == {s.id for s in rules.SIGNALS} - {"name_imitation", "fresh_obfuscated_funding"} - {
        s.id for s in rules.SIGNALS if not V1["signals"].get(s.id, {}).get("enabled", True)}


@pytest.mark.core
def test_the_methodology_marks_the_new_rules_as_expert_rules_not_fitted():
    m = rules.methodology()
    e = m["expert"]
    assert [s["id"] for s in e["signals"]] == ["name_imitation", "fresh_obfuscated_funding"] and e["config_version"] == "v1-rules-1"
    assert "not fitted" in e["intro"].lower() and "not live" in e["intro"].lower() and "few" in e["intro"].lower()
    assert e["live"] is False and "justhoot launches" in e["intro"]
    assert e["notable_list"]["version"] == N.notable()["version"] and any(x["account"] == "root.near" for x in e["notable_list"]["accounts"])
    doc = next(s for s in e["signals"] if s["id"] == "name_imitation")
    text = json.dumps(doc)
    assert "7 or more characters" in text and "digits alone do not count" in text and "5 or more" not in text and "or digits added" not in text


@pytest.mark.core
def test_when_v1r_is_the_live_profile_the_page_says_so(monkeypatch):
    monkeypatch.setenv("RUGYOMI_RULES_PROFILE", "v1r")
    e = rules.methodology()["expert"]
    assert e["live"] is True and "not live" not in e["intro"].lower() and "since the switch" in e["intro"]
    from rugyomi.web import app as webapp  # noqa: F401  (the template renders from this dict)
    from pathlib import Path
    html = (Path(rules.__file__).parents[1] / "web" / "templates" / "methodology.html").read_text(encoding="utf-8")
    assert "'live' if m.expert.live else 'not live'" in html
