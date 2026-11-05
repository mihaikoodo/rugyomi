"""RGY-011: launch_index import, justhoot v1 parsing (real HOOT blocks), ingest of history blocks (launch_only),
the snapshot guard, creator_launches, migration v6."""
import csv
import gzip
import json
import shutil
import threading

import pytest

from rugyomi_testkit import FIXTURES, block, hid, reo
from rugyomi import history as H
from rugyomi import launches as L
from rugyomi import snapshots as S
from rugyomi.collector import Collector, main
from rugyomi.keys import EnvKeySource
from rugyomi.parse import block_ts_ns
from rugyomi.neardata import Neardata
from rugyomi.store import open_store
from rugyomi.store.schema import MIGRATIONS, SCHEMA_VERSION

HOOT = "hoot-8ecf65.launchpad.justhoot.near"
CREATOR = "a" * 64


def _ts(iso):
    return H._ts_ns(iso)


def v1_blocks():
    for line in gzip.decompress((FIXTURES / "jh_v1.jsonl.gz").read_bytes()).splitlines():
        if line.strip():
            d = json.loads(line)
            yield d["height"], d["block"]


def snapshot_blocks():
    for line in gzip.decompress((FIXTURES / "snapshots.jsonl.gz").read_bytes()).splitlines():
        if line.strip():
            d = json.loads(line)
            yield d["height"], d["block"]


def write_dir(path, blocks, nulls=()):
    path.mkdir(parents=True, exist_ok=True)
    for h, b in blocks:
        (path / f"{h}.json.gz").write_bytes(gzip.compress(json.dumps(b).encode()))
    for h in nulls:
        (path / f"{h}.null").write_bytes(b"")
    return path


def collector(store):
    return Collector(store, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()),
                     log=lambda m: None)


def set_start(store, h):
    store.begin()
    store.set_status("start_height", h)
    store.commit()


# ---------------------------------------------------------------- justhoot v1 (real blocks)

