"""RGY-015: the rules module — config, signals on synthetic cases, no lookahead, immutable cards, the collector hook,
the `score` CLI, outcome labels, and the REFRAX reference on the real 29.10 blocks."""
import ast
import inspect
import json
import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from rugyomi_testkit import hid, refrax_replay_blocks
from rugyomi import rules
from rugyomi.collector import Collector, main as collector_main
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.rules import card as R
from rugyomi.rules import config as C
from rugyomi.rules import signals as SG
from rugyomi.store import open_store
from rugyomi.store.db import _split, hash_bytes
from rugyomi.store.schema import MIGRATIONS, SCHEMA_VERSION

NEAR = 10**24
MIN = 60 * 10**9
HOUR = 60 * MIN
DAY = 24 * HOUR
T0 = 1_800_000_000 * 10**9          # "now" of the synthetic cases
REFRAX, ISLAND, COIN = "refrax.nearlytrade.near", "island.nearlytrade.near", "coin-2.nearlytrade.near"
FUNDING_TX = "3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG"


# ---------------------------------------------------------------- synthetic database helpers

class Db:
    def __init__(self, path):
        self.s = open_store(path)
        self.n = 0

    def fact(self, kind, a, b, amount=None, ts=T0, extra=None, tx=None, height=None):
        s = self.s
        self.n += 1
        s.con.execute(
            "INSERT INTO facts(kind, height, ts_ns, tx, receipt, origin, a, b, token, amount, extra) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (s.kind_id(kind), height or ts // 10**9 % 10**9, ts, hash_bytes(tx) if tx else None,
             self.n.to_bytes(32, "big"), "t", s.account_id(a), s.account_id(b) if b else None, None,
             str(amount) if amount is not None else None, json.dumps(extra) if extra else None))

    def launch(self, token, creator, ts, dev_buy=40 * NEAR, launchpad="nearly", coverage=None, owner="same", extra=None,
               height=None):
        owner = creator if owner == "same" else owner
        self.s.con.execute(
            "INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, extra, coverage) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (token, launchpad, creator, owner, str(dev_buy), height or ts // 10**9 % 10**9, ts, "x",
             json.dumps(extra) if extra else None, coverage))

    def snap(self, token, minute, sold_bps, launch_ts, pnl=None, error=None):
        self.s.con.execute(
            "INSERT INTO launch_snapshots(token, minute, at_ts_ns, dev_sold_share_bps, buyers_pnl, metric_version, computed_at, error) "
            "VALUES(?,?,?,?,?,?,?,?)", (token, minute, launch_ts + minute * MIN, sold_bps, pnl, 1, "x", error))

    def created(self, account, ts, tx=None):
        self.fact("create_account", account, "near", ts=ts, tx=tx or hid("create", account), extra={"signer": "relayer.near"})

    def fund(self, sender, receiver, near, ts, kind="near_transfer", tx=None):
        self.fact(kind, sender, receiver, amount=int(near * NEAR), ts=ts, tx=tx or hid("fund", sender, receiver, ts))

    def card(self, token, **kw):
        return rules.compute_card(self.s, token, **kw)

    def close(self):
        self.s.con.commit()
        self.s.close()


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "r.db")
    yield d
    d.s.close()


def fired(card):
    return {r["id"]: r for r in card["reasons"]}


def result(card, sid):
    return next(r for r in card["signals"]["results"] if r["id"] == sid)


# ---------------------------------------------------------------- config

def test_default_config_is_valid_and_complete():
    cfg = C.default_config()
    assert set(cfg["signals"]) == {s.id for s in rules.SIGNALS} - {"creator_track_record", "funded_by_rugger", "cluster_rugs"}   # v0
    assert set(C.default_config("v1")["signals"]) == {s.id for s in rules.SIGNALS}                                              # v1 (RGY-021)
    assert cfg["bands"] == {"elevated": 30, "high": 60}
    assert cfg["version"] and len(C.config_hash(cfg)) == 12


def test_override_merges_only_the_named_keys(tmp_path, monkeypatch):
    p = tmp_path / "o.json"
    p.write_text(json.dumps({"version": "prod-1", "signals": {"serial_launcher": {"points": 99}}}))
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(p))
    cfg = C.load_config()
    assert cfg["version"] == "prod-1" and cfg["signals"]["serial_launcher"]["points"] == 99
    assert cfg["signals"]["serial_launcher"]["min_prior"] == C.default_config()["signals"]["serial_launcher"]["min_prior"]
    assert C.config_hash(cfg) != C.config_hash(C.default_config())
    p.write_text(json.dumps({"version": "prod-2"}))                       # a changed file is picked up without a restart
    assert C.load_config()["version"] == "prod-2"


