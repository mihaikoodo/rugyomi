"""RGY-012: quarantine of a height whose processing fails deterministically, network errors retried forever,
reprocess-quarantine, the snapshot coverage guard, an honest status on the failure path."""
import json
import sqlite3
import threading
import time

import pytest

from rugyomi_testkit import FakeNeardata, block, hid, reo, transfer_block
import rugyomi.collector as colmod
from rugyomi import launches as L
from rugyomi import snapshots as S
from rugyomi.collector import QUARANTINE_AFTER, Collector, main, status
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store


def make(store, fake, **kw):
    client = Neardata(EnvKeySource({}), base=fake.base, rps=500, stop=threading.Event())
    return Collector(store, client, log=lambda m: None, retry_delay=0.01, **kw)


def seed(store, start):
    store.begin()
    store.set_status("start_height", start)
    store.commit()


def wait_for(pred, timeout=20.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.02)
    return False


def failing_detect(bad, exc=lambda h: ValueError(f"cannot read block {h}"), times=None):
    """L.detect_in_block that raises on the heights in `bad` (forever, or `times` times per height)."""
    orig = L.detect_in_block
    left = {h: times for h in bad}

    def detect(store, height):
        if height in bad and (times is None or left[height] > 0):
            if times is not None:
                left[height] -= 1
            raise exc(height)
        return orig(store, height)
    return detect


def fetches(fake, h):
    return sum(1 for p in fake.block_paths() if p == f"/v0/block/{h}")


def facts(s):
    return s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]


def test_a_deterministic_processing_error_is_quarantined_after_5_and_the_collector_moves_on(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "detect_in_block", failing_detect({110}))
    with FakeNeardata(100, 130) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        assert col.run(max_blocks=30) == 131                       # 31 heights, one of them quarantined
        row = s.con.execute("SELECT status, process_errors, quarantined_at, quarantine_error, n_facts "
                            "FROM blocks WHERE height = 110").fetchone()
        assert row[0] == "error" and row[1] == QUARANTINE_AFTER and row[2] and "cannot read block 110" in row[3]
        assert row[4] == 0 and facts(s) == 30                        # nothing of 110 was written (rolled back)
        assert fetches(fake, 110) == QUARANTINE_AFTER
        assert s.cursor() == 131 and s.missing_heights(100, 131) == []
        st = status(s)
        assert st["quarantined_open"] == 1 and st["quarantined_total"] == 1 and st["retrying_heights"] == []
        assert st["quarantined_heights"][0]["height"] == 110 and "cannot read" in st["quarantined_heights"][0]["error"]
        assert st["last_error"]["quarantined"] is True and st["last_error"]["resolved_at"] is None
        assert st["missing_heights"] == [] and st["collector"]["quarantined_this_run"] == 1
        # a restart does not go back to it: quarantined = done for the cursor
        before = len(fake.block_paths())
        assert make(s, fake).run(max_blocks=0) == 131 and len(fake.block_paths()) == before


def test_a_network_error_is_retried_forever_and_never_quarantined(tmp_path):
    with FakeNeardata(100, 115) as fake:
        fake.fail = {108: QUARANTINE_AFTER + 2}                         # HTTP 500, 7 times in a row
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        make(s, fake).run(max_blocks=16)
        row = s.con.execute("SELECT status, attempts, process_errors, quarantined_at, error_since FROM blocks "
                            "WHERE height = 108").fetchone()
        assert row[:4] == ("ok", QUARANTINE_AFTER + 3, 0, None) and row[4]
        st = status(s)
        assert st["quarantined_total"] == 0 and st["last_error"]["kind"] == "network"
        assert st["last_error"]["height"] == 108 and st["last_error"]["resolved_at"]   # cleared: the height is ok now


def test_an_environment_error_while_processing_is_not_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "detect_in_block", failing_detect(
        {105}, exc=lambda h: sqlite3.OperationalError("database or disk is full"), times=QUARANTINE_AFTER + 2))
    with FakeNeardata(100, 110) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        make(s, fake).run(max_blocks=11)
        assert s.con.execute("SELECT status, process_errors, quarantined_at FROM blocks WHERE height = 105"
                             ).fetchone() == ("ok", 0, None)
    assert colmod.is_processing_error(ValueError()) and colmod.is_processing_error(KeyError("x"))
    assert not colmod.is_processing_error(sqlite3.OperationalError("locked"))
    assert not colmod.is_processing_error(OSError("io"))


