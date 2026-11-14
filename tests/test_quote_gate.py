"""RGY-066 (Q-046, answer A): a launch whose pool is not paired with wNEAR has no NEAR amounts. Its snapshots keep no NEAR columns, its rug labels are undefined
(label version 5, `undefined_kind` "quote"), the pages print no NEAR figure for it; launches paired with wNEAR are unchanged.

World: the REFRAX / ISLAND / COIN-2 replay (all three are rugs, paired with wrap.near). COIN-2's pool id is changed to a PRL pair: the facts stay the same, only the quote differs."""
import json
import shutil

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import quote as Q  # noqa: E402
from rugyomi import quotegate as QG  # noqa: E402
from rugyomi import snapshots as S  # noqa: E402
from rugyomi.rules import stored as O  # noqa: E402
from rugyomi.rules.rug import LABEL_VERSION, QUOTE_REASON, rug_labels  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp, queries  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402

ISLAND, COIN, REFRAX = "island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near"
PRL = "prl.omft.near"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    p = tmp_path_factory.mktemp("qg") / "base.db"
    refrax_world_db(p)
    return p


@pytest.fixture
def db(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    yield s
    s.close()


def _unpair(s, token=COIN, quote=PRL):
    """The pool account is renamed (the swap facts keep pointing at it): the same trades, another quote token in the pool id."""
    old = s.con.execute("SELECT pool FROM launches WHERE token = ?", (token,)).fetchone()[0]
    new = f"{token}|{quote}|10000"
    s.con.execute("UPDATE accounts SET name = ? WHERE name = ?", (new, old))
    s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (new, token))


def _snap(s, token):
    cols = ("minute",) + S.COLUMNS
    return {r[0]: dict(zip(cols, r)) for r in s.con.execute(f"SELECT {','.join(cols)} FROM launch_snapshots WHERE token = ?", (token,))}


# ---------------------------------------------------------------- the pool id

def test_the_quote_token_is_the_other_side_of_the_pool_id():
    assert Q.quote_token("mop.nearlytrade.near", "mop.nearlytrade.near|prl.omft.near|10000") == "prl.omft.near"
    assert Q.quote_token("mop.nearlytrade.near", "prl.omft.near|mop.nearlytrade.near|10000") == "prl.omft.near"
    assert Q.not_near_quote("a.near", "a.near|wrap.near|10000") is None
    assert Q.not_near_quote("a.near", "a.near|usdt.tether-token.near|10000") == "usdt.tether-token.near"
    for bad in (None, "", "a.near", "x|y|10000", "a.near|b.near|c.near|10000"):                      # not readable: the launch stays as it was
        assert Q.not_near_quote("a.near", bad) is None


# ---------------------------------------------------------------- snapshots and outcomes

def test_new_snapshots_of_a_non_near_pool_have_no_near_amounts_and_keep_the_rest(db):
    before = _snap(db, COIN)
    assert all(before[60][c] is not None for c in S.NEAR_COLUMNS)
    _unpair(db)
    S.rebuild_snapshots(db, token=COIN)
    after = _snap(db, COIN)
    for m, row in after.items():
        assert all(row[c] is None for c in S.NEAR_COLUMNS), (m, row)
        for c in S.COLUMNS:                                               # everything that does not depend on the quote's units is as before
            if c not in S.NEAR_COLUMNS and c not in ("insiders_evidence",):
                assert row[c] == before[m][c], (m, c)


def test_the_outcome_of_a_non_near_pool_is_undefined_with_the_quote_reason(db):
    _unpair(db)
    S.rebuild_snapshots(db, token=COIN)
    O.rebuild_outcomes(db)
    o = O.outcome_row(db, COIN)
    assert o["rug"]["value"] is None and o["rug"]["undefined"] and o["rug"]["undefined_kind"] == "quote"
    assert o["rug_fast"]["value"] is None and o["rug_late"]["value"] is None
    assert o["rug"]["evidence"] == {"quote_token": PRL} and PRL in o["rug"]["reason"] and QUOTE_REASON in o["rug"]["reason"]
    assert o["label_version"] == LABEL_VERSION == 5
    row = db.con.execute("SELECT rug_fast, rug_late, rug, severity_near, insiders_pnl, buyers_loss FROM launch_outcomes WHERE token = ?", (COIN,)).fetchone()
    assert row == (None, None, None, None, None, None)
    for tok in (ISLAND, REFRAX):                                          # paired with wNEAR: unchanged
        assert O.outcome_row(db, tok)["rug"]["value"] is True