@pytest.mark.parametrize("bad", [{"nope": 1}, {"signals": {"nope": {}}}, {"signals": {"fingerprint": {"points_2_of_3": "x"}}},
                                 {"signals": {"fingerprint": {"enabled": 1}}}, {"bands": {"elevated": 70, "high": 60}},
                                 {"bands": 5}, {"signals": {"funded_by_dumper": {"minutes": ["5"]}}}])
def test_a_broken_override_is_an_error_not_a_silent_default(tmp_path, bad):
    p = tmp_path / "o.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(C.ConfigError):
        C.load_config(p)


def test_an_unreadable_or_invalid_override_file_is_an_error(tmp_path):
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(C.ConfigError):
        C.load_config(tmp_path / "bad.json")


# ---------------------------------------------------------------- migration and immutability

def test_migration_v7_adds_cards_and_keeps_old_rows(tmp_path):
    p = tmp_path / "m.db"
    con = sqlite3.connect(p)
    for i in range(6):
        for stmt in _split(MIGRATIONS[i]):
            con.execute(stmt)
    con.execute("PRAGMA user_version=6")
    con.execute("INSERT INTO launches(token, launchpad, height, ts_ns, detected_at) VALUES('old.near','nearly',1,2,'x')")
    con.commit()
    con.close()
    s = open_store(p)
    try:
        assert s.user_version() == SCHEMA_VERSION >= 7
        assert s.con.execute("SELECT token FROM launches").fetchall() == [("old.near",)]
        assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert {r[0] for r in s.con.execute("SELECT name FROM sqlite_master WHERE type='trigger'")} >= {
            "cards_no_update", "cards_no_delete", "cards_no_replace"}
    finally:
        s.close()


