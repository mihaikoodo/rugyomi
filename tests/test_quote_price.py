"""RGY-070 (B of Q-046): the NEAR price of a quote token from swaps of its pool with wNEAR, and the conversion of a launch's trades to NEAR.

Pure part: hand-built trades. The same trades are attributed twice - once in a pool paired with wNEAR whose amounts are NEAR, once in a pool paired with a quote token whose
amounts are raw units converted with the price series - and every NEAR column must be identical (6- and 24-decimal quotes). Store part: the REFRAX / ISLAND / COIN-2 replay
with COIN-2's pool turned into a quote-token pool: without prices the outcome stays undefined (RGY-066), with the price swaps stored it is computed again."""
import json
import shutil
from decimal import Decimal

import pytest

import rugyomi_testkit  # noqa: F401

from rugyomi import quote_price as QP  # noqa: E402
from rugyomi import snapshots as S  # noqa: E402
from rugyomi.parse import DCL, WRAP  # noqa: E402
from rugyomi.rules import stored as O  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402

NS = 10**9
MIN = 60 * NS
T = "t.nearlytrade.near"
Q = "q.omft.near"
T0 = 1_800_000_000 * NS
POOL_W = f"{T}|{WRAP}|10000"
POOL_Q = f"{T}|{Q}|10000"
CASES = {                                   # quote decimals: raw units per token, price points (yocto NEAR per RAW unit), the raw quote amounts of the three trades
    "6-decimal quote (USDT-like), 1 Q = 0.25 NEAR then 0.31": (Decimal("2.5e17"), Decimal("3.1e17"), (5_000_000, 12_000_000, 2_000_000)),
    "24-decimal quote (memetoken), 1 Q = 0.5 NEAR then 0.75": (Decimal("0.5"), Decimal("0.75"), (4 * 10**24, 9 * 10**24, 10**24)),
}


def pt(ts, price, tx="PX"):
    return QP.Point(ts, Decimal(price), f"{Q}|{WRAP}|2000", tx)


def trades(a1, a2, a3, quote_in_near=None):
    """buy (tx1), buy (tx2), sell of 400 tokens (tx3). `quote_in_near`: the three NEAR amounts when the amounts are already NEAR (the wNEAR pool)."""
    qn = quote_in_near
    qa = qn or (a1, a2, a3)
    q = WRAP if qn else Q
    legs = [{"id": 1, "kind": "ft_transfer", "ts": T0 + 60 * NS, "tx": "tx1", "a": DCL, "b": "b1.near", "amount": 1000},
            {"id": 2, "kind": "ft_transfer", "ts": T0 + 120 * NS, "tx": "tx2", "a": DCL, "b": "b2.near", "amount": 3000},
            {"id": 3, "kind": "ft_transfer", "ts": T0 + 600 * NS, "tx": "tx3", "a": "b1.near", "b": DCL, "amount": 400}]
    swaps = [{"id": 1, "ts": T0 + 60 * NS, "tx": "tx1", "swapper": "b1.near", "token_in": q, "token_out": T, "amount_in": qa[0], "amount_out": 1000},
             {"id": 2, "ts": T0 + 120 * NS, "tx": "tx2", "swapper": "b2.near", "token_in": q, "token_out": T, "amount_in": qa[1], "amount_out": 3000},
             {"id": 3, "ts": T0 + 600 * NS, "tx": "tx3", "swapper": "b1.near", "token_in": T, "token_out": q, "amount_in": 400, "amount_out": qa[2]}]
    return legs, swaps


def launch(pool):
    return {"token": T, "creator": "dev.near", "owner": "dev.near", "pool": pool, "ts_ns": T0, "tx": "tx0"}


def series(p1, p2):
    return QP.Series(Q, [pt(T0 - 60 * NS, p1, "P1"), pt(T0 + 300 * NS, p2, "P2")])


# ---------------------------------------------------------------- the price of one swap

def test_the_price_is_yocto_near_per_raw_unit_from_either_direction_and_dust_is_no_price():
    buy_q = {"token_in": WRAP, "token_out": Q, "amount_in": 10**24, "amount_out": 4 * 10**6}          # 1 NEAR buys 4 USDT-like (6 decimals)
    sell_q = {"token_in": Q, "token_out": WRAP, "amount_in": 4 * 10**6, "amount_out": 10**24}
    assert QP.price_of(buy_q, Q) == QP.price_of(sell_q, Q) == Decimal(10**24) / Decimal(4 * 10**6) == Decimal("2.5e17")
    assert QP.price_of({**buy_q, "amount_in": QP.MIN_WNEAR - 1}, Q) is None                          # dust
    assert QP.price_of({**buy_q, "amount_out": 0}, Q) is None and QP.price_of({**buy_q, "amount_in": 0}, Q) is None
    assert QP.price_of({**buy_q, "token_out": "other.near"}, Q) is None                              # not a swap of this pair
    assert QP.price_of({"token_in": Q, "token_out": T, "amount_in": 5, "amount_out": 5}, Q) is None


