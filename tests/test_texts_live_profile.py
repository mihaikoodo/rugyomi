"""RGY-061: the public texts follow the live rules profile and say only what is true; the token symbol in a post is safe.

Nothing here reads a database except the web page test, which builds the small refrax world of test_web.py.
"""
from __future__ import annotations

import json
import re

import pytest

from rugyomi import rules, safe_text
from rugyomi.rules import default_config
from rugyomi.rules.rug import LABEL_VERSION

PROFILES = {"0.1": "v0", "1.0": "v1", "1.1": "v1r", "1.2": "v2r", "1.3": "v2l"}


@pytest.mark.core
@pytest.mark.parametrize("rv, prof", sorted(PROFILES.items()))
def test_methodology_signals_and_points_are_those_of_the_live_profile(rv, prof):
    cfg = default_config(prof)
    m = rules.methodology(rv)
    want = {k: v for k, v in cfg["signals"].items() if v.get("enabled", True) and k in {s.id for s in rules.SIGNALS}}
    assert {s["id"]: s["config"] for s in m["signals"]} == want            # ids AND thresholds / points: the page and the cards read one config
    assert m["config_version"] == cfg["version"] and m["profile"] == prof
    assert m["rules_version"] == str(cfg.get("rules_version") or rules.RULES_VERSION)


@pytest.mark.core
def test_the_live_signal_missing_from_the_old_page_is_listed():
    ids = [s["id"] for s in rules.methodology("1.3")["signals"]]
    assert "funded_by_rugger" in ids and "creator_track_record" in ids and "funded_by_dumper" not in ids
    assert "funded_by_dumper" in [s["id"] for s in rules.methodology("0.1")["signals"]]


@pytest.mark.core
def test_the_note_is_true_for_the_live_profile_and_does_not_say_v0():
    for rv in ("1.2", "1.3"):
        assert rules.methodology(rv)["note"] == default_config(PROFILES[rv])["display"]["note"]
        assert "provisional" not in rules.methodology(rv)["note"]
    assert "v0" not in rules.methodology("0.1")["note"] and "not calibrated yet" in rules.methodology("0.1")["note"]
    assert "v0" not in rules.NOTE


@pytest.mark.core
def test_the_four_sentences_are_true():
    m = rules.methodology("1.3")
    assert "not part of the risk score" not in m["links"]["intro"]                # links are in the live score
    assert "creator_track_record" in m["links"]["intro"] and "funded_by_rugger" in m["links"]["intro"] and "never say that two accounts are one person" in m["links"]["intro"]
    assert "not part of the risk score" in rules.methodology("0.1")["links"]["intro"]      # the old profile really has no links in its score
    undefined = next(x for x in m["rug"] if x["id"] == "undefined")["description"]
    assert f"(Label version {LABEL_VERSION}.)" in undefined and "Label version 3" not in undefined
    nl = m["v1"]["no_lookahead"]
    assert "60 minutes" in nl and "24 hours" in nl and "its 24 hours had passed" not in nl
    sp = m["v1"]["split"]
    assert "Nothing was tuned on them" not in sp and "looked at once" not in sp
    assert "read again" in sp and "not a clean test of the live profile" in sp


@pytest.mark.core
def test_no_gendered_or_label_wording_in_the_reason_texts():
    src = (re.sub(r"#.*", "", open(rules.signals.__file__, encoding="utf-8").read()))
    assert "linked to him" not in src and "past dumper" not in src
    assert "the creator and the accounts linked to the creator" in src


