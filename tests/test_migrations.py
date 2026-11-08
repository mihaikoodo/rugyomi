"""Every migration on an existing database keeps the rows of older tables byte-identical (master §13).

A real v1 database is filled by the v1 collector code path (real mainnet fixture blocks), then migrated step by
step v1 -> v2 -> … -> latest; after each step the v1 rows of facts / blocks / launches / accounts / kinds are
compared byte for byte (and later-version rows written in between, too)."""
import shutil
import sqlite3
import threading

import pytest

from rugyomi_testkit import fixture_blocks
import rugyomi.snapshots as snapmod
import rugyomi.store.db as dbmod
from rugyomi.collector import Collector
from rugyomi.keys import EnvKeySource
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.schema import SCHEMA_VERSION

V1_TABLES = ("facts", "blocks", "launches", "accounts", "kinds", "parse_errors")


# v5 (RGY-012) added nullable columns to blocks: the v1 columns are compared byte for byte, the new ones are NULL
# v6 (RGY-011) added the nullable `launches.coverage`
V1_COLUMNS = {"blocks": "height, status, block_ts_ns, n_facts, error, attempts, processed_at",
              "launches": "token, launchpad, creator, owner, owner_evidence, pool, dev_buy, height, ts_ns, tx, "
                          "detected_at, extra"}


def dump(path, tables):
    con = sqlite3.connect(path)
    out = {t: con.execute(f"SELECT {V1_COLUMNS.get(t, '*')} FROM {t} ORDER BY rowid").fetchall() for t in tables}
    con.close()
    return out


def open_at(path, version):
    """Open with the code limited to `version` (as an older release of the collector would)."""
    orig = dbmod.SCHEMA_VERSION
    dbmod.SCHEMA_VERSION = version
    try:
        return open_store(path)
    finally:
        dbmod.SCHEMA_VERSION = orig


@pytest.fixture
def v1_db(tmp_path, monkeypatch):
    db = tmp_path / "v1.db"
    s = open_at(db, 1)
    assert s.user_version() == 1
    monkeypatch.setattr(snapmod, "compute_due", lambda *a: 0)      # the v1 collector had no snapshots
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    blocks = dict(fixture_blocks("refrax"))                          # trimmed: heights in between were not kept
    for h in range(min(blocks), max(blocks) + 1):
        c.process(h, blocks.get(h))                                   # ... stored as null: a contiguous collection
    s.close()
    monkeypatch.undo()
    assert dump(db, ["facts"])["facts"]
    return db


def test_each_step_keeps_old_rows_byte_identical(v1_db, tmp_path):
    db = tmp_path / "copy.db"
    shutil.copy(v1_db, db)
    before = dump(db, V1_TABLES)
    for version in range(2, SCHEMA_VERSION + 1):
        s = open_at(db, version)
        assert s.user_version() == version
        assert s.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        s.close()
        assert dump(db, V1_TABLES) == before, f"v1 rows changed by migration to v{version}"
    s = open_store(db)                                       # the current code: nothing left to apply
    assert s.user_version() == SCHEMA_VERSION and s.migrate() == SCHEMA_VERSION
    s.close()
    assert dump(db, V1_TABLES) == before


V2_SNAP_COLS = ("token, minute, at_ts_ns, n_swaps, unattributed_swaps, price_last, price_first_buyer, max_x, "
                "dev_acquired, dev_sold, dev_sold_share_bps, dev_transferred_out, dev_first_sell_after_s, "
                "buyers_count, buyers_spent, buyers_received, buyers_unsold_value, buyers_pnl, holders_count, "
                "top10_share_bps, metric_version, computed_at, error")
V8_SNAP_COLS = ("price_peak", "peak_at", "insiders_count", "insiders_acquired", "insiders_sold",
                "insiders_sold_share_bps", "insiders_pnl", "insiders_evidence")