def test_the_series_gives_the_last_swap_not_older_than_the_max_age_and_never_looks_ahead():
    s = QP.Series(Q, [pt(1000 * NS, 5), pt(100 * NS, 3)], max_age_ns=60 * NS)                         # unsorted input
    assert s.at(99 * NS) is None                                                                       # before the first swap: no look-ahead
    assert s.at(100 * NS).price == 3 and s.at(159 * NS).price == 3 and s.at(160 * NS).price == 3
    assert s.at(161 * NS) is None                                                                      # older than the max age
    assert s.at(1000 * NS).price == 5 and s.at(1060 * NS).price == 5 and s.at(1061 * NS) is None
    assert QP.with_age(s, 1000).at(900 * NS).price == 3 and QP.Series(Q).at(5) is None


# ---------------------------------------------------------------- the conversion

@pytest.mark.parametrize("name", list(CASES))
def test_a_converted_pool_gives_the_same_numbers_as_the_same_trades_in_a_wnear_pool(name):
    p1, p2, raw = CASES[name]
    ser = series(p1, p2)
    near = (int(Decimal(raw[0]) * p1), int(Decimal(raw[1]) * p1), int(Decimal(raw[2]) * p2))        # the sell at +10 min uses the price of +5 min
    lg_w, sw_w = trades(0, 0, 0, quote_in_near=near)
    want = S.metrics(launch(POOL_W), lg_w, sw_w, T0 + 3600 * NS)
    lg, sw = trades(*raw)
    got = S.metrics(launch(POOL_Q), lg, sw, T0 + 3600 * NS, ser)
    assert want["buyers_spent"] == str(near[0] + near[1]) and int(want["buyers_received"]) == near[2]
    for c in S.COLUMNS:
        if c not in ("insiders_evidence",):
            assert got[c] == want[c], c                                                                # every column, NEAR ones included
    ev = json.loads(got["insiders_evidence"])["quote_conversion"]
    assert ev["quote"] == Q and ev["trades"] == 3 and ev["pools"] == [f"{Q}|{WRAP}|2000"] and ev["price_txs"] and ev["max_price_age_s"] == 300.0
    assert "quote_conversion" not in json.loads(want["insiders_evidence"])


def test_without_a_price_or_with_one_trade_unpriced_the_launch_stays_undetermined():
    lg, sw = trades(5_000_000, 12_000_000, 2_000_000)
    for ser in (None, QP.Series(Q), QP.Series(Q, [pt(T0 + 100 * NS, "2.5e17")]),                      # no series / no points / the first buy has no earlier price
                QP.Series(Q, [pt(T0 - 60 * NS, "2.5e17")])):                                           # the sell at +10 min: the price is 11 min old... still fine (max age 15) -> see below
        m = S.metrics(launch(POOL_Q), lg, sw, T0 + 3600 * NS, ser)
        if ser is not None and len(ser.points) == 1 and ser.points[0].ts == T0 - 60 * NS:
            assert m["buyers_spent"] is not None                                                       # 11 min <= 15 min: converted
            continue
        assert all(m[c] is None for c in S.NEAR_COLUMNS), ser
        assert m["buyers_count"] == 2 and m["n_swaps"] == 3                                            # what does not depend on the units is still computed
    stale = QP.Series(Q, [pt(T0 - 60 * NS, "2.5e17")], max_age_ns=5 * MIN)                             # the same price, max age 5 min: the buys (1-2 min after) fit, the sell (11 min) does not
    r = QP.convert_swaps(T, Q, sw, stale)
    assert not r["ok"] and r["missing"] == 1 and r["swaps"] is sw                                      # all or nothing: the input comes back untouched
    assert all(m is None for m in [S.metrics(launch(POOL_Q), lg, sw, T0 + 3600 * NS, stale)["buyers_pnl"]])


