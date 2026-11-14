"""RGY-075: the trader pages show the last known price of the quote token up to 24 h old (amounts with «≈» and the price age); outcomes, labels, cards and accuracy numbers keep the strict
15 minutes. ONE function reads a price (`quote_price.Series.at(ts, max_age_ns)` behind `convert_swaps` / `near_at`), both rules are two limits of it.

World: as in test_quote_trader.py (COIN-2's pool turned into a PRL pool); the only price of PRL is placed `hours` before the launch, so every trade of the launch has a price that is older than
15 min and (when hours <= 24) not older than 24 h."""
import shutil

import pytest

import rugyomi_testkit  # noqa: F401

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import livestate as LS  # noqa: E402
from rugyomi import quote_price as QP  # noqa: E402
from rugyomi import snapshots as S  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.rules.context import LaunchContext  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi.web import queries  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402
from test_quote_price import COIN, ISLAND, K, MIN, PRL, add_price_swaps, unpair  # noqa: E402

CFG = C.load_config(profile="v1")
ACCOUNT = "976b2ad5b28f7665d2890ee05b331a8996e37b548f7072ed9d4bbf0069493fe8"          # bought ISLAND, COIN-2 and REFRAX
HOUR = 60 * MIN


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    p = tmp_path_factory.mktemp("qd") / "base.db"
    refrax_world_db(p)
    s = open_store(p)
    BU.build_launch_buyers(s, CFG)
    s.close()
    return p