def _insert_card(con, token, score=10, verb="INSERT"):
    return con.execute(
        f"{verb} INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, config_version, "
        "config_hash, rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (token, "x", 1, 2, score, "Low", "[]", "{}", "v", "h", "0.1", 0))


def test_cards_are_immutable(db):
    con = db.s.con
    _insert_card(con, "t.near", 10)
    for verb in ("INSERT", "INSERT OR IGNORE", "INSERT OR REPLACE"):
        _insert_card(con, "t.near", 99, verb)                              # a second card for a token: ignored
        assert con.execute("SELECT score FROM cards WHERE token = 't.near'").fetchone()[0] == 10
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        con.execute("UPDATE cards SET score = 0")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        con.execute("DELETE FROM cards")
    assert con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 1


def test_issue_card_never_overwrites_and_is_not_issued_twice(db):
    db.launch("a.nearlytrade.near", "dev.near", T0)
    assert R.issue_card(db.s, "a.nearlytrade.near", 5) is True
    first = R.card_row(db.s, "a.nearlytrade.near")
    db.launch("b.nearlytrade.near", "dev.near", T0 + MIN)                  # would change a re-computed card
    assert R.issue_card(db.s, "a.nearlytrade.near", 6) is False
    assert R.card_row(db.s, "a.nearlytrade.near") == first


def test_only_live_launches_get_a_card(db):
    db.launch("live.nearlytrade.near", "d.near", T0, height=50)
    db.launch("hist.nearlytrade.near", "d.near", T0, coverage="launch_only", height=50)
    assert R.issue_for_height(db.s, 50) == 1
    assert [r[0] for r in db.s.con.execute("SELECT token FROM cards")] == ["live.nearlytrade.near"]


# ---------------------------------------------------------------- signals: fire and stay silent

def test_fingerprint_three_of_three_two_of_three_and_silent(db):
    L = T0
    db.launch("x.nearlytrade.near", "fresh.near", L, dev_buy=40 * NEAR)
    db.created("fresh.near", L - 6 * MIN)
    db.fund("rich.near", "fresh.near", 41, L - 5 * MIN)
    c = db.card("x.nearlytrade.near")
    r = result(c, "fingerprint")
    assert r["fired"] and r["contribution"] == 35 and r["value"]["conditions_met"] == 3
    assert {e["kind"] for e in r["evidence"]} == {"tx"} and len(r["evidence"]) == 2
    assert "created 6 min before" in r["reason"] and "41.00 NEAR" in r["reason"]
    # dev buy far below the funding: 2 of 3
    db.launch("y.nearlytrade.near", "fresh2.near", L, dev_buy=5 * NEAR)
    db.created("fresh2.near", L - 2 * MIN)
    db.fund("rich.near", "fresh2.near", 41, L - 2 * MIN)
    r = result(db.card("y.nearlytrade.near"), "fingerprint")
    assert r["fired"] and r["contribution"] == 25 and r["value"]["dev_buy_vs_funding"] is False
    # an old account that was funded and then spends the funding: also 2 of 3 (funded + dev buy)
    db.launch("w.nearlytrade.near", "old2.near", L, dev_buy=40 * NEAR)
    db.fund("rich.near", "old2.near", 41, L - 3 * MIN)
    r = result(db.card("w.nearlytrade.near"), "fingerprint")
    assert r["fired"] and r["contribution"] == 25 and r["value"]["fresh_account"] is False
    # only one condition: silent
    db.launch("z.nearlytrade.near", "old.near", L, dev_buy=5 * NEAR)
    db.fund("rich.near", "old.near", 41, L - 3 * MIN)
    assert not result(db.card("z.nearlytrade.near"), "fingerprint")["fired"]


def test_fingerprint_windows_and_funders(db):
    L = T0
    db.launch("x.nearlytrade.near", "d.near", L)
    db.created("d.near", L - 11 * MIN)                               # outside the 10 min window
    db.fund("dclv2.ref-labs.near", "d.near", 41, L - MIN)            # a service account is not a funder
    db.fund("rich.near", "d.near", 0.5, L - MIN)                     # below the minimum funding
    db.fund("rich.near", "d.near", 41, L - 11 * MIN)                 # outside the window
    r = result(db.card("x.nearlytrade.near"), "fingerprint")
    assert not r["fired"] and r["value"]["conditions_met"] == 0 and r["value"]["funded"] is False


def test_funded_by_dumper(db):
    L = T0
    db.launch("early.nearlytrade.near", "dumper.near", L - 20 * MIN)
    db.snap("early.nearlytrade.near", 5, 10000, L - 20 * MIN)
    db.launch("x.nearlytrade.near", "new.near", L)
    db.fund("dumper.near", "new.near", 41, L - 5 * MIN, tx=FUNDING_TX)
    c = db.card("x.nearlytrade.near")
    r = result(c, "funded_by_dumper")
    assert r["fired"] and r["contribution"] == 35
    assert {"kind": "tx", "id": FUNDING_TX} in r["evidence"] and {"kind": "launch", "id": "early.nearlytrade.near"} in r["evidence"]
    assert r["value"]["funder"] == "dumper.near" and r["value"]["at_minute"] == 5


@pytest.mark.parametrize("case", ["sold_80", "snapshot_not_due", "old_funding", "no_snapshot", "snapshot_error", "service_funder"])
def test_funded_by_dumper_silent_cases(db, case):
    L = T0
    db.launch("early.nearlytrade.near", "dumper.near", L - 20 * MIN)
    if case == "sold_80":
        db.snap("early.nearlytrade.near", 5, 8000, L - 20 * MIN)
    elif case == "snapshot_not_due":
        db.snap("early.nearlytrade.near", 15, 10000, L - 20 * MIN)       # cutoff = L - 5 min: knowable...
        db.s.con.execute("UPDATE launch_snapshots SET at_ts_ns = ?", (L + MIN,))   # ... unless its cutoff is after the launch
    elif case == "snapshot_error":
        db.snap("early.nearlytrade.near", 5, 10000, L - 20 * MIN, error="boom")
    elif case != "no_snapshot":
        db.snap("early.nearlytrade.near", 5, 10000, L - 20 * MIN)
    db.launch("x.nearlytrade.near", "new.near", L)
    db.fund("dclv2.ref-labs.near" if case == "service_funder" else "dumper.near", "new.near", 41,
            L - (30 * HOUR if case == "old_funding" else 5 * MIN))
    assert not result(db.card("x.nearlytrade.near"), "funded_by_dumper")["fired"]


def test_funded_by_dumper_through_wnear_and_launchbot_owner(db):
    L = T0
    db.launch("early.nearlytrade.near", "launchbot.nearlytrade.near", L - 40 * MIN, owner="dumper.near")
    db.snap("early.nearlytrade.near", 15, 9500, L - 40 * MIN)
    db.launch("x.nearlytrade.near", "launchbot.nearlytrade.near", L, owner="new.near")
    db.fund("dumper.near", "new.near", 20, L - HOUR, kind="wnear_transfer")
    r = result(db.card("x.nearlytrade.near"), "funded_by_dumper")
    assert r["fired"] and r["value"]["at_minute"] == 15


def test_serial_launcher_counts_prior_launches_only(db):
    L = T0
    for i in range(5):
        db.launch(f"p{i}.nearlytrade.near", "spam.near", L - (i + 1) * 3 * MIN)
    db.launch("old.nearlytrade.near", "spam.near", L - 25 * HOUR)             # outside 24 h
    db.launch("later.nearlytrade.near", "spam.near", L + MIN)                 # after: never counted
    db.launch("x.nearlytrade.near", "spam.near", L)
    r = result(db.card("x.nearlytrade.near"), "serial_launcher")
    assert r["fired"] and r["value"]["prior_launches"] == 5 and r["contribution"] == 25 and "5 other launches" in r["reason"]
    db.launch("y.nearlytrade.near", "two.near", L)
    for i in range(2):
        db.launch(f"t{i}.nearlytrade.near", "two.near", L - (i + 1) * HOUR)
    r = result(db.card("y.nearlytrade.near"), "serial_launcher")
    assert r["fired"] and r["contribution"] == 15
    db.launch("w.nearlytrade.near", "one.near", L)
    db.launch("w0.nearlytrade.near", "one.near", L - HOUR)
    r = result(db.card("w.nearlytrade.near"), "serial_launcher")                 # RGY-017: the 2nd launch counts (+10)
    assert r["fired"] and r["contribution"] == 10 and r["value"]["prior_launches"] == 1
    db.launch("v.nearlytrade.near", "zero.near", L)
    assert not result(db.card("v.nearlytrade.near"), "serial_launcher")["fired"]


def test_serial_launcher_counts_every_source(db):
    L = T0
    db.launch("x.nearlytrade.near", "spam.near", L)
    db.launch("a.nearlytrade.near", "spam.near", L - HOUR, coverage="launch_only")
    db.s.con.execute("INSERT INTO launch_index(token, launchpad, creator, create_height, create_ts_ns, source, imported_at) "
                     "VALUES('b.nearlytrade.near','nearly','spam.near',1,?,'chain_index','x')", (L - 2 * HOUR,))
    assert result(db.card("x.nearlytrade.near"), "serial_launcher")["value"]["prior_launches"] == 2


def test_prior_fast_dump(db):
    L = T0
    db.launch("e1.nearlytrade.near", "dev.near", L - HOUR)
    db.snap("e1.nearlytrade.near", 5, 9000, L - HOUR)
    db.launch("x.nearlytrade.near", "dev.near", L)
    r = result(db.card("x.nearlytrade.near"), "prior_fast_dump")
    assert r["fired"] and r["contribution"] == 25 and r["evidence"] == [{"kind": "launch", "id": "e1.nearlytrade.near"}]


@pytest.mark.parametrize("case", ["sold_89", "history_launch", "not_yet_due"])
def test_prior_fast_dump_silent(db, case):
    L = T0
    db.launch("e1.nearlytrade.near", "dev.near", L - HOUR if case != "not_yet_due" else L - 2 * MIN,
              coverage="launch_only" if case == "history_launch" else None)
    db.snap("e1.nearlytrade.near", 5, 8999 if case == "sold_89" else 10000, db.s.con.execute(
        "SELECT ts_ns FROM launches WHERE token = 'e1.nearlytrade.near'").fetchone()[0])
    db.launch("x.nearlytrade.near", "dev.near", L)
    assert not result(db.card("x.nearlytrade.near"), "prior_fast_dump")["fired"]


def test_name_copy_other_creator_and_same_creator_other_launchpad(db):
    L = T0
    db.launch("panda.nearlytrade.near", "someone.near", L - 2 * DAY)
    db.launch("panda-2.nearlytrade.near", "copycat.near", L)
    r = result(db.card("panda-2.nearlytrade.near"), "name_copy")
    assert r["fired"] and r["contribution"] == 10 and r["value"]["kind"] == "other_creator" and "panda" in r["reason"]
    # same creator, other launchpad, 10 min earlier
    db.launch("nearloween.v2-launchpad.justhoot.near", "heanko.near", L - 10 * MIN, launchpad="justhoot_v2")
    db.launch("nearloween.nearlytrade.near", "heanko.near", L)
    r = result(db.card("nearloween.nearlytrade.near"), "name_copy")
    assert r["fired"] and r["value"]["kind"] == "same_creator_other_launchpad"
    # by symbol
    db.launch("zzz-1.nearlytrade.near", "a.near", L - DAY, extra={"symbol": "MOONCAT"})
    db.launch("yyy.nearlytrade.near", "b.near", L, extra={"symbol": "mooncat"})
    assert result(db.card("yyy.nearlytrade.near"), "name_copy")["fired"]


@pytest.mark.parametrize("case", ["older_than_7d", "same_creator_same_launchpad", "same_creator_other_launchpad_old",
                                  "different_name", "short_name"])
def test_name_copy_silent(db, case):
    L = T0
    other = {"older_than_7d": ("panda.nearlytrade.near", "someone.near", L - 8 * DAY, "nearly"),
             "same_creator_same_launchpad": ("panda.nearlytrade.near", "me.near", L - HOUR, "nearly"),
             "same_creator_other_launchpad_old": ("panda.v2-launchpad.justhoot.near", "me.near", L - 2 * DAY, "justhoot_v2"),
             "different_name": ("bear.nearlytrade.near", "someone.near", L - HOUR, "nearly"),
             "short_name": ("ab.nearlytrade.near", "someone.near", L - HOUR, "nearly")}[case]
    db.launch(other[0], other[1], other[2], launchpad=other[3])
    mine = "ab-2.nearlytrade.near" if case == "short_name" else "panda-2.nearlytrade.near"
    db.launch(mine, "me.near", L)
    assert not result(db.card(mine), "name_copy")["fired"]


def test_creator_tax(db):
    L = T0
    db.launch("t.nearlytrade.near", "d.near", L, height=77, extra={"id": 9})
    assert not result(db.card("t.nearlytrade.near"), "creator_tax")["fired"]                 # no tax_opts at all
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L, height=77,
            extra={"contract": "nearlytrade.near", "data": {"id": 9, "creator_bps": 200, "buy_bps": 100, "sell_bps": 100}})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L, height=77,
            extra={"contract": "nearlytrade.near", "data": {"id": 10, "creator_bps": 0}})     # another launch's options
    r = result(db.card("t.nearlytrade.near"), "creator_tax")
    assert r["fired"] and r["contribution"] == 5 and r["value"]["creator_bps"] == 200 and "2.00%" in r["reason"]
    db.launch("u.nearlytrade.near", "d.near", L + MIN, height=78, extra={"id": 10})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L + MIN, height=78,
            extra={"contract": "nearlytrade.near", "data": {"id": 10, "creator_bps": 0}})
    assert not result(db.card("u.nearlytrade.near"), "creator_tax")["fired"]