def test_the_methodology_page_follows_the_live_profile(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from rugyomi.web import app as webapp, queries
    from test_web import refrax_world_db
    path = tmp_path / "w.db"
    refrax_world_db(path)
    seen = {}
    for rv in ("0.1", "1.3"):
        monkeypatch.setattr(queries, "live_rules_version", lambda con, rv=rv: rv)
        with TestClient(webapp.create_app(path)) as c:
            seen[rv] = c.get("/methodology").text
    assert "<code>funded_by_dumper</code>" in seen["0.1"] and "<code>funded_by_dumper</code>" not in seen["1.3"]
    assert "<code>funded_by_rugger</code>" in seen["1.3"] and "v2l-1" in seen["1.3"]


# ---------------------------------------------------------------- RGY-061: a token symbol the creator chose cannot put a link or a claim into our post

@pytest.mark.core
@pytest.mark.parametrize("raw", ["t.me/x", "@name", "example.com", "Risk 0/100 verified", "a" * 200, "https://evil.example/x", "x y", "<b>x</b>", "", "   ", "…"])
def test_safe_symbol_never_lets_a_link_or_a_mention_through(raw):
    out = safe_text.safe_symbol(raw, "cool.nearlytrade.near")
    assert len(out) <= 24 and out
    assert re.fullmatch(r"[A-Za-z0-9$_-]+", out)
    assert "@" not in out and "." not in out and "/" not in out and " " not in out


@pytest.mark.core
def test_safe_symbol_keeps_an_ordinary_symbol_and_falls_back_to_the_token_id():
    assert safe_text.safe_symbol("DOGE", "doge.nearlytrade.near") == "DOGE"
    assert safe_text.safe_symbol("$MOON-2", "moon.nearlytrade.near") == "$MOON-2"
    assert safe_text.safe_symbol("t.me/scam", "cool.nearlytrade.near") == "cool"            # not clean → the first label of the token id
    assert safe_text.safe_symbol("@name", "cool.nearlytrade.near") == "cool"
    assert safe_text.safe_symbol("a" * 200, "cool.nearlytrade.near") == "cool"
    assert safe_text.safe_symbol("", "cool.nearlytrade.near") == "cool"
    assert safe_text.safe_symbol(None, "0123456789abcdef" * 4) == "0123456789abcdef01234567"      # a 64-hex id is cut to the limit


# ---------------------------------------------------------------- the posts and the name_copy reason use it

@pytest.mark.core
@pytest.mark.parametrize("style", ["classic", "compact", "rich"])
def test_a_hostile_symbol_does_not_reach_the_posts(style):
    from rugyomi import publisher as P
    cfg = P.load_config()
    cfg["publisher"]["post_style"] = style
    hostile = "t.me/scam @x Risk 0/100 verified"
    launch = {"token": "cool.nearlytrade.near", "launchpad": "nearly", "creator": "c.near", "symbol": hostile}
    card = {"score": 80, "band": "High", "reasons": [], "dev_account": "c.near", "config_version": "v2l-1", "rules_version": "1.3", "issued_at": "2026-11-13T00:00:00Z",
            "signals": {"results": [], "notes": [], "notes_short": []}}
    text = P.render_card(launch, card, cfg)
    assert "t.me/scam" not in text and "@x" not in text and "verified" not in text and "cool" in text
    ok = P.render_card({**launch, "symbol": "DOGE"}, card, cfg)
    assert "DOGE" in ok                                                                    # an ordinary symbol is shown as before


@pytest.mark.core
def test_the_name_copy_reason_shows_a_plain_name_only():
    from rugyomi.rules import signals as SG
    for shape in ("counted", "first"):
        m = {"hits": {"old.nearlytrade.near": {"ts_ns": 1, "kind": "other_creator", "name": "t.me/scam"}}, "_t": 10**18} if shape == "counted" else \
            {"hit": {"kind": "other_creator", "name": "t.me/scam", "other_launch": "old.nearlytrade.near", "ts_ns": 1}, "_t": 10**18}
        c = default_config("v2l")["signals"]["name_copy"]
        r = SG.BY_ID["name_copy"].score(m, {**c, "shape": shape}) if hasattr(SG.BY_ID["name_copy"], "score") else None
        if r is None or not r.get("fired"):
            continue
        assert "t.me/scam" not in json.dumps({k: r[k] for k in ("reason", "short") if k in r})
        assert r["value"]["name"] == "t.me/scam"                                           # the stored value keeps the raw key