def test_rows_written_at_v2_survive_the_v8_table_rebuild(v1_db, tmp_path):
    """v8 REBUILDS launch_snapshots (the CHECK had to accept 1440): every row is copied unchanged, the new columns
    are NULL (= computed before v8: metric_version 1) and the table accepts a T+24 h row. Refilling them is
    `rebuild-snapshots` (test_snapshots: a rebuilt row has metric_version 3 and the insider columns)."""
    db = tmp_path / "copy.db"
    shutil.copy(v1_db, db)
    s = open_at(db, 2)
    for token, ts in s.con.execute("SELECT token, ts_ns FROM launches").fetchall():
        for minute in (5, 15, 30, 60):                       # rows as the v2 code wrote them (v1 metrics, no insiders)
            m = snapmod.compute(s, token, minute)
            cols = ("token", "minute", "at_ts_ns") + tuple(c for c in snapmod.COLUMNS if c not in V8_SNAP_COLS)
            s.con.execute(f"INSERT INTO launch_snapshots({','.join(cols)}, metric_version, computed_at) "
                          f"VALUES({','.join('?' * (len(cols) + 2))})",
                          (token, minute, m["at_ts_ns"], *(m[c] for c in cols[3:]), 1, "2026-10-30T00:00:00Z"))
    s.close()
    con = sqlite3.connect(db)
    old = con.execute(f"SELECT {V2_SNAP_COLS} FROM launch_snapshots ORDER BY token, minute").fetchall()
    con.close()
    assert len(old) >= 4
    s = open_store(db)                                       # the current code: v2 -> ... -> latest
    assert s.user_version() == SCHEMA_VERSION
    assert s.con.execute(f"SELECT {V2_SNAP_COLS} FROM launch_snapshots ORDER BY token, minute").fetchall() == old
    assert s.con.execute(f"SELECT COUNT(*) FROM launch_snapshots WHERE {' IS NOT NULL OR '.join(V8_SNAP_COLS)} "
                         "IS NOT NULL").fetchone()[0] == 0
    assert {r[1] for r in s.con.execute("PRAGMA table_info(launch_snapshots)")} >= set(V8_SNAP_COLS)
    assert s.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    token = old[0][0]
    s.begin()                                                # a T+24 h row is now allowed
    s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at) "
                  "VALUES(?, 1440, 1, 2, 'x')", (token,))
    s.rollback()
    assert snapmod.METRIC_VERSION == 3                        # the copied rows say 1: they are told apart from rebuilt ones
    assert {r[0] for r in s.con.execute("SELECT metric_version FROM launch_snapshots")} == {1}
    s.close()


def test_rows_written_at_v3_survive_later_migrations(v1_db, tmp_path):
    db = tmp_path / "copy.db"
    shutil.copy(v1_db, db)
    s = open_at(db, 3)
    s.begin()
    s.con.execute("INSERT INTO manual_labels(account,label,reason,source,set_by,set_at) "
                  "VALUES('a.near','independent_sniper','manual: test','manual','cli','2026-10-31T00:00:00Z')")
    s.commit()
    s.close()
    before = dump(db, V1_TABLES + ("manual_labels",))
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION
    s.close()
    assert dump(db, V1_TABLES + ("manual_labels",)) == before


def test_v5_quarantine_columns_are_null_on_old_rows_and_the_cursor_is_unchanged(v1_db, tmp_path):
    db = tmp_path / "copy.db"
    shutil.copy(v1_db, db)
    s = open_at(db, 4)
    s.begin()
    s.mark_block(10**9, "error", None, 0, "an old failing height")      # a height failing before v5
    s.commit()
    before_cursor = s.cursor(start=s.done_heights_range()[0])
    s.close()
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION
    assert s.con.execute("SELECT COUNT(*) FROM blocks WHERE quarantined_at IS NOT NULL OR quarantine_error IS NOT "
                         "NULL OR process_errors IS NOT NULL OR error_since IS NOT NULL").fetchone()[0] == 0
    assert s.cursor(start=s.done_heights_range()[0]) == before_cursor
    assert s.quarantined_open() == [] and not s.is_quarantined(10**9)
    from rugyomi.collector import status
    st = status(s)
    assert st["quarantined_open"] == 0 and st["retrying_heights"][0]["height"] == 10**9
    s.close()


