"""RGY-043: the Live state of a launch.

Pinned: each of the four parts at 0 and at 100 and in between, the mean and its rounding, "no trading", the history-gap state (never a number over missing data), the version in
the output, ONE holdings code path for "now" and the fixed minutes, the card hash and the pinned profile hashes untouched, the schedule (active / idle / dormant), the fixed
minutes, the additive migration, and the page and the methodology. Nothing here fits a weight: the formula is fixed."""
import json
import math
import re
import shutil
import time
from decimal import Decimal
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import holdings as HO  # noqa: E402
from rugyomi import livestate as LS  # noqa: E402
from rugyomi import rules  # noqa: E402
from rugyomi.parse import DCL  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.store.schema import MIGRATIONS  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402

NS, MIN, HOUR = 10**9, 60 * 10**9, 3600 * 10**9
TOK, DEV, T0 = "x.nearlytrade.near", "dev.near", 1_000 * NS
REFRAX, ISLAND = "refrax.nearlytrade.near", "island.nearlytrade.near"
CFG = C.load_config(profile="v1")
PINNED = {"v0": "2cb6d1e9c0c3", "v1": "6fedfdb50583", "v2": "3d68b94c6371", "v1r": "576d19235a70", "v2r": "410c8c947216", "v2l": "6fe0b20cacc0"}


# ---------------------------------------------------------------------------------------------- synthetic trades (price = near / tokens, so every price is chosen)
class World:
    def __init__(self, supply: int = 10_000):
        self.legs = [{"id": -1, "kind": "ft_mint", "ts": T0, "tx": "launchtx", "a": "lock_1.nearlytrade.near", "b": None, "amount": supply}]
        self.swaps: list[dict] = []
        self.n = 0

    def buy(self, who: str, tokens: int, near: int, at_min: float):
        self.n += 1
        i, ts = self.n, T0 + int(at_min * MIN) + self.n
        self.legs.append({"id": 2 * i, "kind": "ft_transfer", "ts": ts, "tx": f"t{i}", "a": DCL, "b": who, "amount": tokens})
        self.swaps.append({"id": 2 * i + 1, "ts": ts, "tx": f"t{i}", "swapper": "lock_1.nearlytrade.near", "token_in": "wrap.near", "token_out": TOK,
                           "amount_in": near, "amount_out": tokens})

    def sell(self, who: str, tokens: int, near: int, at_min: float):
        self.n += 1
        i, ts = self.n, T0 + int(at_min * MIN) + self.n
        self.legs.append({"id": 2 * i, "kind": "ft_transfer", "ts": ts, "tx": f"t{i}", "a": who, "b": DCL, "amount": tokens})
        self.swaps.append({"id": 2 * i + 1, "ts": ts, "tx": f"t{i}", "swapper": who, "token_in": TOK, "token_out": "wrap.near", "amount_in": tokens, "amount_out": near})


LAUNCH = {"token": TOK, "owner": DEV, "creator": DEV, "ts_ns": T0, "tx": "launchtx", "pool": "pool.dcl"}


def early_of(*wallets):
    return [{"buyer": w, "first_buy_ns": T0 + (i + 1) * NS} for i, w in enumerate(wallets)]


def scenario() -> World:
    """A buys at price 1 (the first outside buyer's entry), B at 2, C at 3 (the peak); A sells at 1.5 (a profit), B sells half at 1 (the last price)."""
    w = World()
    w.buy(DEV, 500, 500, 0.5)
    w.buy("a.near", 100, 100, 1)
    w.buy("b.near", 100, 200, 2)
    w.buy("c.near", 100, 300, 3)
    w.sell("a.near", 100, 150, 4)
    w.sell("b.near", 50, 50, 5)
    return w


def state(w: World, now_min: float, cfg=CFG, early=None, final=True):
    return LS.compute(LAUNCH, w.legs, w.swaps, T0 + int(now_min * MIN), early if early is not None else early_of("a.near", "b.near", "c.near"), {}, {}, cfg, final)


