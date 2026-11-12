"""RGY-036: who holds the supply of a launch at T+5 / T+15 / T+60.

The rules are pinned on small synthetic trades (every row, the priority of the rows, never-negative holdings, the supply that cannot be told, fresh /
unknown age, the bundle-adjusted top-10); the numbers on the REFRAX / ISLAND / COIN-2 replay (the same trades the snapshots use); the table, the CLI,
the hook and the pages on the replay."""
import json
import re
import shutil
import threading
from pathlib import Path

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import holdings as HO  # noqa: E402
from rugyomi import rules  # noqa: E402
from rugyomi.collector import Collector, main as collector_main  # noqa: E402
from rugyomi.keys import EnvKeySource  # noqa: E402
from rugyomi.neardata import Neardata  # noqa: E402
from rugyomi.parse import DCL  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.store.schema import MIGRATIONS  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi_testkit import refrax_replay_blocks, refrax_world_db  # noqa: E402

NS, MIN = 10**9, 60 * 10**9
ISLAND, COIN, REFRAX = "island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near"
CFG = C.load_config(profile="v1")
TOK, DEV, T0, SUPPLY = "x.nearlytrade.near", "dev.near", 1_000 * NS, 1000
FRESH_S = 24 * 3600


# ---------------------------------------------------------------------------------------------- synthetic trades
class Trades:
    """Legs and swaps the attribution reads: a buy = the pool's leg to the buyer + the swap; a sell = the seller's leg to the pool + the swap."""

    def __init__(self, supply: int | None = SUPPLY, dev_buy: int = 0):
        self.legs: list[dict] = []
        self.swaps: list[dict] = []
        self.n = 0
        if supply is not None:
            self.legs.append({"id": -1, "kind": "ft_mint", "ts": T0, "tx": "launchtx", "a": "lock_1.nearlytrade.near", "b": None, "amount": supply})
        if dev_buy:
            self.buy(DEV, dev_buy, 0)

    def _id(self):
        self.n += 1
        return self.n

    def buy(self, who: str, tokens: int, at_min: float):
        i, ts = self._id(), T0 + int(at_min * MIN) + self.n
        self.legs.append({"id": 2 * i, "kind": "ft_transfer", "ts": ts, "tx": f"t{i}", "a": DCL, "b": who, "amount": tokens})
        self.swaps.append({"id": 2 * i + 1, "ts": ts, "tx": f"t{i}", "swapper": "lock_1.nearlytrade.near", "token_in": "wrap.near", "token_out": TOK,
                           "amount_in": tokens * 10, "amount_out": tokens})

    def sell(self, who: str, tokens: int, at_min: float):
        i, ts = self._id(), T0 + int(at_min * MIN) + self.n
        self.legs.append({"id": 2 * i, "kind": "ft_transfer", "ts": ts, "tx": f"t{i}", "a": who, "b": DCL, "amount": tokens})
        self.swaps.append({"id": 2 * i + 1, "ts": ts, "tx": f"t{i}", "swapper": who, "token_in": TOK, "token_out": "wrap.near",
                           "amount_in": tokens, "amount_out": tokens * 10})

    def send(self, a: str, b: str, tokens: int, at_min: float):
        i = self._id()
        self.legs.append({"id": 2 * i, "kind": "ft_transfer", "ts": T0 + int(at_min * MIN) + self.n, "tx": f"s{i}", "a": a, "b": b, "amount": tokens})


LAUNCH = {"token": TOK, "owner": DEV, "creator": DEV, "ts_ns": T0, "tx": "launchtx"}


def early_of(*wallets):
    return [{"buyer": w, "first_buy_ns": T0 + (i + 1) * NS} for i, w in enumerate(wallets)]


def run(t: Trades, early, tags=None, ages=None, minute=60):
    return HO.compute_minute(LAUNCH, t.legs, t.swaps, minute, early, tags or {}, ages or {}, FRESH_S)


def row(res, key):
    return next(r for r in res["rows"] if r["key"] == key)


def held(res, key):
    return int(row(res, key)["held"])