def test_v9_adds_launch_outcomes_and_keeps_every_old_row(tmp_path):
    """RGY-017: a database written by the v8 code (snapshots with the insider columns, cards) gets the empty
    launch_outcomes table; nothing else changes; `rebuild-outcomes` then fills it."""
    import threading
    from rugyomi.collector import Collector, main as collector_main
    from rugyomi.keys import EnvKeySource
    from rugyomi.neardata import Neardata
    from rugyomi_testkit import refrax_replay_blocks
    db = tmp_path / "v8.db"
    s = open_at(db, 8)
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in refrax_replay_blocks():
        c.process(h, b)
    assert s.user_version() == 8 and not s.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'launch_outcomes'").fetchone()
    before = {t: s.con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()
              for t in ("launch_snapshots", "cards", "launches", "blocks")}
    n_facts = s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    s.close()
    s = open_store(db)                                                    # the current code: v8 -> v9
    assert s.user_version() == SCHEMA_VERSION >= 9
    assert s.con.execute("SELECT COUNT(*) FROM launch_outcomes").fetchone()[0] == 0
    # later migrations append nullable columns (v11: launch_snapshots.source): compare the columns the v8 database had
    assert {t: [r[:len(before[t][0])] for r in s.con.execute(f"SELECT * FROM {t} ORDER BY 1, 2")] for t in before} == before
    assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == n_facts
    s.close()
    assert collector_main(["rebuild-outcomes", "--db", str(db)]) == 0
    s = open_store(db)
    assert s.con.execute("SELECT COUNT(*), SUM(rug) FROM launch_outcomes").fetchone() == (3, 3)
    s.close()


def test_v11_adds_source_columns_and_the_backfill_tables_and_keeps_every_old_row(tmp_path):
    """RGY-020: a v10 database gets launch_snapshots.source / launch_outcomes.source (NULL = live), api_facts and backfill_queue;
    every old row is kept and reads source NULL."""
    import threading
    from rugyomi.collector import Collector
    from rugyomi.keys import EnvKeySource
    from rugyomi.neardata import Neardata
    from rugyomi_testkit import refrax_replay_blocks
    db = tmp_path / "v10.db"
    s = open_at(db, 10)
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in refrax_replay_blocks():
        c.process(h, b)
    assert s.user_version() == 10
    snaps = s.con.execute("SELECT COUNT(*) FROM launch_snapshots").fetchone()[0]
    outs = s.con.execute("SELECT COUNT(*) FROM launch_outcomes").fetchone()[0]
    facts = s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    s.close()
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION >= 11
    assert s.con.execute("SELECT COUNT(*) FROM launch_snapshots WHERE source IS NULL").fetchone()[0] == snaps > 0
    assert s.con.execute("SELECT COUNT(*) FROM launch_outcomes WHERE source IS NULL").fetchone()[0] == outs
    assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == facts
    assert s.con.execute("SELECT COUNT(*) FROM api_facts").fetchone()[0] == 0
    assert s.con.execute("SELECT COUNT(*) FROM backfill_queue").fetchone()[0] == 0
    s.close()


def test_v12_adds_transfers_api_and_keeps_every_old_row(tmp_path):
    """RGY-020 phase C: a v11 database gets the empty `transfers_api` table; nothing else changes."""
    db = tmp_path / "v11.db"
    s = open_at(db, 11)
    s.begin()
    s.con.execute("INSERT INTO backfill_queue(kind, key, status, queued_at) VALUES('launch', 't.near', 'done', 'x')")
    s.commit()
    assert s.user_version() == 11 and not s.con.execute("SELECT 1 FROM sqlite_master WHERE name = 'transfers_api'").fetchone()
    s.close()
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION >= 12
    assert s.con.execute("SELECT COUNT(*) FROM transfers_api").fetchone()[0] == 0
    assert s.con.execute("SELECT COUNT(*) FROM backfill_queue").fetchone()[0] == 1
    s.close()


def test_v13_adds_signer_and_pred_and_keeps_every_old_row(tmp_path):
    """RGY-021 step 0: a v12 database keeps its transfers_api rows; the new columns are NULL until the reload."""
    db = tmp_path / "v12.db"
    s = open_at(db, 12)
    s.begin()
    s.con.execute("INSERT INTO transfers_api(account, dir, height, ts_ns, tx, receipt, tindex, other, asset, amount, ttype) "
                  "VALUES('a.near','in',1,2,'tx','rc',0,'f.near','NEAR','1000000000000000000000000','NativeTransfer')")
    s.commit()
    assert s.user_version() == 12 and "signer" not in [r[1] for r in s.con.execute("PRAGMA table_info(transfers_api)")]
    s.close()
    s = open_store(db)
    assert s.user_version() == SCHEMA_VERSION >= 13
    cols = [r[1] for r in s.con.execute("PRAGMA table_info(transfers_api)")]
    assert "signer" in cols and "pred" in cols
    assert s.con.execute("SELECT account, other, signer, pred FROM transfers_api").fetchall() == [("a.near", "f.near", None, None)]
    s.close()
