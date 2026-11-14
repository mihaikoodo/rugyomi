"""RGY-073: the trader pages for a launch whose pool is not paired with wNEAR. The prices RGY-070 collected now reach the early buyers (`launch_buyers`), the live numbers and
the dev buy: ONE conversion (`snapshots.to_near` over `quote_price.convert_swaps`), all or nothing per launch, never a guessed price.

World: the REFRAX / ISLAND / COIN-2 replay with COIN-2's pool turned into a PRL pool (raw PRL = yocto NEAR / K) as in test_quote_price.py, so the converted NEAR amounts can be
compared with the same trades in the wNEAR pool (the difference is only the integer truncation of the raw units, below K per trade)."""
import shutil

import pytest

import rugyomi_testkit  # noqa: F401

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import livestate as LS  # noqa: E402
from rugyomi import quote_price as QP  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import queries  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402
from test_quote_price import COIN, ISLAND, K, MIN, PRL, REFRAX, add_price_swaps, unpair  # noqa: E402

CFG = C.load_config(profile="v1")
B = "976b2ad5b28f7665d2890ee05b331a8996e37b548f7072ed9d4bbf0069493fe8"          # bought ISLAND, COIN-2 and REFRAX


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    p = tmp_path_factory.mktemp("qt") / "base.db"
    refrax_world_db(p)
    s = open_store(p)
    BU.build_launch_buyers(s, CFG)                                              # the buyers of COIN-2 while its pool is still paired with wNEAR: the NEAR amounts to compare with
    s.close()
    return p