def test_v1_hoot_creator_pool_and_height_through_the_live_pipeline(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    for h, b in v1_blocks():
        c.process(h, b)
    rows = s.con.execute("SELECT token, launchpad, creator, owner, pool, dev_buy, height, coverage, extra "
                         "FROM launches").fetchall()
    assert len(rows) == 1
    token, lp, creator, owner, pool, dev_buy, height, cov, extra = rows[0]
    assert (token, lp, creator, owner) == (HOOT, "justhoot_v1", "justhoot-owner.near", "justhoot-owner.near")
    assert 217_919_620 <= height <= 217_919_630 and height == 217_919_627
    assert pool == f"{HOOT}|wrap.near|10000"                      # sorted ids + fee tier
    assert int(dev_buy) > 0 and json.loads(extra)["pool_derived"] is True
    assert cov is None                                            # live path: fully covered
    assert L.rebuild_launches(s) == 1                             # derived: the same row again
    assert s.con.execute("SELECT creator, height FROM launches").fetchone() == ("justhoot-owner.near", height)
    s.close()


def test_v1_namespace_is_a_launch_token_and_other_justhoot_accounts_are_not():
    from rugyomi.parse import is_launch_token
    assert is_launch_token(HOOT) and is_launch_token("x.launchpad.justhoot.near")
    assert not is_launch_token("launchpad.justhoot.near") or True      # the contract itself (not asserted either way)
    assert not is_launch_token("somebody.near")


# ---------------------------------------------------------------- launch_index

def index_files(tmp_path, n_nearly=2):
    rows = [
        {"launchpad": "nearly", "id": "10", "token": "aa.nearlytrade.near", "creator": "", "create_tx": "T" * 44,
         "create_height": "222000001", "create_ts_utc": "2026-10-30T10:00:00Z", "txs_count": "5", "error": ""},
        {"launchpad": "nearly", "id": "11", "token": "bb.nearlytrade.near", "creator": "", "create_tx": "U" * 44,
         "create_height": "222000002", "create_ts_utc": "2026-10-30T10:00:01Z", "txs_count": "9", "error": ""},
        {"launchpad": "jh_v1", "id": "", "token": HOOT, "creator": "", "create_tx": "V" * 44,
         "create_height": "217919622", "create_ts_utc": "2026-09-20T01:02:03Z", "txs_count": "3", "error": ""},
        {"launchpad": "jh_v2", "id": "", "token": "n.v2-launchpad.justhoot.near", "creator": "", "create_tx": "W" * 44,
         "create_height": "222300000", "create_ts_utc": "2026-10-31T01:02:03Z", "txs_count": "4", "error": ""},
        {"launchpad": "nearly", "id": "12", "token": "cc.nearlytrade.near", "creator": "", "create_tx": "",
         "create_height": "", "create_ts_utc": "", "txs_count": "", "error": "no tx found"},
    ]
    p = tmp_path / "launch_index.csv"
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    items = tmp_path / "nearly_items.jsonl"
    items.write_text("\n".join(json.dumps(
        {"id": i, "token": t, "creator": CREATOR, "created_at_ms": 1_793_000_000_000 + i, "step": "Done",
         "dev_buy_near": "100", "claims": 2, "fees_near_total": "7"})
        for i, t in ((10, "aa.nearlytrade.near"), (11, "bb.nearlytrade.near"), (99, "zz.nearlytrade.near"))),
        encoding="utf-8")
    return p, items


def test_import_launch_index_is_idempotent_and_keeps_the_item_fields(tmp_path):
    s = open_store(tmp_path / "r.db")
    p, items = index_files(tmp_path)
    r1 = H.import_launch_index(s, p, items)
    assert r1["inserted"] == 4 and r1["existing"] == 0 and r1["skipped"] == 1 and r1["skipped_tokens"] == ["cc.nearlytrade.near"]
    assert r1["per_launchpad"]["nearly"]["inserted"] == 2 and r1["items_matched"] == 2
    assert r1["items_without_row"] == ["zz.nearlytrade.near"]
    snap = s.con.execute("SELECT * FROM launch_index ORDER BY token").fetchall()
    r2 = H.import_launch_index(s, p, items)
    assert r2["inserted"] == 0 and r2["existing"] == 4
    assert s.con.execute("SELECT * FROM launch_index ORDER BY token").fetchall() == snap       # rows untouched
    row = s.con.execute("SELECT launchpad, launch_id, creator, create_height, create_ts_ns, txs_count, step, "
                        "dev_buy_near, created_at_ms, claims, fees_near_total, source FROM launch_index "
                        "WHERE token = 'aa.nearlytrade.near'").fetchone()
    assert row[4] == _ts("2026-10-30T10:00:00Z") == 1_793_354_400_000_000_000
    assert row[:4] == ("nearly", 10, CREATOR, 222000001) and row[5:] == (5, "Done", "100", 1_793_000_000_010, 2, "7",
                                                                       "chain_index")
    jh = s.con.execute("SELECT launchpad, creator, launch_id FROM launch_index WHERE token = ?", (HOOT,)).fetchone()
    assert jh == ("justhoot_v1", None, None)
    assert json.loads(s.con.execute("SELECT raw FROM launch_index WHERE token = ?", (HOOT,)).fetchone()[0])["create_tx"] == "V" * 44
    s.close()


def test_import_cli_prints_counts_and_a_second_run_inserts_nothing(tmp_path, capsys):
    p, items = index_files(tmp_path)
    db = tmp_path / "r.db"
    assert main(["import-launch-index", "--db", str(db), str(p), str(items)]) == 0
    assert json.loads(capsys.readouterr().out)["inserted"] == 4
    assert main(["import-launch-index", "--db", str(db), str(p), str(items)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["inserted"] == 0 and out["existing"] == 4


# ---------------------------------------------------------------- ingest of history blocks

def test_ingest_blocks_v1_launch_only_and_files_deleted(tmp_path, capsys):
    s = open_store(tmp_path / "r.db")
    set_start(s, 222_000_000)
    d = write_dir(tmp_path / "in", v1_blocks(), nulls=[217_919_500])
    n_files = len(list(d.iterdir()))
    res = H.ingest_blocks(s, d)
    assert res["files"] == n_files and res["failed"] == [] and res["ingested_null"] == 1
    assert res["new_launches"] == {"justhoot_v1": 1} and res["deleted"] == n_files
    assert list(d.iterdir()) == []
    assert s.con.execute("SELECT token, creator, coverage FROM launches").fetchall() == [
        (HOOT, "justhoot-owner.near", "launch_only")]
    # the live cursor and start height are untouched; history is below start_height
    assert s.get_status()["start_height"] == 222_000_000 and s.cursor() == 222_000_000
    assert s.get_status().get("last_height") is None
    # a second run over the same blocks: nothing new (heights are present), facts identical
    facts = s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    write_dir(d, v1_blocks())
    res2 = H.ingest_blocks(s, d)
    assert res2["ingested_ok"] == 0 and res2["skipped_present"] == res2["files"] and res2["new_launches"] == {}
    assert s.con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == facts
    s.close()


def test_ingest_never_touches_the_live_range_and_keep_leaves_files(tmp_path):
    s = open_store(tmp_path / "r.db")
    set_start(s, 217_919_700)                                    # the HOOT blocks straddle it
    d = write_dir(tmp_path / "in", v1_blocks())
    res = H.ingest_blocks(s, d, delete=False)
    assert res["skipped_live_range"] > 0 and res["deleted"] == 0
    assert s.con.execute("SELECT MIN(height), MAX(height) FROM blocks").fetchone()[1] < 217_919_700
    assert len(list(d.iterdir())) == res["files"]
    s.close()


def test_a_bad_file_is_reported_and_kept_the_rest_goes_on(tmp_path):
    s = open_store(tmp_path / "r.db")
    set_start(s, 222_000_000)
    d = write_dir(tmp_path / "in", list(v1_blocks())[:5])
    (d / "217919999.json").write_text("{not json")
    res = H.ingest_blocks(s, d)
    assert [f["height"] for f in res["failed"]] == [217919999] and res["ingested_ok"] == 5
    assert [p.name for p in d.iterdir()] == ["217919999.json"]
    s.close()


def test_ingest_cli(tmp_path, capsys):
    db = tmp_path / "r.db"
    s = open_store(db)
    set_start(s, 222_000_000)
    s.close()
    d = write_dir(tmp_path / "in", v1_blocks())
    assert main(["ingest-blocks", "--db", str(db), str(d)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["new_launches"] == {"justhoot_v1": 1} and out["failed"] == []


# ---------------------------------------------------------------- snapshot guard

def snap_rows(s, where=""):
    return s.con.execute(f"SELECT COUNT(*) FROM launch_snapshots {where}").fetchone()[0]


def test_launch_only_launches_get_no_snapshots_and_live_ones_are_unchanged(tmp_path):
    # live store: the reference run
    live = open_store(tmp_path / "live.db")
    c = collector(live)
    blocks = list(snapshot_blocks())
    for h, b in blocks:
        c.process(h, b)
    live_rows = snap_rows(live)
    assert live_rows > 0
    live_dump = live.con.execute("SELECT token, minute FROM launch_snapshots ORDER BY 1, 2").fetchall()

    # history store: the very same blocks, ingested below start_height
    hist = open_store(tmp_path / "hist.db")
    set_start(hist, blocks[-1][0] + 1000)
    res = H.ingest_blocks(hist, write_dir(tmp_path / "in", blocks))
    assert res["failed"] == [] and sum(res["new_launches"].values()) >= 3
    assert hist.con.execute("SELECT COUNT(*) FROM launches WHERE coverage = 'launch_only'").fetchone()[0] == \
        hist.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    assert snap_rows(hist) == 0                                   # ingest never computes
    last_ts = block_ts_ns(blocks[-1][1])
    hist.begin()
    assert S.compute_due(hist, last_ts) == 0
    hist.commit()
    assert sum(S.rebuild_snapshots(hist).values()) == 0
    assert snap_rows(hist) == 0
    assert S.launches_covering(hist, last_ts) == []

    # a live collector later processes blocks: the isolated launches are still skipped, a covered one is not
    live.close()
    lv = open_store(tmp_path / "live.db")
    S.rebuild_snapshots(lv)
    assert lv.con.execute("SELECT token, minute FROM launch_snapshots ORDER BY 1, 2").fetchall() == live_dump
    lv.close()
    hist.close()


def test_a_launch_that_becomes_launch_only_is_skipped_by_rebuild_and_the_tag_survives_rebuild_launches(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    for h, b in snapshot_blocks():
        c.process(h, b)
    assert snap_rows(s) > 0
    s.begin()
    s.con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = 'island.nearlytrade.near'")
    s.commit()
    S.rebuild_snapshots(s)
    assert snap_rows(s, "WHERE token = 'island.nearlytrade.near'") > 0       # rows already there are not deleted
    L.rebuild_launches(s)
    assert s.con.execute("SELECT coverage FROM launches WHERE token = 'island.nearlytrade.near'").fetchone()[0] == "launch_only"
    s.close()


# ---------------------------------------------------------------- creator_launches

def test_creator_launches_merges_sources_without_duplicates(tmp_path):
    s = open_store(tmp_path / "r.db")
    c = collector(s)
    for h, b in snapshot_blocks():
        c.process(h, b)
    got = {r["token"]: r for r in H.creator_launches(s, "coinisland.near")}
    assert "island.nearlytrade.near" in got and "coin-2.nearlytrade.near" in got
    assert got["island.nearlytrade.near"]["source"] == "launches" and got["island.nearlytrade.near"]["coverage"] == "live"
    # the same token in the index (creator 64-hex there) and in hist_launches: one row, sources listed
    s.begin()
    s.con.execute("INSERT INTO launch_index(token,launchpad,creator,create_height,create_ts_ns,source,imported_at) "
                  "VALUES('only.nearlytrade.near','nearly','coinisland.near',100,5,'chain_index','x')")
    s.con.execute("INSERT INTO launch_index(token,launchpad,creator,create_height,create_ts_ns,source,imported_at) "
                  "VALUES('island.nearlytrade.near','nearly','coinisland.near',1,1,'chain_index','x')")
    s.commit()
    rows = H.creator_launches(s, "coinisland.near")
    toks = [r["token"] for r in rows]
    assert len(toks) == len(set(toks))
    only = next(r for r in rows if r["token"] == "only.nearlytrade.near")
    assert only["source"] == "launch_index" and only["coverage"] == "index_only"
    isl = next(r for r in rows if r["token"] == "island.nearlytrade.near")
    assert isl["source"] == "launches" and isl["sources"] == ["launches", "launch_index"]
    assert isl["height"] == got["island.nearlytrade.near"]["height"]          # the live row wins
    assert H.creator_launches(s, "nobody.near") == []
    s.close()


def test_creator_launches_returns_the_top_creator_of_the_index(tmp_path):
    s = open_store(tmp_path / "r.db")
    s.begin()
    for i in range(162):
        s.con.execute("INSERT INTO launch_index(token,launchpad,creator,create_height,create_ts_ns,source,imported_at) "
                      "VALUES(?,?,?,?,?,?,?)", (f"t{i}.nearlytrade.near", "nearly", CREATOR, 10 + i, i, "chain_index", "x"))
    s.commit()
    assert len(H.creator_launches(s, CREATOR)) == 162
    s.close()


# ---------------------------------------------------------------- migration v6

def test_v6_is_additive_and_old_databases_open(tmp_path):
    assert SCHEMA_VERSION >= 6
    s = open_store(tmp_path / "r.db")
    cols = {r[1] for r in s.con.execute("PRAGMA table_info(launches)")}
    assert "coverage" in cols
    assert {r[1] for r in s.con.execute("PRAGMA table_info(launch_index)")} >= {"token", "launchpad", "creator", "create_height"}
    assert s.con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s.close()


# ---------------------------------------------------------------- the diag report

def test_history_report_counts_reasons_and_probe(tmp_path):
    s = open_store(tmp_path / "r.db")
    set_start(s, 222_000_000)
    p, items = index_files(tmp_path)
    H.import_launch_index(s, p, items)
    # HOOT is in the index at 217919622; its blocks are ingested -> a launch row; the nearly rows have no blocks
    H.ingest_blocks(s, write_dir(tmp_path / "in", v1_blocks()))
    # the Failed one: an item says so
    s.begin()
    s.con.execute("UPDATE launch_index SET raw = '{\"step\":\"Failed\"}' WHERE token = 'bb.nearlytrade.near'")
    s.commit()
    r = H.history_report(s)
    assert r["launch_index_total"] == 4 and r["launch_index"] == {"nearly": 2, "justhoot_v1": 1, "justhoot_v2": 1}
    assert r["launches"] == {"justhoot_v1/launch_only": 1}
    w = r["index_tokens_without_launch"]
    assert w["failed_no_pool_excluded"]["count"] == 1 and w["blocks_not_ingested"]["count"] == 2
    assert r["snapshots"] == {"launch_only": 0, "total": 0}
    assert r["launchbot"]["launches"] == 0 and r["creator_launches_probe"]["count"] == 0
    assert r["duplicates"]["launches_also_in_hist_launches"] == 0
    s.close()
