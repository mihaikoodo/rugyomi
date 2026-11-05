"""The follow loop against a fake neardata: nulls, waiting at the tip, retries without gaps, kill + restart,
SIGTERM, the CLI and the key on every hop."""
import json
import os
import signal
import subprocess
import sys
import threading
import time

from rugyomi_testkit import PUBLIC, FakeNeardata
from rugyomi.collector import Collector, main, status
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.schema import SCHEMA_VERSION

KEY = "k" * 64


def make(store, fake, keys=None, **kw):
    client = Neardata(keys or EnvKeySource({}), base=fake.base, rps=500, stop=threading.Event())
    return Collector(store, client, log=lambda m: None, retry_delay=0.05, **kw)


def seed(store, start):
    store.begin()
    store.set_status("start_height", start)
    store.commit()


def done(store):
    return [r for r in store.con.execute("SELECT height, status FROM blocks ORDER BY height")]


def wait_for(pred, timeout=20.0):
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_empty_db_starts_at_the_final_head(tmp_path):
    with FakeNeardata(100, 140) as fake:
        s = open_store(tmp_path / "r.db")
        col = make(s, fake)
        assert col.run(max_blocks=1) == 141
        assert s.get_status()["start_height"] == 140 and done(s) == [(140, "ok")]


def test_catch_up_with_nulls_in_order(tmp_path):
    with FakeNeardata(100, 160, nulls={103, 104, 150}) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        assert col.run(max_blocks=61) == 161
        rows = done(s)
        assert [h for h, _ in rows] == list(range(100, 161))
        assert {h for h, st in rows if st == "null"} == {103, 104, 150}
        assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 58
        assert s.missing_heights(100, 161) == []
        assert status(s)["missing_heights"] == []


def test_waits_at_the_tip_and_never_writes_a_future_height(tmp_path):
    with FakeNeardata(100, 200) as fake:
        fake.head = 110
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            assert wait_for(lambda: open_store(tmp_path / "r.db", readonly=True).get_status().get("last_height") == 110)
            time.sleep(0.6)
            ro = open_store(tmp_path / "r.db", readonly=True)
            assert ro.con.execute("SELECT MAX(height) FROM blocks").fetchone()[0] == 110   # 111 not written as null
            fake.head = 115
            assert wait_for(lambda: ro.get_status().get("last_height") == 115)
            assert [st for _h, st in ro.con.execute("SELECT height, status FROM blocks WHERE height > 110")] == ["ok"] * 5
        finally:
            col.stop.set()
            t.join(10)
        assert not t.is_alive()


def test_an_erroring_height_is_retried_without_a_gap(tmp_path):
    with FakeNeardata(100, 130) as fake:
        fake.fail = {110: 3}
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        assert col.run(max_blocks=31) == 131
        assert [h for h, _ in done(s)] == list(range(100, 131))
        assert s.con.execute("SELECT status, attempts, error FROM blocks WHERE height=110").fetchone() == ("ok", 4, None)
        assert s.get_status()["last_error"]["height"] == 110
        assert sum(1 for p in fake.block_paths() if p == "/v0/block/110") == 4


def test_key_on_every_hop_and_status_keys(tmp_path):
    with FakeNeardata(100, 105) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake, keys=EnvKeySource({"FASTNEAR_API_KEY": f"{KEY}:5000"}))
        col.run(max_blocks=6)
        assert all(k == KEY for _p, k in fake.hits)
        st = status(s)
        assert st["keys"][0]["spent_this_run"] == len(fake.hits) and KEY not in json.dumps(st)