@pytest.fixture
def db(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    yield s
    s.close()


def rows_of(s, token):
    return {r["buyer"]: r for r in BU.early_buyers(s.con, token, 50)}


def prices(s, token=COIN, every=5, upto=24 * 60):
    ts0 = s.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (token,)).fetchone()[0]
    add_price_swaps(s, [ts0 - 5 * MIN + i * every * MIN for i in range(0, upto // every + 3)])
    return ts0


@pytest.mark.core
def test_a_priced_quote_launch_shows_the_same_near_amounts_as_the_wnear_pool(db):
    want = rows_of(db, COIN)
    unpair(db)
    prices(db)
    out = BU.build_launch_buyers(db, CFG, only=[COIN])
    assert out["computed"] == 1 and out["quote_converted"] == 1 and out["quote_unpriced"] == 0
    got = rows_of(db, COIN)
    assert got.keys() == want.keys() and got
    for who, r in got.items():
        w = want[who]
        assert r["quote_token"] == PRL and r["amount_note"] == f"converted from {PRL} at the swap-time price"
        for col in ("spent", "received", "pnl"):
            assert r[col] is not None and abs(int(r[col]) - int(w[col])) <= K * (r["buys"] + 2), (who, col)        # only the truncation of the raw units
        assert (r["bought"], r["sold"], r["first_buy_ns"], r["first_buy_tx"]) == (w["bought"], w["sold"], w["first_buy_ns"], w["first_buy_tx"])
    t = BU.account_trades(db.con, B)
    row = next(r for r in t["rows"] if r["token"] == COIN)
    assert row["spent"] is not None and row["quote_token"] == PRL and "converted" in row["amount_note"]
    assert t["summary"]["other_quote_rows"] == 0 and t["summary"]["pnl_launches"] == t["summary"]["launches"] == 3        # a converted row is in the NEAR sum


@pytest.mark.core
def test_an_unpriced_quote_launch_stays_na_and_never_adds_raw_units(db):
    unpair(db)
    out = BU.build_launch_buyers(db, CFG, only=[COIN])                         # no price swap is stored
    assert out["quote_unpriced"] == 1 and out["quote_converted"] == 0
    assert db.con.execute("SELECT DISTINCT spent, received, pnl FROM launch_buyers WHERE token = ?", (COIN,)).fetchall() == [(BU.NO_NEAR, BU.NO_NEAR, BU.NO_NEAR)]
    rows = rows_of(db, COIN)
    assert rows and all(r["spent"] is None and r["pnl"] is None and r["amount_note"] == f"n/a (quote: {PRL})" for r in rows.values())
    assert all(r["bought"] and r["first_buy_ns"] for r in rows.values())        # the token amounts and times stay
    t = BU.account_trades(db.con, B)
    assert t["summary"]["other_quote_rows"] == 1 and t["summary"]["pnl_launches"] == 2
    exact = sum(int(r[0]) for r in db.con.execute("SELECT pnl FROM launch_buyers WHERE buyer = ? AND token != ?", (B, COIN)))
    assert t["summary"]["pnl_t60"] == str(exact)


@pytest.mark.core
def test_one_trade_without_a_price_keeps_the_whole_launch_na(db):
    unpair(db)
    ts0 = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (COIN,)).fetchone()[0]
    first_buy = db.con.execute("SELECT MIN(first_buy_ns) FROM launch_buyers WHERE token = ?", (COIN,)).fetchone()[0]
    add_price_swaps(db, [first_buy + 2 * MIN + i * 5 * MIN for i in range(0, 40)])        # the first price comes after the first trade
    BU.build_launch_buyers(db, CFG, only=[COIN])
    assert all(r["spent"] is None for r in rows_of(db, COIN).values())
    assert ts0 < first_buy


@pytest.mark.core
def test_rows_of_the_earlier_hash_are_never_shown_as_near_and_only_quote_launches_are_recomputed(db):
    unpair(db)
    prices(db)                                                                  # prices exist, but the stored rows were computed before the conversion existed
    assert all(r["spent"] is None and r["amount_note"].startswith("n/a") for r in rows_of(db, COIN).values())
    todo = [t for t, _, _ in BU.pending(db, CFG)]
    assert todo == [COIN]                                                       # ISLAND, REFRAX and the history launches keep their rows
    BU.build_launch_buyers(db, CFG)
    assert BU.pending(db, CFG) == []
    assert all(r["spent"] is not None for r in rows_of(db, COIN).values())
    assert all("amount_note" not in r for r in rows_of(db, ISLAND).values())


@pytest.mark.core
def test_the_dev_buy_is_converted_at_the_launch_time_or_stays_empty(db):
    unpair(db)
    ts0 = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (COIN,)).fetchone()[0]
    db.con.execute("UPDATE launches SET dev_buy = ? WHERE token = ?", (str(5_000_000), COIN))                # raw PRL
    db.con.commit()
    d = queries.launch(db.con, COIN)
    assert d["quote_token"] == PRL and d["dev_buy"] is None and d["dev_buy_note"] is None                    # no price yet
    assert next(r for r in queries.feed(db.con, 50) if r["token"] == COIN)["dev_buy"] is None
    prices(db)
    d = queries.launch(db.con, COIN)
    assert d["dev_buy"] == str(5_000_000 * K) and "converted from" in d["dev_buy_note"]
    assert next(r for r in queries.feed(db.con, 50) if r["token"] == COIN)["dev_buy"] == str(5_000_000 * K)
    plain = queries.launch(db.con, ISLAND)
    assert plain["quote_token"] is None and plain["dev_buy_note"] is None
    got = QP.near_at(db.con, PRL, 5_000_000, ts0)
    assert got and got["near"] == str(5_000_000 * K) and got["tx"] is not None and QP.near_at(db.con, PRL, 5_000_000, ts0 - 3 * 3600 * 10**9) is None


@pytest.mark.core
def test_the_live_numbers_have_near_amounts_when_the_trades_are_converted(db):
    cfg = C.load_config()
    launch = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (COIN,)).fetchone()[0]
    now = launch + 60 * MIN
    want = LS.compute_token(db, COIN, now, cfg)[0]
    assert want["facts"]["activity"][0]["buys_near"] is not None                 # the wNEAR pool: amounts shown
    unpair(db)
    masked = LS.compute_token(db, COIN, now, cfg)[0]
    assert masked["facts"]["activity"][0]["buys_near"] is None and masked["parts"]["volume"]["buys_near"] is None          # no price: «—», as before
    prices(db)
    res = LS.compute_token(db, COIN, now, cfg)[0]
    assert res["facts"]["activity"][0]["buys_near"] is not None and res["parts"]["volume"]["buys_near"] is not None
    for a, b in zip(res["facts"]["activity"], want["facts"]["activity"]):
        assert abs(int(a["buys_near"]) - int(b["buys_near"])) <= K * (a["buys"] + 1) and a["buys"] == b["buys"] and a["sells"] == b["sells"]
    fixed = LS.compute_fixed(db, COIN, [5, 60], "live", cfg, LS.Context())
    assert fixed[60]["parts"]["volume"]["buys_near"] is not None