def test_score_is_capped_sorted_and_banded(db):
    L = T0
    cfg = C.default_config()
    assert R.band_of(29, cfg) == "Low" and R.band_of(30, cfg) == "Elevated" and R.band_of(59, cfg) == "Elevated" \
        and R.band_of(60, cfg) == "High" and R.band_of(100, cfg) == "High"
    # many signals at once: capped at 100, reasons ordered by contribution
    db.launch("e.nearlytrade.near", "dumper.near", L - 30 * MIN)
    db.snap("e.nearlytrade.near", 5, 10000, L - 30 * MIN)
    for i in range(5):
        db.launch(f"p{i}.nearlytrade.near", "fresh.near", L - (i + 1) * 3 * MIN)
        db.snap(f"p{i}.nearlytrade.near", 5, 9500, L - (i + 1) * 3 * MIN)
    db.launch("panda.nearlytrade.near", "someone.near", L - DAY)
    db.launch("panda-2.nearlytrade.near", "fresh.near", L, height=1, extra={"id": 3})
    db.fact("launch:tax_opts", "nearlytrade.near", None, ts=L, height=1,
            extra={"data": {"id": 3, "creator_bps": 100}})
    db.created("fresh.near", L - 2 * MIN)
    db.fund("dumper.near", "fresh.near", 41, L - MIN)
    c = db.card("panda-2.nearlytrade.near")
    assert sum(r["contribution"] for r in c["reasons"]) > 100 and c["score"] == 100 and c["band"] == "High"
    pts = [r["contribution"] for r in c["reasons"]]
    assert pts == sorted(pts, reverse=True)