@pytest.fixture
def db(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    yield s
    s.close()


def old_price(s, hours):
    ts0 = unpair(s)
    add_price_swaps(s, [ts0 - int(hours * 60) * MIN])
    s.con.commit()
    return ts0


def fresh_prices(s):
    ts0 = unpair(s)
    add_price_swaps(s, [ts0 - 5 * MIN + i * 5 * MIN for i in range(0, 24 * 60 // 5 + 3)])
    s.con.commit()
    return ts0


def launch_of(s, token=COIN):
    return S._launch(s, token)


@pytest.mark.core
def test_the_same_launch_is_converted_by_the_display_rule_and_not_by_the_outcome_rule(db):
    old_price(db, 3)
    launch = launch_of(db)
    cutoff = launch["ts_ns"] + 60 * S.NS_MIN
    legs, swaps = S._rows(db, launch, cutoff)
    wide = S.to_near_age(launch, swaps, S.quote_series(db, launch, cutoff))                       # the display rule (24 h)
    strict = S.to_near_age(launch, swaps, S.quote_series(db, launch, cutoff, QP.MAX_AGE_MIN))     # the outcome rule (15 min)
    assert wide[1] is True and wide[2] >= 3 * 3600 - 60
    assert strict[1] is False
    raw = 5_000_000
    db.con.execute("UPDATE launches SET dev_buy = ? WHERE token = ?", (str(raw), COIN))
    db.con.commit()
    row = dict(launch_of(db), dev_buy=str(raw))
    ctx = LaunchContext(db, row, CFG)                                                    # the rules (cards, the fingerprint signal) read the strict price
    assert QP.near_at(db.con, PRL, raw, launch["ts_ns"]) is None                                    # strict: no price within 15 min
    assert QP.near_at(db.con, PRL, raw, launch["ts_ns"], QP.DISPLAY_MAX_AGE_MIN)["near"] == str(raw * K)
    assert ctx.dev_buy_near is None


@pytest.mark.core
def test_buyer_rows_show_the_old_price_with_the_mark_and_its_age(db):
    old_price(db, 3)
    out = BU.build_launch_buyers(db, CFG, only=[COIN])
    assert out["quote_converted"] == 1 and out["quote_approx"] == 1 and out["quote_unpriced"] == 0
    rows = BU.early_buyers(db.con, COIN, 50)
    assert rows and all(r["spent"] is not None and r["approx"] is True for r in rows)
    assert all(10_800 - 60 <= r["price_age_s"] <= 10_800 + 3600 and r["price_age_text"] == "3 h" for r in rows)
    assert all(r["amount_note"] == f"≈ at the last known price of {PRL}, 3 h old" for r in rows)
    h = db.con.execute("SELECT config_hash FROM launch_buyers_done WHERE token = ?", (COIN,)).fetchone()[0]
    assert ":q3~" in h and BU.pending(db, CFG, only=[COIN]) == []                                    # stored with the age, not pending again


@pytest.mark.core
def test_a_fresh_price_is_exact_and_carries_no_mark(db):
    fresh_prices(db)
    BU.build_launch_buyers(db, CFG, only=[COIN])
    rows = BU.early_buyers(db.con, COIN, 50)
    assert rows and all(r["spent"] is not None and r["approx"] is False and r["price_age_s"] is None and r["amount_note"] == f"converted from {PRL} at the swap-time price" for r in rows)
    assert "~" not in db.con.execute("SELECT config_hash FROM launch_buyers_done WHERE token = ?", (COIN,)).fetchone()[0]


@pytest.mark.core
def test_a_price_older_than_24_h_stays_na_without_a_mark(db):
    old_price(db, 30)
    BU.build_launch_buyers(db, CFG, only=[COIN])
    rows = BU.early_buyers(db.con, COIN, 50)
    assert rows and all(r["spent"] is None and r["pnl"] is None and r["approx"] is False and r["amount_note"] == f"n/a (quote: {PRL})" for r in rows)


@pytest.mark.core
def test_the_account_summary_is_marked_when_any_row_is_approximate_and_exact_rows_stay_unmarked(db):
    old_price(db, 3)
    BU.build_launch_buyers(db, CFG, only=[COIN])
    got = BU.account_trades(db.con, ACCOUNT)
    by = {r["token"]: r for r in got["rows"]}
    assert by[COIN]["approx"] is True and by[COIN]["price_age_s"] > 900
    assert by[ISLAND]["approx"] is False and by[ISLAND]["price_age_s"] is None                       # a pool paired with wNEAR: exact, no mark
    s = got["summary"]
    assert s["approx"] is True and s["approx_rows"] == 1 and s["price_age_text"] == "3 h"
    assert all(isinstance(r["approx"], bool) for r in got["rows"])


@pytest.mark.core
def test_the_account_summary_has_no_mark_when_every_price_is_fresh(db):
    fresh_prices(db)
    BU.build_launch_buyers(db, CFG, only=[COIN])
    s = BU.account_trades(db.con, ACCOUNT)["summary"]
    assert s["approx"] is False and s["approx_rows"] == 0 and s["price_age_s"] is None


@pytest.mark.core
def test_live_state_marks_the_amounts_and_the_reader_exposes_the_keys(db):
    ts0 = old_price(db, 3)
    cfg = C.load_config()
    now = ts0 + 60 * MIN
    res, src = LS.compute_token(db, COIN, now, cfg)
    qp = res["facts"]["quote_price"]
    assert qp["quote"] == PRL and qp["near"] is True and qp["approx"] is True and qp["price_age_s"] >= 3 * 3600 - 60
    assert res["facts"]["activity"][0]["buys_near"] is not None and res["parts"]["volume"]["buys_near"] is not None
    db.begin()
    LS.write_now(db, COIN, res, src, cfg)
    db.commit()
    got = LS.launch_live_state(db.con, COIN)
    assert got["quote_token"] == PRL and got["approx"] is True and got["price_age_s"] == qp["price_age_s"] and got["price_age_text"] == "3 h"


@pytest.mark.core
def test_live_state_with_a_fresh_price_is_exact_and_without_a_price_masked(db):
    ts0 = fresh_prices(db)
    res = LS.compute_token(db, COIN, ts0 + 60 * MIN, C.load_config())[0]
    assert res["facts"]["quote_price"]["approx"] is False and res["facts"]["quote_price"]["price_age_s"] is not None or res["facts"]["quote_price"]["price_age_s"] is None
    assert res["facts"]["quote_price"]["near"] is True
    other = LS.compute_token(db, ISLAND, ts0 + 60 * MIN, C.load_config())[0]
    assert "quote_price" not in other["facts"]                                                      # a pool paired with wNEAR: nothing to mark


@pytest.mark.core
def test_live_state_beyond_24_h_has_no_near_parts(db):
    ts0 = old_price(db, 30)
    res = LS.compute_token(db, COIN, ts0 + 60 * MIN, C.load_config())[0]
    assert res["facts"]["quote_price"]["near"] is False and res["facts"]["quote_price"]["approx"] is False
    assert res["facts"]["activity"][0]["buys_near"] is None and res["parts"]["volume"]["buys_near"] is None


@pytest.mark.core
def test_the_dev_buy_page_value_is_approximate_with_the_age_and_none_beyond_24_h(db):
    old_price(db, 3)
    db.con.execute("UPDATE launches SET dev_buy = ? WHERE token = ?", (str(5_000_000), COIN))
    db.con.commit()
    d = queries.launch(db.con, COIN)
    assert d["dev_buy"] == str(5_000_000 * K) and d["dev_buy_approx"] is True and d["dev_buy_price_age_text"] == "3 h" and d["dev_buy_note"].startswith("≈ at the last known price")
    row = next(r for r in queries.feed(db.con, 50) if r["token"] == COIN)
    assert row["dev_buy"] == str(5_000_000 * K) and row["dev_buy_approx"] is True
    assert queries.launch(db.con, ISLAND)["dev_buy_approx"] is False


@pytest.mark.core
def test_the_dev_buy_with_a_price_beyond_24_h_is_empty(db):
    old_price(db, 30)
    db.con.execute("UPDATE launches SET dev_buy = ? WHERE token = ?", (str(5_000_000), COIN))
    db.con.commit()
    d = queries.launch(db.con, COIN)
    assert d["dev_buy"] is None and d["dev_buy_approx"] is False and d["dev_buy_note"] is None


@pytest.mark.core
def test_age_helpers():
    assert not QP.is_approx(900) and QP.is_approx(901)
    assert QP.age_text(20 * 60) == "20 min" and QP.age_text(3 * 3600 + 5 * 60) == "3 h"


def spent_cells(html):
    sect = html[html.index('id="early-buyers"'):]
    sect = sect[:sect.index("</section>")]
    return [c.split("</td>")[0] for c in sect.split('data-label="Spent">')[1:]]


@pytest.mark.parametrize("hours, kind", [(3, "approx"), (None, "exact"), (30, "na")])
def test_the_pages_show_the_mark_only_for_an_approximate_amount(db, hours, kind):
    """RGY-075: the launch page and the account page print an approximate amount with «≈» and the price age, an exact one plainly, and an amount without a price as the note."""
    if hours is None:
        fresh_prices(db)
    else:
        old_price(db, hours)
    BU.build_launch_buyers(db, CFG, only=[COIN])
    path = db.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        cells = spent_cells(c.get(f"/launch/{COIN}").text)
        a = c.get(f"/creator/{ACCOUNT}").text
        blk = a[a.index('<section id="as-trader">'):]
        blk = blk[:blk.index("</section>")]
    assert cells
    for cell in cells:
        if kind == "approx":
            assert cell.startswith("≈ ") and "price 3 h old" in cell
        elif kind == "exact":
            assert "≈" not in cell and "NEAR" in cell and "price" not in cell
        else:
            assert cell == f'<span class="muted">n/a (quote: {PRL})</span>'
    if kind == "approx":
        assert "(includes 1 launch(es) at the last known price" in blk and "<strong>≈ " in blk
    else:
        assert "≈" not in blk and "includes" not in blk