def test_a_swap_that_does_not_touch_the_named_quote_is_not_converted_and_zero_swaps_pass_through():
    _, sw = trades(5_000_000, 12_000_000, 2_000_000)
    odd = [dict(sw[0], token_in="other.near")] + sw[1:]
    r = QP.convert_swaps(T, Q, odd, series(Decimal("2.5e17"), Decimal("3e17")))
    assert not r["ok"] and r["missing"] == 1
    zero = {"id": 9, "ts": T0, "tx": "tz", "swapper": "x", "token_in": Q, "token_out": T, "amount_in": 0, "amount_out": 0}
    r = QP.convert_swaps(T, Q, [zero] + sw, series(Decimal("2.5e17"), Decimal("3e17")))
    assert r["ok"] and r["total"] == 3 and r["swaps"][0] is zero
    assert QP.convert_swaps(T, Q, [], None)["ok"]                                                      # no trade at all: nothing to price


def test_a_wnear_launch_is_untouched_by_the_price_series():
    lg, sw = trades(0, 0, 0, quote_in_near=(10**24, 2 * 10**24, 3 * 10**23))
    a = S.metrics(launch(POOL_W), lg, sw, T0 + 3600 * NS)
    b = S.metrics(launch(POOL_W), lg, sw, T0 + 3600 * NS, series(Decimal(1), Decimal(1)))
    assert a == b and a["buyers_spent"] == str(3 * 10**24)


# ---------------------------------------------------------------- the stored world

ISLAND, COIN, REFRAX = "island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near"
PRL = "prl.omft.near"
K = 1000                                    # yocto NEAR per raw unit of PRL in the test world
PAIR = f"{PRL}|{WRAP}|2000"


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    p = tmp_path_factory.mktemp("qp") / "base.db"
    refrax_world_db(p)
    return p


@pytest.fixture
def db(base, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(base, p)
    s = open_store(p)
    yield s
    s.close()


def unpair(s, token=COIN):
    """COIN-2's pool becomes a PRL pool whose amounts are raw PRL = NEAR yocto / K: the same trades, another unit."""
    old = s.con.execute("SELECT pool FROM launches WHERE token = ?", (token,)).fetchone()[0]
    new = f"{token}|{PRL}|10000"
    s.con.execute("UPDATE accounts SET name = ? WHERE name = ?", (new, old))
    s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (new, token))
    pid, kind = s.lookup_account(new), s.lookup_kind("dcl_swap")
    s.begin()
    for id_, extra in s.con.execute("SELECT id, extra FROM facts WHERE kind = ? AND b = ?", (kind, pid)).fetchall():
        e = json.loads(extra)
        for side in ("in", "out"):
            if e[f"token_{side}"] == WRAP:
                e[f"token_{side}"] = PRL
                e[f"amount_{side}"] = str(int(e[f"amount_{side}"]) // K)
        s.con.execute("UPDATE facts SET extra = ? WHERE id = ?", (json.dumps(e), id_))
    s.commit()
    return s._launch_ts(token) if hasattr(s, "_launch_ts") else s.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (token,)).fetchone()[0]