def _spawn(db, fake, *extra):
    env = {**os.environ, "PYTHONPATH": str(PUBLIC), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("FASTNEAR_API_KEY", None)
    return subprocess.Popen([sys.executable, "-m", "rugyomi.collector", "run", "--db", str(db), "--base", fake.base,
                             "--rps", "500", *extra], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_kill_and_restart_resumes_from_the_cursor_then_sigterm_exits_cleanly(tmp_path):
    db = tmp_path / "r.db"
    with FakeNeardata(1000, 1300, nulls={1005, 1206}) as fake:
        fake.latency = 0.01
        s = open_store(db)
        seed(s, 1000)
        s.close()
        p = _spawn(db, fake)
        ro = open_store(db, readonly=True)
        assert wait_for(lambda: (ro.get_status().get("last_height") or 0) >= 1080)
        p.kill()                                                     # SIGKILL: no chance to clean up
        p.wait(10)
        p2 = _spawn(db, fake)
        assert wait_for(lambda: ro.get_status().get("last_height") == 1300, timeout=60)
        p2.send_signal(signal.SIGTERM)
        out, err = p2.communicate(timeout=30)
        assert p2.returncode == 0, err
        assert "finishing the current block" in err
        heights = [h for (h,) in ro.con.execute("SELECT height FROM blocks WHERE status IN ('ok','null') ORDER BY height")]
        assert heights == list(range(1000, 1301))
        assert ro.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 301 - 2      # one per block, no duplicates
        assert ro.missing_heights(1000, 1301) == []
        ro.close()
        s2 = open_store(db)
        assert s2.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        s2.close()


def test_cli_help_and_status_offline(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0
    assert main(["status", "--db", str(tmp_path / "none.db")]) == 1
    s = open_store(tmp_path / "r.db")
    s.close()
    capsys.readouterr()
    assert main(["status", "--db", str(tmp_path / "r.db")]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["schema_version"] == SCHEMA_VERSION and st["missing_heights"] == [] and st["last_height"] is None


def test_status_write_with_real_parse_stats(tmp_path):
    """Regression (30.10, server): a failed receipt fills ParseStats.skipped_failed (a Counter); the periodic
    status write must still serialise, otherwise final_head / keys / http / collector stay null."""
    from rugyomi_testkit import block, hid, reo
    with FakeNeardata(100, 101) as fake:
        s = open_store(tmp_path / "r.db")
        col = make(s, fake, keys=EnvKeySource({"FASTNEAR_API_KEY": KEY}))
        col.process(100, block(100, [reo(hid("r", 1), hid("t", 1), "a.near", "b.near",
                                         [{"Transfer": {"deposit": "1"}}], ok=False)]))
        assert col.stats.skipped_failed["near_transfer"] == 1
        col.known_head = 101
        col.write_status(force=True)
        st = status(s)
        assert st["status_write_error"] is None
        assert st["final_head"] == 101 and st["keys"] and st["http"] is not None and st["collector"]["pid"]
        s.begin(); s.set_status("last_height", 105); s.commit()          # passed the recorded head
        assert status(s)["lag_blocks"] == 0
        assert s.get_status()["parse_stats"]["skipped_failed"] == {"near_transfer": 1}


def test_a_failing_status_write_is_shown_by_status(tmp_path, monkeypatch):
    with FakeNeardata(100, 101) as fake:
        s = open_store(tmp_path / "r.db")
        col = make(s, fake)
        monkeypatch.setattr(col.client.keys, "status", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        col.write_status(force=True)
        assert "boom" in status(s)["status_write_error"]["error"]


def test_tip_race_a_null_before_finality_is_not_stored(tmp_path):
    """RGY-005 #1: h answers null while not final, then the final head passes h before the client reads it.
    The old loop stored a permanent null for an existing block; the null must only count when the head was
    read BEFORE the request."""
    with FakeNeardata(100, 130) as fake:
        fake.head = 105
        fake.bump_on_null = {106: 109}
        s = open_store(tmp_path / "r.db")
        seed(s, 100)
        col = make(s, fake)
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            assert wait_for(lambda: open_store(tmp_path / "r.db", readonly=True).get_status().get("last_height") == 109)
        finally:
            col.stop.set()
            t.join(10)
        rows = dict(s.con.execute("SELECT height, status FROM blocks WHERE height BETWEEN 106 AND 109"))
        assert rows == {106: "ok", 107: "ok", 108: "ok", 109: "ok"}
        assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 10


def test_recheck_nulls_recovers_wrong_nulls_without_duplicates(tmp_path, capsys):
    """RGY-005 #2: a null stored for an existing block (the old tip race) is recovered; true nulls stay; re-run = 0."""
    with FakeNeardata(100, 110, nulls={104}) as fake:
        db = tmp_path / "r.db"
        s = open_store(db)
        seed(s, 100)
        col = make(s, fake)
        for h in range(100, 111):
            col.process(h, None if h in (104, 106, 107) else fake.make(h))   # 106, 107: wrong nulls
        s.begin(); s.set_status("last_height", 110); s.commit()
        res = col.recheck_nulls()
        assert (res["null_heights"], res["checked"], res["recovered"], res["recovered_heights"]) == (3, 3, 2, [106, 107])
        assert dict(s.con.execute("SELECT height, status FROM blocks WHERE height IN (104,106,107)")) == \
            {104: "null", 106: "ok", 107: "ok"}
        assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 10
        assert s.get_status()["last_height"] == 110                    # the cursor status is not moved back
        before = s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        res2 = col.recheck_nulls()
        assert (res2["checked"], res2["recovered"]) == (1, 0)
        assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == before
        st = status(s)
        assert st["null_heights"] == 1 and st["null_recheck_totals"] == {"runs": 2, "checked": 4, "recovered": 2}
        s.close()
        # the CLI command
        s = open_store(db)
        s.begin(); s.mark_block(108, "null", None, 0); s.commit(); s.close()
        assert main(["recheck-nulls", "--db", str(db), "--base", fake.base, "--rps", "500"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["recovered_heights"] == [108]


def test_neardata_scrubs_the_rejection_body_before_cutting(tmp_path):
    """RGY-005 #3 for the product client: what reaches KeySource.rejected() holds no key fragment."""
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    key = hashlib.sha256(b"neardata-straddle").hexdigest()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = (("x" * 180) + key).encode()
            self.send_response(402)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen = []

    class Rec(EnvKeySource):
        def rejected(self, k, status, body_snippet=""):
            seen.append(body_snippet)
            super().rejected(k, status, body_snippet)
    try:
        client = Neardata(Rec({"FASTNEAR_API_KEY": key}), base=f"http://127.0.0.1:{srv.server_address[1]}", rps=500)
        import pytest
        with pytest.raises(Exception):
            client.block(1)            # the key is rejected; the next request goes keyless and is rejected too
        assert seen and all(len(b) <= 200 for b in seen)
        for b in seen:
            for i in range(len(key) - 7):
                assert key[i:i + 8] not in b
    finally:
        srv.shutdown()


# ---------------------------------------------------------------- RGY-008: extend-start fills a gap

from rugyomi.collector import extend_start  # noqa: E402
import rugyomi.collector as colmod  # noqa: E402


def test_extend_start_only_lowers(tmp_path):
    import pytest
    s = open_store(tmp_path / "r.db")
    with pytest.raises(ValueError, match="no start height"):
        extend_start(s, 10)
    seed(s, 150)
    with pytest.raises(ValueError, match="only lowers"):
        extend_start(s, 151)
    assert extend_start(s, 150) == {"start_height": 150, "changed": False}
    r = extend_start(s, 100)
    assert r["changed"] and r["previous"] == 150 and r["gap_heights"] == 50
    st = s.get_status()
    assert st["start_height"] == 100 and st["start_height_changes"][0]["from"] == 150
    assert main(["extend-start", "--db", str(tmp_path / "r.db"), "--to", "120"]) == 2       # raising: refused


def test_gap_is_filled_in_order_without_reprocessing_and_status_counts_it(tmp_path):
    with FakeNeardata(100, 200) as fake:
        s = open_store(tmp_path / "r.db")
        seed(s, 150)
        make(s, fake).run(max_blocks=51)                     # 150..200 live
        assert s.cursor() == 201
        extend_start(s, 100)
        col = make(s, fake)
        col.known_head = 200
        col.write_status(force=True)
        st = status(s)
        assert st["catch_up_remaining"] == 50 and st["cursor"] == 100 and st["missing_heights"] == []
        before = len(fake.block_paths())
        assert make(s, fake).run(max_blocks=50) == 201       # the gap, then straight to the next height not done
        paths = fake.block_paths()[before:]
        assert sorted({int(p.rsplit("/", 1)[1]) for p in paths}) == list(range(100, 150))   # nothing re-fetched
        order = [h for (h,) in s.con.execute("SELECT height FROM blocks WHERE height < 150 ORDER BY processed_at, rowid")]
        assert order == list(range(100, 150))                # committed strictly in height order
        assert s.missing_heights(100, 201) == [] and s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 101
        before = s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        assert make(s, fake).run(max_blocks=0) == 201        # re-run: nothing to do, nothing inserted
        assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == before


def test_a_running_collector_goes_back_for_the_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(colmod, "CURSOR_EVERY_S", 0.2)
    with FakeNeardata(100, 200) as fake:
        fake.head = 160
        s = open_store(tmp_path / "r.db")
        seed(s, 150)
        col = make(s, fake)
        t = threading.Thread(target=col.run, daemon=True)
        t.start()
        try:
            ro = open_store(tmp_path / "r.db", readonly=True)
            assert wait_for(lambda: ro.get_status().get("last_height") == 160)
            other = open_store(tmp_path / "r.db")                # a second, short-lived writer (the CLI)
            extend_start(other, 120)
            other.close()
            assert wait_for(lambda: ro.undone_count(120, 160) == 0)
            fake.head = 170
            assert wait_for(lambda: ro.get_status().get("last_height") == 170)
        finally:
            col.stop.set()
            t.join(10)
        assert ro.missing_heights(120, 171) == []
        assert ro.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 51


def _launch_chain(lo, hi, launch_at, token="ggg.nearlytrade.near", step_s=10):
    """A synthetic chain with one nearly launch; one block every `step_s` seconds."""
    from rugyomi_testkit import T0, block, hid, reo

    def make_block(h):
        ts = T0 + h * step_s * 10**9
        if h != launch_at:
            return block(h, [reo(hid("r", h), hid("t", h), "alice.near", "bob.near",
                                 [{"Transfer": {"deposit": str(h)}}])], ts_ns=ts)
        ev = "EVENT_JSON:" + json.dumps({"standard": "nearly", "event": "launch_started", "data": [
            {"creator": "maker.near", "token": token, "pool_id": f"{token}|wrap.near|10000", "dev_buy": "0"}]})
        return block(h, [reo(hid("l", h), hid("lt", h), "maker.near", "nearlytrade.near", [], logs=[ev],
                             executor="nearlytrade.near")], ts_ns=ts)
    return FakeNeardata(lo, hi, make=make_block)


def test_late_snapshot_of_a_backfilled_launch_fires_once(tmp_path):
    """A launch inside the filled gap: T+5/15/30 fall inside the gap (computed in order while it is filled), the
    T+60 cutoff lies in heights processed BEFORE the gap — computed once, right after the gap, from facts already
    in the DB. A real re-run (the collector processes new blocks after it) changes nothing."""
    with _launch_chain(100, 700, launch_at=120) as fake:      # 10 s per block: T+5/15/30/60 = 150/210/300/480
        s = open_store(tmp_path / "r.db")
        seed(s, 400)
        fake.head = 600
        make(s, fake).run(max_blocks=201)                    # 400..600 live (T+60 at 480 passes with no launch yet)
        assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0] == 0
        extend_start(s, 100)
        make(s, fake).run(max_blocks=300)                    # the gap 100..399
        rows = s.con.execute("SELECT minute, at_ts_ns, error, computed_at FROM launch_snapshots ORDER BY minute"
                             ).fetchall()
        assert [(m, e) for m, _t, e, _c in rows] == [(5, None), (15, None), (30, None), (60, None)]
        t120 = int(fake.make(120)["block"]["header"]["timestamp_nanosec"])
        assert [t for _m, t, _e, _c in rows] == [t120 + m * 60 * 10**9 for m in (5, 15, 30, 60)]
        assert s.con.execute("SELECT token FROM launches").fetchall() == [("ggg.nearlytrade.near",)]
        # T+60 counted the facts up to its cutoff (height 480), which were processed before the gap
        n_swaps_60 = s.con.execute("SELECT n_swaps FROM launch_snapshots WHERE minute = 60").fetchone()[0]
        assert n_swaps_60 == 0
        # a REAL re-run: the collector goes on with new blocks (601..700); every hook call sees the rows as done
        fake.head = 700
        before = len(fake.block_paths())
        assert make(s, fake).run(max_blocks=100) == 701
        assert len(fake.block_paths()) > before
        assert s.con.execute("SELECT minute, at_ts_ns, error, computed_at FROM launch_snapshots ORDER BY minute"
                             ).fetchall() == rows
        assert s.missing_heights(100, 701) == []
