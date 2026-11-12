"""RGY-045b: the justhoot creator fee. The collector keeps the token-creation call (`launch_call`: transfer_fee_pips, shares); the profile v1r (only) reads it into `creator_tax`.
The block is a REAL mainnet block (220657475: the creation call of JUX, fee 2 %, all to the token owner); the profiles v0 / v1 / v2 give the same cards as before."""
import base64
import json

import pytest

from rugyomi_testkit import block, hid, load_block, reo, FIXTURES
from test_rules import MIN, NEAR, T0, Db, result
from rugyomi import launches as L
from rugyomi import rules
from rugyomi.parse import keep_option_a, parse_block
from rugyomi.rules import config as C
from rugyomi.rules import signals as SG
from rugyomi.store.db import hash_bytes

V1R = C.effective(None, "v1r")
V1 = C.effective(None, "v1")
JH2 = "v2-launchpad.justhoot.near"
TOKEN = "bagflip.v2-launchpad.justhoot.near"
TX = "HZ8V7kYc8GipWvgbwq9U3PaJ7KKQ7PPcYkMgyv2pU8ih"


def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def call(args, recv=JH2, method="launch_meme", ok=True, dep="1000000000000000000000000"):
    return reo(hid("r", method, recv), hid("t", method, recv), "dev.near", recv,
               [{"FunctionCall": {"method_name": method, "args": args if isinstance(args, str) else b64(args), "gas": 1, "deposit": dep}}], ok=ok)


# ---------------------------------------------------------------- the parser
@pytest.mark.core
def test_the_creation_call_of_a_real_block_becomes_one_launch_call_fact_without_the_icon():
    b = load_block(FIXTURES / "cache" / "220657475.json.gz")
    errs = []
    facts = parse_block(b, errors=errs)
    assert not errs
    (f,) = [x for x in facts if x.kind == "launch_call"]
    assert (f.a, f.b) == ("juvrix.near", JH2) and f.amount == "20986551155259087052079104" and f.tx_hash == "H9An9dNVJNMspv3zEvZNd53MPc1YhZEJLH2vmuhx7GwD"
    a = f.extra["args"]["args"]
    assert f.extra["method"] == "launch_meme" and a["transfer_fee_pips"] == 20000 and a["shares"] == {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}
    assert a["icon"] == "<6755 chars>" and a["symbol"] == "JUX" and len(json.dumps(f.extra)) < 1500
    assert keep_option_a(f)
    assert len({x.key for x in facts}) == len(facts)                                             # the same action also gives its near_fc_deposit: another origin
    assert f.origin.endswith(".c") and any(x.kind == "near_fc_deposit" and x.origin == f.origin[:-2] for x in facts)


@pytest.mark.core
@pytest.mark.parametrize("make", [
    lambda: call({"x": 1}, recv="other.near"),                                  # not a justhoot launchpad
    lambda: call({"x": 1}, method="buy"),                                       # another method
    lambda: call("!!!not-base64!!!"),
    lambda: call(b64([1, 2])),                                                  # arguments that are not an object
    lambda: call({"transfer_fee_pips": 1}, ok=False),                           # a failed receipt changed nothing
])
def test_no_launch_call_fact_where_it_is_not_the_creation_call(make):
    assert [f for f in parse_block(block(1, [make()])) if f.kind == "launch_call"] == []


@pytest.mark.core
def test_the_v1_launchpad_call_is_kept_too():
    (f,) = [x for x in parse_block(block(1, [call({"args": {"transfer_fee_pips": 10000, "shares": {}}}, recv="launchpad.justhoot.near")])) if x.kind == "launch_call"]
    assert f.b == "launchpad.justhoot.near"


# ---------------------------------------------------------------- the reader
@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


def justhoot(db, token=TOKEN, dev="bagflip.near", args=None, tx=TX, launchpad="justhoot_v2", with_call=True):
    db.launch(token, dev, T0, dev_buy=5 * NEAR, launchpad=launchpad)
    db.s.con.execute("UPDATE launches SET tx = ? WHERE token = ?", (tx, token))
    if with_call:
        db.fact("launch_call", dev, JH2, amount=NEAR, ts=T0 - 5 * 10**9, tx=tx,
                extra={"method": "launch_meme", "args": {"args": args or {"transfer_fee_pips": 50000, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}}}})
    row = db.s.con.execute("SELECT token, launchpad, tx FROM launches WHERE token = ?", (token,)).fetchone()
    return L.justhoot_fee(db.s, dict(zip(("token", "launchpad", "tx"), row)))


@pytest.mark.core
def test_the_fee_of_bagflip_in_the_shape_of_the_nearly_tax_options(db):
    assert justhoot(db) == {"transfer_fee_pips": 50000, "creator_bps": 10000, "holders_bps": 0, "burn_bps": 0, "source": "justhoot", "buy_bps": 500, "sell_bps": 500}


@pytest.mark.core
def test_a_fee_that_is_not_a_whole_number_of_bps_has_no_rate_and_a_split_fee_keeps_its_shares(db):
    t = justhoot(db, args={"transfer_fee_pips": 12345, "shares": {"token_owner_bps": 4000, "burn_bps": 3000, "holders_bps": 3000}})
    assert "buy_bps" not in t and t["creator_bps"] == 4000 and t["burn_bps"] == 3000 and t["holders_bps"] == 3000 and "platform_bps" not in t


