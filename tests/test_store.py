"""rugyomi.store: migrations, one transaction per block, idempotent re-processing, parse errors, cursor and gaps."""
import json
import sqlite3
import threading

import pytest

from rugyomi_testkit import block, fixture_blocks, hid, reo, transfer_block
from rugyomi.collector import Collector
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.schema import SCHEMA_VERSION


def collector(store):
    return Collector(store, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()),
                     log=lambda m: None)


def counts(con):
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("facts", "accounts", "kinds", "blocks", "parse_errors", "launches")}


@pytest.mark.core
def test_migrations_on_an_empty_db_and_reopen(tmp_path):
    db = tmp_path / "r.db"
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION >= 3
    tables = {r[0] for r in s.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"accounts", "kinds", "facts", "blocks", "parse_errors", "launches", "collector_status",
            "launch_snapshots", "manual_labels"} <= tables
    assert s.con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.close()
    s2 = open_store(db)                       # existing database: opens, nothing re-applied
    assert s2.user_version() == SCHEMA_VERSION and s2.migrate() == SCHEMA_VERSION
    assert s2.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s2.close()


@pytest.mark.core
def test_newer_schema_is_refused(tmp_path):
    db = tmp_path / "r.db"
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version=99")
    con.close()
    with pytest.raises(RuntimeError, match="newer"):
        open_store(db)


def test_reprocessing_a_block_inserts_nothing(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    blocks = [(h, b) for h, b in fixture_blocks("cache") if b is not None][:3]
    for h, b in blocks:
        assert c.process(h, b) > 0
    before = counts(s.con)
    for h, b in blocks:
        assert c.process(h, b) == 0
    assert counts(s.con) == before
    attempts = s.con.execute("SELECT attempts FROM blocks WHERE height=?", (blocks[0][0],)).fetchone()[0]
    assert attempts == 2


@pytest.mark.core
def test_stored_keys_match_the_parser_and_are_unique(tmp_path):
    from rugyomi.parse import keep_option_a, parse_block
    from rugyomi.store.db import hash_str
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    h, b = next((h, b) for h, b in fixture_blocks("cache") if b is not None)
    c.process(h, b)
    stored = {(hash_str(r), o) for r, o in s.con.execute("SELECT receipt, origin FROM facts")}
    assert stored == {f.key for f in parse_block(b) if keep_option_a(f)}


@pytest.mark.core
def test_a_parse_error_is_a_row_and_the_receipt_keeps_its_facts(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    rid = hid("r", 7)
    b = block(7, [reo(rid, hid("t", 7), "alice.near", "intents.near", [{"Transfer": {"deposit": "9"}}],
                      logs=["EVENT_JSON:{nope"], executor="intents.near")])
    assert c.process(7, b) == 1
    assert s.con.execute("SELECT height, receipt_id, origin FROM parse_errors").fetchall() == [(7, rid, "l0")]
    assert s.con.execute("SELECT status, n_facts FROM blocks WHERE height=7").fetchone() == ("ok", 1)


@pytest.mark.core
def test_a_failure_inside_the_block_transaction_leaves_nothing(tmp_path, monkeypatch):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    before = counts(s.con)
    import rugyomi.launches as L
    monkeypatch.setattr(L, "detect_in_block", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        c.process(10, transfer_block(10))
    assert counts(s.con) == before                      # no half-written block, not even its accounts
    monkeypatch.undo()
    assert c.process(10, transfer_block(10)) == 1       # the account cache was rolled back with the transaction
    assert s.con.execute("SELECT COUNT(*) FROM accounts WHERE name IN ('alice.near','bob.near')").fetchone()[0] == 2


@pytest.mark.core
def test_cursor_and_gaps(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    s.begin(); s.set_status("start_height", 100); s.commit()
    assert s.cursor() == 100
    for h in (100, 101, 102, 105):
        c.process(h, transfer_block(h) if h != 101 else None)
    assert s.cursor() == 103
    assert s.missing_heights(100, 106) == [[103, 104]]
    assert s.missing_heights(100, 103) == []
    s.record_error(103, "HTTP 500")
    assert s.cursor() == 103                           # an error row is not "done"
    assert s.con.execute("SELECT status, attempts FROM blocks WHERE height=103").fetchone() == ("error", 1)
    c.process(103, transfer_block(103))
    assert s.con.execute("SELECT status, attempts, error FROM blocks WHERE height=103").fetchone() == ("ok", 2, None)
    st = s.get_status()
    assert json.loads(json.dumps(st["last_error"]))["height"] == 103


@pytest.mark.core
def test_hash_starting_with_0xff_round_trips_and_does_not_block_a_block(tmp_path):
    """Regression (31.10, server stuck on 222446037): a real 32-byte hash whose first byte is 0xff was read back as
    the text-fallback marker -> UnicodeDecodeError on every retry of the block (launch detection reads tx hashes)."""
    import json as _json
    import os
    from rugyomi.store.db import b58encode, hash_bytes, hash_str
    for first in (b"\xff", b"\x00", b"\x7f"):
        h = b58encode(first + b"A\xf9" + os.urandom(29))
        assert len(hash_bytes(h)) == 32 and hash_str(hash_bytes(h)) == h
    for odd in ("not-base58-0", "x" * 31, "y" * 40, "1"):                 # never on NEAR: text fallback, any length
        b = hash_bytes(odd)
        assert len(b) != 32 and hash_str(b) == odd
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    tx = b58encode(b"\xffA\xf9" + bytes(range(29)))
    ev = "EVENT_JSON:" + _json.dumps({"standard": "nearly", "event": "launch_started", "data": [
        {"creator": "maker.near", "token": "ff.nearlytrade.near", "pool_id": "ff.nearlytrade.near|wrap.near|10000",
         "dev_buy": "0"}]})
    c.process(77, block(77, [reo(hid("rff"), tx, "maker.near", "nearlytrade.near", [], logs=[ev],
                                 executor="nearlytrade.near")]))
    assert s.con.execute("SELECT tx FROM launches").fetchone()[0] == tx
