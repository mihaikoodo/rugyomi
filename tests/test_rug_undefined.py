"""RGY-024 step 2 (Q-012) narrowed by RGY-025 (Q-016): where the insiders' sold share does not exist AND they sold something, the rug label is
UNDEFINED; if they sold nothing there was nothing to dump = a plain "no"."""
import json

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi.rules.rug import LABEL_VERSION, UNDEFINED_REASON, rug_labels

YOCTO = 10**24
LAUNCH = 10**18


def snap(minute, bps, pnl, buyers=3, peak="100", last="100", acquired=1, sold=0):
    return {"minute": minute, "at_ts_ns": LAUNCH + minute * 60 * 10**9, "error": None, "metric_version": 8, "insiders_acquired": acquired, "insiders_sold": str(sold),
            "insiders_sold_share_bps": bps, "buyers_pnl": str(int(pnl * YOCTO)), "buyers_count": buyers, "price_peak": peak,
            "price_last": last, "insiders_count": 1, "insiders_pnl": "0", "insiders_evidence": None}


def labels(*snaps):
    return rug_labels(list(snaps))


@pytest.mark.core
def test_share_unknown_and_buyers_lost_is_undefined_not_no():
    r = labels(snap(60, None, -5, acquired=0, sold=7), snap(1440, None, -5, peak="100", last="50", acquired=0, sold=7))
    assert r["rug"]["value"] is None and r["rug"]["undefined"] and r["rug"]["reason"] == UNDEFINED_REASON
    assert r["rug_fast"]["undefined"] and r["severity_near"] is None


@pytest.mark.core
def test_it_is_still_pending_until_the_late_snapshot_exists():
    r = labels(snap(60, None, -5, acquired=0, sold=7))
    assert r["rug"]["value"] is None and not r["rug"].get("undefined") and r["rug"]["reason"] == "pending"


@pytest.mark.core
def test_unknown_share_but_nobody_hurt_or_nobody_bought_is_a_plain_no():
    assert labels(snap(60, None, 1, acquired=0), snap(1440, None, 1, acquired=0))["rug"]["value"] is False
    assert labels(snap(60, None, 0, buyers=0, acquired=0), snap(1440, None, 0, buyers=0, acquired=0))["rug"]["value"] is False
    r = labels(snap(60, None, 1, acquired=0), snap(1440, None, -5, acquired=0, peak="100", last="100"))
    assert r["rug"]["value"] is False                    # the price held: nobody would call it a rug whatever the share


@pytest.mark.core
def test_defined_shares_are_labelled_exactly_as_before():
    assert labels(snap(60, 9000, -5), snap(1440, 9000, -5, last="50"))["rug"]["value"] is True            # rug_fast
    r = labels(snap(60, 2000, -5), snap(1440, 2000, -5, last="50"))
    assert r["rug"]["value"] is False and r["rug_fast"]["reason"].startswith("insiders had sold 20.0 % of what they acquired, below the 80 % threshold")     # a real "no"
    assert not r["rug"].get("undefined")


@pytest.mark.core
def test_a_defined_late_rug_wins_over_an_undefined_fast_label():
    r = labels(snap(60, None, -5, acquired=0, sold=7), snap(1440, 9000, -5, peak="100", last="5"))
    assert r["rug"]["value"] is True and r["rug"]["reason"] == "rug_late" and r["severity_near"] == pytest.approx(5.0)


@pytest.mark.core
def test_stored_row_marks_final_and_the_label_version(tmp_path):
    from rugyomi.rules.config import load_config
    from rugyomi.rules.stored import outcome_row, rebuild_outcomes
    from rugyomi.store import open_store
    s = open_store(tmp_path / "u.db")
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, height, ts_ns, detected_at) VALUES('t','nearly','d.near','d.near',1,2,'x')")
    for sn in (snap(60, None, -5, acquired=0, sold=7), snap(1440, None, -5, acquired=0, last="50", sold=7)):
        s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, insiders_acquired, insiders_sold, insiders_sold_share_bps, "
                      "buyers_pnl, buyers_count, price_peak, price_last, insiders_count, insiders_pnl) VALUES('t',?,?,8,'x',?,?,?,?,?,?,?,1,'0')",
                      (sn["minute"], sn["at_ts_ns"], sn["insiders_acquired"], sn["insiders_sold"], sn["insiders_sold_share_bps"], sn["buyers_pnl"], sn["buyers_count"],
                       sn["price_peak"], sn["price_last"]))
    out = rebuild_outcomes(s, cfg=load_config())
    assert out["undefined"] == 1 and out["no_rug"] == 0 and out["pending"] == 0
    row = s.con.execute("SELECT rug, final FROM launch_outcomes WHERE token = 't'").fetchone()
    assert tuple(row) == (None, 1)
    o = outcome_row(s, "t")
    assert o["rug"]["undefined"] and o["label_version"] == LABEL_VERSION == 5 and o["final"] is True