def test_a_disabled_signal_is_skipped(db):
    db.launch("x.nearlytrade.near", "d.near", T0)
    cfg = C.default_config()
    cfg["signals"]["name_copy"]["enabled"] = False
    ids = [r["id"] for r in db.card("x.nearlytrade.near", config=cfg)["signals"]["results"]]
    assert "name_copy" not in ids and len(ids) == len(cfg["signals"]) - 1


def test_unknown_dev_is_noted_and_creator_signals_are_silent(db):
    db.launch("x.nearlytrade.near", "launchbot.nearlytrade.near", T0, owner=None)
    c = db.card("x.nearlytrade.near")
    assert c["dev_account"] is None and c["score"] == 0 and c["signals"]["notes"]
    assert result(c, "fingerprint")["value"] == {"skipped": "dev account unknown"}


def test_reasons_never_use_the_forbidden_words(db):
    L = T0
    db.launch("early.nearlytrade.near", "dumper.near", L - 20 * MIN)
    db.snap("early.nearlytrade.near", 5, 10000, L - 20 * MIN)
    db.launch("x.nearlytrade.near", "new.near", L)
    db.created("new.near", L - MIN)
    db.fund("dumper.near", "new.near", 41, L - MIN)
    text = json.dumps(db.card("x.nearlytrade.near")).lower() + json.dumps(rules.methodology()).lower()
    assert "scam" not in text and "sniper" not in text