# ---------------------------------------------------------------------------------------------- the rules
@pytest.mark.core
def test_every_wallet_is_counted_once_in_the_first_row_that_applies_and_the_rows_add_up():
    t = Trades(dev_buy=100)
    t.sell(DEV, 40, 1)
    t.send(DEV, "recv.near", 30, 2)                        # dev held = 100 - 40 - 30; recv.near holds the 30
    for w, n in (("a.near", 50), ("b.near", 20), ("c.near", 10), ("d.near", 7), ("e.near", 6), ("f.near", 5), ("g.near", 4)):
        t.buy(w, n, 3)
    t.sell("b.near", 5, 4)
    t.sell("g.near", 10, 4)                                 # sells more than it bought (the tax leg is in the gross): held is 0, never negative
    tags = {"a.near": {"dev_side": True, "bundle": "src.near", "group": "k", "trader_group": "k", "pair": True},     # applies to three rows: counted in dev_side only
            "b.near": {"bundle": "src.near", "pair": True},
            "c.near": {"pair": True, "group": "k", "trader_group": "k"}}
    ages = {"a.near": 5, "b.near": 5, "c.near": 5, "d.near": 3600, "e.near": None, "f.near": 90_000, "g.near": 90_000}
    res = run(t, early_of("a.near", "b.near", "c.near", "d.near", "e.near", "f.near", "g.near"), tags, ages)
    got = {r["key"]: (r["wallets"], int(r["held"])) for r in res["rows"]}
    assert got == {"dev": (1, 30), "insiders": (1, 30), "dev_side": (1, 50), "bundle": (1, 15), "group": (1, 10), "fresh": (1, 7),
                   "unknown_age": (1, 6), "other": (2, 5)}
    assert sum(w for w, _ in got.values()) == 9 and sum(h for _, h in got.values()) == 153
    assert res["supply"] == "1000" and res["supply_note"] == "ok"
    assert res["extra"]["dev_transferred_out"] == "30" and res["extra"]["early_wallets"] == 7 and res["extra"]["ages_known"] == 6
    assert all(int(r["held"]) >= 0 for r in res["rows"])


@pytest.mark.core
def test_an_early_buyer_who_got_tokens_from_the_dev_is_an_insider_not_an_early_buyer():
    t = Trades(dev_buy=100)
    t.buy("a.near", 10, 1)
    t.send(DEV, "a.near", 5, 2)
    res = run(t, early_of("a.near"))
    assert held(res, "insiders") == 15 and row(res, "insiders")["wallets"] == 1 and row(res, "other")["wallets"] == 0 and row(res, "unknown_age")["wallets"] == 0
    assert res["extra"]["early_wallets"] == 0


@pytest.mark.core
def test_only_what_happened_by_the_minute_counts():
    t = Trades(dev_buy=100)
    t.buy("a.near", 10, 3)
    t.buy("b.near", 20, 10)
    t.sell("a.near", 10, 12)
    early = early_of("a.near", "b.near")
    early[0]["first_buy_ns"], early[1]["first_buy_ns"] = T0 + 3 * MIN, T0 + 10 * MIN
    ages = {"a.near": None, "b.near": None}
    r5, r15, r60 = (run(t, early, None, ages, m) for m in (5, 15, 60))
    assert (held(r5, "unknown_age"), row(r5, "unknown_age")["wallets"]) == (10, 1)
    assert (held(r15, "unknown_age"), row(r15, "unknown_age")["wallets"]) == (20, 2)         # a sold out at minute 12: held 0 but still counted as a wallet
    assert row(r15, "unknown_age")["holding"] == 1 and held(r60, "unknown_age") == 20


@pytest.mark.core
def test_fresh_unknown_and_old_accounts_and_a_record_after_the_first_buy():
    t = Trades(dev_buy=10)
    for w in ("n.near", "u.near", "o.near", "edge.near"):
        t.buy(w, 1, 1)
    res = run(t, early_of("n.near", "u.near", "o.near", "edge.near"), None,
              {"n.near": FRESH_S - 1, "u.near": None, "o.near": FRESH_S + 1, "edge.near": FRESH_S})
    assert [row(res, k)["wallets"] for k in ("fresh", "unknown_age", "other")] == [1, 1, 2]            # exactly the threshold is not fresh


