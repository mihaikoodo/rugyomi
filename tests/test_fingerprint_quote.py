"""RGY-074: the `fingerprint` signal compares the dev buy with the creator's funding in NEAR. On a pool not paired with wNEAR `launches.dev_buy` is raw units of the quote
token: it is converted at the launch time by the strict price rule of the outcomes (`quote_price.near_at`, 15 min), and with no price the dev buy condition is not counted (unknown, never read as raw units); the two other conditions score as always.
A pool paired with wNEAR, and a zero dev buy, read exactly as before."""
import pytest

import rugyomi_testkit  # noqa: F401

from test_quote_price import K, MIN as NS_MIN, PRL, add_price_swaps  # noqa: E402
from test_rules import MIN, NEAR, T0, Db, result  # noqa: E402

TOKEN = "q.nearlytrade.near"
RAW_40 = 40 * NEAR // K                              # 40 NEAR of PRL in raw units of the test world (K yocto per raw unit)


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "r.db")
    yield d
    d.s.close()


def launch(db, dev_buy, pool, token=TOKEN):
    db.launch(token, "fresh.near", T0, dev_buy=dev_buy)
    db.s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (pool, token))
    db.created("fresh.near", T0 - 6 * MIN)
    db.fund("rich.near", "fresh.near", 41, T0 - 5 * MIN)
    return result(db.card(token), "fingerprint")


@pytest.mark.core
def test_a_prl_dev_buy_is_converted_at_the_launch_time(db):
    add_price_swaps(db.s, [T0 - 4 * NS_MIN])
    r = launch(db, RAW_40, f"{TOKEN}|{PRL}|10000")
    v = r["value"]
    assert v["dev_buy_near"] == pytest.approx(40, abs=0.01) and v["dev_buy_converted_from"] == PRL
    assert v["dev_buy_vs_funding"] is True and v["conditions_met"] == 3 and r["contribution"] == 35           # as the same launch paired with wNEAR (three of three)


@pytest.mark.core
def test_raw_units_are_never_read_as_near(db):
    add_price_swaps(db.s, [T0 - 4 * NS_MIN])
    r = launch(db, 41 * NEAR, f"{TOKEN}|{PRL}|10000")                                      # 41 * 10^24 RAW PRL is worth far more than the funding, not "41 NEAR"
    assert r["value"]["dev_buy_near"] == pytest.approx(41 * NEAR * K / NEAR, rel=1e-6)


@pytest.mark.core
@pytest.mark.parametrize("case", ["no_price", "price_too_old", "price_after_launch"])
def test_without_a_price_within_the_age_limit_the_dev_buy_is_not_counted(db, case):
    if case == "price_too_old":
        add_price_swaps(db.s, [T0 - 16 * NS_MIN])
    elif case == "price_after_launch":
        add_price_swaps(db.s, [T0 + 1 * NS_MIN])
    r = launch(db, RAW_40, f"{TOKEN}|{PRL}|10000")                                         # fresh account + funded: two of three, the dev buy condition is unknown, not met
    v = r["value"]
    assert v["dev_buy_near"] is None and v["dev_buy_vs_funding"] is None and v["dev_buy_unknown"] == PRL and "dev_buy_converted_from" not in v
    assert v["conditions_met"] == 2 and r["fired"] and r["contribution"] == 25                  # points only from the two conditions that are known
    assert "no price within the age limit" in r["reason"] and "not counted" in r["reason"]


@pytest.mark.core
def test_an_unknown_dev_buy_never_adds_the_third_condition(db):
    db.launch(TOKEN, "old.near", T0, dev_buy=RAW_40)                                       # an old account, funded, no price: one of three, silent
    db.s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (f"{TOKEN}|{PRL}|10000", TOKEN))
    db.fund("rich.near", "old.near", 41, T0 - 5 * MIN)
    r = result(db.card(TOKEN), "fingerprint")
    assert not r["fired"] and r["contribution"] == 0 and r["value"]["conditions_met"] == 1 and r["value"]["dev_buy_unknown"] == PRL


@pytest.mark.core
def test_a_pool_paired_with_wnear_and_a_zero_dev_buy_read_as_before(db):
    r = launch(db, 40 * NEAR, f"{TOKEN}|wrap.near|10000")
    assert r["fired"] and r["contribution"] == 35 and r["value"]["dev_buy_near"] == 40.0 and "dev_buy_converted_from" not in r["value"]
    db.launch("z.nearlytrade.near", "fresh.near", T0, dev_buy=0)
    db.s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (f"z.nearlytrade.near|{PRL}|10000", "z.nearlytrade.near"))
    z = result(db.card("z.nearlytrade.near"), "fingerprint")                               # no dev buy: 0 is 0 in any unit, no price needed
    assert z["value"]["dev_buy_near"] == 0.0 and "skipped" not in z["value"] and z["value"]["dev_buy_vs_funding"] is False