# ---------------------------------------------------------------- no lookahead

def _strip(card):
    return {k: v for k, v in card.items() if k != "computed_ms"}


def _story(db):
    L = T0
    db.launch("e.nearlytrade.near", "dumper.near", L - 20 * MIN)
    db.snap("e.nearlytrade.near", 5, 10000, L - 20 * MIN)
    db.launch("panda.nearlytrade.near", "someone.near", L - DAY)
    db.launch("panda-2.nearlytrade.near", "new.near", L)
    db.created("new.near", L - 5 * MIN)
    db.fund("dumper.near", "new.near", 41, L - 4 * MIN)
    return L


def test_future_facts_never_change_a_card(db):
    L = _story(db)
    before = _strip(db.card("panda-2.nearlytrade.near"))
    assert before["score"] > 0
    # everything that happens AFTER the launch
    db.fund("other.near", "new.near", 500, L + MIN)                           # a later funding
    db.launch("later1.nearlytrade.near", "new.near", L + 2 * MIN)             # later launches of the same dev
    db.launch("later2.nearlytrade.near", "new.near", L + 3 * MIN)
    db.launch("panda-3.nearlytrade.near", "third.near", L + 4 * MIN)         # a later copy of the name
    db.snap("e.nearlytrade.near", 15, 10000, L - 20 * MIN)                    # cutoff L - 5 min: knowable, but...
    db.s.con.execute("UPDATE launch_snapshots SET at_ts_ns = ? WHERE minute = 15", (L + 10 * MIN,))   # ... computed later
    db.snap("later1.nearlytrade.near", 5, 10000, L + 2 * MIN)
    db.fact("near_transfer", "new.near", "x.near", amount=NEAR, ts=L + 5 * MIN)             # the dev's later sells / moves
    db.fact("dcl_swap", "new.near", "panda-2.nearlytrade.near|wrap.near|10000", amount=NEAR, ts=L + 6 * MIN)
    assert _strip(db.card("panda-2.nearlytrade.near")) == before


def test_a_late_account_creation_is_not_a_fresh_account(db):
    L = T0
    db.launch("x.nearlytrade.near", "d.near", L)
    db.created("d.near", L + MIN)                                             # created after the launch (impossible, but a test)
    assert result(db.card("x.nearlytrade.near"), "fingerprint")["value"]["fresh_account"] is False


def test_signals_module_has_no_database_access_of_its_own():
    src = inspect.getsource(SG)
    tree = ast.parse(src)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"execute", "con", "sqlite3", "cursor", "executemany"}, names & {"execute", "con", "sqlite3"}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "Store" not in imported and "open_store" not in imported
    assert "context" in src


# ---------------------------------------------------------------- outcome labels