@pytest.mark.core
def test_load_ages_uses_stored_creation_data_and_never_guesses(tmp_path):
    s = open_store(tmp_path / "a.db")
    s.con.execute("INSERT INTO hist_accounts(account, creator, ts_ns, height, source, imported_at) VALUES('old.near','near',?,1,'t','x')", (T0 - 5 * 86400 * NS,))
    s.con.execute("INSERT INTO hist_accounts(account, creator, ts_ns, height, source, imported_at) VALUES('late.near','near',?,1,'t','x')", (T0 + 100 * NS,))
    s.con.commit()
    early = [{"buyer": w, "first_buy_ns": T0} for w in ("old.near", "late.near", "nobody.near")]
    got = HO.load_ages(s, early, {})
    assert got == {"old.near": 5 * 86400, "late.near": None, "nobody.near": None}                   # a record after the first buy is no age; no record is no age
    s.close()


@pytest.mark.core
def test_supply_unknown_or_smaller_than_the_rows_is_said_and_no_share_is_shown(tmp_path):
    t = Trades(supply=None, dev_buy=100)
    res = run(t, [])
    assert res["supply"] is None and res["supply_note"] == "no_mint"
    t = Trades(supply=50, dev_buy=100)                                                            # the rows hold more than was minted
    res = run(t, [])
    assert res["supply"] == "50" and res["supply_note"] == "below_holdings"
    s = open_store(tmp_path / "s.db")
    for m, r in ((5, run(Trades(supply=None, dev_buy=100), [], minute=5)), (15, run(Trades(supply=SUPPLY, dev_buy=100), [], minute=15))):
        s.con.execute("INSERT INTO launch_holdings(token, minute, supply, supply_note, rows, extra, computed_at) VALUES(?,?,?,?,?,?,'x')",
                      (TOK, m, r["supply"], r["supply_note"], json.dumps(r["rows"]), json.dumps(r["extra"])))
    d = HO.launch_holdings(s.con, TOK)
    m5, m15 = d["minutes"]
    assert not m5["shares_ok"] and m5["supply_note"] == "no_mint" and all(c["bps"] is None for c in d["rows"][0]["cells"][:1])
    assert m15["shares_ok"] and d["rows"][0]["cells"][1]["bps"] == 1000 and m15["rest_bps"] == 9000
    s.close()


@pytest.mark.core
def test_supply_is_the_net_of_mint_and_burn_by_the_cutoff():
    t = Trades(supply=1000)
    t.legs.append({"id": -2, "kind": "ft_mint", "ts": T0 + 10 * MIN, "tx": "later", "a": "lock_1.nearlytrade.near", "b": None, "amount": 500})
    t.legs.append({"id": -3, "kind": "ft_burn", "ts": T0 + 20 * MIN, "tx": "burn", "a": "lock_1.nearlytrade.near", "b": None, "amount": 100})
    assert [run(t, [], minute=m)["supply"] for m in (5, 15, 60)] == ["1000", "1500", "1400"]


@pytest.mark.core
def test_the_bundle_adjusted_top10_is_never_below_the_raw_one():
    t = Trades(supply=1000)
    ws = [f"w{i}.near" for i in range(12)]
    for w in ws:
        t.buy(w, 10, 1)
    tags = {w: {"bundle": "src.near"} for w in ws[:4]}
    res = run(t, early_of(*ws), tags, {w: None for w in ws})
    x = res["extra"]
    assert x["top10_bps"] == 10 * 10_000 // 12 and x["top10_adjusted_bps"] == 10_000          # four wallets are one holder: nine holders, all inside the top 10
    assert x["holders"] == 12 and x["holders_adjusted"] == 9 and x["merged_holders"] == 1
    assert x["top10_adjusted_bps"] >= x["top10_bps"]
    res = run(t, early_of(*ws), {}, {w: None for w in ws})                                      # no bundle: identical
    assert res["extra"]["top10_adjusted_bps"] == res["extra"]["top10_bps"] and res["extra"]["merged_holders"] == 0


