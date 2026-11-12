"""RGY-021 step 1: rules profile v1 — history-aware signals and the NO-LOOKAHEAD guarantee.

The guarantee is tested the strong way: a card computed on the full database is IDENTICAL to the card computed on a copy from which
everything that happened after the launch time was deleted. A mutation test then leaks one future outcome on purpose and shows that
the very same check fails."""
import json

import pytest

from test_rules import DAY, HOUR, MIN, NEAR, T0, Db
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import context as CX
from rugyomi.store import open_store

# These tests check the MECHANICS of each signal, so they run on the draft weights, not on the fitted ones the profile ships
# with (a fit changes points and may switch signals off; test_fitted_v1_profile below pins what is shipped).
DRAFT = {"signals": {
    "creator_tax": {"points": 5},
    "creator_track_record": {"points_mid": 25, "points_high": 40},
    "funded_by_rugger": {"points": 30, "points_multi": 40},
    "name_copy": {"points": 10},
    **{k: {"enabled": True} for k in ("cluster_rugs", "funded_by_dumper", "prior_fast_dump", "serial_launcher")},
}}
V1 = C.effective(DRAFT, "v1")
V0 = C.load_config(profile="v0")
INS_COLS = ("insiders_count", "insiders_acquired", "insiders_sold", "insiders_sold_share_bps", "insiders_pnl", "insiders_evidence")


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "v1.db")
    yield d
    d.s.close()


def outcome(db, token, launch_ts, rug, decided=("fast",), source="backfill"):
    """Snapshots that make `rug_labels` say rug / not rug. decided: "fast" = T+60 only, "late" = T+60 and T+24 h."""
    for minute, sold, price_last, price_peak in ((60, 9500 if rug else 0, "1", "10"), (1440, 9500 if rug else 0, "1", "10")):
        if minute == 1440 and "late" not in decided:
            continue
        db.s.con.execute(
            "INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, buyers_count, buyers_pnl, "
            "price_last, price_peak, insiders_count, insiders_acquired, insiders_sold, insiders_sold_share_bps, insiders_pnl, source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, minute, launch_ts + minute * MIN, 3, "x", 3, str(-5 * NEAR if rug else NEAR), price_last, price_peak,
             1, "1000", "950" if rug else "0", sold, "0", source))


def edge(db, a, b, ts, typ="funded"):
    db.s.con.execute("INSERT INTO link_edges(type, src, dst, ts_ns, tx, amount, asset, token, source, group_id) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (typ, a, b, ts, "tx", str(5 * NEAR), "NEAR", None, "facts", a))


def api_transfer(db, account, sender, ts, near=5):
    db.s.con.execute("INSERT INTO transfers_api(account, dir, height, ts_ns, tx, receipt, tindex, other, asset, amount, ttype, signer, pred) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (account, "in", 1, ts, f"tx-{sender}-{ts}", f"rc-{sender}-{ts}", 0, sender, "NEAR", str(int(near * NEAR)),
                      "NativeTransfer", sender, sender))


def card(db, token="now.nearlytrade.near", cfg=V1):
    return rules.compute_card(db.s, token, cfg)


def result(c, sid):
    return next(r for r in c["signals"]["results"] if r["id"] == sid)


# ------------------------------------------------------------------------------------------------ profiles
@pytest.mark.core
def test_the_v0_config_and_its_hash_do_not_change_and_v1_is_a_separate_profile():
    assert V0["version"] == "v0-default-1" and "rules_version" not in V0 and "context" not in V0
    assert V1["rules_version"] == "1.0" and V1["context"]["read_backfill"] is True
    assert C.config_hash(V0) != C.config_hash(V1)
    assert set(V1["signals"]) - set(V0["signals"]) == {"creator_track_record", "funded_by_rugger", "cluster_rugs"}
    with pytest.raises(C.ConfigError):
        C.load_config(profile="v9")


@pytest.mark.core
def test_a_v0_card_has_none_of_the_v1_signals_and_keeps_its_rules_version(db):
    db.launch("now.nearlytrade.near", "dev.near", T0)
    c = card(db, cfg=V0)
    assert c["rules_version"] == rules.RULES_VERSION == "0.1"
    assert {r["id"] for r in c["signals"]["results"]}.isdisjoint({"creator_track_record", "funded_by_rugger", "cluster_rugs"})
    assert card(db)["rules_version"] == "1.0"


