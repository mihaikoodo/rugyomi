"""RGY-016: the T+24 h snapshot, the price peak, the insider set and the rug label (`rules.rug_labels`).

Synthetic launches on the helpers of test_snapshots (a nearly launch by DEV: dev buy 990 tokens for 10 NEAR through
lock_3, one outside buyer 148 tokens for 2 NEAR) with the dev / wallets selling at chosen times, plus the real 29.10
blocks (REFRAX, ISLAND, COIN) for the reference launch."""
import copy
import json
import threading

import pytest

from rugyomi_testkit import block, hid, refrax_replay_blocks
from rugyomi import rules, snapshots as S
from rugyomi.collector import Collector
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from test_snapshots import DEV, NEAR, POOL, TOK, ft_reo, scenario, swap_reo  # noqa: F401 — helpers of the same fixture family
from test_snapshots import at, run, snap

REFRAX, ISLAND, COIN = "refrax.nearlytrade.near", "island.nearlytrade.near", "coin-2.nearlytrade.near"
MIN = 60 * 10**9
H = {"n": 1000}


def sell(who, tokens, near, sec, tag):
    """The pool sell of `who`: the token leg to the pool and the swap (own tx), two blocks."""
    h = H["n"]
    H["n"] += 2
    tx = hid("sell", tag)
    return [(h, block(h, [ft_reo(tx, 0, who, "dclv2.ref-labs.near", tokens)], at(sec))),
            (h + 1, block(h + 1, [swap_reo(tx, 0, who, TOK, tokens, "wrap.near", near)], at(sec + 0.5)))]


def buy(who, tokens, near, sec, tag):
    h = H["n"]
    H["n"] += 2
    tx = hid("buy", tag)
    return [(h, block(h, [swap_reo(tx, 0, "router.near", "wrap.near", near, TOK, tokens)], at(sec))),
            (h + 1, block(h + 1, [ft_reo(tx, 0, "dclv2.ref-labs.near", who, tokens)], at(sec + 0.5)))]


def move(src, dst, tokens, sec, tag):
    h = H["n"]
    H["n"] += 1
    return [(h, block(h, [ft_reo(hid("mv", tag), 0, src, dst, tokens)], at(sec)))]


def launch_blocks(with_buyer=True):
    """The launch, the dev buy (990 tokens for 10 NEAR) and optionally one outside buyer (148 tokens for 2 NEAR)."""
    base = scenario()
    keep = base[:5] if with_buyer else base[:3]
    return [b for b in keep]


def world(extra, with_buyer=True, tmp_path=None, end_h=24 * 60 + 5):
    """Run the launch + `extra` blocks + empty blocks at T+61 min, T+24 h and T+25 h (the cutoffs are reached)."""
    blocks = sorted(launch_blocks(with_buyer) + list(extra), key=lambda x: x[1]["header"]["timestamp_nanosec"] if False else x[0])
    last = blocks[-1][0]
    tail = [(last + 1, block(last + 1, [], at(61 * 60))), (last + 2, block(last + 2, [], at(24 * 3600 + 30))),
            (last + 3, block(last + 3, [], at(25 * 3600)))]
    return run(blocks + tail, tmp_path)


def rows(store, token=TOK):
    cols = ("minute", "at_ts_ns", "error", "metric_version", "buyers_count", "buyers_pnl", "price_last", "price_peak",
            "peak_at", "insiders_count", "insiders_acquired", "insiders_sold", "insiders_sold_share_bps",
            "insiders_pnl", "insiders_evidence", "dev_sold_share_bps")
    return [dict(zip(cols, r)) for r in store.con.execute(
        f"SELECT {','.join(cols)} FROM launch_snapshots WHERE token=? ORDER BY minute", (token,))]


# ---------------------------------------------------------------- the T+24 h snapshot and the peak

