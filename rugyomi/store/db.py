"""The SQLite store: open + migrate, one transaction per block, cursor and gaps, status values.

One writer process (the collector). Readers (status, site, API) open their own connections; WAL lets them
read while the writer writes. All times are UTC.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from ..parse import Fact, ParseError
from .schema import MIGRATIONS, SCHEMA_VERSION

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


HASH_LEN = 32          # every NEAR receipt / transaction hash is base58 of exactly 32 bytes


def hash_bytes(s: str | None) -> bytes | None:
    """Receipt / tx id -> stored bytes: base58 decoded, exactly 32 bytes. Anything else (never seen on NEAR) is kept
    as b"\xff" + UTF-8 text, padded so that its length is never 32 — the length alone tells the two apart.
    (Before the F-006 fix the marker byte alone was checked, and a real hash whose first byte is 0xff — 1 in 256 — broke
    reading it back.)"""
    if s is None:
        return None
    try:
        b = b58decode(s)
    except KeyError:
        b = None
    if b is not None and len(b) == HASH_LEN:
        return b
    t = b"\xff" + s.encode()
    return t + b"\x00" if len(t) == HASH_LEN else t


def hash_str(b: bytes | None) -> str | None:
    if b is None:
        return None
    if len(b) == HASH_LEN:
        return b58encode(b)
    if b[:1] == b"\xff":
        return b[1:].rstrip(b"\x00").decode()
    return b58encode(b)                     # rows written before the F-006 fix for unusual base58 ids


def utcnow_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _LRU:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.d: OrderedDict[str, int] = OrderedDict()

    def get(self, k: str) -> int | None:
        v = self.d.get(k)
        if v is not None:
            self.d.move_to_end(k)
        return v

    def put(self, k: str, v: int) -> None:
        self.d[k] = v
        self.d.move_to_end(k)
        if len(self.d) > self.cap:
            self.d.popitem(last=False)

    def drop(self, keys: Iterable[str]) -> None:
        for k in keys:
            self.d.pop(k, None)


def open_store(path: str | Path, readonly: bool = False) -> "Store":
    return Store(path, readonly)


class Store:
    ACCOUNT_CACHE = 100_000            # bounded: the account dictionary grows without limit over weeks

    def __init__(self, path: str | Path, readonly: bool = False) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self.snap_touched: set[str] = set()      # tokens whose T+60 / T+24 h snapshot was written (rules.stored reads it)
        if readonly:
            self.con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, isolation_level=None,
                                       check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.con = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.migrate()
        # "done" for the cursor: ok / null, and (v5+) a quarantined height — skipped on purpose and shown in the
        # status. Coverage checks (snapshots) use OK_NULL: a quarantined height is NOT covered.
        self.DONE = DONE_V5 if self.user_version() >= 5 else OK_NULL
        self._acc = _LRU(self.ACCOUNT_CACHE)
        self._kinds: dict[str, int] = {}
        self._txn_new_accounts: list[str] = []
        self._txn_new_kinds: list[str] = []

    def close(self) -> None:
        self.con.close()

    # ---------------------------------------------------------------- schema

    def user_version(self) -> int:
        return int(self.con.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> int:
        v = self.user_version()
        if v > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{v} is newer than this code (v{SCHEMA_VERSION})")
        for i in range(v, SCHEMA_VERSION):
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for stmt in _split(MIGRATIONS[i]):
                    self.con.execute(stmt)
                self.con.execute(f"PRAGMA user_version={i + 1}")
                self.con.execute("COMMIT")
            except BaseException:
                self.con.execute("ROLLBACK")
                raise
        return self.user_version()

    # ---------------------------------------------------------------- dictionaries

    def account_id(self, name: str | None) -> int | None:
        if name is None:
            return None
        v = self._acc.get(name)
        if v is not None:
            return v
        row = self.con.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        if row is None:
            cur = self.con.execute("INSERT INTO accounts(name) VALUES(?)", (name,))
            v = int(cur.lastrowid)
            self._txn_new_accounts.append(name)
        else:
            v = int(row[0])
        self._acc.put(name, v)
        return v

    def lookup_account(self, name: str) -> int | None:
        """Read-only lookup (no insert)."""
        v = self._acc.get(name)
        if v is not None:
            return v
        row = self.con.execute("SELECT id FROM accounts WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else None

    def account_name(self, id_: int | None) -> str | None:
        if id_ is None:
            return None
        row = self.con.execute("SELECT name FROM accounts WHERE id=?", (id_,)).fetchone()
        return row[0] if row else None

    def kind_id(self, name: str) -> int:
        v = self._kinds.get(name)
        if v is not None:
            return v
        row = self.con.execute("SELECT id FROM kinds WHERE name=?", (name,)).fetchone()
        if row is None:
            v = int(self.con.execute("INSERT INTO kinds(name) VALUES(?)", (name,)).lastrowid)
            self._txn_new_kinds.append(name)
        else:
            v = int(row[0])
        self._kinds[name] = v
        return v

    def lookup_kind(self, name: str) -> int | None:
        if name in self._kinds:
            return self._kinds[name]
        row = self.con.execute("SELECT id FROM kinds WHERE name=?", (name,)).fetchone()
        return int(row[0]) if row else None

    # ---------------------------------------------------------------- transactions

    def begin(self) -> None:
        self._txn_new_accounts = []
        self._txn_new_kinds = []
        self.con.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.con.execute("COMMIT")
        self._txn_new_accounts = []
        self._txn_new_kinds = []

    def rollback(self) -> None:
        if self.con.in_transaction:
            self.con.execute("ROLLBACK")
        # ids handed out inside the rolled-back transaction no longer exist
        self._acc.drop(self._txn_new_accounts)
        for k in self._txn_new_kinds:
            self._kinds.pop(k, None)
        self._txn_new_accounts = []
        self._txn_new_kinds = []

    # ---------------------------------------------------------------- writes (inside begin/commit)

    def insert_facts(self, facts: Iterable[Fact]) -> int:
        """INSERT OR IGNORE by the natural key (receipt, origin). Returns the number of NEW rows."""
        n = 0
        sql = ("INSERT OR IGNORE INTO facts(kind,height,ts_ns,tx,receipt,origin,a,b,token,amount,extra) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?)")
        for f in facts:
            extra = json.dumps(f.extra, separators=(",", ":"), sort_keys=True) if f.extra else None
            cur = self.con.execute(sql, (self.kind_id(f.kind), f.height, f.ts_ns, hash_bytes(f.tx_hash),
                                         hash_bytes(f.receipt_id), f.origin, self.account_id(f.a),
                                         self.account_id(f.b), self.account_id(f.token), f.amount, extra))
            n += cur.rowcount
        return n

    def insert_parse_errors(self, errors: Iterable[ParseError]) -> int:
        n = 0
        at = utcnow_iso()
        for e in errors:
            cur = self.con.execute("INSERT OR IGNORE INTO parse_errors(height,receipt_id,origin,error,at) "
                                   "VALUES(?,?,?,?,?)", (e.height, e.receipt_id, e.origin, e.error[:1000], at))
            n += cur.rowcount
        return n

    def mark_block(self, height: int, status: str, block_ts_ns: int | None, n_facts: int,
                   error: str | None = None) -> None:
        """Upsert the bookkeeping row of a height. `attempts` counts every write for the height."""
        self.con.execute(
            "INSERT INTO blocks(height,status,block_ts_ns,n_facts,error,attempts,processed_at) VALUES(?,?,?,?,?,1,?) "
            "ON CONFLICT(height) DO UPDATE SET status=excluded.status, block_ts_ns=COALESCE(excluded.block_ts_ns, "
            "blocks.block_ts_ns), n_facts=excluded.n_facts, error=excluded.error, attempts=blocks.attempts+1, "
            "processed_at=excluded.processed_at",
            (height, status, block_ts_ns, n_facts, error, utcnow_iso()))

    def set_status(self, key: str, value: Any) -> None:
        self.con.execute("INSERT INTO collector_status(key,value,updated_at) VALUES(?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                         (key, json.dumps(value, separators=(",", ":")), utcnow_iso()))

    def record_error(self, height: int, error: str, processing: bool = False) -> int:
        """A failed attempt on `height`, in its own small transaction (the block itself was rolled back).
        processing=True: the block was fetched and its processing failed — counted in process_errors.
        Returns the height's process_errors after this attempt (0 before schema v5)."""
        now = utcnow_iso()
        self.begin()
        try:
            if self.user_version() >= 5:
                self.con.execute(
                    "INSERT INTO blocks(height,status,block_ts_ns,n_facts,error,attempts,processed_at,error_since,"
                    "process_errors) VALUES(?,'error',NULL,0,?,1,?,?,?) ON CONFLICT(height) DO UPDATE SET "
                    "status='error', error=excluded.error, attempts=blocks.attempts+1, processed_at=excluded.processed_at, "
                    "error_since=COALESCE(blocks.error_since, excluded.error_since), "
                    "process_errors=COALESCE(blocks.process_errors, 0) + excluded.process_errors",
                    (height, error[:2000], now, now, 1 if processing else 0))
                n = int(self.con.execute("SELECT COALESCE(process_errors, 0) FROM blocks WHERE height = ?",
                                         (height,)).fetchone()[0])
            else:
                self.mark_block(height, "error", None, 0, error[:2000])
                n = 0
            self.set_status("last_error", {"height": height, "error": error[:2000], "at": now,
                                           "kind": "processing" if processing else "network"})
            self.commit()
            return n
        except BaseException:
            self.rollback()
            raise

    def quarantine(self, height: int, error: str) -> None:
        """Put a failing height in quarantine (status stays 'error'; the cursor treats it as done). Own transaction."""
        now = utcnow_iso()
        self.begin()
        try:
            cur = self.con.execute("UPDATE blocks SET quarantined_at = ?, quarantine_error = ? "
                                   "WHERE height = ? AND status = 'error'", (now, error[:2000], height))
            if cur.rowcount != 1:
                raise RuntimeError(f"height {height}: no error row to quarantine")
            self.set_status("last_error", {"height": height, "error": error[:2000], "at": now, "kind": "processing",
                                           "quarantined": True})
            self.commit()
        except BaseException:
            self.rollback()
            raise

    def is_quarantined(self, height: int) -> bool:
        """Quarantined and not reprocessed yet (status still 'error')."""
        if self.user_version() < 5:
            return False
        return self.con.execute("SELECT 1 FROM blocks WHERE height = ? AND status = 'error' AND quarantined_at IS NOT "
                                "NULL", (height,)).fetchone() is not None

    def quarantined_open(self, limit: int = 1000) -> list[tuple[int, str | None, str | None]]:
        """(height, quarantine_error, quarantined_at) of the heights still in quarantine, by height."""
        if self.user_version() < 5:
            return []
        return [(int(h), e, a) for h, e, a in self.con.execute(
            "SELECT height, quarantine_error, quarantined_at FROM blocks WHERE status = 'error' AND quarantined_at IS "
            "NOT NULL ORDER BY height LIMIT ?", (limit,))]

    def covered(self, lo: int, hi: int) -> bool:
        """Every height in [lo, hi] is stored as ok / null (a quarantined or missing height is not covered)."""
        if hi < lo:
            return True
        n = self.con.execute(f"SELECT COUNT(*) FROM blocks WHERE {OK_NULL} AND height BETWEEN ? AND ?",
                             (lo, hi)).fetchone()[0]
        return int(n) == hi - lo + 1

    # ---------------------------------------------------------------- reads

    def get_status(self) -> dict[str, Any]:
        out = {}
        for k, v, at in self.con.execute("SELECT key, value, updated_at FROM collector_status"):
            try:
                out[k] = json.loads(v) if v is not None else None
            except json.JSONDecodeError:
                out[k] = v
            out.setdefault("_updated_at", {})[k] = at
        return out

    def done_heights_range(self) -> tuple[int | None, int | None, int]:
        row = self.con.execute(f"SELECT MIN(height), MAX(height), COUNT(*) FROM blocks WHERE {self.DONE}").fetchone()
        return row[0], row[1], int(row[2])

    def cursor(self, start: int | None = None) -> int | None:
        """Next height to process: the lowest height >= start that is not done (ok / null / quarantined); None on an
        empty DB.
        `start` defaults to the collector's recorded start height, else the lowest done height."""
        if start is None:
            st = self.get_status().get("start_height")
            start = st if isinstance(st, int) else self.done_heights_range()[0]
        if start is None:
            return None
        d = self.DONE
        row = self.con.execute(
            f"SELECT MIN(h) FROM (SELECT ? AS h UNION ALL SELECT height + 1 FROM blocks WHERE {d} "
            f"AND height >= ?) WHERE h NOT IN (SELECT height FROM blocks WHERE {d})",
            (start, start)).fetchone()
        return int(row[0])

    def is_done(self, height: int) -> bool:
        return self.con.execute(f"SELECT 1 FROM blocks WHERE height = ? AND {self.DONE}",
                                (height,)).fetchone() is not None

    def next_done(self, height: int) -> int | None:
        """The lowest done (ok / null / quarantined) height >= `height`, if any (the end of a gap being filled)."""
        r = self.con.execute(f"SELECT MIN(height) FROM blocks WHERE height >= ? AND {self.DONE}",
                             (height,)).fetchone()
        return int(r[0]) if r and r[0] is not None else None

    def undone_count(self, start: int, end: int) -> int:
        """Heights in [start, end] that are not done (ok / null / quarantined)."""
        if end < start:
            return 0
        done = self.con.execute(f"SELECT COUNT(*) FROM blocks WHERE {self.DONE} AND height BETWEEN ? AND ?",
                                (start, end)).fetchone()[0]
        return end - start + 1 - int(done)

    def missing_heights(self, start: int, cursor: int, limit: int = 100) -> list[list[int]]:
        """Ranges [from, to] in [start, cursor) that are not done (no ok / null row, not quarantined). Must be empty;
        quarantined heights are listed separately (status `quarantined_heights`)."""
        d = self.DONE
        rows = self.con.execute(
            f"SELECT prev + 1, height - 1 FROM (SELECT height, LAG(height) OVER (ORDER BY height) AS prev "
            f"FROM blocks WHERE {d} AND height >= ? AND height < ?) "
            f"WHERE prev IS NOT NULL AND height - prev > 1 LIMIT ?", (start, cursor, limit)).fetchall()
        gaps = [[int(a), int(b)] for a, b in rows]
        lo = self.con.execute(f"SELECT MIN(height) FROM blocks WHERE {d} AND height >= ?",
                              (start,)).fetchone()[0]
        if cursor > start and (lo is None or lo > start):
            gaps.insert(0, [start, (lo - 1) if lo is not None else cursor - 1])
        return gaps[:limit]


OK_NULL = "status IN ('ok','null')"
DONE_V5 = "(status IN ('ok','null') OR quarantined_at IS NOT NULL)"


def _split(script: str) -> list[str]:
    """Statements of a migration script. A statement ends at the first ';' where SQLite sees it as complete, so the
    body of a CREATE TRIGGER (BEGIN ... ; END) stays in one piece (v7)."""
    out: list[str] = []
    cur = ""
    for piece in script.split(";"):
        cur += piece + ";"
        if sqlite3.complete_statement(cur):
            if cur.strip(" \n;"):
                out.append(cur.strip())
            cur = ""
    if cur.strip(" \n;"):
        out.append(cur.strip())
    return out