@pytest.mark.core
def test_display_section_is_not_part_of_the_config_hash_and_defaults_to_no_chance():
    from rugyomi.rules import default_config, note_for, rug_chance_enabled
    from rugyomi.rules.config import config_hash
    c = default_config("v0")
    assert c["display"]["rug_chance"] is False and default_config("v1")["display"]["rug_chance"] is False
    h = config_hash(c)
    c["display"]["rug_chance"] = True
    c["display"]["note"] = "other"
    assert config_hash(c) == h                                  # a display switch never changes the hash of the issued numbers
    assert rug_chance_enabled() is False
    assert note_for("0.1").startswith("v0") and note_for("1.0").startswith("v1") and "not a probability" in note_for("1.0")


@pytest.mark.core
def test_unknown_share_and_the_insiders_sold_nothing_is_no_rug_even_if_the_buyers_lost():
    """RGY-025 (Q-016): nothing sold = nothing dumped; the buyers' loss is ordinary fading, not a rug and not undefined."""
    r = labels(snap(60, None, -5, acquired=0, sold=0), snap(1440, None, -5, acquired=0, sold=0, last="50"))
    assert r["rug"]["value"] is False and not r["rug"].get("undefined") and r["rug_fast"]["reason"] == "insiders' sold share unknown and the insiders sold nothing"
    assert r["rug_late"]["reason"] == "insiders' sold share unknown and the insiders sold nothing by T+24 h" and r["severity_near"] is None


@pytest.mark.core
def test_insiders_sold_is_read_at_the_snapshot_each_label_looks_at():
    # nothing sold by T+60 (rug_fast: a plain no), something sold by T+24 h (rug_late: undefined) -> the whole label is undefined
    r = labels(snap(60, None, -5, acquired=0, sold=0), snap(1440, None, -5, acquired=0, sold=3, last="10"))
    assert r["rug_fast"]["value"] is False and r["rug_late"]["undefined"] and r["rug"]["undefined"]
    # sold at T+60 but nothing more: the T+24 h snapshot is cumulative, so it stays undefined
    r = labels(snap(60, None, -5, acquired=0, sold=3), snap(1440, None, -5, acquired=0, sold=3, last="10"))
    assert r["rug"]["undefined"]
    # sold, but the price held: no
    assert labels(snap(60, None, 1, acquired=0, sold=3), snap(1440, None, -5, acquired=0, sold=3, last="100"))["rug"]["value"] is False


@pytest.mark.core
def test_a_known_share_does_not_depend_on_the_sold_field_and_the_evidence_names_it():
    r = labels(snap(60, 9000, -5, sold=0), snap(1440, 9000, -5, last="50", sold=0))
    assert r["rug"]["value"] is True                                    # unchanged: the share decides when it exists
    r = labels(snap(60, None, -5, acquired=0, sold=7), snap(1440, None, -5, acquired=0, sold=7, last="50"))
    assert r["rug"]["evidence"]["insiders_sold"] == "7"
    assert labels(snap(60, None, -5, acquired=0), snap(1440, None, -5, acquired=0, last="50"))["rug"]["value"] is False
    old = snap(60, None, -5, acquired=0); old.pop("insiders_sold")       # a row without the field counts as nothing sold
    assert labels(old, snap(1440, None, -5, acquired=0, last="50"))["rug"]["value"] is False


# ---------------------------------------------------------------- RGY-026 (Q-017): every "no" names the first failed condition
@pytest.mark.core
def test_each_no_of_rug_fast_names_the_first_failed_condition_with_its_value():
    cases = [
        (snap(60, 9000, -5, buyers=0), "no outside buyers"),
        (snap(60, 2000, -5), "insiders had sold 20.0 % of what they acquired, below the 80 % threshold"),
        (snap(60, None, -5, acquired=0, sold=0), "insiders' sold share unknown and the insiders sold nothing"),
        (snap(60, 9000, 3), "outside buyers' net result +3.00 NEAR, short of the 0.1 NEAR loss a rug needs"),
        (snap(60, None, 3, acquired=0, sold=7), "outside buyers' net result +3.00 NEAR, short of the 0.1 NEAR loss a rug needs"),      # share unknown, sold, buyers not hurt
    ]
    for sn, reason in cases:
        r = labels(sn)["rug_fast"]
        assert r["value"] is False and not r.get("undefined") and r["reason"] == reason, (reason, r["reason"])
    r = labels(snap(60, 9000, -0.05))["rug_fast"]                        # lost 0.05 NEAR < the 0.1 line: the loss is named
    assert r["value"] is False and r["reason"] == "outside buyers' net result -0.05 NEAR, short of the 0.1 NEAR loss a rug needs"