# ---------------------------------------------------------------------------------------------- the four parts
@pytest.mark.core
def test_profit_part_is_the_share_of_outside_buyers_in_profit_at_the_last_price():
    buyers = {"a": {"spent": 100, "bought": 10, "received": 0, "sold": 0},       # holds 10 at 20 = 200 > 100: in profit
              "b": {"spent": 100, "bought": 10, "received": 150, "sold": 10},    # sold everything for 150: in profit
              "c": {"spent": 100, "bought": 10, "received": 0, "sold": 0},
              "d": {"spent": 100, "bought": 10, "received": 40, "sold": 10}}     # sold for 40: a loss
    assert LS.part_profit(buyers, Decimal(20)) == {"value": 75, "in_profit": 3, "buyers": 4}
    assert LS.part_profit(buyers, Decimal("0.01"))["in_profit"] == 1                       # a price that fell: only b (it already sold) stays in profit
    assert LS.part_profit({k: dict(v, received=0, sold=0) for k, v in buyers.items()}, Decimal("0.01"))["value"] == 0     # 0
    assert LS.part_profit({"a": buyers["a"]}, Decimal(20))["value"] == 100                  # 100
    assert LS.part_profit({}, Decimal(20)) == {"value": 0, "in_profit": 0, "buyers": 0}      # nobody to divide by: 0, the part is never left out
    assert LS.part_profit({"a": {"spent": 10, "bought": 5, "received": 10, "sold": 5}}, Decimal(1))["in_profit"] == 0     # exactly even is not a profit


@pytest.mark.core
def test_volume_part_is_the_buy_share_of_the_near_traded_in_the_window():
    now = T0 + 100 * MIN
    tr = [{"side": "buy", "near": 300, "ts": now - 10 * MIN}, {"side": "sell", "near": 100, "ts": now - 20 * MIN},
          {"side": "sell", "near": 9999, "ts": now - 90 * MIN}, {"side": "buy", "near": 9999, "ts": now + 1}]
    p = LS.part_volume(tr, now, 60)
    assert p["value"] == 75 and p["buys_near"] == "300" and p["sells_near"] == "100"       # outside the window or after now: not counted
    assert LS.part_volume([t for t in tr if t["side"] == "buy" and t["near"] == 300], now, 60)["value"] == 100
    assert LS.part_volume([t for t in tr if t["side"] == "sell" and t["near"] == 100], now, 60)["value"] == 0
    assert LS.part_volume([], now, 60)["value"] == 0                                         # no trade in the window: 0
    assert LS.part_volume(tr, now, 5)["value"] == 0 and LS.part_volume(tr, now, 15)["value"] == 100          # the window is a parameter


@pytest.mark.core
def test_peak_part_is_the_last_price_as_a_share_of_the_highest():
    assert LS.part_peak(Decimal(3), Decimal(3))["value"] == 100
    assert LS.part_peak(Decimal(1), Decimal(4))["value"] == 25
    assert LS.part_peak(Decimal(1), Decimal(3))["value"] == 33 and LS.part_peak(Decimal(2), Decimal(3))["value"] == 67
    assert LS.part_peak(None, None)["value"] == 0 and LS.part_peak(Decimal(1), None)["value"] == 0


@pytest.mark.core
def test_buyers_part_is_log_scaled_zero_to_the_full_count():
    assert LS.part_buyers(0, 20, 60)["value"] == 0
    assert LS.part_buyers(20, 20, 60)["value"] == 100 and LS.part_buyers(35, 20, 60)["value"] == 100
    assert LS.part_buyers(1, 20, 60)["value"] == round(100 * math.log(2) / math.log(21))
    assert LS.part_buyers(10, 20, 60)["value"] == round(100 * math.log(11) / math.log(21))
    assert [LS.part_buyers(n, 20, 60)["value"] for n in range(0, 21)] == sorted(LS.part_buyers(n, 20, 60)["value"] for n in range(0, 21))     # monotone
    assert LS.part_buyers(5, 10, 30)["full_at"] == 10 and LS.part_buyers(10, 10, 30)["value"] == 100                                       # a parameter


@pytest.mark.core
def test_the_number_is_the_mean_of_the_four_integer_parts_rounded_half_up():
    def n(*v):
        return LS.number_of({k: {"value": x} for k, x in zip(LS.PARTS, v)})
    assert n(0, 0, 0, 0) == 0 and n(100, 100, 100, 100) == 100
    assert n(100, 0, 50, 25) == 44                                                           # 175 / 4 = 43.75
    assert n(1, 1, 0, 0) == 1 and n(1, 0, 0, 0) == 0                                          # 0.5 up, 0.25 down
    assert n(3, 0, 0, 0) == 1 and n(2, 0, 0, 0) == 1 and n(5, 0, 0, 0) == 1 and n(6, 0, 0, 0) == 2