@pytest.mark.core
def test_a_trader_group_is_one_holder_but_a_pair_is_not():
    t = Trades(supply=1000)
    ws = [f"w{i}.near" for i in range(12)]
    for w in ws:
        t.buy(w, 10, 1)
    res = run(t, early_of(*ws), {ws[0]: {"pair": True}, ws[1]: {"pair": True}}, {})
    assert res["extra"]["holders_adjusted"] == 12 and row(res, "group")["wallets"] == 2
    res = run(t, early_of(*ws), {w: {"group": "k", "trader_group": "k"} for w in ws[:3]}, {})
    assert res["extra"]["holders_adjusted"] == 10 and res["extra"]["merged_holders"] == 1


# ---------------------------------------------------------------------------------------------- the replay
@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    path = tmp_path_factory.mktemp("holdings") / "r.db"
    refrax_world_db(path)
    s = open_store(path)
    BU.rebuild(s)
    s.close()
    return path


@pytest.fixture
def db(replay, tmp_path):
    p = tmp_path / "h.db"
    shutil.copy(replay, p)
    s = open_store(p)
    yield s
    s.close()


@pytest.mark.core
def test_the_reference_launches_dev_holds_nothing_or_what_the_snapshots_say(db):
    out = HO.build(db)
    assert out["computed"] == 3 and out["failed"] == 0 and out["rows"] == 9 and out["supply_unknown"] == 0 and out["left"] == 0
    for tok in (ISLAND, COIN, REFRAX):
        d = HO.launch_holdings(db.con, tok)
        assert [m["minute"] for m in d["minutes"]] == [5, 15, 60] and all(m["supply"] == str(10**27) and m["shares_ok"] for m in d["minutes"])
        for m in (5, 15, 60):
            s = db.con.execute("SELECT dev_acquired, dev_sold, dev_transferred_out, holders_count, top10_share_bps FROM launch_snapshots WHERE token = ? AND minute = ?",
                               (tok, m)).fetchone()
            r = db.con.execute("SELECT rows, extra FROM launch_holdings WHERE token = ? AND minute = ?", (tok, m)).fetchone()
            dev = next(x for x in json.loads(r[0]) if x["key"] == "dev")
            assert int(dev["held"]) == max(0, int(s[0]) - int(s[1]) - int(s[2]))                       # the dev line is the snapshot's numbers
            ex = json.loads(r[1])
            assert (ex["holders"], ex["top10_bps"]) == (s[3], s[4])                                      # the raw top-10 is the snapshot's
    cells = {t: HO.launch_holdings(db.con, t)["rows"][0]["cells"] for t in (ISLAND, COIN, REFRAX)}
    assert [c["bps"] for c in cells[ISLAND]] == [0, 0, 0] and [c["bps"] for c in cells[COIN]] == [0, 0, 0]       # ISLAND / COIN-2: sold it all within a minute
    assert [c["bps"] for c in cells[REFRAX]] == [47, 47, 0] and [c["holding"] for c in cells[REFRAX]] == [1, 1, 0]   # REFRAX: sold 89 % in 36 s, the rest by T+60


@pytest.mark.core
def test_the_two_f005_wallets_are_early_buyers_with_unknown_age(db):
    HO.build(db)
    d = HO.launch_holdings(db.con, ISLAND)
    assert next(r for r in d["rows"] if r["key"] == "unknown_age")["cells"][0]["wallets"] == 2
    assert d["minutes"][0]["extra"]["early_wallets"] == 2 and d["minutes"][0]["extra"]["ages_known"] == 0