def add_price_swaps(s, ts_list, table="facts", scope=None, tag=b"p"):
    """Swaps of the pool PRL / wNEAR at the given times, price K yocto per raw PRL (10^24 yocto for 10^24 / K raw)."""
    s.begin()
    kind, pool = s.kind_id("dcl_swap"), s.account_id(PAIR)
    for i, ts in enumerate(ts_list):
        extra = json.dumps({"token_in": WRAP, "token_out": PRL, "amount_in": str(10**24), "amount_out": str(10**24 // K), "launch_pool": False})
        rec = tag + str(i).zfill(30).encode()[:31]
        if table == "facts":
            s.con.execute("INSERT INTO facts(kind, height, ts_ns, tx, receipt, origin, a, b, extra) VALUES(?,?,?,?,?,?,?,?,?)", (kind, 1 + i, ts, rec, rec, "l0.0", None, pool, extra))
        else:
            s.con.execute("INSERT INTO api_facts(scope, height, ord, seq, kind, ts_ns, tx, receipt, origin, b, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (scope, 1 + i, 0, 0, kind, ts, rec, rec, "l0.0", pool, extra))
    s.commit()


def snap(s, token):
    cols = ("minute",) + S.COLUMNS
    return {r[0]: dict(zip(cols, r)) for r in s.con.execute(f"SELECT {','.join(cols)} FROM launch_snapshots WHERE token = ?", (token,))}


def test_the_series_is_read_from_facts_and_api_facts_once_per_swap_and_only_from_the_pool_of_the_pair(db):
    ts0 = unpair(db)
    add_price_swaps(db, [ts0 + i * MIN for i in (1, 2, 3)])
    add_price_swaps(db, [ts0 + 2 * MIN], table="api_facts", scope=f"quote:{PRL}:1", tag=b"p")          # the same receipt as a facts row: read once
    add_price_swaps(db, [ts0 + 50 * MIN], table="api_facts", scope=f"quote:{PRL}:1", tag=b"q")
    add_price_swaps(db, [ts0 + 60 * MIN], table="api_facts", scope="quote:other.near:1", tag=b"r")     # another token's scope: not read
    ser = QP.load(db, PRL, ts0, ts0 + 24 * 60 * MIN)
    assert [p.ts for p in ser.points] == [ts0 + 1 * MIN, ts0 + 2 * MIN, ts0 + 3 * MIN, ts0 + 50 * MIN]
    assert {p.pool for p in ser.points} == {PAIR} and all(p.price == Decimal(K) for p in ser.points)
    assert QP.pair_pools(db, PRL) == {db.lookup_account(PAIR): PAIR} and QP.pair_pools(db, "none.near") == {}
    assert QP.series_for(db, S._launch(db, COIN), PRL).points[0].ts == ts0 + MIN


def test_a_launch_without_stored_prices_stays_undefined_and_with_them_gets_its_labels_back(db):
    before = {m: r for m, r in snap(db, COIN).items()}
    was = O.outcome_row(db, COIN)
    assert was["rug"]["value"] is True
    ts0 = unpair(db)
    S.rebuild_snapshots(db, token=COIN)
    O.rebuild_outcomes(db)
    assert O.outcome_row(db, COIN)["rug"]["undefined_kind"] == "quote" and all(r["buyers_pnl"] is None for r in snap(db, COIN).values())    # RGY-066: no price, no NEAR
    add_price_swaps(db, [ts0 - 5 * MIN + i * 10 * MIN for i in range(0, 24 * 6 + 3)])                 # a price every 10 min from just before the launch to past T+24 h
    S.rebuild_snapshots(db, token=COIN)
    O.rebuild_outcomes(db)
    after = snap(db, COIN)
    for m, row in after.items():
        for c in S.NEAR_COLUMNS:
            if before[m][c] is None:
                assert row[c] is None
            else:
                assert abs(int(row[c]) - int(before[m][c])) <= 10 * K * 5, (m, c)                     # integer division by K: a few K at most
        for c in ("n_swaps", "buyers_count", "dev_sold_share_bps", "insiders_sold_share_bps", "holders_count"):
            assert row[c] == before[m][c], (m, c)
    o = O.outcome_row(db, COIN)
    assert o["rug"]["value"] is True and not o["rug"].get("undefined")                                 # the labels are the ones the wNEAR pool gave
    assert abs(o["severity_near"] - was["severity_near"]) < 1e-9 * max(1.0, abs(was["severity_near"]))
    ev = db.con.execute("SELECT evidence FROM launch_outcomes WHERE token = ?", (COIN,)).fetchone()[0]
    assert json.loads(ev)["quote_conversion"] == {"quote": PRL}
    for t in (ISLAND, REFRAX):
        assert "quote_conversion" not in db.con.execute("SELECT evidence FROM launch_outcomes WHERE token = ?", (t,)).fetchone()[0]


def test_one_trade_without_a_price_gates_the_snapshots_that_contain_it(db):
    ts0 = unpair(db)
    add_price_swaps(db, [ts0 + int(0.07 * MIN), ts0 + 5 * MIN])                                       # the first price comes after the first trade (+0.06 min): no look-ahead
    S.rebuild_snapshots(db, token=COIN)
    O.rebuild_outcomes(db)
    s = snap(db, COIN)
    assert s and all(r["buyers_pnl"] is None and all(r[c] is None for c in S.NEAR_COLUMNS) for r in s.values())
    assert O.outcome_row(db, COIN)["rug"]["undefined_kind"] == "quote"
    assert "quote_conversion" not in db.con.execute("SELECT evidence FROM launch_outcomes WHERE token = ?", (COIN,)).fetchone()[0]


def test_the_labels_leave_the_undefined_state_only_when_every_label_snapshot_has_its_number():
    cfg = {"rug": {"fast_minute": 60, "late_minute": 1440}}
    ok = lambda m: {"minute": m, "error": None, "buyers_pnl": "5"}  # noqa: E731
    gated = lambda m: {"minute": m, "error": None, "buyers_pnl": None}  # noqa: E731
    assert O._converted([ok(5), ok(60), ok(1440)], cfg) and O._converted([ok(5), ok(60)], cfg)          # a launch still inside its first day: T+60 only
    assert not O._converted([ok(60), gated(1440)], cfg) and not O._converted([gated(60), ok(1440)], cfg)
    assert not O._converted([ok(5)], cfg) and not O._converted([], cfg)
    assert not O._converted([ok(60), {**ok(1440), "error": "boom"}], cfg)