# ---------------------------------------------------------------------------------------------- the whole computation
@pytest.mark.core
def test_a_whole_launch_gives_the_parts_the_number_and_the_facts():
    res = state(scenario(), 30)
    p = res["parts"]
    assert res["state"] == "ok" and res["version"] == "ls-1" and res["asof_ns"] == T0 + 30 * MIN
    assert p["profit"]["value"] == 33 and (p["profit"]["in_profit"], p["profit"]["buyers"]) == (1, 3)         # A alone is in profit
    assert p["volume"]["value"] == 85 and p["volume"]["buys_near"] == "1100" and p["volume"]["sells_near"] == "200"       # the dev's buy is volume too
    assert p["peak"]["value"] == 33                                                          # last price 1, peak 3
    assert p["buyers"]["unique"] == 3 and p["buyers"]["value"] == round(100 * math.log(4) / math.log(21))     # the dev is not an outside buyer
    assert res["number"] == (33 + 85 + 33 + p["buyers"]["value"] + 2) // 4
    f = res["facts"]
    assert f["price"]["vs_entry_x"] == 1.0 and f["price"]["vs_peak_x"] == pytest.approx(0.333333, abs=1e-6)         # entry 1 (A), last 1, peak 3
    assert f["outside_buyers"] == {"count": 3, "in_profit": 1, "share_bps": 3333}
    assert f["last_trade_ns"] == res["last_trade_ns"] and f["last_trade_ns"] > T0 + 5 * MIN
    assert [(a["minutes"], a["buys"], a["sells"]) for a in f["activity"]] == [(15, 0, 0), (60, 4, 2)] and f["buyers_hour"] == 3
    assert [(a["minutes"], a["buys"], a["sells"]) for a in state(scenario(), 10)["facts"]["activity"]] == [(15, 4, 2), (60, 4, 2)]      # the 15-minute window slides
    assert f["holdings"]["supply"] == "10000" and f["holdings"]["supply_note"] == "ok" and f["holdings"]["early_final"] is True
    rows = {r["key"]: r for r in f["holdings"]["rows"]}
    assert rows["dev"]["held"] == "500" and rows["dev"]["bps"] == 500 and int(rows["unknown_age"]["held"]) == 150       # c 100 + b 50; a sold out
    # the facts are beside the number: they do not enter it
    other = state(scenario(), 30, early=[])
    assert other["number"] == res["number"] and other["facts"]["holdings"]["rows"] != f["holdings"]["rows"]


@pytest.mark.core
def test_only_the_last_hour_counts_for_volume_and_buyers_and_the_window_slides():
    w = scenario()
    at90 = state(w, 90)                                                                      # every trade is older than an hour, the last one 85 min ago
    assert at90["state"] == "ok" and at90["parts"]["volume"]["value"] == 0 and at90["parts"]["buyers"]["value"] == 0
    assert at90["parts"]["profit"]["value"] == 33 and at90["parts"]["peak"]["value"] == 33          # the cumulative parts do not slide
    assert at90["number"] == (33 + 0 + 33 + 0 + 2) // 4
    assert at90["facts"]["activity"][1]["buys"] == 0 and at90["facts"]["buyers_hour"] == 0
    w.buy("d.near", 100, 100, 85)                                                           # one new buyer at price 1: back in the window
    assert state(w, 90)["parts"]["buyers"]["unique"] == 1 and state(w, 90)["parts"]["volume"]["value"] == 100


@pytest.mark.core
def test_no_trading_for_a_day_or_no_trade_at_all_gives_no_number():
    w = scenario()
    quiet = state(w, 5 + 24 * 60 + 1)                                                       # the last trade (minute 5 + a few ns) is just over 24 h old
    assert quiet["state"] == "no_trading" and quiet["number"] is None and quiet["last_trade_ns"] is not None
    assert quiet["parts"]["profit"]["value"] == 33                                           # the facts are still there
    assert state(w, 5 + 24 * 60 - 1)["state"] == "ok" and state(w, 5 + 24 * 60 - 1)["number"] is not None
    none = LS.compute(LAUNCH, World().legs, [], T0 + 10 * MIN, [], {}, {}, CFG)
    assert none["state"] == "no_trading" and none["number"] is None and none["last_trade_ns"] is None
    assert none["parts"]["profit"]["value"] == 0 and none["parts"]["peak"]["value"] == 0       # nothing to divide: 0, still no number shown


@pytest.mark.core
def test_a_trade_after_the_moment_is_not_in_it():
    w = scenario()
    early = state(w, 3.5)
    assert early["facts"]["outside_buyers"]["count"] == 3 and early["parts"]["profit"]["in_profit"] == 2 and early["facts"]["last_trade_ns"] < T0 + 4 * MIN
    assert early["facts"]["activity"][1]["sells"] == 0 and early["parts"]["volume"]["value"] == 100          # C at price 3 is not above its cost (equal), A and B are; no sell yet