def test_outcome_labels():
    cfg = C.default_config()
    snaps = [{"minute": 30, "dev_sold_share_bps": 9000, "buyers_pnl": None, "error": None},
             {"minute": 60, "dev_sold_share_bps": 9900, "buyers_pnl": str(-NEAR // 2), "error": None}]
    o = rules.outcome_labels(snaps, cfg)
    assert o["dev_exit"]["value"] is True and o["buyers_loss"]["value"] is True and o["buyers_loss"]["buyers_pnl_near"] == -0.5
    o = rules.outcome_labels({30: {"minute": 30, "dev_sold_share_bps": 8999, "error": None},
                              60: {"minute": 60, "buyers_pnl": "0", "error": None}}, cfg)
    assert o["dev_exit"]["value"] is False and o["buyers_loss"]["value"] is False            # 0 is not a loss
    o = rules.outcome_labels([{"minute": 30, "dev_sold_share_bps": 10000, "error": "boom"}], cfg)
    assert o["dev_exit"]["value"] is None and o["buyers_loss"]["value"] is None             # error / missing -> unknown
    cfg["outcomes"]["dev_exit"]["sold_bps"] = 5000
    assert rules.outcome_labels({30: {"minute": 30, "dev_sold_share_bps": 6000, "error": None}}, cfg)["dev_exit"]["value"] is True


# ---------------------------------------------------------------- the collector hook and the CLI (real blocks)

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    path = tmp_path_factory.mktemp("rules") / "real.db"
    s = open_store(path)
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in refrax_replay_blocks():
        c.process(h, b)
    yield s, c, path
    s.close()


def test_refrax_reference_card(real):
    s, _c, _p = real
    k = R.card_row(s, REFRAX)
    assert k["band"] == "High" and k["score"] == 70 and k["config_version"] == "v0-default-1" and k["rules_version"] == R.RULES_VERSION
    by = {r["id"]: r for r in k["reasons"]}
    assert set(by) == {"fingerprint", "funded_by_dumper"}
    fd = by["funded_by_dumper"]
    assert {"kind": "tx", "id": FUNDING_TX} in fd["evidence"] and {"kind": "launch", "id": COIN} in fd["evidence"]
    assert fd["value"]["funder"] == "coinisland.near" and fd["value"]["dev_sold_bps"] >= 9000
    fp = by["fingerprint"]
    assert fp["value"]["conditions_met"] == 3 and fp["contribution"] == 35
    assert fp["value"]["funding_near"] == pytest.approx(40.9688, abs=1e-3) and fp["value"]["dev_buy_near"] == 40.0
    assert {"kind": "tx", "id": "5SZ7gjkVnWSSS2KNuwZj2ztrUFkWxrk688jf5BVUGCKB"} in fp["evidence"]      # the creation of thecoins.near
    assert "5 min before" in fp["reason"] and "created 6 min before" in fp["reason"]


def test_island_and_coin_cards_after_rgy017(real):
    """ISLAND opens the chain (nothing before it): 0. COIN is the creator's 2nd launch in 4 min: serial_launcher +10 (RGY-017);
    both stay Low — the start of the chain is still mostly invisible to v0."""
    s, _c, _p = real
    k = R.card_row(s, ISLAND)
    assert k["score"] == 0 and k["band"] == "Low" and k["reasons"] == []
    k = R.card_row(s, COIN)
    assert k["score"] == 10 and k["band"] == "Low"
    assert [(r["id"], r["contribution"]) for r in k["reasons"]] == [("serial_launcher", 10)]


def test_the_live_card_equals_a_later_replay(real):
    """Issued in the block transaction, read back later: the same as computing it now (all later facts exist by then)."""
    s, _c, _p = real
    for t in (REFRAX, ISLAND, COIN):
        live = R.card_row(s, t)
        now = rules.compute_card(s, t)
        assert (live["score"], live["band"], live["reasons"], live["signals"], live["dev_account"], live["config_hash"]) == \
            (now["score"], now["band"], now["reasons"], now["signals"], now["dev_account"], now["config_hash"])


def test_refrax_card_ignores_its_own_post_launch_sells(real, tmp_path):
    s, _c, p = real
    k = R.card_row(s, REFRAX)
    q = tmp_path / "cut.db"
    shutil.copy(p, q)
    con = sqlite3.connect(q)
    con.execute("DELETE FROM facts WHERE ts_ns > ?", (k["launch_ts_ns"],))               # the future does not exist at all
    con.execute("DELETE FROM launch_snapshots WHERE at_ts_ns > ?", (k["launch_ts_ns"],))
    con.execute("DELETE FROM launches WHERE ts_ns > ?", (k["launch_ts_ns"],))
    con.commit()
    con.close()
    s2 = open_store(q)
    try:
        again = rules.compute_card(s2, REFRAX)
        assert (again["score"], again["reasons"]) == (k["score"], k["reasons"])
    finally:
        s2.close()


def test_one_card_per_launch_and_none_for_the_rest(real):
    s, _c, _p = real
    assert s.con.execute("SELECT COUNT(*), COUNT(DISTINCT token) FROM cards").fetchone() == (3, 3)
    assert s.con.execute("SELECT COUNT(*) FROM launches WHERE token NOT IN (SELECT token FROM cards)").fetchone()[0] == 0


def test_status_reports_the_cards(real):
    from rugyomi.collector import status
    s, _c, _p = real
    st = status(s)
    assert st["cards"]["total"] == 3 and st["cards"]["last_issued_at"]


def test_score_cli_writes_nothing(real, capsys):
    s, _c, p = real
    before = s.con.execute("SELECT COUNT(*), MAX(issued_at) FROM cards").fetchone()
    mtime = Path(p).stat().st_mtime_ns
    assert collector_main(["score", "--token", REFRAX, "--db", str(p)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["token"] == REFRAX and out["score"] == 70 and out["band"] == "High"
    assert collector_main(["score", "--token", "nope.near", "--db", str(p)]) == 1
    assert "no such launch" in capsys.readouterr().out
    assert s.con.execute("SELECT COUNT(*), MAX(issued_at) FROM cards").fetchone() == before
    assert Path(p).stat().st_mtime_ns == mtime


def test_card_computation_is_fast(real):
    s, _c, _p = real
    assert max(rules.compute_card(s, t)["computed_ms"] for t in (REFRAX, ISLAND, COIN)) < 1000


# ---------------------------------------------------------------- failures never break the block

def _collector(path):
    s = open_store(path)
    return s, Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)


def test_a_failing_card_is_remembered_and_the_block_still_commits(tmp_path, monkeypatch):
    s, c = _collector(tmp_path / "f.db")
    try:
        monkeypatch.setattr(R, "compute_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        for h, b in refrax_replay_blocks():
            c.process(h, b)
        assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 3
        assert s.con.execute("SELECT status FROM blocks WHERE height = 222136928").fetchone() == ("ok",)
        assert "boom" in s.get_status()["cards_last_error"]["error"]
    finally:
        s.close()


def test_a_broken_config_issues_no_card_but_the_block_commits(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text('{"signals": {"nope": {}}}')
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(bad))
    s, c = _collector(tmp_path / "g.db")
    try:
        for h, b in refrax_replay_blocks():
            c.process(h, b)
        assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 0
        assert s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 3
        assert "ConfigError" in s.get_status()["cards_last_error"]["error"]
    finally:
        s.close()


def test_the_override_is_recorded_on_the_card(tmp_path, monkeypatch):
    over = tmp_path / "o.json"
    over.write_text(json.dumps({"version": "prod-9", "signals": {"funded_by_dumper": {"points": 10}}}))
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(over))
    s, c = _collector(tmp_path / "h.db")
    try:
        for h, b in refrax_replay_blocks():
            c.process(h, b)
        k = R.card_row(s, REFRAX)
        assert k["config_version"] == "prod-9" and k["score"] == 45 and k["config_hash"] != C.config_hash(C.default_config())
    finally:
        s.close()


def test_reprocessing_a_height_does_not_issue_a_second_card(real):
    s, c, _p = real
    first = R.card_row(s, REFRAX)
    c.process(222136928, dict(refrax_replay_blocks())[222136928])
    assert R.card_row(s, REFRAX) == first
    assert s.con.execute("SELECT COUNT(*) FROM cards").fetchone()[0] == 3


# ---------------------------------------------------------------- RGY-017: the same-block bound

def test_same_block_transfer_after_the_launch_event_is_not_prelaunch_knowledge(db):
    """Facts of one block share a timestamp; their `facts.id` order is the execution order. A transfer to the dev that
    executes AFTER the launch event in the launch block is not known at launch; one before it, and one in the launch
    transaction itself, are."""
    from rugyomi.rules import context as X
    H, LTX = 5000, hid("launch-tx")
    db.launch("blk.nearlytrade.near", "dev.near", T0, height=H)
    db.s.con.execute("UPDATE launches SET tx = ? WHERE token = 'blk.nearlytrade.near'", (LTX,))
    db.fund("early.near", "dev.near", 5, T0, tx=hid("f-before"))                       # same block, before the event
    db.fact("launch:launch_started", "nearlytrade.near", "dev.near", ts=T0, tx=LTX, height=H)
    db.fund("late.near", "dev.near", 7, T0, tx=hid("f-after"))                         # same block, after the event
    db.fund("inner.near", "dev.near", 3, T0, tx=LTX)                                   # inside the launch transaction
    db.fund("next.near", "dev.near", 9, T0 + 1)                                        # a later block
    db.s.con.execute("UPDATE facts SET height = ? WHERE ts_ns = ?", (H, T0))
    db.s.con.execute("UPDATE facts SET height = ? WHERE ts_ns = ?", (H + 1, T0 + 1))
    ctx = X.LaunchContext(db.s, X.load_launch(db.s, "blk.nearlytrade.near"), C.default_config())
    senders = {r["sender"] for r in ctx.inflows("dev.near", 10 * MIN)}
    assert senders == {"early.near", "inner.near"}


def test_a_launch_without_an_event_fact_is_bounded_by_the_block_time_only(db):
    """Hand-made launch rows (no event fact in the database) keep the RGY-015 bound: ts_ns <= launch time."""
    from rugyomi.rules import context as X
    db.launch("nofact.nearlytrade.near", "dev.near", T0)
    db.fund("a.near", "dev.near", 5, T0 - MIN)
    db.fund("b.near", "dev.near", 5, T0 + MIN)
    ctx = X.LaunchContext(db.s, X.load_launch(db.s, "nofact.nearlytrade.near"), C.default_config())
    assert {r["sender"] for r in ctx.inflows("dev.near", 10 * MIN)} == {"a.near"}
