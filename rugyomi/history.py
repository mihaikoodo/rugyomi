"""History of all launches (RGY-011): the launch index, the blocks around each launch, and one reader for a creator's
launches.

  import_launch_index(store, csv, items)   the chain index of every launch -> `launch_index` (idempotent, rows are
                                           never changed)
  ingest_blocks(store, dir)                blocks of an import dir -> the SAME parser -> `facts` + `blocks` rows and
                                           launch rows tagged `launch_only` (no snapshots, no window facts)
  creator_launches(store, account)         one list of a creator's launches from `launches`, `launch_index` and
                                           `hist_launches`, deduplicated by token, each row with its source and
                                           coverage

Rules:
  * Isolated old blocks never disturb the live cursor: the cursor, `catch_up_remaining` and `missing_heights` look
    only at heights >= start_height, and ingested history lies below it. A height >= start_height is never
    ingested (the collector owns that range; a block already stored there is not touched).
  * `ingest_blocks` is the only other writer besides the collector: one short transaction per block, heights in
    ascending order (a launchbot owner is found from payments in earlier blocks, so the earlier blocks go first),
    `INSERT OR IGNORE` facts. It sets no `last_height` / `last_block_ts_ns` status and computes no snapshots.
  * A block already stored (ok / null) is skipped, never re-parsed: facts of an old block are not appended later
    (handoff, «Порядок внутри блока = facts.id»).
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import re
from pathlib import Path
from typing import Any, Callable

from . import launches as L
from .parse import ParseStats, block_ts_ns, keep_option_a, parse_block
from .store import Store
from .store.db import utcnow_iso

# index file value -> `launches.launchpad` name
LAUNCHPADS = {"nearly": "nearly", "jh_v1": "justhoot_v1", "jh_v2": "justhoot_v2"}
SOURCE = "chain_index"


def _ts_ns(iso: str | None) -> int | None:
    if not iso:
        return None
    t = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    return int(t.timestamp()) * 10**9


def _int(v: Any) -> int | None:
    try:
        return int(str(v)) if v not in (None, "") else None
    except ValueError:
        return None


# ---------------------------------------------------------------- the index

def import_launch_index(store: Store, csv_path: Path, items_path: Path | None = None) -> dict[str, Any]:
    """Import `launch_index.csv` (columns launchpad, id, token, creator, create_tx, create_height, create_ts_utc,
    txs_count, error) and, for nearly, `nearly_items.jsonl` (one JSON item per launch). One transaction.
    Idempotent: a token already in `launch_index` is left as it is (counted in `existing`). Rows with an error or
    no creation height are not imported (counted in `skipped`, listed in `skipped_tokens`)."""
    items: dict[str, dict] = {}
    if items_path is not None:
        with Path(items_path).open(encoding="utf-8", newline="\n") as f:      # not splitlines(): U+2028 in a text
            for line in f:
                if line.strip():
                    it = json.loads(line)
                    if it.get("token"):
                        items[it["token"]] = it
    rows = list(csv.DictReader(Path(csv_path).open(encoding="utf-8", newline="")))
    now = utcnow_iso()
    out: dict[str, Any] = {"rows": len(rows), "inserted": 0, "existing": 0, "skipped": 0, "skipped_tokens": [],
                           "per_launchpad": {}, "items": len(items), "items_matched": 0}
    seen: set[str] = set()
    store.begin()
    try:
        for r in rows:
            token = r["token"]
            lp = LAUNCHPADS.get(r["launchpad"])
            if lp is None or r.get("error") or not _int(r.get("create_height")) or not token:
                out["skipped"] += 1
                out["skipped_tokens"].append(token)
                continue
            it = items.get(token) if lp == "nearly" else None
            if it is not None:
                out["items_matched"] += 1
                seen.add(token)
            raw = json.dumps(it if it is not None else {k: v for k, v in r.items() if k != "error"},
                             separators=(",", ":"), sort_keys=True)
            cur = store.con.execute(
                "INSERT OR IGNORE INTO launch_index(token,launchpad,launch_id,creator,create_tx,create_height,"
                "create_ts_ns,txs_count,step,dev_buy_near,created_at_ms,fees_near_total,claims,raw,source,imported_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (token, lp, _int((it or {}).get("id", r.get("id"))), (it or {}).get("creator") or r.get("creator") or None,
                 r.get("create_tx") or None, _int(r["create_height"]), _ts_ns(r.get("create_ts_utc")),
                 _int(r.get("txs_count")), (it or {}).get("step"), (it or {}).get("dev_buy_near"),
                 _int((it or {}).get("created_at_ms")), (it or {}).get("fees_near_total"), _int((it or {}).get("claims")),
                 raw, SOURCE, now))
            k = out["per_launchpad"].setdefault(lp, {"inserted": 0, "existing": 0})
            if cur.rowcount:
                out["inserted"] += 1
                k["inserted"] += 1
            else:
                out["existing"] += 1
                k["existing"] += 1
        store.commit()
    except BaseException:
        store.rollback()
        raise
    out["items_without_row"] = sorted(set(items) - seen)[:20]
    return out


# ---------------------------------------------------------------- ingest of blocks

_NAME = re.compile(r"^(\d+)\.(json\.gz|json|null)$")


def block_files(dir_: Path) -> list[tuple[int, Path]]:
    """[(height, file)] of an import dir, ascending. `{h}.json.gz`, `{h}.json` (a block) or `{h}.null` (a skipped height)."""
    out = []
    for p in Path(dir_).iterdir():
        m = _NAME.match(p.name)
        if m and p.is_file():
            out.append((int(m.group(1)), p))
    return sorted(out)


def load_block(path: Path) -> dict | None:
    if path.name.endswith(".null"):
        return None
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    data = json.loads(raw) if raw.strip() else None
    return data


def ingest_blocks(store: Store, dir_: Path, *, delete: bool = True, limit: int | None = None,
                  log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Run every block of `dir_` through the live parser. Skipped: heights already stored (ok / null / quarantined)
    and heights >= start_height. A block that fails stays in the dir and is listed in `failed` (the rest goes on).
    After a block is committed (or found already stored) its file is deleted unless delete=False.
    Returns the counts; `new_launches` per launchpad."""
    st = store.get_status().get("start_height")
    start = st if isinstance(st, int) else None
    stats = ParseStats()
    res: dict[str, Any] = {"files": 0, "ingested_ok": 0, "ingested_null": 0, "skipped_present": 0,
                           "skipped_live_range": 0, "failed": [], "new_launches": {}, "facts_added": 0,
                           "parse_errors": 0, "deleted": 0, "dir": str(dir_)}
    todo = block_files(dir_)
    res["files"] = len(todo)
    done = 0
    for h, path in todo:
        if limit is not None and done >= limit:
            break
        if start is not None and h >= start:
            res["skipped_live_range"] += 1
            continue
        present = store.con.execute("SELECT status, quarantined_at FROM blocks WHERE height = ?", (h,)).fetchone()
        if present is not None and (present[0] in ("ok", "null") or present[1]):
            res["skipped_present"] += 1
            _remove(path, delete, res)
            continue
        try:
            block = load_block(path)
            errors: list = []
            facts = [f for f in parse_block(block, stats, errors) if keep_option_a(f)] if block is not None else []
            store.begin()
            try:
                if block is None:
                    store.mark_block(h, "null", None, 0)
                    n_new = 0
                else:
                    n_new = store.insert_facts(facts)
                    store.insert_parse_errors(errors)
                    store.mark_block(h, "ok", block_ts_ns(block), len(facts))
                    new = _detect(store, h)
                    for lp, n in new.items():
                        res["new_launches"][lp] = res["new_launches"].get(lp, 0) + n
                store.commit()
            except BaseException:
                store.rollback()
                raise
        except Exception as e:  # noqa: BLE001 — the file stays, the run goes on, the failure is reported
            res["failed"].append({"height": h, "error": f"{type(e).__name__}: {e}"[:300]})
            log(f"height {h} failed: {e}")
            continue
        done += 1
        res["facts_added"] += n_new
        res["parse_errors"] += len(errors) if block is not None else 0
        res["ingested_null" if block is None else "ingested_ok"] += 1
        _remove(path, delete, res)
        if done % 500 == 0:
            log(f"ingested {done} blocks")
    return res