@pytest.mark.core
def test_the_history_gap_state_has_no_number_and_only_the_facts_that_need_no_gap():
    hist = {**LAUNCH, "ts_ns": T0 - 20 * 24 * HOUR}
    live_from = T0 - 3600 * NS
    w = World()
    w.buy("a.near", 100, 100, 1)
    w.sell("a.near", 50, 80, 2)
    res = LS.compute_gap(hist, w.legs, w.swaps, T0 + 10 * MIN, CFG, live_from, {"ts": hist["ts_ns"] + 23 * HOUR, "price": Decimal(7)})
    assert res["state"] == "history_gap" and res["number"] is None and res["parts"] == {} and res["version"] == "ls-1"
    f = res["facts"]
    assert f["last_trade_where"] == "live" and f["live_swaps"] == 2 and f["gap_from_ns"] == hist["ts_ns"] + 1440 * MIN and f["gap_to_ns"] == live_from
    assert f["activity"][0]["buys"] == 1 and f["activity"][0]["sells"] == 1
    assert "outside_buyers" not in f and "holdings" not in f and set(f["price"]) == {"last"}          # nothing that needs the missing trades
    old = LS.compute_gap(hist, [], [], T0 + 10 * MIN, CFG, live_from, {"ts": hist["ts_ns"] + 23 * HOUR, "price": Decimal(7)})
    assert old["facts"]["last_trade_where"] == "window" and old["facts"]["last_trade_ns"] == hist["ts_ns"] + 23 * HOUR and old["facts"]["live_swaps"] == 0
    assert LS.compute_gap(hist, [], [], T0, CFG, live_from, None)["facts"]["last_trade_ns"] is None
    # what lies before the complete live data is sparse and is ignored; the later of the live trade and the window's last trade is "the last trade"
    sparse = World()
    sparse.buy("a.near", 100, 100, -90)                                                      # 90 min before the launch timestamp used as "T0" here, i.e. before live_from
    assert LS.compute_gap(hist, sparse.legs, sparse.swaps, T0 + 10 * MIN, CFG, live_from, None)["facts"]["live_swaps"] == 0
    later = LS.compute_gap(hist, w.legs, w.swaps, T0 + 10 * MIN, CFG, live_from, {"ts": T0 + 5 * MIN, "price": Decimal(7)})
    assert later["facts"]["last_trade_where"] == "window" and later["facts"]["last_trade_ns"] == T0 + 5 * MIN
    assert LS.settings(CFG)["live_from_ns"] == LS.live_from_of("2026-10-30 12:14") == (1793318400 + 12 * 3600 + 14 * 60) * NS and LS.live_from_of("2026-10-30") == 1793318400 * NS


@pytest.mark.core
def test_now_and_the_fixed_minutes_use_the_same_holdings_code(monkeypatch):
    calls = []
    real = HO.compute_minute

    def spy(launch, legs, swaps, minute, early, tags, ages, fresh_s, cutoff=None):
        calls.append((minute, cutoff))
        return real(launch, legs, swaps, minute, early, tags, ages, fresh_s, cutoff)

    monkeypatch.setattr(HO, "compute_minute", spy)
    w = scenario()
    state(w, 30)
    state(w, 60)
    assert calls == [(30, T0 + 30 * MIN), (60, T0 + 60 * MIN)]                              # the holdings function, with an explicit cutoff, nothing else
    monkeypatch.undo()
    # the same numbers as the T+N table: an explicit cutoff at launch + N min IS the table row
    for m in (5, 15, 60):
        table = HO.compute_minute(LAUNCH, w.legs, w.swaps, m, early_of("a.near", "b.near", "c.near"), {}, {}, 24 * 3600)
        live = state(w, m)["facts"]["holdings"]
        assert [(r["key"], r["wallets"], r["holding"], r["held"]) for r in live["rows"]] == [(r["key"], r["wallets"], r["holding"], r["held"]) for r in table["rows"]]
        assert live["supply"] == table["supply"] and live["supply_note"] == table["supply_note"]
    src = Path(LS.__file__).read_text(encoding="utf-8")
    assert "compute_minute" in src and not re.search(r"def (_?held|_?holdings|_?insiders|_?bundle)\w*\(", src)      # no second copy of the rules


