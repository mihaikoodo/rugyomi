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


def test_rows_written_at_v2_survive_later_migrations(v1_db, tmp_path):
    db = tmp_path / "copy.db"
    shutil.copy(v1_db, db)
    s = open_at(db, 2)
    snapmod.rebuild_snapshots(s)
    s.close()
    snaps = dump(db, ["launch_snapshots"])
    assert snaps["launch_snapshots"]
    s = open_store(db)
    s.close()
    assert dump(db, ["launch_snapshots"]) == snaps


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
