"""RGY-070 stage 2: the read-only before / after (`quote_apply.plan`) and the rebuild (`quote_apply.apply`).

World: the REFRAX / ISLAND / COIN-2 replay with COIN-2's pool turned into a PRL pool (as in test_quote_price). Checked: plan writes nothing and says what the rebuild would
store; the rebuild by the existing paths stores exactly that; a history (`launch_only`) launch goes the other path (`backfill.write_snapshots`, rows of the live collector kept)
and gives the same numbers; the cards part recomputes the fingerprint signal with the dev buy converted and never touches a card."""
import json
import shutil
import sys
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_quote_price import COIN, K, MIN, PRL, add_price_swaps, snap, unpair  # noqa: E402

from rugyomi import quote_apply as QA  # noqa: E402
from rugyomi import snapshots as S  # noqa: E402
from rugyomi.rules import stored as O  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    p = tmp_path_factory.mktemp("qa") / "base.db"
    refrax_world_db(p)
    return p


@pytest.fixture
def db(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    yield s
    s.close()


def gated(s):
    """COIN-2 as the RGY-066 state the server is in now: PRL pool, NEAR amounts NULL, outcome undefined."""
    ts0 = unpair(s)
    S.rebuild_snapshots(s, token=COIN)
    O.rebuild_outcomes(s)
    return ts0


def priced(s, ts0):
    add_price_swaps(s, [ts0 - 5 * MIN + i * 10 * MIN for i in range(0, 24 * 6 + 3)])


def dump(s):
    return {t: s.con.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in ("launch_snapshots", "launch_outcomes", "cards", "facts", "api_facts", "launches")} \
        | {"values": [tuple(r) for r in s.con.execute(f"SELECT token, minute, {','.join(S.COLUMNS)} FROM launch_snapshots ORDER BY token, minute")]}


def test_category_names_the_four_states():
    f = {"rug": 1, "final": 1}
    assert QA.category(f) == "rug" and QA.category({"rug": 0, "final": 1}) == "no rug"
    assert QA.category({"rug": None, "final": 1}) == "undetermined" and QA.category({"rug": None, "final": 0}) == "pending" and QA.category(None) == "pending"


def test_without_prices_nothing_changes_and_the_reason_is_named(db):
    gated(db)
    r = QA.plan(db, only=[COIN])
    assert r["launches"] == 1 and r["before_affected"] == r["after_affected"] == {"pending": 1}
    assert r["transitions"] == {"pending -> pending": 1} and r["changed_tokens"] == []
    u = r["undetermined_after"][COIN]
    assert u["quote"] == PRL and not u["ok"] and u["price_points"] == 0 and u["missing"] == u["trades"] > 0 and not u["partial_unit"]
    assert r["undetermined_by_reason"] == {f"{PRL} | no price source (no swap of the pair in the window)": 1}


def test_with_prices_the_plan_gives_the_labels_back_writes_nothing_and_names_the_price_txs(db):
    ts0 = gated(db)
    priced(db, ts0)
    before = dump(db)
    r = QA.plan(db, only=[COIN])
    assert dump(db) == before                                                                          # read only: not one row changed
    assert r["before_affected"] == {"pending": 1} and r["after_affected"] == {"rug": 1}
    assert r["transitions"] == {"pending -> rug": 1} and r["changed_tokens"] == [COIN] and r["undetermined_after"] == {}
    assert r["after_all"]["rug"] == r["before_all"]["rug"] + 1 and r["after_all"]["pending"] == r["before_all"]["pending"] - 1
    ex = QA.pick_examples(r["examples_pool"])
    assert [e["token"] for e in ex] == [COIN] and ex[0]["before"] == "pending" and ex[0]["after"] == "rug"
    assert ex[0]["price_txs"] and ex[0]["launch_tx"] and ex[0]["trades"] > 0 and ex[0]["max_price_age_s"] is not None
    json.dumps(r, default=str)


def test_apply_stores_what_the_plan_said_by_the_existing_paths_and_is_idempotent(db):
    ts0 = gated(db)
    priced(db, ts0)
    planned = QA.plan(db, only=[COIN])
    cards_before = db.con.execute("SELECT * FROM cards ORDER BY token").fetchall()
    facts_before = db.con.execute("SELECT COUNT(*), TOTAL(rowid) FROM facts").fetchone()
    res = QA.apply(db, planned["changed_tokens"])
    assert res["launches"] == 1 and res["snapshots"]["ok"] >= 2 and res["outcomes"]["rows"] == 1
    o = db.con.execute("SELECT rug, final FROM launch_outcomes WHERE token = ?", (COIN,)).fetchone()
    assert QA.category(dict(zip(("rug", "final"), o))) == "rug"                                       # what plan said
    assert all(r["buyers_pnl"] is not None for r in snap(db, COIN).values() if r["n_swaps"])
    again = QA.plan(db, only=[COIN])                                                                  # nothing left to change
    assert again["before_affected"] == again["after_affected"] == {"rug": 1}
    state = dump(db)
    QA.apply(db, [COIN])
    assert {k: v for k, v in dump(db).items() if k not in ("launch_outcomes", "launch_snapshots")} == {k: v for k, v in state.items() if k not in ("launch_outcomes", "launch_snapshots")}
    assert db.con.execute("SELECT * FROM cards ORDER BY token").fetchall() == cards_before          # cards are never touched
    assert db.con.execute("SELECT COUNT(*), TOTAL(rowid) FROM facts").fetchone() == facts_before


def to_history(s, token):
    """COIN-2 as a launch_only launch: its facts copied to api_facts (scope = the token), its live snapshots dropped (the backfill path writes them)."""
    tok = s.lookup_account(token)
    pool = s.con.execute("SELECT pool FROM launches WHERE token = ?", (token,)).fetchone()[0]
    pid = s.lookup_account(pool)
    s.begin()
    s.con.execute(
        "INSERT INTO api_facts(scope, height, ord, seq, kind, ts_ns, tx, receipt, origin, a, b, token, amount, extra) "
        "SELECT ?, height, id, 0, kind, ts_ns, tx, receipt, origin, a, b, token, amount, extra FROM facts WHERE token = ? OR b = ?", (token, tok, pid))
    s.con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = ?", (token,))
    s.con.execute("DELETE FROM launch_snapshots WHERE token = ?", (token,))
    s.commit()


def test_a_history_launch_is_planned_and_rebuilt_from_its_api_rows_and_keeps_live_rows(db):
    ts0 = unpair(db)
    O.rebuild_outcomes(db)
    priced(db, ts0)
    full = {}
    S.rebuild_snapshots(db, token=COIN)
    full = {m: {c: r[c] for c in S.NEAR_COLUMNS} for m, r in snap(db, COIN).items()}                 # what the full-coverage path gives with the prices
    to_history(db, COIN)
    from rugyomi import backfill as BF
    db.begin()
    BF.write_snapshots(db, BF._launch_row(db, COIN))                                                    # the history rows as they exist today (priced: the test wants the plan to see them)
    db.con.execute("UPDATE launch_snapshots SET source = 'backfill' WHERE token = ?", (COIN,))
    db.con.execute(f"UPDATE launch_snapshots SET {', '.join(c + ' = NULL' for c in S.NEAR_COLUMNS)} WHERE token = ?", (COIN,))           # the RGY-066 state of the history rows
    db.con.execute("UPDATE launch_snapshots SET source = NULL WHERE token = ? AND minute = 5", (COIN,))                                  # one row of the live collector: kept
    db.commit()
    O.rebuild_outcomes(db)
    r = QA.plan(db, only=[COIN])
    assert r["history"] == 1 and r["full"] == 0 and r["transitions"] == {"undetermined -> rug": 1}
    res = QA.apply(db, [COIN])
    assert res["snapshots"]["kept_live"] == 1 and res["snapshots"]["ok"] == 4
    got = snap(db, COIN)
    for m in (15, 30, 60):                                                                              # the full path has no T+24 h row in the replay world (not due)
        assert {c: got[m][c] for c in S.NEAR_COLUMNS} == full[m], m                                    # the history path gives the numbers of the full path
    assert all(got[5][c] is None for c in S.NEAR_COLUMNS)                                              # the live row of minute 5 was not replaced
    assert QA.category(dict(zip(("rug", "final"), db.con.execute("SELECT rug, final FROM launch_outcomes WHERE token = ?", (COIN,)).fetchone()))) in ("rug", "undetermined")


def test_the_card_part_recomputes_the_fingerprint_with_the_dev_buy_in_near_and_never_touches_a_card(db):
    ts0 = gated(db)
    priced(db, ts0)
    card = db.con.execute("SELECT signals FROM cards WHERE token = ?", (COIN,)).fetchone()
    if card is None:
        pytest.skip("the replay world issued no card for the coin")
    launch = S._launch(db, COIN)
    launch["dev_buy"] = db.con.execute("SELECT dev_buy FROM launches WHERE token = ?", (COIN,)).fetchone()[0]
    before = db.con.execute("SELECT * FROM cards").fetchall()
    out = QA.card_impact(db, {COIN: launch}, {COIN: S.prices_for(db, launch)})
    assert out["cards"] == 1 and out["same"] + out["no_fingerprint"] + len(out["changed"]) + len(out["no_price"]) == 1
    assert db.con.execute("SELECT * FROM cards").fetchall() == before
    json.dumps(out, default=str)


def test_a_card_whose_fingerprint_changes_with_the_converted_dev_buy_is_listed_with_the_would_be_score(db):
    ts0 = gated(db)
    priced(db, ts0)
    raw = db.con.execute("SELECT signals, score FROM cards WHERE token = ?", (COIN,)).fetchone()
    sig = json.loads(raw[0])
    fp = next(r for r in sig["results"] if r["id"] == "fingerprint")
    # as issued: a fresh, funded creator; the dev buy read as 40 NEAR (4e25 raw PRL taken for yocto NEAR) is below 80 % of the 40 000 NEAR funding -> two of three, 25 points
    fp.update({"fired": True, "contribution": 25, "value": {"fresh_account": True, "funded": True, "dev_buy_vs_funding": False, "conditions_met": 2,
                                                              "funding_near": 40000.0, "dev_buy_near": 40.0}})
    db.begin()
    for trig in [r[0] for r in db.con.execute("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'cards'")]:
        db.con.execute(f"DROP TRIGGER {trig}")
    db.con.execute("UPDATE cards SET signals = ?, score = ? WHERE token = ?", (json.dumps(sig), raw[1] + 25, COIN))
    db.commit()
    launch = S._launch(db, COIN)
    launch["dev_buy"] = db.con.execute("SELECT dev_buy FROM launches WHERE token = ?", (COIN,)).fetchone()[0]
    out = QA.card_impact(db, {COIN: launch}, {COIN: S.prices_for(db, launch)})
    assert out["same"] == 0 and len(out["changed"]) == 1
    c = out["changed"][0]                                                                           # 4e25 raw x 1000 yocto = 40 000 NEAR >= 0.8 x 40 000: the third condition holds
    assert c["spends"] is True and c["issued_spends"] is False and c["conditions_met"] == 3 and c["points"] > c["issued_points"] == 25
    assert c["dev_buy_near"] == 40000.0 and c["score"] == raw[1] + 25 + (c["points"] - 25) and c["price_tx"]
    assert db.con.execute("SELECT score FROM cards WHERE token = ?", (COIN,)).fetchone()[0] == raw[1] + 25      # the card as issued is not changed by the part
    # no price at the launch time: named, not guessed
    none = QA.card_impact(db, {COIN: launch}, {COIN: QA.QP.Series(PRL, [])})
    assert none["changed"] == [] and none["no_price"][0]["why"] == "no price at the launch time"


def test_why_reads_the_trades_of_a_history_launch_from_its_api_rows_and_splits_the_missing_prices(db):
    ts0 = unpair(db)
    O.rebuild_outcomes(db)
    to_history(db, COIN)
    add_price_swaps(db, [ts0 + 3 * MIN])                                                               # one price, 3 min after the launch: the first trades come before it
    launch = S._launch(db, COIN)
    prices = S.prices_for(db, launch)
    w = QA._why(db, launch, PRL, prices, True)                                                         # history: the api rows (the live facts hold the same trades in this world, but must not be read)
    assert w["trades"] > 0 and not w["ok"] and w["price_points"] == 1 and w["missing"] == w["no_earlier_price"] + w["stale"] and w["no_earlier_price"] > 0
    db.begin()
    db.con.execute("DELETE FROM api_facts WHERE scope = ? AND kind = ?", (COIN, db.lookup_kind("dcl_swap")))
    db.commit()
    assert QA._why(db, launch, PRL, prices, True)["trades"] == 0                                       # nothing in the api rows -> nothing to convert, whatever the live facts say
    assert QA._why(db, launch, PRL, prices, False)["trades"] > 0
    far = QA.QP.Series(PRL, [QA.QP.Point(ts0 - 100 * MIN, QA.QP.Decimal(1), "p", "t")])               # a price far before the trades: stale, with its age
    w2 = QA._why(db, launch, PRL, far, False)
    assert w2["stale"] == w2["missing"] > 0 and w2["no_earlier_price"] == 0 and w2["max_stale_min"] >= 100