def test_t24h_snapshot_exists_with_the_peak_and_is_only_computed_once_its_window_is_covered(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    r = rows(s)
    assert [x["minute"] for x in r] == [5, 15, 30, 60, 1440] and all(x["error"] is None for x in r)
    assert all(x["metric_version"] == S.METRIC_VERSION == 3 for x in r)
    last = r[-1]
    assert last["at_ts_ns"] == S._launch(s, TOK)["ts_ns"] + 1440 * MIN
    # the highest price is the outside buyer's (2 NEAR / 150 tokens), at the block time of that trade
    assert last["price_peak"].startswith("13333333333333333333333")
    assert last["peak_at"] == at(10) and last["peak_at"] <= last["at_ts_ns"]
    # a gap inside the window (after T+60) -> no T+24 h row; the RGY-012 coverage guard also covers the long window
    blocks = launch_blocks() + sell(DEV, 990, 6 * NEAR, 20, "a2")
    last = blocks[-1][0]
    blocks += [(last + 1, block(last + 1, [], at(61 * 60))), (last + 5000, block(last + 5000, [], at(25 * 3600)))]
    s2 = open_store(tmp_path / "gap.db")                            # `run` stores skipped heights as null: do it by hand
    c = Collector(s2, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    prev = None
    for h, b in blocks:
        for g in range(prev + 1 if prev is not None and h < last + 5000 else h, h):
            c.process(g, None)
        c.process(h, b)
        prev = h
    assert [x["minute"] for x in rows(s2)] == [5, 15, 30, 60]       # heights last+2 .. last+4999 were never seen


def test_compute_due_writes_a_bounded_number_of_t24h_rows_per_block(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "LATE_PER_BLOCK", 1)
    toks = ["t1.nearlytrade.near", "t2.nearlytrade.near"]
    blocks, h = [], 100
    for i, t in enumerate(toks):
        L = hid("l", t)
        from test_snapshots import ev, reo, NEARLY
        launch = reo(hid("le", t), L, f"dev{i}.near", NEARLY, [], logs=[ev("nearly", "launch_started", {
            "creator": f"dev{i}.near", "token": t, "pool_id": f"{t}|wrap.near|10000", "dev_buy": str(NEAR)})],
            executor=NEARLY)
        blocks.append((h, block(h, [launch], at(i))))
        h += 1
    blocks += [(h, block(h, [], at(25 * 3600))), (h + 1, block(h + 1, [], at(25 * 3600 + 1))),
               (h + 2, block(h + 2, [], at(25 * 3600 + 2)))]
    s = run(blocks, tmp_path)
    counts = [s.con.execute("SELECT COUNT(*) FROM launch_snapshots WHERE minute=1440").fetchone()[0]]
    assert counts == [2]                                       # one per block: all arrived by the third block
    # and after the first block past the cutoff only one existed (checked by running one block less)
    s2 = run(blocks[:-2], tmp_path / "b")
    assert s2.con.execute("SELECT COUNT(*) FROM launch_snapshots WHERE minute=1440").fetchone()[0] == 1


# ---------------------------------------------------------------- the insider set

def test_insiders_include_a_fresh_wallet_the_dev_sent_tokens_to_which_then_sells(tmp_path):
    extra = move(DEV, "fresh.near", 800, 10, "m1") + sell("fresh.near", 800, NEAR // 100, 90 * 60, "f1")
    s = world(extra, tmp_path=tmp_path)
    r5, r60, r24 = (next(x for x in rows(s) if x["minute"] == m) for m in (5, 60, 1440))
    assert r5["insiders_count"] == 2 and r5["insiders_sold"] == "0"            # the wallet exists from T+5, has not sold
    assert r60["insiders_sold"] == "0" and r60["dev_sold_share_bps"] == 0
    assert r24["insiders_count"] == 2 and r24["insiders_sold"] == "800"
    assert r24["insiders_sold_share_bps"] == 800 * 10000 // 990              # the dev alone sold nothing
    assert r24["dev_sold_share_bps"] == 0                                      # ... the old dev-only metric misses it
    ev = json.loads(r24["insiders_evidence"])
    assert ev["sellers"] == [{"account": "fresh.near", "role": "recipient", "sold": "800"}]
    assert ev["recipients"] == [{"account": "fresh.near", "received": "800"}]
    assert ev["first_sell_ts_ns"] == at(90 * 60 + 0.5) or ev["first_sell_ts_ns"] == at(90 * 60)
    assert r24["buyers_count"] == 1                                            # buyer.near only; fresh.near is no buyer
    lab = rules.rug_labels(rows(s))
    assert lab["rug_fast"]["value"] is False and lab["rug_late"]["value"] is True and lab["rug"]["value"] is True
    assert lab["rug_late"]["evidence"]["sellers"][0]["account"] == "fresh.near"
    assert lab["rug_late"]["evidence"]["crossed_after_s"] > 3600               # crossed 80 % after T+60
    assert lab["severity_near"] == pytest.approx(2.0, abs=0.05)               # the buyer paid 2 NEAR, tokens now worth ~0


def test_a_recipient_who_also_bought_from_the_pool_is_an_insider_and_not_a_buyer(tmp_path):
    extra = buy("alt.near", 50, NEAR, 20, "ab") + move(DEV, "alt.near", 10, 30, "m2")
    s = world(extra, tmp_path=tmp_path)
    r5 = next(x for x in rows(s) if x["minute"] == 5)
    assert r5["insiders_count"] == 2 and r5["buyers_count"] == 1             # buyer.near only
    assert r5["insiders_acquired"] == str(990 + 50)


def test_service_accounts_and_the_pool_are_never_insiders(tmp_path):
    # the base scenario has dev -> alt.near; a transfer to the token contract (tax), the pool and lock_3 must not add
    extra = move(DEV, TOK, 3, 15, "x1") + move(DEV, "dclv2.ref-labs.near", 4, 16, "x2") + move(DEV, "lock_3.nearlytrade.near", 5, 17, "x3")
    s = world(extra, tmp_path=tmp_path)
    assert next(x for x in rows(s) if x["minute"] == 5)["insiders_count"] == 1


def test_the_insider_set_is_recomputed_per_snapshot(tmp_path):
    s = world(move(DEV, "late.near", 5, 45 * 60, "m3"), tmp_path=tmp_path)
    counts = {x["minute"]: x["insiders_count"] for x in rows(s)}
    assert counts == {5: 1, 15: 1, 30: 1, 60: 2, 1440: 2}


# ---------------------------------------------------------------- the labels

def test_rug_fast_the_dev_sells_everything_in_seconds(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    lab = rules.rug_labels(rows(s))
    assert lab["rug_fast"]["value"] is True and lab["rug_late"] == {"value": False, "reason": "already rug_fast", "evidence": {}}
    assert lab["rug"]["value"] is True and lab["severity_near"] > 0.1
    e = lab["rug_fast"]["evidence"]
    assert e["sellers"][0]["account"] == DEV and e["crossed_after_s"] < 60 and e["minute"] == 60


def test_rug_late_a_dump_after_t60_with_a_price_collapse(tmp_path):
    s = world(sell(DEV, 300, 3 * NEAR, 20, "e1") + sell(DEV, 650, NEAR // 100, 3 * 3600, "e2"), tmp_path=tmp_path)
    r = rows(s)
    assert next(x for x in r if x["minute"] == 60)["insiders_sold_share_bps"] < 8000
    lab = rules.rug_labels(r)
    assert lab["rug_fast"]["value"] is False
    assert lab["rug_late"]["value"] is True, lab["rug_late"]
    assert lab["rug"]["value"] is True and lab["severity_near"] > 1.5
    assert lab["rug_late"]["evidence"]["price_vs_peak"] <= 0.1
    assert lab["rug_late"]["evidence"]["crossed_after_s"] > 3600


def test_price_recovery_is_not_a_late_rug(tmp_path):
    # the dev sells 85 % after T+60 into a falling price, then a new buyer lifts the price back to ~half of the peak
    extra = (sell(DEV, 840, 2 * NEAR, 2 * 3600, "r1") + buy("big.near", 100, 5 * NEAR // 10, 5 * 3600, "r2"))
    s = world(extra, tmp_path=tmp_path)
    r = rows(s)
    last = r[-1]
    assert last["insiders_sold_share_bps"] >= 8000 and int(last["buyers_pnl"]) < -NEAR // 10    # sold out, buyers lost
    lab = rules.rug_labels(r)
    assert lab["rug_late"]["value"] is False and "price held" in lab["rug_late"]["reason"]
    assert lab["rug"]["value"] is False and lab["severity_near"] is None


def test_no_outside_buyers_is_not_a_rug(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "n1"), with_buyer=False, tmp_path=tmp_path)
    r = rows(s)
    assert all(x["buyers_count"] == 0 for x in r) and r[-1]["insiders_sold_share_bps"] >= 8000
    lab = rules.rug_labels(r)
    assert lab["rug_fast"] == {"value": False, "reason": "no outside buyers", "evidence": lab["rug_fast"]["evidence"]}
    assert lab["rug"]["value"] is False and lab["severity_near"] is None


def test_selling_alone_or_dev_exit_alone_is_not_a_rug(tmp_path):
    # the dev sells 100 % at a PROFIT for the buyers (price above their entry): dev_exit is true, no rug
    s = world(sell(DEV, 990, 30 * NEAR, 20, "p1"), tmp_path=tmp_path)
    r = rows(s)
    assert rules.outcome_labels(r)["dev_exit"]["value"] is True
    lab = rules.rug_labels(r)
    assert int(next(x for x in r if x["minute"] == 60)["buyers_pnl"]) > 0
    assert lab["rug_fast"]["value"] is False and lab["rug"]["value"] is False


def test_insiders_past_the_threshold_by_t60_but_buyers_hurt_only_later_is_rug_late(tmp_path):
    """RGY-017, variant B: the Q-007 gap case. Insiders sold 85% in the first minute at a price above the buyer's entry
    (the buyers are still in profit at T+60), the buyer is then wiped out and the price collapses by T+24 h."""
    extra = sell(DEV, 840, 15 * NEAR, 30, "g1") + sell("buyer.near", 148, NEAR // 100, 4 * 3600, "g2")
    s = world(extra, tmp_path=tmp_path)
    r = rows(s)
    assert int(next(x for x in r if x["minute"] == 60)["buyers_pnl"]) > 0 and int(r[-1]["buyers_pnl"]) < 0
    lab = rules.rug_labels(r)
    assert lab["rug_fast"]["value"] is False
    assert lab["rug_late"]["value"] is True and "gap" not in lab["rug_late"]
    assert lab["rug_late"]["evidence"]["crossed_after_s"] < 60              # crossed early: no longer a condition
    assert lab["rug"]["value"] is True and lab["severity_near"] > 1.5


def test_labels_are_pending_or_unknown_until_the_snapshots_exist():
    ok = {"minute": 5, "at_ts_ns": 5 * MIN, "error": None, "insiders_acquired": "10", "buyers_pnl": "0", "buyers_count": 1,
          "insiders_sold_share_bps": 0, "insiders_count": 1, "insiders_pnl": "0", "insiders_evidence": "{}",
          "price_last": "1", "price_peak": "1"}
    assert rules.rug_labels([])["rug"] == {"value": None, "reason": "pending", "evidence": {}}
    only5 = rules.rug_labels([ok])
    assert only5["rug"]["value"] is None and only5["rug_fast"]["reason"] == "pending" and only5["severity_near"] is None
    s60 = {**ok, "minute": 60, "at_ts_ns": 60 * MIN}
    no_24 = rules.rug_labels([s60])
    assert no_24["rug_fast"]["value"] is False and no_24["rug_late"]["value"] is None and no_24["rug"]["value"] is None
    err = rules.rug_labels([{**s60, "error": "boom"}])
    assert err["rug_fast"] == {"value": None, "reason": "snapshot error", "evidence": {}} and err["rug"]["value"] is None
    old = rules.rug_labels([{**s60, "insiders_acquired": None}])           # a row computed before v8
    assert old["rug_fast"]["value"] is None and "rebuild-snapshots" in old["rug_fast"]["reason"]


def test_thresholds_come_from_the_config(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    r = rows(s)
    cfg = rules.default_config()
    assert set(cfg["rug"]) == {"insiders_sold_bps", "buyers_loss_near", "price_max_share", "fast_minute", "late_minute"}
    assert rules.rug_labels(r, cfg)["rug"]["value"] is True
    hard = copy.deepcopy(cfg)
    hard["rug"]["buyers_loss_near"] = 100                                   # losses must exceed 100 NEAR
    assert rules.rug_labels(r, hard)["rug"]["value"] is False
    soft = copy.deepcopy(cfg)
    soft["rug"]["insiders_sold_bps"] = 10_001
    assert rules.rug_labels(r, soft)["rug"]["value"] is False
    over = tmp_path / "o.json"
    over.write_text(json.dumps({"rug": {"insiders_sold_bps": 9000}}))
    assert rules.config.load_config(over)["rug"]["insiders_sold_bps"] == 9000
    over.write_text(json.dumps({"rug": {"insiders_sold": 9000}}))
    with pytest.raises(rules.ConfigError):
        rules.config.load_config(over)


# ---------------------------------------------------------------- the real blocks: REFRAX is a fast rug

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    s = open_store(tmp_path_factory.mktemp("rug") / "r.db")
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in refrax_replay_blocks():
        c.process(h, b)
    yield s
    s.close()


def test_refrax_is_rug_fast_on_the_real_blocks(real):
    r = rows(real, REFRAX)
    assert [x["minute"] for x in r] == [5, 15, 30, 60] and all(x["error"] is None for x in r)
    lab = rules.rug_labels(r)
    assert lab["rug_fast"]["value"] is True and lab["rug"]["value"] is True
    assert lab["rug_late"]["value"] is False
    assert lab["severity_near"] == pytest.approx(2.53, abs=0.02)             # the outside buyers' loss (planner: -2.53)
    e = lab["rug_fast"]["evidence"]
    assert e["sellers"][0]["account"] == "thecoins.near" and e["insiders_sold_bps"] == 10000
    assert e["crossed_after_s"] < 120 and e["crossed_tx"]
    assert e["buyers_count"] == 2
    # the buyers' number is the same as before v8 where no insider other than the dev exists (RGY-006 recount stays true)
    assert r[-1]["insiders_count"] == 1 and r[-1]["insiders_sold_share_bps"] == r[-1]["dev_sold_share_bps"]


def test_island_and_coin_labels_on_the_real_blocks(real):
    for tok in (ISLAND, COIN):
        lab = rules.rug_labels(rows(real, tok))
        assert lab["rug_fast"]["value"] is True and lab["rug"]["value"] is True and lab["severity_near"] > 2


def test_pending_in_a_replay_that_ends_before_t24h(real):
    lab = rules.rug_labels(rows(real, REFRAX))
    assert lab["rug_late"]["value"] is False                                  # already rug_fast
    # a non-rug launch of the same window would stay pending for T+24 h; the label function is pure, so cut the rows
    cut = [x for x in rows(real, REFRAX) if x["minute"] < 60]
    assert rules.rug_labels(cut)["rug"]["value"] is None


# ---------------------------------------------------------------- rebuild-snapshots fills T+24 h and the peak

def test_rebuild_snapshots_fills_the_t24h_row_and_the_insider_columns(tmp_path):
    s = world(sell(DEV, 990, 6 * NEAR, 20, "a"), tmp_path=tmp_path)
    before = rows(s)
    s.begin()
    s.con.execute("DELETE FROM launch_snapshots WHERE minute = 1440")                    # as on a v7 database after the migration
    s.con.execute("UPDATE launch_snapshots SET price_peak = NULL, peak_at = NULL, insiders_count = NULL, "
                  "insiders_acquired = NULL, insiders_sold = NULL, insiders_sold_share_bps = NULL, "
                  "insiders_pnl = NULL, insiders_evidence = NULL, metric_version = 1")
    s.commit()
    assert rules.rug_labels(rows(s))["rug"]["value"] is None
    res = S.rebuild_snapshots(s)
    assert res["error"] == 0 and res["ok"] == 5 and res["not_due"] == 0 and res["not_covered"] == 0
    after = rows(s)
    assert [x["minute"] for x in after] == [5, 15, 30, 60, 1440]
    assert after == before                                                              # the same numbers as the live run
    assert rules.rug_labels(after)["rug"]["value"] is True
    # the CLI path
    from rugyomi import collector
    assert collector.main(["rebuild-snapshots", "--db", str(s.path)]) in (0, None)