@pytest.mark.core
def test_the_parts_and_the_buyers_use_the_snapshot_rules_not_a_copy():
    """The outside buyers and the prices are read from `snapshots` (one copy): the same buyers, the same peak, as the T+60 snapshot."""
    from rugyomi import snapshots as S
    w = scenario()
    m = S.metrics(LAUNCH, w.legs, w.swaps, T0 + 60 * MIN)
    res = state(w, 60)
    assert res["facts"]["outside_buyers"]["count"] == m["buyers_count"] == 3
    assert Decimal(res["facts"]["price"]["peak"]) == Decimal(m["price_peak"]) and Decimal(res["facts"]["price"]["last"]) == Decimal(m["price_last"])
    assert res["facts"]["price"]["vs_entry_x"] == round(float(Decimal(m["price_last"]) / Decimal(m["price_first_buyer"])), 6)


# ---------------------------------------------------------------------------------------------- config and the card hash
@pytest.mark.core
def test_the_card_hash_and_every_pinned_profile_hash_are_untouched_by_the_live_state_section():
    for name, h in PINNED.items():
        cfg = C.effective(None, name)
        assert "live_state" in cfg and cfg["live_state"]["version"] == "ls-1", name
        assert C.config_hash(cfg) == h, name
    other = json.loads(json.dumps(CFG))
    other["live_state"]["buyers_full_at"] = 99
    other["live_state"]["version"] = "ls-9"
    assert C.config_hash(other) == C.config_hash(CFG) and C.live_state_config_hash(other) != C.live_state_config_hash(CFG)
    assert LS.settings(other)["buyers_full_at"] == 99


@pytest.mark.core
def test_the_live_state_is_not_a_card_a_signal_or_a_channel_post():
    pkg = Path(LS.__file__).parent
    for name in ("publisher.py", "rules/card.py", "rules/context.py", "rules/signals.py", "rules/rug.py", "rules/stored.py", "rules/accuracy.py", "rules/backtest.py",
                 "rules/features.py", "retro.py", "links.py", "buyers.py", "holdings.py", "snapshots.py"):
        txt = (pkg / name).read_text(encoding="utf-8")
        assert not re.search(r"livestate|live_state|launch_live_state|Live state", txt), name
    channel = [p for p in pkg.rglob("*.py") if "channel" in p.name or "telegram" in p.name]
    assert all("livestate" not in p.read_text(encoding="utf-8") for p in channel)
    assert not re.search(r"\b(urllib|requests|httpx|http\.client)\b", (pkg / "livestate.py").read_text(encoding="utf-8"))        # 0 credits


@pytest.mark.core
def test_no_health_safety_or_verdict_words_in_the_text_the_site_shows():
    text = json.dumps(LS.describe(CFG)).lower()
    tpl = (Path(LS.__file__).parent / "web" / "templates" / "launch.html").read_text(encoding="utf-8").lower()
    block = tpl[tpl.index('id="live-state"'):tpl.index("risk card")]
    for t in (text, block):
        for word in ("health", "safe", "safety", "good", "bad", "retro", "will rise", "will fall", "predict"):
            assert word not in t, word
        assert "color" not in t and "red" not in re.sub(r"[^a-z]", " ", t).split() and "green" not in t
    assert "not a forecast" in block and "as of" in block
    assert "not a forecast" in LS.describe(CFG)["intro"]


# ---------------------------------------------------------------------------------------------- the database
@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("livestate") / "r.db"
    refrax_world_db(path)
    s = open_store(path)
    BU.rebuild(s)
    s.close()
    return path


@pytest.fixture
def db(replay, tmp_path):
    p = tmp_path / "l.db"
    shutil.copy(replay, p)
    s = open_store(p)
    yield s
    s.close()


def path_of(s):
    return s.con.execute("PRAGMA database_list").fetchone()[2]


@pytest.mark.core
def test_the_migration_is_additive_and_an_old_database_opens(tmp_path):
    mig = MIGRATIONS[19]
    assert "launch_live_state" in mig and "launch_live_state_at" in mig and len(MIGRATIONS) >= 20
    assert not any(w in mig.upper() for w in ("DROP ", "ALTER ", "DELETE ", "UPDATE ", "INSERT "))
    p = tmp_path / "old.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    for t in ("launch_live_state", "launch_live_state_at"):
        s.con.execute(f"DROP TABLE {t}")
    s.con.execute("PRAGMA user_version=19")
    s.con.commit()
    s.close()
    s = open_store(p)
    assert s.user_version() >= 20 and s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
    assert LS.launch_live_state(s.con, "a.nearlytrade.near") is None
    s.close()