@pytest.mark.core
def test_each_no_of_rug_late_names_the_first_failed_condition_with_its_value():
    fast_no = snap(60, 2000, -5)                                           # rug_fast: no
    cases = [
        (snap(1440, 9000, -5, buyers=0), "no outside buyers"),
        (snap(1440, 2000, -5, last="5"), "insiders had sold 20.0 % of what they acquired by T+24 h, below the 80 % threshold"),
        (snap(1440, None, -5, acquired=0, sold=0, last="5"), "insiders' sold share unknown and the insiders sold nothing by T+24 h"),
        (snap(1440, 9000, -5, last="50"), "the price held at 50 % of its peak (a rug needs 10 % or less)"),
        (snap(1440, 9000, 3, last="5"), "outside buyers' net result +3.00 NEAR by T+24 h, short of the 0.1 NEAR loss a rug needs"),
    ]
    for sn, reason in cases:
        r = labels(fast_no, sn)["rug_late"]
        assert r["value"] is False and not r.get("undefined") and r["reason"] == reason, (reason, r["reason"])


@pytest.mark.core
def test_ghost_like_case_share_unknown_sold_price_held_is_no_rug_with_the_price_reason():
    """`ghost-0523c3`: nothing sold at T+60, something sold by T+24 h, the buyers lost, the price held (RGY-026)."""
    r = labels(snap(60, None, -5, acquired=0, sold=0), snap(1440, None, -5, acquired=0, sold=7, last="50"))
    assert r["rug"]["value"] is False and not r["rug"].get("undefined")
    assert r["rug_late"]["reason"] == "the price held at 50 % of its peak (a rug needs 10 % or less)"
    assert r["rug_fast"]["reason"] == "insiders' sold share unknown and the insiders sold nothing"


@pytest.mark.core
def test_a_missing_price_is_a_visible_undefined_never_a_silent_no():
    from rugyomi.rules import rug as R
    fast_no = snap(60, 2000, -5)
    for sn in (snap(1440, 9000, -5, peak=None, last=None), snap(1440, 9000, -5, peak="100", last=None), snap(1440, 9000, -5, peak="0", last="0")):
        r = labels(fast_no, sn)
        late = r["rug_late"]
        assert late["value"] is None and late["undefined"] and late["undefined_kind"] == "price" and late["reason"] == R.PRICE_MISSING_REASON
        assert late["evidence"]["price_vs_peak"] is None
        assert r["rug"]["value"] is None and r["rug"]["undefined"] and r["rug"]["undefined_kind"] == "price" and r["rug"]["reason"] == R.PRICE_MISSING_REASON
    # both inputs missing: the share reason, both kinds named
    r = labels(snap(60, 2000, -5), snap(1440, None, -5, acquired=0, sold=7, peak=None, last=None))["rug_late"]
    assert r["undefined"] and r["undefined_kind"] == "share+price" and r["reason"] == R.UNDEFINED_REASON
    # a missing price does not hide another failed condition: the buyers were not hurt -> a plain no with that reason
    r = labels(fast_no, snap(1440, 9000, 3, peak=None, last=None))["rug_late"]
    assert r["value"] is False and r["reason"].startswith("outside buyers' net result +3.00 NEAR")
    # ... and a share below the threshold is the reason when it fails first
    r = labels(fast_no, snap(1440, 2000, -5, peak=None, last=None))["rug_late"]
    assert r["value"] is False and r["reason"].startswith("insiders had sold 20.0 %")


@pytest.mark.core
def test_the_rebuild_counts_the_launches_with_a_missing_price(tmp_path):
    from rugyomi.rules.config import load_config
    from rugyomi.rules.stored import rebuild_outcomes
    from rugyomi.store import open_store
    s = open_store(tmp_path / "m.db")
    for tok, peak, last in (("a", None, None), ("b", "100", "5"), ("c", "100", None)):
        s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, height, ts_ns, detected_at) VALUES(?,'nearly','d.near','d.near',1,2,'x')", (tok,))
        for sn in (snap(60, 2000, -5), snap(1440, 9000, -5, peak=peak, last=last)):
            s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, insiders_acquired, insiders_sold, insiders_sold_share_bps, "
                          "buyers_pnl, buyers_count, price_peak, price_last, insiders_count, insiders_pnl) VALUES(?,?,?,8,'x',?,?,?,?,?,?,?,1,'0')",
                          (tok, sn["minute"], sn["at_ts_ns"], sn["insiders_acquired"], sn["insiders_sold"], sn["insiders_sold_share_bps"], sn["buyers_pnl"],
                           sn["buyers_count"], sn["price_peak"], sn["price_last"]))
    out = rebuild_outcomes(s, cfg=load_config())
    assert out["price_missing"] == 2 and out["price_missing_undefined"] == 2 and out["undefined"] == 2 and out["rug_late"] == 1


@pytest.mark.core
def test_the_rug_level_undefined_names_every_missing_input_of_both_branches():
    r = labels(snap(60, None, -5, acquired=0, sold=7), snap(1440, 9000, -5, peak=None, last=None))
    assert r["rug_fast"]["undefined_kind"] == "share" and r["rug_late"]["undefined_kind"] == "price"
    assert r["rug"]["undefined"] and r["rug"]["undefined_kind"] == "share+price"