@pytest.mark.core
def test_the_build_is_resumable_idempotent_and_follows_its_parameters(db):
    assert HO.build(db, limit=1)["left"] == 2
    assert HO.build(db)["computed"] == 2
    first = db.con.execute("SELECT token, minute, supply, rows, extra FROM launch_holdings ORDER BY 1, 2").fetchall()
    assert HO.build(db)["computed"] == 0
    assert HO.build(db, force=True)["computed"] == 3
    assert db.con.execute("SELECT token, minute, supply, rows, extra FROM launch_holdings ORDER BY 1, 2").fetchall() == first
    other = json.loads(json.dumps(CFG))
    other["holdings"]["fresh_hours"] = 1
    assert HO.build(db, other)["computed"] == 3                                                      # other parameters: computed again
    other["holdings"]["minutes"] = [5, 30]
    HO.build(db, other)
    assert [r[0] for r in db.con.execute("SELECT minute FROM launch_holdings WHERE token = ? ORDER BY minute", (REFRAX,))] == [5, 30]


@pytest.mark.core
def test_a_launch_whose_buyers_are_not_final_or_have_no_trades_waits(db):
    db.con.execute("UPDATE launch_buyers_done SET final = 0 WHERE token = ?", (REFRAX,))
    db.con.execute("UPDATE launch_buyers_done SET source = 'none' WHERE token = ?", (COIN,))
    db.con.commit()
    assert [t for t, _ in HO.pending(db, CFG)] == [ISLAND]


@pytest.mark.core
def test_a_failing_launch_is_counted_and_does_not_stop_the_rest(db, monkeypatch):
    real = HO.compute_launch

    def boom(launch, *a, **k):
        if launch["token"] == COIN:
            raise RuntimeError("boom")
        return real(launch, *a, **k)

    monkeypatch.setattr(HO, "compute_launch", boom)
    out = HO.build(db)
    assert out["computed"] == 2 and out["failed"] == 1 and "boom" in out["failed_tokens"][0]
    assert db.con.execute("SELECT COUNT(DISTINCT token) FROM launch_holdings").fetchone()[0] == 2


@pytest.mark.core
def test_the_cli_prints_the_counts_and_the_hook_waits_for_the_first_manual_build(replay, tmp_path, capsys):
    p = tmp_path / "c.db"
    shutil.copy(replay, p)
    s = open_store(p)
    log = []
    assert HO.refresh(s, log.append) is None and s.con.execute("SELECT COUNT(*) FROM launch_holdings").fetchone()[0] == 0     # never the initial backlog
    s.close()
    capsys.readouterr()
    assert collector_main(["rebuild-holdings", "--db", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["computed"] == 3 and out["failed"] == 0 and out["left"] == 0
    s = open_store(p)
    assert HO.refresh(s, log.append) is None                                                         # built a moment ago: not due
    assert HO.refresh(s, log.append, force=True)["computed"] == 0 and not log
    s.close()


@pytest.mark.core
def test_the_hook_keeps_up_with_new_launches_and_never_raises(replay, tmp_path, monkeypatch):
    p = tmp_path / "k.db"
    shutil.copy(replay, p)
    s = open_store(p)
    HO.build(s, limit=1)                                                                             # the first manual build; two launches are left
    log = []
    assert HO.refresh(s, log.append, now=1e12)["computed"] == 2 and any("holdings: 2 launches" in m for m in log)
    monkeypatch.setattr(HO, "build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk")))
    assert HO.refresh(s, log.append, now=2e12) is None
    assert "disk" in s.get_status()["holdings_last_error"]["error"]
    s.close()


# ---------------------------------------------------------------------------------------------- storage and pages
@pytest.mark.core
def test_the_migration_is_additive_and_an_old_database_opens(tmp_path):
    assert "launch_holdings" in MIGRATIONS[17] and len(MIGRATIONS) >= 18
    assert not any(w in MIGRATIONS[17].upper() for w in ("DROP ", "ALTER ", "DELETE ", "UPDATE "))
    p = tmp_path / "old.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a','a','1',1,1,'x')")
    for t in ("launch_holdings", "launch_holdings_done", "signal_features", "launch_live_state", "launch_live_state_at"):
        s.con.execute(f"DROP TABLE {t}")
    s.con.execute("PRAGMA user_version=17")
    s.con.commit()
    s.close()
    s = open_store(p)
    assert s.user_version() >= 18 and s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
    assert HO.launch_holdings(s.con, "a.nearlytrade.near") is None
    s.close()