def _detect(store: Store, height: int) -> dict[str, int]:
    """Launches detected in this block, tagged launch_only; returns the count per launchpad."""
    if not L.detect_in_block(store, height, coverage=L.COVERAGE_LAUNCH_ONLY):
        return {}
    return {lp: int(n) for lp, n in store.con.execute(
        "SELECT launchpad, COUNT(*) FROM launches WHERE height = ? AND coverage = ? GROUP BY launchpad",
        (height, L.COVERAGE_LAUNCH_ONLY))}


def _remove(path: Path, delete: bool, res: dict) -> None:
    if not delete:
        return
    try:
        path.unlink()
        res["deleted"] += 1
    except OSError:
        pass


# ---------------------------------------------------------------- one reader for a creator's launches

COVERAGE_OF = {"launches": "live", "launch_index": "index_only", "hist_launches": "legacy_window"}
PRIORITY = ("launches", "launch_index", "hist_launches")


def creator_launches(store: Store, account: str) -> list[dict[str, Any]]:
    """Every launch of `account` known to the database, deduplicated by token and sorted by time.
    Sources: `launches` (creator or real owner; for justhoot and for launches the collector or `ingest-blocks` saw),
    `launch_index` (creator; nearly only: a launch made through launchbot shows launchbot there, its real owner is
    in `launches`), `hist_launches` (earlier collection, creator).
    A row: token, launchpad, creator, owner, height, ts_ns, source (the first of the three that has the token),
    sources (all of them) and coverage: 'live' (launches, tag NULL), 'launch_only' (launches, ingested history),
    'index_only' (only the index), 'legacy_window' (only the earlier collection)."""
    found: dict[str, dict[str, Any]] = {}

    def add(src: str, token: str, **kw: Any) -> None:
        row = found.setdefault(token, {"token": token, "sources": {}})
        row["sources"][src] = kw

    cov = "coverage" if store.user_version() >= 6 else "NULL"
    for token, lp, creator, owner, height, ts, c in store.con.execute(
            f"SELECT token, launchpad, creator, owner, height, ts_ns, {cov} FROM launches "
            f"WHERE creator = ? OR owner = ?", (account, account)):
        add("launches", token, launchpad=lp, creator=creator, owner=owner, height=height, ts_ns=ts,
            coverage=c or "live")
    if store.user_version() >= 6:
        for token, lp, creator, height, ts in store.con.execute(
                "SELECT token, launchpad, creator, create_height, create_ts_ns FROM launch_index WHERE creator = ?",
                (account,)):
            add("launch_index", token, launchpad=lp, creator=creator, owner=None, height=height, ts_ns=ts,
                coverage="index_only")
    if store.user_version() >= 4:
        for token, lp, creator, height, ts in store.con.execute(
                "SELECT token, launchpad, creator, height, ts_ns FROM hist_launches WHERE creator = ?", (account,)):
            add("hist_launches", token, launchpad=lp, creator=creator, owner=None, height=height, ts_ns=ts,
                coverage="legacy_window")
    out = []
    for token, row in found.items():
        srcs = [s for s in PRIORITY if s in row["sources"]]
        main = dict(row["sources"][srcs[0]])
        for s in srcs[1:]:                                   # fill what the main source lacks
            for k, v in row["sources"][s].items():
                if main.get(k) is None and k != "coverage":
                    main[k] = v
        out.append({"token": token, **{k: main.get(k) for k in ("launchpad", "creator", "owner", "height", "ts_ns")},
                    "source": srcs[0], "sources": srcs, "coverage": main["coverage"]})
    return sorted(out, key=lambda r: (r["ts_ns"] or 0, r["token"]))