def test_burst_guard_a_systemic_error_is_retried_not_quarantined(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "detect_in_block", failing_detect({110, 111, 112, 113}))
    with FakeNeardata(100, 130) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            ro = open_store(tmp_path / "r.db", readonly=True)
            assert wait_for(lambda: (ro.con.execute("SELECT process_errors FROM blocks WHERE height = 113").fetchone()
                                     or [0])[0] >= QUARANTINE_AFTER + 2)
        finally:
            col.stop.set()
            t.join(10)
        assert [h for h, _e, _a in s.quarantined_open()] == [110, 111, 112]
        assert not s.is_quarantined(113) and s.cursor() == 113
        assert status(s)["retrying_heights"][0]["height"] == 113


def test_reprocess_quarantine_turns_the_height_ok_without_duplicates(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(L, "detect_in_block", failing_detect({110}))
    with FakeNeardata(100, 120) as fake:
        db = tmp_path / "r.db"
        s = open_store(db)
        seed(s, 100)
        make(s, fake).run(max_blocks=20)
        monkeypatch.undo()                                          # "the bug is fixed"
        assert facts(s) == 20
        res = make(s, fake).reprocess_quarantine()
        assert res["ok"] == [110] and res["failed"] == [] and res["still_quarantined"] == 0
        row = s.con.execute("SELECT status, n_facts, quarantined_at, quarantine_error FROM blocks WHERE height = 110"
                            ).fetchone()
        assert row[0] == "ok" and row[1] == 1 and row[2] and "cannot read" in row[3]    # history kept
        assert facts(s) == 21 and s.get_status()["last_height"] == 120                 # live status not moved back
        st = status(s)
        assert st["quarantined_open"] == 0 and st["quarantined_total"] == 1 and st["quarantined_heights"] == []
        assert st["last_error"]["resolved_at"] and st["quarantine_reprocess_last"]["ok"] == [110]
        # nothing left: a re-run does nothing, no duplicates; a height that is not quarantined is refused
        assert make(s, fake).reprocess_quarantine()["requested"] == 0 and facts(s) == 21
        with pytest.raises(ValueError, match="not in quarantine"):
            make(s, fake).reprocess_quarantine(110)
        s.close()
        assert main(["reprocess-quarantine", "--db", str(db), "--base", fake.base, "--rps", "500",
                     "--height", "111"]) == 2
        assert "not in quarantine" in json.loads(capsys.readouterr().out)["error"]


def test_reprocess_of_a_height_that_still_fails_keeps_it_in_quarantine(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "detect_in_block", failing_detect({105}))
    with FakeNeardata(100, 110) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        make(s, fake).run(max_blocks=10)
        monkeypatch.setattr(L, "detect_in_block", failing_detect({105}, exc=lambda h: KeyError("still broken")))
        res = make(s, fake).reprocess_quarantine()
        assert res["ok"] == [] and res["failed"][0]["height"] == 105 and res["still_quarantined"] == 1
        assert "still broken" in s.quarantined_open()[0][1]


def _launch_fake(lo, hi, launch_at, token="qqq.nearlytrade.near"):
    def make_block(h):
        if h != launch_at:
            return transfer_block(h)
        ev = "EVENT_JSON:" + json.dumps({"standard": "nearly", "event": "launch_started", "data": [
            {"creator": "maker.near", "token": token, "pool_id": f"{token}|wrap.near|10000", "dev_buy": "0"}]})
        return block(h, [reo(hid("l", h), hid("lt", h), "maker.near", "nearlytrade.near", [], logs=[ev],
                             executor="nearlytrade.near")])
    return FakeNeardata(lo, hi, make=make_block)


def test_no_snapshot_while_its_window_holds_a_quarantined_height_then_exactly_one(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "detect_in_block", failing_detect({200}))
    with _launch_fake(100, 460, launch_at=120) as fake:            # 1 block / s: T+5 of height 120 = height 420
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        make(s, fake).run(max_blocks=360)                          # 100..460, 200 quarantined
        assert s.is_quarantined(200) and s.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0] == 1
        assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 0     # blocked by the guard
        assert S.rebuild_snapshots(s)["not_covered"] == 1                                   # rebuild obeys it too
        assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 0
        monkeypatch.undo()
        make(s, fake).reprocess_quarantine()
        rows = s.con.execute("SELECT minute, error, computed_at FROM launch_snapshots").fetchall()
        assert [(m, e) for m, e, _c in rows] == [(5, None)]
        fake.hi = fake.head = 480
        make(s, fake).run(max_blocks=20)                           # later blocks: the hook does not write it again
        assert s.con.execute("SELECT minute, computed_at FROM launch_snapshots").fetchall() == [(5, rows[0][2])]