@pytest.mark.core
@pytest.mark.parametrize("args", [
    {"shares": {"token_owner_bps": 1, "burn_bps": 0, "holders_bps": 0}},                      # no fee
    {"transfer_fee_pips": 0, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}},   # fee 0: the owner gets nothing (lunc, arsoncoin on the server)
    {"transfer_fee_pips": 5, "shares": None},
    {"transfer_fee_pips": -1, "shares": {"token_owner_bps": 1, "burn_bps": 0, "holders_bps": 0}},
    {"transfer_fee_pips": 5, "shares": {"token_owner_bps": "all", "burn_bps": 0, "holders_bps": 0}},
    {"transfer_fee_pips": 5, "shares": {"token_owner_bps": 10001, "burn_bps": 0, "holders_bps": 0}},
])
def test_a_call_without_a_usable_fee_gives_none(db, args):
    assert justhoot(db, args=args) is None


@pytest.mark.core
def test_a_launch_whose_call_was_not_stored_or_that_is_not_justhoot_has_no_fee(db):
    assert justhoot(db, with_call=False) is None
    db.launch("n.nearlytrade.near", "x.near", T0)
    db.s.con.execute("UPDATE launches SET tx = ? WHERE token = ?", (TX, "n.nearlytrade.near"))
    assert L.justhoot_fee(db.s, {"token": "n.nearlytrade.near", "launchpad": "nearly", "tx": TX}) is None                  # the same tx hash, another launchpad
    assert L.justhoot_fee(db.s, {"token": TOKEN, "launchpad": "justhoot_v2", "tx": None}) is None


# ---------------------------------------------------------------- the signal
@pytest.mark.core
def test_bagflip_fires_creator_tax_in_v1r_only_and_the_words_are_careful(db):
    justhoot(db)
    c = db.card(TOKEN, config=V1R)
    r = result(c, "creator_tax")
    assert r["fired"] and r["contribution"] == 20 and r["value"] == {"creator_bps": 10000, "source": "justhoot", "transfer_fee_pips": 50000}
    assert r["reason"] == "Token transfer fee 5%; the token owner's share of it is 100% (justhoot launch call: transfer_fee_pips 50000, token_owner_bps 10000)."
    assert r["short"] == "token fee 5%, the owner gets 100% of it" and len(r["reason"]) <= 170
    assert "nearly" not in r["reason"].lower() and "platform" not in r["reason"].lower()
    assert c["score"] == 20 and c["band"] == "Low"
    for prof in ("v0", "v1", "v2"):                                                           # the live and the older profiles do not read it
        cc = db.card(TOKEN, config=C.effective(None, prof))
        assert not result(cc, "creator_tax")["fired"] and result(cc, "creator_tax")["value"] == {"creator_bps": None}


@pytest.mark.core
def test_a_zero_fee_launch_does_not_fire_creator_tax_in_v1r(db):
    justhoot(db, args={"transfer_fee_pips": 0, "shares": {"token_owner_bps": 10000, "burn_bps": 0, "holders_bps": 0}})
    r = result(db.card(TOKEN, config=V1R), "creator_tax")
    assert not r["fired"] and r["value"] == {"creator_bps": None}


@pytest.mark.core
def test_a_fee_for_the_holders_only_does_not_fire_creator_tax_in_v1r(db):
    justhoot(db, args={"transfer_fee_pips": 10000, "shares": {"token_owner_bps": 0, "burn_bps": 0, "holders_bps": 10000}})        # elonmusk, dabi on the server
    r = result(db.card(TOKEN, config=V1R), "creator_tax")
    assert not r["fired"] and r["value"] == {"creator_bps": 0}


@pytest.mark.core
def test_the_worst_case_text_fits_the_post(db):
    t = {"source": "justhoot", "transfer_fee_pips": 1000000, "creator_bps": 10000}
    assert len(SG._tax_options_text(t, 10000)) <= 170 and len(SG._tax_short(t, 10000)) <= 60


@pytest.mark.core
def test_a_justhoot_launch_without_a_stored_call_stays_silent_like_before(db):
    justhoot(db, with_call=False)
    r = result(db.card(TOKEN, config=V1R), "creator_tax")
    assert not r["fired"] and r["value"] == {"creator_bps": None}


@pytest.mark.core
def test_nearly_cards_do_not_change_in_v1r(db):
    db.launch("a.nearlytrade.near", "dev.near", T0)
    for cfg in (V1R, V1):
        r = result(db.card("a.nearlytrade.near", config=cfg), "creator_tax")
        assert not r["fired"] and r["value"] == {"creator_bps": None}


@pytest.mark.core
def test_the_profile_pins_and_the_methodology_mention(db):
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2")] == ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371"]
    assert V1R["signals"]["creator_tax"]["justhoot"] is True and "justhoot" not in V1["signals"]["creator_tax"]
    assert "justhoot launch counts the same way" in SG._creator_tax_doc(V1R["signals"]["creator_tax"])
    assert "justhoot" not in SG._creator_tax_doc(V1["signals"]["creator_tax"])
