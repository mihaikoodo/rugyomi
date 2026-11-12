"""RGY-041: every signal and every rug label also returns a `short` text for the channel post — facts and numbers, one line, in the same place as the long
reason (one source). An unfired signal has none; the config hash does not change."""
import re

import pytest

from rugyomi import rules
from rugyomi.rules import config as C
from test_rules import DAY, HOUR, MIN, NEAR, T0, db, result  # noqa: F401  (fixture `db`)
from test_rug import DEV, buy, move, rows, sell, world  # noqa: F401

FUNDING_TX = "3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG"


def check(r, expected=None):
    s = r["short"]
    assert s and len(s) <= 100, s
    assert FUNDING_TX not in s and not re.search(r"[A-Za-z0-9]{30,}", s), s                       # no raw hash in the text
    assert "[" not in s and "]" not in s
    if expected is not None:
        assert s == expected, s


@pytest.mark.core
def test_fingerprint_short(db):
    L = T0
    db.launch("x.nearlytrade.near", "fresh.near", L, dev_buy=40 * NEAR)
    db.created("fresh.near", L - 6 * MIN)
    db.fund("rich.near", "fresh.near", 41, L - 5 * MIN)
    check(result(db.card("x.nearlytrade.near"), "fingerprint"), "creator account 6 min old, got 41.0 NEAR 5 min before launch, dev buy 98% of it")
    db.launch("y.nearlytrade.near", "fresh2.near", L, dev_buy=5 * NEAR)                          # two of three: no dev buy part
    db.created("fresh2.near", L - 2 * MIN)
    db.fund("rich.near", "fresh2.near", 41, L - 2 * MIN)
    check(result(db.card("y.nearlytrade.near"), "fingerprint"), "creator account 2 min old, got 41.0 NEAR 2 min before launch")


@pytest.mark.core
def test_funded_by_dumper_short(db):
    L = T0
    db.launch("early.nearlytrade.near", "dumper.near", L - 20 * MIN)
    db.snap("early.nearlytrade.near", 5, 10000, L - 20 * MIN)
    db.launch("x.nearlytrade.near", "new.near", L)
    db.fund("dumper.near", "new.near", 41, L - 5 * MIN, tx=FUNDING_TX)
    check(result(db.card("x.nearlytrade.near"), "funded_by_dumper"), "funded 41.0 NEAR by a past dumper (early: dev sold 100% in 5 min)")


@pytest.mark.core
def test_serial_launcher_prior_fast_dump_and_name_copy_short(db):
    L = T0
    for i in range(5):
        db.launch(f"p{i}.nearlytrade.near", "spam.near", L - (i + 1) * 3 * MIN)
    db.launch("x.nearlytrade.near", "spam.near", L)
    check(result(db.card("x.nearlytrade.near"), "serial_launcher"), "5 other launches by the creator in 24 h")
    db.launch("e1.nearlytrade.near", "dev.near", L - HOUR)
    db.snap("e1.nearlytrade.near", 5, 9000, L - HOUR)
    db.launch("d.nearlytrade.near", "dev.near", L)
    check(result(db.card("d.nearlytrade.near"), "prior_fast_dump"), "creator's earlier launch e1: dev sold 90% in 5 min")
    db.launch("panda.nearlytrade.near", "someone.near", L - 2 * DAY)
    db.launch("panda-2.nearlytrade.near", "copycat.near", L)
    check(result(db.card("panda-2.nearlytrade.near"), "name_copy"), "name 'panda' used 48.0 h earlier by another creator")
    db.launch("nearloween.v2-launchpad.justhoot.near", "heanko.near", L - 10 * MIN, launchpad="justhoot_v2")
    db.launch("nearloween.nearlytrade.near", "heanko.near", L)
    check(result(db.card("nearloween.nearlytrade.near"), "name_copy"), "same creator used name 'nearloween' on another launchpad 10 min earlier")


@pytest.mark.core
def test_creator_tax_short_states_the_tax_without_the_trade_share_mistake(db):
    L = T0
    db.launch("t.nearlytrade.near", "d.near", L, height=77, extra={"id": 9})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L, height=77,
            extra={"contract": "nearlytrade.near", "data": {"id": 9, "creator_bps": 200, "buy_bps": 100, "sell_bps": 100}})
    check(result(db.card("t.nearlytrade.near"), "creator_tax"), "the creator gets 2% of the trade tax")          # platform / burn not in the options: no rate per trade
    db.launch("u.nearlytrade.near", "d.near", L + MIN, height=78, extra={"id": 11})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L + MIN, height=78,
            extra={"contract": "nearlytrade.near", "data": {"id": 11, "creator_bps": 5000, "buy_bps": 500, "sell_bps": 500, "platform_bps": 4000, "burn_bps": 0}})
    check(result(db.card("u.nearlytrade.near"), "creator_tax"), "tax 5% buy / 5% sell, the creator gets 30% of it")


@pytest.mark.core
def test_an_unfired_signal_has_no_short_and_the_card_notes_have_one(db):
    db.launch("x.nearlytrade.near", "calm.near", T0)
    c = db.card("x.nearlytrade.near")
    for r in c["signals"]["results"]:
        assert not r["fired"] and "short" not in r
    db.launch("anon.nearlytrade.near", None, T0 + MIN, owner=None)
    c2 = db.card("anon.nearlytrade.near")
    assert c2["signals"]["notes_short"][0] == "creator not known at launch" and len(c2["signals"]["notes_short"]) == len(c2["signals"]["notes"])


@pytest.mark.core
def test_the_config_hash_is_not_touched_by_the_short_texts():
    assert C.config_hash(C.effective(None, "v0")) == "2cb6d1e9c0c3"
    assert C.config_hash(C.effective(None, "v1")) == "6fedfdb50583"
    assert C.config_hash(C.effective(None, "v2")) == "3d68b94c6371"


# ---------------------------------------------------------------- the rug label

@pytest.mark.core
def test_rug_label_short_clauses(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    lab = rules.rug_labels(rows(s))
    assert lab["rug_fast"]["short"] == lab["rug"]["short"] and re.fullmatch(r"insiders sold \d+ % in \d+(\.\d)? s", lab["rug"]["short"]), lab["rug"]
    assert "short" not in lab["rug_late"]                                                         # «already rug_fast» needs none


@pytest.mark.core
def test_no_rug_short_names_the_failed_condition_with_its_number(tmp_path):
    extra = (sell(DEV, 840, 2 * NEAR, 2 * 3600, "r1") + buy("big.near", 100, 5 * NEAR // 10, 5 * 3600, "r2"))
    lab = rules.rug_labels(rows(world(extra, tmp_path=tmp_path)))
    assert lab["rug"]["value"] is False and re.fullmatch(r"price held at \d+ % of peak", lab["rug"]["short"]), lab["rug"]
    assert lab["rug_late"]["short"] == lab["rug"]["short"]


@pytest.mark.core
def test_undefined_short_is_the_label_reason():
    from rugyomi.rules import rug as R
    assert R._label(None, R.PRICE_MISSING_REASON, None, R.PRICE_MISSING_REASON, undefined=True)["short"] == "price missing at T+24 h"
    assert R._after(35) == "35 s" and R._after(300) == "5 min" and R._after(7300) == "2 h"