def test_rug_labels_with_a_quote_never_read_the_amounts():
    snaps = [{"minute": 60, "at_ts_ns": 1, "buyers_pnl": "-999999999999999999999999999", "insiders_acquired": "1", "insiders_sold": "1",
              "insiders_sold_share_bps": 10000, "buyers_count": 3, "error": None}]
    lab = rug_labels(snaps, quote=PRL)
    assert lab["rug"]["value"] is None and lab["severity_near"] is None
    assert all(lab[k]["undefined_kind"] == "quote" for k in ("rug_fast", "rug_late", "rug"))
    assert rug_labels(snaps)["rug_fast"]["value"] is True                 # the same rows without the quote: the loss test decides


# ---------------------------------------------------------------- the one-off application to the stored rows

def test_apply_quote_gate_dry_run_changes_nothing_and_apply_clears_the_stored_amounts(db):
    _unpair(db)
    dry = QG.apply(db)
    assert dry["dry_run"] and dry["before"]["launches"] == 1 and dry["before"]["snapshot_rows_with_near"] == dry["before"]["snapshot_rows"] > 0
    assert dry["before"]["outcomes_affected"]["rug=1"] == 1 and "after" not in dry
    assert _snap(db, COIN)[60]["buyers_pnl"] is not None                # dry run: nothing written
    keep = _snap(db, ISLAND)
    res = QG.apply(db, dry_run=False)
    assert res["after"]["snapshot_rows_with_near"] == 0 and res["after"]["outcomes_affected"]["rug NULL"] == 1 and "rug=1" not in res["after"]["outcomes_affected"]
    assert res["snapshot_rows_updated"] == dry["before"]["snapshot_rows"]
    assert res["after"]["outcomes_all"]["rug=1"] == 2                     # ISLAND and REFRAX are still rugs
    assert _snap(db, ISLAND) == keep                                      # the wNEAR launches are not touched
    again = QG.apply(db, dry_run=False)                                   # idempotent
    assert again["after"] == res["after"]


# ---------------------------------------------------------------- the pages

def test_pages_for_a_non_near_pool_print_no_near_amounts(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    _unpair(s)
    QG.apply(s, dry_run=False)
    assert queries.launch(s.con, COIN)["dev_buy"] is None and queries.launch(s.con, COIN)["quote_token"] == PRL
    assert queries.launch(s.con, ISLAND)["dev_buy"] is not None and queries.launch(s.con, ISLAND)["quote_token"] is None
    s.close()
    with TestClient(webapp.create_app(p)) as c:
        r = c.get(f"/launch/{COIN}")
        assert r.status_code == 200 and "undefined" in r.text.lower()
        assert "-2.43 NEAR" not in r.text and "2.43 NEAR" not in r.text   # the old severity of COIN-2 (2.4338) is gone from the page
        assert c.get(f"/launch/{ISLAND}").status_code == 200
        assert c.get("/accuracy").status_code == 200 and c.get("/rankings").status_code == 200


def test_the_live_state_masks_the_near_volumes_of_a_non_near_pool():
    from rugyomi import livestate as LS
    parts = {"volume": {"value": 40, "buys_near": "5", "sells_near": "7", "window_min": 10}}
    act = [{"minutes": 5, "buys": 2, "sells": 1, "buys_near": "5", "sells_near": "7"}]
    LS._mask_near({"token": "a.near", "pool": "a.near|wrap.near|10000"}, parts, act)
    assert parts["volume"]["buys_near"] == "5" and act[0]["sells_near"] == "7"
    LS._mask_near({"token": "a.near", "pool": f"a.near|{PRL}|10000"}, parts, act)
    assert parts["volume"]["buys_near"] is None and parts["volume"]["sells_near"] is None and parts["volume"]["value"] == 40
    assert act[0]["buys_near"] is None and act[0]["sells_near"] is None and act[0]["buys"] == 2


def test_the_methodology_says_what_an_unpriced_quote_means():
    from rugyomi.rules import methodology
    text = json.dumps(methodology(), ensure_ascii=False)
    assert "another token than wNEAR" in text and f"Label version {LABEL_VERSION}" in text