# ---------------------------------------------------------------- the report (diag)

AFTER_HEIGHTS = {"nearly": 3, "justhoot_v2": 3, "justhoot_v1": 8}      # blocks after the creation tx (the fetch window)
SAMPLE = 25


def history_report(store: Store, probe: str = "coinisland.near") -> dict[str, Any]:
    """Read-only picture of the history: counts per launchpad and coverage, index tokens without a launch row (with the
    reason), launchbot owners, snapshots, duplicates, and the `creator_launches` checks."""
    con = store.con
    q = lambda sql, *a: con.execute(sql, a).fetchall()          # noqa: E731
    out: dict[str, Any] = {"schema": store.user_version()}
    out["launch_index"] = dict(q("SELECT launchpad, COUNT(*) FROM launch_index GROUP BY 1"))
    out["launch_index_total"] = sum(out["launch_index"].values())
    out["launches"] = {f"{lp}/{cov or 'live'}": n for lp, cov, n in q(
        "SELECT launchpad, coverage, COUNT(*) FROM launches GROUP BY 1, 2 ORDER BY 1, 2")}
    out["blocks"] = dict(q("SELECT status, COUNT(*) FROM blocks GROUP BY 1"))
    st = store.get_status().get("start_height")
    out["history_blocks_below_start"] = q("SELECT COUNT(*) FROM blocks WHERE height < ?", st)[0][0] if isinstance(st, int) else None
    # index tokens without a launch row, by reason
    reasons: dict[str, list] = {}
    for token, lp, height, raw in q("SELECT i.token, i.launchpad, i.create_height, i.raw FROM launch_index i "
                                    "LEFT JOIN launches l ON l.token = i.token WHERE l.token IS NULL ORDER BY i.create_height"):
        try:
            step = json.loads(raw or "{}").get("step")
        except ValueError:
            step = None
        if lp == "nearly" and step == "Failed":
            why = "failed_no_pool_excluded"
        else:
            hi = height + AFTER_HEIGHTS.get(lp, 3)
            have = q("SELECT COUNT(*) FROM blocks WHERE status IN ('ok','null') AND height BETWEEN ? AND ?", height, hi)[0][0]
            why = "no_launch_event_in_ingested_blocks" if have == hi - height + 1 else (
                "blocks_not_ingested" if have == 0 else "blocks_partly_ingested")
        reasons.setdefault(why, []).append((token, height))
    out["index_tokens_without_launch"] = {k: {"count": len(v), "first": [list(x) for x in v[:SAMPLE]]}
                                          for k, v in sorted(reasons.items())}
    out["launches_without_index_row"] = q("SELECT COUNT(*) FROM launches l LEFT JOIN launch_index i ON i.token = l.token "
                                          "WHERE i.token IS NULL AND l.coverage = 'launch_only'")[0][0]
    # launchbot owners
    lb = q("SELECT token, owner, height, extra FROM launches WHERE creator = ? ORDER BY height", L.LAUNCHBOT)
    out["launchbot"] = {"launches": len(lb), "owner_resolved": sum(1 for r in lb if r[1]),
                        "index_rows_with_launchbot_creator": q("SELECT COUNT(*) FROM launch_index WHERE creator = ?", L.LAUNCHBOT)[0][0],
                        "unresolved": [[t, h] for t, o, h, _e in lb if not o][:200]}
    # snapshots: none for launch_only; the live ones are not touched
    out["snapshots"] = {"launch_only": q("SELECT COUNT(*) FROM launch_snapshots s JOIN launches l ON l.token = s.token "
                                         "WHERE l.coverage = 'launch_only'")[0][0],
                        "total": q("SELECT COUNT(*) FROM launch_snapshots")[0][0]}
    out["duplicates"] = {"launches_also_in_hist_launches": q(
        "SELECT COUNT(*) FROM launches l JOIN hist_launches h ON h.token = l.token")[0][0]}
    cl = creator_launches(store, probe)
    out["creator_launches_probe"] = {"account": probe, "count": len(cl),
                                     "rows": [{k: r[k] for k in ("token", "source", "sources", "coverage")} for r in cl[:10]]}
    top = q("SELECT creator, COUNT(*) c FROM launch_index WHERE creator IS NOT NULL AND creator != ? GROUP BY 1 "
            "ORDER BY c DESC LIMIT 3", L.LAUNCHBOT)
    out["top_index_creators"] = [{"creator": c, "index_rows": n, "creator_launches": len(creator_launches(store, c))}
                                 for c, n in top]
    return out