def test_window_covered_needs_every_height_ok_or_null(tmp_path):
    from test_launches import collector
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    ts = lambda h: int(transfer_block(h)["block"]["header"]["timestamp_nanosec"])     # noqa: E731
    for h in (10, 11, 13):
        c.process(h, transfer_block(h))
    assert not S.window_covered(s, 10, ts(12))                     # 12 missing
    c.process(12, None)
    assert S.window_covered(s, 10, ts(12))                         # null counts as covered
    assert not S.window_covered(s, 10, ts(14))                     # the cutoff is not reached yet


def test_status_is_written_on_the_failure_path_and_a_stale_head_never_reads_0(tmp_path, monkeypatch):
    monkeypatch.setattr(colmod, "STATUS_EVERY_S", 0.1)
    with FakeNeardata(100, 130) as fake:
        fake.fail = {105: 10**6}                                     # the source fails on 105 for good
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = Collector(s, Neardata(EnvKeySource({}), base=fake.base, rps=500, stop=threading.Event()),
                        log=lambda m: None, retry_delay=0.5)          # waits grow to 60 s between attempts
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            ro = open_store(tmp_path / "r.db", readonly=True)
            assert wait_for(lambda: ro.get_status().get("last_height") == 104)
            assert wait_for(lambda: (ro.con.execute("SELECT attempts FROM blocks WHERE height=105").fetchone()
                                     or [0])[0] >= 3)
            first = ro.get_status()["status_written_at"]
            assert wait_for(lambda: ro.get_status()["status_written_at"] != first, timeout=3)   # while waiting
        finally:
            col.stop.set()
            t.join(10)
        st = status(s)
        assert st["last_height"] == 104 and st["retrying_heights"][0]["height"] == 105
        assert st["retrying_heights"][0]["since"] and st["last_ok_commit_at"] and st["status_age_s"] < 5
        assert st["head_stale"] is False and st["lag_blocks"] == 26 and st["catch_up_remaining"] == 26
        later = status(s, now_ns=time.time_ns() + 600 * 10**9)      # 10 min later, nothing written since
        assert later["head_stale"] is True and later["final_head_age_s"] > 590
        assert later["lag_blocks"] is None and later["catch_up_remaining"] is None and later["catch_up_eta_s"] is None
        assert later["status_age_s"] > 590


def test_launchbot_owner_found_after_a_late_height(tmp_path):
    """The payment to launchbot was in a height processed late (reprocess-quarantine): the owner is found then."""
    from test_launches import LB, NEAR, collector, launch_block, pay_block
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    dep = 3 * NEAR
    c.process(1020, launch_block(1020, "eee.nearlytrade.near", dep, hid("tl", 9)))
    assert s.con.execute("SELECT owner FROM launches").fetchone()[0] is None
    c.process(1010, pay_block(1010, "owner.near", dep + 2 * 10**22), live=False)       # the late height
    assert L.resolve_late_owners(s, [1010]) == ["eee.nearlytrade.near"]
    owner, ev, extra = s.con.execute("SELECT owner, owner_evidence, extra FROM launches").fetchone()
    assert (owner, ev) == ("owner.near", hid("rp", 1010)) and "owner_unresolved" not in json.loads(extra)
    assert L.resolve_late_owners(s, [1010]) == []                                       # nothing left
    assert LB