@pytest.mark.core
def test_only_the_holdings_module_writes_its_tables_and_the_rules_and_the_publisher_never_read_them():
    import rugyomi
    pkg = Path(rugyomi.__file__).parent
    src = {p.relative_to(pkg).as_posix(): p.read_text(encoding="utf-8") for p in pkg.rglob("*.py")}
    assert {k for k, v in src.items() if re.search(r"INTO launch_holdings|DELETE FROM launch_holdings", v)} == {"holdings.py"}
    assert {k for k, v in src.items() if re.search(r"(FROM|INTO|JOIN|TABLE) launch_holdings", v)} <= {"holdings.py", "store/schema.py"}     # the site reads it only through holdings.launch_holdings
    for forbidden in ("publisher.py", "rules/card.py", "rules/context.py", "rules/signals.py", "rules/rug.py", "rules/stored.py", "rules/accuracy.py",
                      "rules/backtest.py", "retro.py", "links.py", "buyers.py", "snapshots.py"):
        assert not re.search(r"import holdings|from \. import holdings|launch_holdings|rugyomi\.holdings", src[forbidden]), forbidden
    assert not re.search(r"\b(urllib|requests|httpx|http\.client)\b", src["holdings.py"])           # 0 credits: no network call


@pytest.mark.core
def test_the_card_hash_does_not_depend_on_the_holdings_section_and_is_the_pinned_one():
    other = json.loads(json.dumps(CFG))
    other["holdings"]["fresh_hours"] = 99
    assert C.config_hash(other) == C.config_hash(CFG) and C.holdings_config_hash(other) != C.holdings_config_hash(CFG)
    assert [C.config_hash(C.effective(None, v)) for v in ("v0", "v1", "v2")] == ["2cb6d1e9c0c3", "6fedfdb50583", "3d68b94c6371"]


def test_the_launch_page_shows_who_holds_the_supply_and_the_methodology_explains_it(db):
    HO.build(db)
    path = db.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        r = c.get(f"/launch/{REFRAX}")
        t = " ".join(r.text.split())
        assert r.status_code == 200 and 'id="holdings"' in r.text and "Who holds the supply" in t
        for label in ("Dev account", "Top 10 holders, raw", "Everyone else"):            # first, the top-10 line and the remainder; the rows in between are HO.ROWS
            assert label in t, label
        assert "0.5% · 1 of 1 hold" not in t and "holds" in t                                       # the dev row says "holds" / "holds nothing", never "1 of 1 hold"
        assert "counted once, in the first row that applies" in t and "do not say who controls a wallet" in t
        assert "<script" not in r.text.lower().replace("<script src", "")
        m = " ".join(c.get("/methodology").text.split())
        assert 'id="holdings"' in c.get("/methodology").text and "Who holds the supply" in m and "never assumed" in m
        assert HO.describe(CFG)["rows"][5]["description"] in m.replace("&lt;", "<")


def test_a_launch_without_stored_holdings_or_a_database_without_the_tables_still_serves_the_page(db, tmp_path):
    path = db.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        r = c.get(f"/launch/{REFRAX}")
        assert r.status_code == 200 and 'id="holdings"' not in r.text
    p = tmp_path / "n.db"
    s = open_store(p)
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at) VALUES('a.nearlytrade.near','nearly','a.near','a.near','1',1,1,'x')")
    s.con.execute("DROP TABLE launch_holdings")
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(p)) as c:
        r = c.get("/launch/a.nearlytrade.near")
        assert r.status_code == 200 and 'id="holdings"' not in r.text


def test_when_the_supply_is_unknown_the_page_says_so_instead_of_a_share(db):
    HO.build(db)
    db.con.execute("UPDATE launch_holdings SET supply = NULL, supply_note = 'no_mint' WHERE token = ? AND minute = 5", (REFRAX,))
    db.con.commit()
    path = db.con.execute("PRAGMA database_list").fetchone()[2]
    with TestClient(webapp.create_app(path)) as c:
        t = " ".join(c.get(f"/launch/{REFRAX}").text.split())
        assert "T+5 min: the supply cannot be determined from our launch data (no mint event), so no shares are shown." in t