@pytest.mark.core
def test_a_pass_stores_the_state_with_its_version_and_fixed_minutes_once(db):
    cfg = C.load_config()
    res = LS.run(db, cfg)
    assert res["failed"] == 0 and res["computed"] == 3 and res["fixed"] > 0
    rows = {r[0]: r for r in db.con.execute("SELECT token, state, number, version, config_hash, source, asof_ns FROM launch_live_state")}
    assert set(rows) == {REFRAX, ISLAND, "coin-2.nearlytrade.near"}
    for token, (_, st, number, ver, ch, src, asof) in rows.items():
        assert st in ("ok", "no_trading") and ver == "ls-1" and ch == C.live_state_config_hash(cfg) and src == "live" and asof == LS.asof_now(db.con)
        assert (number is None) == (st == "no_trading")
    at = db.con.execute("SELECT minute, version, state FROM launch_live_state_at WHERE token = ? ORDER BY minute", (REFRAX,)).fetchall()
    assert [m for m, _, _ in at] == [5, 15, 60] and all(v == "ls-1" for _, v, _ in at)                # T+24 h is not over in the replay
    before = db.con.execute("SELECT COUNT(*), MAX(computed_at) FROM launch_live_state_at").fetchone()
    again = LS.run(db, cfg, now=time.time() + 5)
    assert again["fixed"] == 0 and db.con.execute("SELECT COUNT(*), MAX(computed_at) FROM launch_live_state_at").fetchone() == before       # kept, never rewritten
    # the fixed row at T+60 is the state at launch + 60 min, computed by the same function as "now"
    launch = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (REFRAX,)).fetchone()[0]
    direct = LS.compute_fixed(db, REFRAX, [60], "live", cfg, LS.Context())[60]
    stored = db.con.execute("SELECT number, parts, facts FROM launch_live_state_at WHERE token = ? AND minute = 60", (REFRAX,)).fetchone()
    assert (stored[0], json.loads(stored[1]), json.loads(stored[2])) == (direct["number"], direct["parts"], direct["facts"]) and direct["asof_ns"] == launch + 60 * MIN


@pytest.mark.core
def test_fixed_due_counts_the_longest_window_once_and_a_hole_keeps_the_shorter_ones(db, monkeypatch):
    cfg = C.load_config()
    asof = LS.asof_now(db.con)
    calls = []
    real = LS.S.window_covered

    def counting(store, height, cutoff):
        calls.append(cutoff)
        return real(store, height, cutoff)

    monkeypatch.setattr(LS.S, "window_covered", counting)
    due = {t: m for t, m, _ in LS.fixed_due(db, cfg, asof)}
    assert due[REFRAX] == [5, 15, 60]
    # one count per launch when the longest passed minute is covered (the shorter windows lie inside it)
    n_launch = len(due)
    assert len(calls) == n_launch
    # a hole in the long window: the longest one is skipped (and remembered), the shorter ones are still due
    launch = db.con.execute("SELECT ts_ns FROM launches WHERE token = ?", (REFRAX,)).fetchone()[0]
    monkeypatch.setattr(LS.S, "window_covered", lambda store, height, cutoff: cutoff <= launch + 15 * MIN)
    ctx = LS.Context()
    got = {t: m for t, m, _ in LS.fixed_due(db, cfg, asof, ctx, now=1000.0)}
    assert got[REFRAX] == [5, 15] and ctx.skip[(REFRAX, 60)] == 1000.0 + LS.SKIP_S


@pytest.mark.core
def test_a_new_version_adds_its_own_fixed_rows_and_recomputes_now(db):
    cfg = C.load_config()
    LS.run(db, cfg)
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["live_state"]["version"] = "ls-2"
    assert LS.due(db, cfg2, LS.asof_now(db.con)) and not LS.due(db, cfg, LS.asof_now(db.con), now=time.time())        # the other version is due at once, the same one is not
    LS.run(db, cfg2)
    assert {v for (v,) in db.con.execute("SELECT DISTINCT version FROM launch_live_state")} == {"ls-2"}
    assert {v for (v,) in db.con.execute("SELECT DISTINCT version FROM launch_live_state_at")} == {"ls-1", "ls-2"}