# ------------------------------------------------------------------------------------------------ creator_track_record
@pytest.mark.core
def test_creator_track_record_counts_decided_rugs_of_the_creator_from_history_snapshots(db):
    for i in range(2):
        t = T0 - (10 + i) * HOUR
        db.launch(f"old{i}.nearlytrade.near", "dev.near", t, coverage="launch_only")
        outcome(db, f"old{i}.nearlytrade.near", t, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    r = result(card(db), "creator_track_record")
    assert r["fired"] and r["value"]["known_launches"] == 2 and r["value"]["rugs"] == 2 and r["contribution"] == 40   # share 0.55 -> high
    assert {e["id"] for e in r["evidence"]} == {"old0.nearlytrade.near", "old1.nearlytrade.near"}
    assert "smoothed rug share" in r["reason"]
    # profile v0 does not read history snapshots (Q-010) and does not have the signal
    assert "creator_track_record" not in {x["id"] for x in card(db, cfg=V0)["signals"]["results"]}


@pytest.mark.core
def test_one_clean_launch_and_a_pending_one_do_not_fire(db):
    db.launch("clean.nearlytrade.near", "dev.near", T0 - 30 * HOUR, coverage="launch_only")
    outcome(db, "clean.nearlytrade.near", T0 - 30 * HOUR, rug=False, decided=("fast", "late"))
    db.launch("pending.nearlytrade.near", "dev.near", T0 - 2 * HOUR, coverage="launch_only")
    outcome(db, "pending.nearlytrade.near", T0 - 2 * HOUR, rug=False, decided=("fast",))      # not rug_fast; the 24 h answer is not in yet
    db.launch("now.nearlytrade.near", "dev.near", T0)
    r = result(card(db), "creator_track_record")
    assert not r["fired"] and r["value"]["known_launches"] == 1 and r["value"]["rugs"] == 0


@pytest.mark.core
def test_a_launch_whose_60_minute_answer_is_not_in_yet_is_unknown(db):
    db.launch("fresh.nearlytrade.near", "dev.near", T0 - 30 * MIN, coverage="launch_only")
    outcome(db, "fresh.nearlytrade.near", T0 - 30 * MIN, rug=True)                           # its T+60 snapshot lies 30 min AFTER our launch
    db.launch("now.nearlytrade.near", "dev.near", T0)
    r = result(card(db), "creator_track_record")
    assert not r["fired"] and r["value"]["known_launches"] == 0
    # the same launch, an hour earlier: now it is known
    db.s.con.execute("UPDATE launch_snapshots SET at_ts_ns = at_ts_ns - 31 * 60 * 1000000000 WHERE token = 'fresh.nearlytrade.near'")
    assert result(card(db), "creator_track_record")["value"]["rugs"] == 1


@pytest.mark.core
def test_the_linked_group_counts_only_through_edges_that_already_existed(db):
    db.launch("old.nearlytrade.near", "friend.near", T0 - 10 * HOUR, coverage="launch_only")
    outcome(db, "old.nearlytrade.near", T0 - 10 * HOUR, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    edge(db, "friend.near", "dev.near", T0 + HOUR)                                           # the link is recorded AFTER the launch
    assert not result(card(db), "creator_track_record")["fired"]
    db.s.con.execute("UPDATE link_edges SET ts_ns = ?", (T0 - 5 * HOUR,))
    r = result(card(db), "creator_track_record")
    assert r["fired"] and r["value"]["group_rugs"] == 1 and r["value"]["own_known"] == 0 and "linked to him" in r["reason"]


@pytest.mark.core
def test_a_hop_through_an_account_that_launches_only_later_is_not_followed(db):
    """link_edges holds edges between accounts that launch at SOME time. A bridge that had not launched yet at the launch time was
    not a participant then: the group must not reach through it."""
    db.launch("old.nearlytrade.near", "friend.near", T0 - 10 * HOUR, coverage="launch_only")
    outcome(db, "old.nearlytrade.near", T0 - 10 * HOUR, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    db.launch("later.nearlytrade.near", "bridge.near", T0 + 2 * HOUR)                       # the bridge launches AFTER the launch
    edge(db, "friend.near", "bridge.near", T0 - 6 * HOUR)
    edge(db, "bridge.near", "dev.near", T0 - 5 * HOUR)
    assert not result(card(db), "creator_track_record")["fired"]
    db.launch("before.nearlytrade.near", "bridge.near", T0 - 20 * HOUR, coverage="launch_only")      # now it had launched before
    outcome(db, "before.nearlytrade.near", T0 - 20 * HOUR, rug=False)
    r = result(card(db), "creator_track_record")
    assert r["fired"] and r["value"]["group_rugs"] == 1


# ------------------------------------------------------------------------------------------------ funded_by_rugger
@pytest.mark.core
def test_funded_by_rugger_uses_the_full_history_of_transfers_and_the_funders_group(db):
    db.launch("rug.nearlytrade.near", "rugger.near", T0 - 40 * DAY, coverage="launch_only")
    outcome(db, "rug.nearlytrade.near", T0 - 40 * DAY, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    api_transfer(db, "dev.near", "rugger.near", T0 - 30 * DAY)                              # 30 days: far beyond v0's 24 h lookback
    r = result(card(db), "funded_by_rugger")
    assert r["fired"] and r["contribution"] == 30 and r["value"]["funder"] == "rugger.near"
    assert {e["kind"] for e in r["evidence"]} == {"tx", "launch"}
    assert not result(card(db, cfg=V0), "funded_by_dumper")["fired"]                          # the old signal cannot see it


@pytest.mark.core
def test_a_funding_after_the_launch_is_not_used(db):
    db.launch("rug.nearlytrade.near", "rugger.near", T0 - 40 * DAY, coverage="launch_only")
    outcome(db, "rug.nearlytrade.near", T0 - 40 * DAY, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    api_transfer(db, "dev.near", "rugger.near", T0 + 5 * MIN)
    assert not result(card(db), "funded_by_rugger")["fired"]


@pytest.mark.core
def test_a_non_direct_transfer_and_a_service_sender_are_ignored(db):
    db.launch("rug.nearlytrade.near", "rugger.near", T0 - 40 * DAY, coverage="launch_only")
    outcome(db, "rug.nearlytrade.near", T0 - 40 * DAY, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    api_transfer(db, "dev.near", "rugger.near", T0 - DAY)
    db.s.con.execute("UPDATE transfers_api SET signer = 'someone.near'")                   # a contract paid it out
    assert not result(card(db), "funded_by_rugger")["fired"]


# ------------------------------------------------------------------------------------------------ cluster_rugs, group serial
@pytest.mark.core
def test_cluster_rugs_needs_a_peer_that_launched_earlier_and_rugged(db):
    db.launch("peer-old.nearlytrade.near", "peer.near", T0 - 20 * HOUR, coverage="launch_only")
    outcome(db, "peer-old.nearlytrade.near", T0 - 20 * HOUR, rug=True)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    for acc, funded, lt, launch_ns in (("peer.near", T0 - 21 * HOUR, "peer-old.nearlytrade.near", T0 - 20 * HOUR),
                                       ("dev.near", T0 - 2 * HOUR, "now.nearlytrade.near", T0)):
        db.s.con.execute("INSERT INTO link_cluster_members(source, account, funded_ns, tx, amount, amount_total, fundings, asset, "
                         "edge_source, launch_token, launch_ns, seconds_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         ("src.near", acc, funded, "tx", "1", "1", 1, "NEAR", "facts", lt, launch_ns, 60))
    r = result(card(db), "cluster_rugs")
    assert r["fired"] and r["value"]["peer_rugs"] == 1 and r["contribution"] == 25
    db.s.con.execute("UPDATE link_cluster_members SET launch_ns = ? WHERE account = 'peer.near'", (T0 + HOUR,))   # the peer launches AFTER us
    assert not result(card(db), "cluster_rugs")["fired"]


@pytest.mark.core
def test_serial_launcher_at_group_level_counts_the_linked_accounts(db):
    for i in range(3):
        db.launch(f"f{i}.nearlytrade.near", "friend.near", T0 - (i + 1) * HOUR, coverage="launch_only")
    db.launch("now.nearlytrade.near", "dev.near", T0)
    assert not result(card(db), "serial_launcher")["fired"]
    edge(db, "friend.near", "dev.near", T0 - 5 * HOUR)
    r = result(card(db), "serial_launcher")
    assert r["fired"] and r["version"] == 3 and r["value"]["prior_launches"] == 3 and r["contribution"] == 15
    assert result(card(db, cfg=V0), "serial_launcher")["version"] == 2                           # v0 keeps its own definition


# ------------------------------------------------------------------------------------------------ no lookahead, the strong way
def future_world(db):
    """Past (rug history before T0) and future (everything after T0) around the launch `now` of dev.near."""
    past = [("p0.nearlytrade.near", "dev.near", T0 - 30 * HOUR, True, ("fast", "late")),
            ("p1.nearlytrade.near", "friend.near", T0 - 20 * HOUR, True, ("fast", "late")),
            ("p2.nearlytrade.near", "rugger.near", T0 - 40 * DAY, True, ("fast", "late"))]
    for tok, creator, ts, rug, dec in past:
        db.launch(tok, creator, ts, coverage="launch_only")
        outcome(db, tok, ts, rug, dec)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    # a prior launch whose answers arrive AFTER T0, and launches/links/fundings that only exist after it
    db.launch("late.nearlytrade.near", "dev.near", T0 - 20 * MIN, coverage="launch_only")
    outcome(db, "late.nearlytrade.near", T0 - 20 * MIN, rug=True, decided=("fast", "late"))
    db.launch("after.nearlytrade.near", "dev.near", T0 + HOUR, coverage="launch_only")
    outcome(db, "after.nearlytrade.near", T0 + HOUR, rug=True, decided=("fast", "late"))
    edge(db, "friend.near", "dev.near", T0 - 25 * HOUR)
    edge(db, "stranger.near", "dev.near", T0 + 2 * HOUR)
    db.launch("s.nearlytrade.near", "stranger.near", T0 - 15 * HOUR, coverage="launch_only")
    outcome(db, "s.nearlytrade.near", T0 - 15 * HOUR, rug=True, decided=("fast", "late"))
    api_transfer(db, "dev.near", "rugger.near", T0 - 3 * DAY)
    api_transfer(db, "dev.near", "stranger.near", T0 + 3 * HOUR)


def drop_future(src, out, T):
    """A copy (SQLite backup of the open connection `src`) from which everything dated after T is deleted."""
    import sqlite3
    if src.in_transaction:
        src.commit()
    dst = sqlite3.connect(out)
    src.backup(dst)
    dst.close()
    s = open_store(out)
    c = s.con
    s.begin()
    c.execute("DELETE FROM facts WHERE ts_ns > ?", (T,))
    c.execute("DELETE FROM launch_snapshots WHERE at_ts_ns > ?", (T,))
    c.execute("DELETE FROM launches WHERE ts_ns > ?", (T,))
    c.execute("DELETE FROM link_edges WHERE ts_ns IS NULL OR ts_ns > ?", (T,))
    c.execute("DELETE FROM link_cluster_members WHERE funded_ns > ?", (T,))
    c.execute("DELETE FROM transfers_api WHERE ts_ns > ?", (T,))
    s.commit()
    return s


def essence(c):
    return (c["score"], c["band"], [(r["id"], r["contribution"], r["value"]) for r in c["reasons"]],
            [(r["id"], r["fired"], json.dumps(r["value"], sort_keys=True)) for r in c["signals"]["results"]])


@pytest.mark.core
def test_the_card_does_not_change_when_everything_after_the_launch_is_deleted(db, tmp_path):
    future_world(db)
    db.s.con.commit() if db.s.con.in_transaction else None
    full = card(db)
    assert full["score"] > 0 and {r["id"] for r in full["reasons"]} >= {"creator_track_record", "funded_by_rugger"}
    s2 = drop_future(db.s.con, tmp_path / "cut.db", T0)
    try:
        cut = rules.compute_card(s2, "now.nearlytrade.near", V1)
    finally:
        s2.close()
    assert essence(cut) == essence(full)


@pytest.mark.core
def test_a_mutation_that_leaks_one_future_outcome_is_caught_by_that_check(db, tmp_path, monkeypatch):
    future_world(db)
    leaky_calls = []

    def leaky_outcome(self, token):                       # the mutation: the T+60 / T+24 h snapshots are read whatever their cutoff
        from rugyomi.rules.rug import rug_labels
        from rugyomi.rules.stored import SNAP_COLS
        snaps = [dict(zip(SNAP_COLS, r)) for r in self._store.con.execute(
            f"SELECT {','.join(SNAP_COLS)} FROM launch_snapshots WHERE token = ? AND minute IN (60, 1440) ORDER BY minute", (token,))]
        leaky_calls.append(token)
        if not snaps:
            return {"rug": None, "fast": None, "late": None, "known_ns": None}
        lab = rug_labels(snaps, self.config)
        return {"rug": lab["rug"]["value"], "fast": lab["rug_fast"]["value"], "late": lab["rug_late"]["value"], "known_ns": 0}

    full_ok = card(db)
    monkeypatch.setattr(CX.LaunchContext, "outcome", leaky_outcome)
    full_leaky = card(db)
    s2 = drop_future(db.s.con, tmp_path / "cut2.db", T0)
    try:
        cut_leaky = rules.compute_card(s2, "now.nearlytrade.near", V1)
    finally:
        s2.close()
    assert "late.nearlytrade.near" in leaky_calls
    assert essence(cut_leaky) != essence(full_leaky), "the leak must change the card, or the no-lookahead check is blind"
    assert essence(full_leaky) != essence(full_ok)


@pytest.mark.core
def test_signals_still_have_no_database_access_of_their_own():
    import ast
    import inspect
    from rugyomi.rules import signals
    src = inspect.getsource(signals)
    assert "execute(" not in src and "sqlite3" not in src and "con." not in src.replace("ctx.", "")
    ast.parse(src)


@pytest.mark.core
def test_fitted_v1_profile_ships_the_fitted_weights_and_switches_off_what_the_fit_zeroed():
    cfg = C.load_config(profile="v1")
    S = cfg["signals"]
    assert cfg["version"].startswith("v1-fit") and (cfg["bands"]["elevated"], cfg["bands"]["high"]) == (30, 60)
    assert S["creator_tax"]["points"] == 20
    assert (S["creator_track_record"]["points_mid"], S["creator_track_record"]["points_high"]) == (30, 50)
    assert S["funded_by_rugger"]["points"] == S["funded_by_rugger"]["points_multi"] == 10
    assert S["name_copy"]["points"] == 5
    for k in ("cluster_rugs", "funded_by_dumper", "prior_fast_dump", "serial_launcher"):
        assert S[k]["enabled"] is False, k
    assert C.config_hash(C.load_config(profile="v0")) == "2cb6d1e9c0c3"      # v0 untouched


# ------------------------------------------------------------------------------------------------ RGY-024: history not loaded yet (Q-011)
def _coverage(db):
    db.fact("near_transfer", "x.near", "y.near", amount=NEAR, ts=T0 - 5 * DAY, tx="cov")          # the live facts start 5 days before the launch


def _notes(db, cfg=V1):
    return card(db, cfg=cfg)["signals"]["notes"]


@pytest.mark.core
def test_a_creator_without_a_loaded_history_gets_a_note_and_is_queued_at_once(db):
    _coverage(db)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    notes = _notes(db)
    assert len(notes) == 1 and "creator's history before" in notes[0] and "not loaded yet" in notes[0]
    assert _notes(db, V0) == []                                                              # v0 does not read the history: no note
    assert rules.issue_card(db.s, "now.nearlytrade.near", 1, V1)
    q = db.s.con.execute("SELECT kind, key, status FROM backfill_queue WHERE key = 'dev.near'").fetchall()
    assert [tuple(r) for r in q] == [("transfers_in", "dev.near", "pending")]
    stored = rules.card_row(db.s, "now.nearlytrade.near")
    assert any("not loaded yet" in n for n in stored["signals"]["notes"])                  # frozen into the card


@pytest.mark.core
def test_no_note_when_the_history_is_loaded_or_the_account_was_created_inside_the_live_coverage(db):
    _coverage(db)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    db.s.con.execute("INSERT INTO backfill_queue(kind, key, status, queued_at) VALUES('transfers_in', 'dev.near', 'done', 'x')")
    assert _notes(db) == []
    db.s.con.execute("DELETE FROM backfill_queue")
    db.created("dev.near", T0 - 2 * DAY)                                                    # everything it ever received is in the facts
    assert _notes(db) == []


@pytest.mark.core
def test_the_largest_funder_is_checked_too_and_an_existing_queue_entry_is_kept(db):
    _coverage(db)
    db.launch("now.nearlytrade.near", "dev.near", T0)
    db.s.con.execute("INSERT INTO backfill_queue(kind, key, status, queued_at) VALUES('transfers_in', 'dev.near', 'done', 'x')")
    api_transfer(db, "dev.near", "small.near", T0 - 3 * DAY, near=2)
    api_transfer(db, "dev.near", "big.near", T0 - 2 * DAY, near=50)
    notes = _notes(db)
    assert len(notes) == 1 and "largest funder's (big.near)" in notes[0]
    db.s.con.execute("INSERT INTO backfill_queue(kind, key, status, queued_at) VALUES('transfers_in', 'big.near', 'running', 'x')")
    assert rules.issue_card(db.s, "now.nearlytrade.near", 1, V1) is True
    assert db.s.con.execute("SELECT status FROM backfill_queue WHERE key = 'big.near'").fetchone()[0] == "running"     # not reset