@pytest.mark.core
def test_the_schedule_every_minute_while_trading_hourly_after_that_daily_when_dormant(db):
    cfg = C.load_config()
    LS.run(db, cfg)
    asof = LS.asof_now(db.con) + 400 * HOUR                                                  # a clock far from the replay's swaps: no pool is "active" by itself
    now = time.time()
    db.con.execute("UPDATE launch_live_state SET last_trade_ns = ?, computed_at = ?", (asof - 5 * MIN, LS._iso_ago(30, now)))        # traded 5 min ago, computed 30 s ago
    db.con.commit()
    assert LS.due(db, cfg, asof, now) == []
    db.con.execute("UPDATE launch_live_state SET computed_at = ?", (LS._iso_ago(61, now),))
    db.con.commit()
    assert len(LS.due(db, cfg, asof, now)) == 3                                              # active: every minute
    db.con.execute("UPDATE launch_live_state SET last_trade_ns = ?, computed_at = ?", (asof - 3 * HOUR, LS._iso_ago(61, now)))
    db.con.commit()
    assert LS.due(db, cfg, asof, now) == []                                                  # traded 3 h ago: hourly
    db.con.execute("UPDATE launch_live_state SET computed_at = ?", (LS._iso_ago(3601, now),))
    db.con.commit()
    assert len(LS.due(db, cfg, asof, now)) == 3
    db.con.execute("UPDATE launch_live_state SET last_trade_ns = ?, computed_at = ?", (asof - 30 * HOUR, LS._iso_ago(7200, now)))
    db.con.commit()
    assert [t for t in LS.due(db, cfg, asof, now)] == []                                     # quiet for over a day: daily
    db.con.execute("UPDATE launch_live_state SET computed_at = ?", (LS._iso_ago(86401, now),))
    db.con.commit()
    assert len(LS.due(db, cfg, asof, now)) == 3
    db.con.execute("DELETE FROM launch_live_state WHERE token = ?", (REFRAX,))
    db.con.commit()
    assert REFRAX in LS.due(db, cfg, asof, now) and LS.due(db, cfg, asof, now)[0] == REFRAX       # never computed: first


@pytest.mark.core
def test_a_new_trade_brings_a_dormant_launch_back_at_once(db):
    cfg = C.load_config()
    asof = LS.asof_now(db.con)
    LS.run(db, cfg)
    now = time.time()
    tokens = {REFRAX, ISLAND, "coin-2.nearlytrade.near"}
    window = LS.active_tokens(db, asof - 60 * MIN)
    assert window == tokens                                                                  # the replay trades in its last hour on all three pools
    # stored as dormant (the last trade 30 h ago) and computed an hour ago: by its own interval it would wait a day, but its pool just traded
    db.con.execute("UPDATE launch_live_state SET last_trade_ns = ?, computed_at = ?", (asof - 30 * HOUR, LS._iso_ago(3600, now)))
    db.con.commit()
    assert set(LS.due(db, cfg, asof, now)) == tokens
    db.con.execute("UPDATE launch_live_state SET computed_at = ?", (LS._iso_ago(30, now),))
    db.con.commit()
    assert LS.due(db, cfg, asof, now) == []                                                  # recomputed 30 s ago: the active interval (60 s) has not passed
    later = asof + 400 * HOUR                                                                # the same rows with no trade in the last hour stay dormant
    db.con.execute("UPDATE launch_live_state SET computed_at = ?", (LS._iso_ago(3600, now),))
    db.con.commit()
    assert LS.due(db, cfg, later, now) == []


@pytest.mark.core
def test_a_history_launch_gets_the_gap_state_never_a_number(db):
    db.con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = ?", (ISLAND,))
    db.con.commit()
    LS.run(db, C.load_config(), only=[ISLAND])
    st = LS.launch_live_state(db.con, ISLAND)
    assert st["state"] == "history_gap" and st["number"] is None and st["parts"] == [] and st["source"] == "history" and st["version"] == "ls-1"
    assert st["facts"]["gap_to_ns"] == LS.live_from_of("2026-10-30 12:14")
    assert db.con.execute("SELECT COUNT(*) FROM launch_live_state_at WHERE token = ?", (ISLAND,)).fetchone()[0] == 0       # not backfilled: no fixed rows, no number anywhere


@pytest.mark.core
def test_a_failing_launch_is_counted_and_does_not_stop_the_pass(db, monkeypatch):
    real = LS.compute_token

    def boom(store, token, *a, **k):
        if token == ISLAND:
            raise RuntimeError("boom")
        return real(store, token, *a, **k)

    monkeypatch.setattr(LS, "compute_token", boom)
    res = LS.run(db, C.load_config())
    assert res["failed"] == 1 and res["computed"] == 2 and ISLAND in res["failed_tokens"][0]
    assert LS.launch_live_state(db.con, ISLAND) is None and LS.launch_live_state(db.con, REFRAX) is not None


@pytest.mark.core
def test_the_hook_never_raises_and_remembers_its_error(db, monkeypatch):
    logs = []
    monkeypatch.setattr(LS, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert LS.refresh(db, logs.append) is None
    assert db.get_status()["live_state_last_error"]["error"].startswith("RuntimeError: down") and any("down" in m for m in logs)
    monkeypatch.undo()
    res = LS.refresh(db, logs.append)
    assert res and res["computed"] == 3 and db.get_status()["live_state_built"]["computed"] == 3 and db.get_status()["live_state_last_error"] is None
    old = open_store(db.con.execute("PRAGMA database_list").fetchone()[2])
    old.con.execute("PRAGMA user_version=19")
    assert LS.refresh(old, logs.append) is None                                              # a database that is not migrated yet: nothing to do


@pytest.mark.core
def test_the_collector_runs_the_pass_between_blocks_at_most_every_interval(db, monkeypatch):
    from rugyomi import collector as CO
    from rugyomi.collector import Collector
    calls = []
    monkeypatch.setattr(LS, "refresh", lambda store, log, ctx=None, now=None: calls.append(1))
    c = Collector.__new__(Collector)
    c.store, c.log, c._live_state_checked, c._live_state_ctx = db, lambda m: None, 0.0, LS.Context()
    c._live_state_tick()
    c._live_state_tick()
    assert calls == [1] and CO.LIVE_STATE_CHECK_S == LS.CHECK_EVERY_S


# ---------------------------------------------------------------------------------------------- the pages
def test_the_launch_page_shows_the_block_with_as_of_the_version_and_the_four_parts(db):
    LS.run(db, C.load_config())
    st = LS.launch_live_state(db.con, REFRAX)
    with TestClient(webapp.create_app(path_of(db))) as c:
        r = c.get(f"/launch/{REFRAX}")
        t = " ".join(r.text.split())
        assert r.status_code == 200 and 'id="live-state"' in r.text and "Live state" in t and "not a forecast" in t and "as of" in t and " UTC" in t
        if st["state"] == "ok":
            assert f"formula {st['version']}" in t and f">{st['number']}</strong>" in r.text.replace(" ", "")  or f"{st['number']}</strong>" in r.text
        for title in ("Buyers in profit", "Buy pressure", "Price vs peak", "Activity"):
            assert title in r.text or title.replace("&#39;", "'") in r.text, title
        for label in ("Last trade", "Price now", "Who still holds the supply", "Dev account"):
            assert label in t, label
        low = t.lower()
        assert "health" not in low and "safe" not in low and "forecast" in low
        m = c.get("/methodology")
        assert 'id="live-state"' in m.text and LS.describe(C.load_config())["parts"][3]["description"] in " ".join(m.text.split()).replace("&lt;", "<")
        assert "ls-1" in m.text


def test_a_no_trading_launch_says_so_and_a_history_launch_says_the_history_is_incomplete(db):
    cfg = C.load_config()
    db.con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = ?", (ISLAND,))
    db.con.commit()
    LS.run(db, cfg)
    asof = LS.asof_now(db.con)
    db.con.execute("UPDATE launch_live_state SET state = 'no_trading', number = NULL, last_trade_ns = ? WHERE token = ?", (asof - 30 * HOUR, REFRAX))
    db.con.commit()
    with TestClient(webapp.create_app(path_of(db))) as c:
        t = " ".join(c.get(f"/launch/{REFRAX}").text.split())
        assert "No trading" in t and "the last trade was" in t and "out of 100" not in t
        t = " ".join(c.get(f"/launch/{ISLAND}").text.split())
        assert "Live state: history incomplete before 2026-10-30" in t and "no number is shown" in t and "out of 100" not in t
        assert "Who still holds the supply" not in t and "Price now" not in t                    # nothing that needs the missing trades
        assert "Last trade" in t


def test_a_launch_without_a_stored_state_or_a_database_without_the_tables_still_serves_the_page(db, tmp_path):
    with TestClient(webapp.create_app(path_of(db))) as c:
        r = c.get(f"/launch/{REFRAX}")
        assert r.status_code == 200 and 'id="live-state"' not in r.text
    p = tmp_path / "n.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a.near','a.near','1',1,1,'x')")
    s.con.execute("DROP TABLE launch_live_state")
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(p)) as c:
        r = c.get("/launch/a.nearlytrade.near")
        assert r.status_code == 200 and 'id="live-state"' not in r.text


def test_the_page_does_not_change_the_card_or_the_feed(db):
    """The cards table has no live-state column and the feed (the channel's shape) does not mention it."""
    LS.run(db, C.load_config())
    cols = {r[1] for r in db.con.execute("PRAGMA table_info(cards)")}
    assert not any("live" in c for c in cols)
    with TestClient(webapp.create_app(path_of(db))) as c:
        assert "Live state" not in c.get("/").text
