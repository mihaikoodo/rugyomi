"""RGY-020 phase B: backfill the history of launches through the FastNear Transactions API.

Why: the live collector holds the full block facts of a launch window only for launches it saw. The 4,164 `launch_only`
launches (and the justhoot tokens that have an index row but no launch event) have none. The Transactions API returns every
transaction that touched a token contract; the same parser (`parse.facts._parse_reo`) turns the receipts into the same facts, so
the same snapshot code (`snapshots.metrics`, pure) computes the same numbers. Measured 06.11 on 10 live launches: 8 identical, the
two others differ by facts of OTHER tokens' pools (not read here) and one swap that has no receipt on the token contract.

One unit of work = one launch (`backfill_queue`, kind 'launch', key = token):
  1. page the account index of the token contract from (launch height - START_MARGIN) in time order until a row is later than
     launch + 24 h  (`/v0/account`, 200 rows per page: a transaction is indexed by the block it was INCLUDED in, which is before
     the launch event executes);
  2. fetch the transactions by hash, 20 per request (`/v0/transactions`);
  3. parse every receipt, keep the facts a snapshot reads (the token's ft_* legs, the swaps of its pool) in `api_facts` with an
     explicit order (height, execution_outcome.index, position) — `receipt_index` does NOT reproduce the block order;
  4. compute the snapshots T+5/15/30/60/1440 from those facts (source 'backfill') and the stored outcome labels.
The write of one launch is ONE short transaction (WAL, busy timeout from the Store): the live collector keeps priority, it is the
other writer. Nothing here touches `facts`, `blocks` (a partly fetched height is never a parsed block), `cards`, the config or the
rules. Re-running a launch replaces its `api_facts` rows. 1 HTTP request = 1 credit; every attempt (a 429 retry too) is counted.

A launch is `partial` (facts kept, NO snapshots) when the index was cut at CAP_TX transactions, the hop cap of the launch was hit,
or a requested transaction did not come back. It is `skipped` when its 24 h window is not over yet, or it has no `launches` row
(the justhoot tokens that never launched): their facts are still stored.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any, Callable

from . import snapshots as S
from .keys import REJECT_STATUSES
from .parse import Fact, ParseError, ParseStats
from .parse import facts as P
from .rules import stored as outcomes
from .store.db import Store, hash_bytes, hash_str, utcnow_iso

TX_BASE = "https://tx.main.fastnear.com"
PAGE = 200                      # /v0/account: 1..200 rows
TX_BATCH = 20                   # /v0/transactions: at most 20 hashes
START_MARGIN = 100              # blocks before the launch height (the launch tx is included 1-8 blocks before the event)
END_BLOCKS = 150_000            # upper height bound of the index query (24 h is ~86-110 k blocks); the time check stops earlier
CAP_TX = 6000                   # transactions of one token (30 index pages + 300 batches); beyond it the launch is `partial`
MAX_HOPS_LAUNCH = 700           # hops of one launch; beyond it the launch is `partial`
WINDOW_MIN = max(S.MINUTES)
NS_MIN = S.NS_MIN
SOURCE = "backfill"
KIND = "launch"
USER_AGENT = "rugyomi-backfill/1"


class ApiError(Exception):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class HopCap(Exception):
    pass


class KeyLost(Exception):
    """The key was rejected (401/402/403) or there is none left: the whole run stops."""


class Api:
    """POST JSON with `Authorization: Bearer`; every attempt is a hop (a credit). 429 -> Retry-After, the gap grows. `keys` is a
    KeySource (current / spend / rejected / scrub)."""

    def __init__(self, keys: Any, gap: float = 0.2, base: str = TX_BASE, sleep: Callable[[float], None] = time.sleep) -> None:
        self.keys, self.gap, self.base, self.sleep = keys, gap, base, sleep
        self.hops = 0                          # whole run
        self.n429 = 0
        self.codes: Counter = Counter()
        self.budget = None                     # hops left for the current launch (None = unlimited)
        self._next = 0.0
        self._lock = threading.Lock()

    def _pace(self) -> None:
        now = time.monotonic()
        wait = self._next - now
        self._next = max(self._next, now) + self.gap
        if wait > 0:
            self.sleep(wait)

    def post(self, path: str, body: dict, tries: int = 8) -> Any:
        for attempt in range(tries):
            if self.budget is not None:
                if self.budget <= 0:
                    raise HopCap("hop cap of the launch reached")
                self.budget -= 1
            key = self.keys.current()
            if not key:
                raise KeyLost("no API key left")
            self.hops += 1
            self._pace()
            req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST", headers={
                "content-type": "application/json", "Accept-Encoding": "gzip", "User-Agent": USER_AGENT,
                "Authorization": "Bearer " + key})
            self.keys.spend(key, 1)
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw, code = r.read(), r.status
            except urllib.error.HTTPError as e:
                raw = e.read() if e.fp else b""
                code = e.code
                self.codes[code] += 1
                snippet = self.keys.scrub(raw[:300].decode("utf-8", "replace"))
                if code in REJECT_STATUSES:
                    self.keys.rejected(key, code, snippet)
                    raise KeyLost(f"key rejected (HTTP {code}) {snippet}") from None
                if code == 429:
                    self.n429 += 1
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        pause = float(ra) if ra else 2.0
                    except ValueError:
                        pause = 2.0
                    self.gap = min(max(self.gap * 1.5, 0.3), 2.0)          # slower from now on
                    self.sleep(min(pause, 30))
                    continue
                raise ApiError(f"HTTP {code} on {path}: {snippet}", code) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self.codes["neterr"] += 1
                if attempt + 1 >= tries:
                    raise ApiError(f"{type(e).__name__} on {path}: {self.keys.scrub(str(e))}") from None
                self.sleep(min(2.0 ** attempt, 20))
                continue
            self.codes[code] += 1
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw) if raw.strip() else None
        raise ApiError(f"too many 429 / network errors on {path}", 429)


# ================================================================================================ adapter
def _ns(v: Any) -> int | None:
    """Block time as UTC ns from ns / us / ms / s numbers or digit strings."""
    if v in (None, ""):
        return None
    try:
        n = int(v) if str(v).isdigit() else int(float(v))
    except ValueError:
        return None
    if n < 10**11:
        return n * 10**9
    if n < 10**14:
        return n * 10**6
    if n < 10**17:
        return n * 10**3
    return n


def extract_reos(tx: Any) -> tuple[list[tuple[int, int, dict, dict]], list[str]]:
    """[(height, ts_ns, order, reo)] — every receipt execution of one API transaction object as a neardata
    `receipt_execution_outcome` ({tx_hash, receipt, execution_outcome}). The real shape (seen 06.11): {transaction,
    execution_outcome, receipts: [{receipt: {block_height, block_timestamp, receipt_index, predecessor_id, receiver_id,
    receipt_id, receipt: {Action}}, execution_outcome: {block_height, block_timestamp, index, outcome}}], data_receipts}; the
    wrapper has no tx hash (taken from `transaction.hash`). `order` = {"outcome": execution_outcome.index, "pos": position}."""
    notes: list[str] = []
    txh = None
    if isinstance(tx, dict):
        t = tx.get("transaction")
        txh = (t.get("hash") if isinstance(t, dict) else None) or tx.get("transaction_hash") or tx.get("hash")
    found: list[dict] = []

    def visit(d: Any) -> None:
        if isinstance(d, dict):
            rcpt = d.get("receipt")
            if isinstance(rcpt, dict) and rcpt.get("receipt_id") and isinstance(d.get("execution_outcome"), dict):
                found.append(d)
                return
            for v in d.values():
                visit(v)
        elif isinstance(d, list):
            for v in d:
                visit(v)

    visit(tx)
    res = []
    for i, d in enumerate(found):
        r, eo = d["receipt"], d["execution_outcome"]
        h = eo.get("block_height") or r.get("block_height")
        ts = _ns(eo.get("block_timestamp") or r.get("block_timestamp"))
        if h is None or ts is None:
            notes.append(f"receipt {r.get('receipt_id')}: no block height / timestamp")
            continue
        res.append((int(h), ts, {"outcome": eo.get("index"), "pos": i}, {**d, "tx_hash": d.get("tx_hash") or txh}))
    return res, notes


def parse_txs(txs: list[Any], token: str, pool: str | None, end_ts: int
              ) -> tuple[list[tuple[Fact, int, int]], list[str], list[ParseError], int]:
    """[(fact, ord, seq)] of the facts a snapshot of `token` reads, in execution order; notes; parse errors; receipts seen."""
    items: list[tuple[int, int, dict, dict]] = []
    notes: list[str] = []
    for tx in txs:
        r, n = extract_reos(tx)
        items += r
        notes += n
    items.sort(key=lambda x: (x[0], 0 if x[2]["outcome"] is not None else 1, int(x[2]["outcome"] or 0), x[2]["pos"]))
    out: list[tuple[Fact, int, int]] = []
    errors: list[ParseError] = []
    seen: set[str] = set()
    stats = ParseStats()
    for h, ts, order, reo in items:
        rid = reo["receipt"]["receipt_id"]
        if rid in seen or ts > end_ts:
            continue
        seen.add(rid)
        facts: list[Fact] = []
        try:
            P._parse_reo(reo, h, ts, stats, facts, errors)
        except Exception as e:  # noqa: BLE001 — recorded, never silent
            errors.append(ParseError(h, rid, "r", f"{type(e).__name__}: {e}"))
            continue
        o = order["outcome"] if order["outcome"] is not None else 10**12
        for seq, f in enumerate(f for f in facts if P.keep_option_a(f) and relevant(f, token, pool)):
            out.append((f, int(o), seq))
    return out, notes, errors, len(seen)


def relevant(f: Fact, token: str, pool: str | None) -> bool:
    """The facts `snapshots._rows` reads: legs of the token (any ft kind with token = it) and the swaps whose b is the pool."""
    if f.token == token and f.kind in ("ft_transfer", "ft_mint", "ft_burn"):
        return True
    return f.kind == "dcl_swap" and bool(pool) and f.b == pool


# ================================================================================================ fetching
def fetch_window(api: Api, token: str, h0: int, end_ts: int, cap_tx: int = CAP_TX) -> dict[str, Any]:
    """Every transaction touching `token` from height h0 to end_ts: {txs, hashes, truncated, missing}. Raises HopCap / ApiError."""
    hashes: list[str] = []
    seen: set[str] = set()
    rt = None
    truncated = False
    while True:
        body: dict[str, Any] = {"account_id": token, "from_tx_block_height": h0, "to_tx_block_height": h0 + END_BLOCKS,
                                "limit": PAGE, "desc": False}
        if rt:
            body["resume_token"] = rt
        r = api.post("/v0/account", body)
        rows = (r or {}).get("account_txs") or []
        past = False
        for row in rows:
            ts = _ns(row.get("tx_block_timestamp"))
            if ts is not None and ts > end_ts:
                past = True
                break
            h = row.get("transaction_hash")
            if h and h not in seen:
                seen.add(h)
                hashes.append(h)
        rt = (r or {}).get("resume_token")
        if past or len(rows) < PAGE or not rt:
            break
        if len(hashes) >= cap_tx:
            truncated = True
            break
    txs: list[Any] = []
    for i in range(0, len(hashes), TX_BATCH):
        r = api.post("/v0/transactions", {"tx_hashes": hashes[i:i + TX_BATCH]})
        txs += (r or {}).get("transactions") or []
    got = {((t.get("transaction") or {}).get("hash")) for t in txs if isinstance(t, dict)}
    return {"txs": txs, "hashes": hashes, "truncated": truncated, "missing": [h for h in hashes if h not in got]}


# ================================================================================================ storage
def write_api_facts(store: Store, scope: str, facts: list[tuple[Fact, int, int]]) -> int:
    """Inside the caller's transaction: replace the scope's rows. Returns the rows written."""
    store.con.execute("DELETE FROM api_facts WHERE scope = ?", (scope,))
    n = 0
    sql = ("INSERT OR IGNORE INTO api_facts(scope,height,ord,seq,kind,ts_ns,tx,receipt,origin,a,b,token,amount,extra,in_blocks) "
           "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    for f, o, seq in facts:
        rid = hash_bytes(f.receipt_id)
        inb = store.con.execute("SELECT 1 FROM facts WHERE receipt = ? AND origin = ?", (rid, f.origin)).fetchone() is not None
        extra = json.dumps(f.extra, separators=(",", ":"), sort_keys=True) if f.extra else None
        n += store.con.execute(sql, (scope, f.height, o, seq, store.kind_id(f.kind), f.ts_ns, hash_bytes(f.tx_hash), rid,
                                     f.origin, store.account_id(f.a), store.account_id(f.b), store.account_id(f.token),
                                     f.amount, extra, int(inb))).rowcount
    return n


def rows_from_api(store: Store, launch: dict, cutoff: int) -> tuple[list[dict], list[dict]]:
    """(token legs, swaps) of the launch from `api_facts`, up to the cutoff, in the explicit order — the same dicts as
    `snapshots._rows`; `id` is the running position (the attribution code only compares ids)."""
    legs: list[dict] = []
    swaps: list[dict] = []
    token = launch["token"]
    for i, (kind, ts, tx, a, b, amount, extra) in enumerate(store.con.execute(
            "SELECT k.name, f.ts_ns, f.tx, aa.name, bb.name, f.amount, f.extra FROM api_facts f JOIN kinds k ON k.id = f.kind "
            "LEFT JOIN accounts aa ON aa.id = f.a LEFT JOIN accounts bb ON bb.id = f.b WHERE f.scope = ? AND f.ts_ns <= ? "
            "ORDER BY f.height, f.ord, f.seq", (token, cutoff))):
        if kind in ("ft_transfer", "ft_mint", "ft_burn"):
            legs.append({"id": i, "kind": kind, "ts": ts, "tx": hash_str(tx), "a": a, "b": b, "amount": int(amount or 0)})
        elif kind == "dcl_swap" and launch.get("pool") and b == launch["pool"]:
            e = json.loads(extra) if extra else {}
            swaps.append({"id": i, "ts": ts, "tx": hash_str(tx), "swapper": a, "token_in": e.get("token_in"),
                          "token_out": e.get("token_out"), "amount_in": int(e.get("amount_in") or 0),
                          "amount_out": int(e.get("amount_out") or 0)})
    return legs, swaps


def write_snapshots(store: Store, launch: dict) -> dict[str, int]:
    """Inside the caller's transaction: the five snapshots of a `launch_only` launch from its api_facts (source 'backfill').
    A row the live collector wrote (source NULL) is never replaced. A failing computation is stored as the row's error."""
    token, ts0 = launch["token"], launch["ts_ns"]
    legs_all, swaps_all = rows_from_api(store, launch, ts0 + WINDOW_MIN * NS_MIN)
    cols = ("token", "minute", "at_ts_ns") + S.COLUMNS + ("metric_version", "computed_at", "error", "source")
    sql = f"INSERT OR REPLACE INTO launch_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    out = {"ok": 0, "error": 0, "kept_live": 0}
    for m in S.MINUTES:
        cutoff = ts0 + m * NS_MIN
        live = store.con.execute("SELECT 1 FROM launch_snapshots WHERE token = ? AND minute = ? AND source IS NULL",
                                 (token, m)).fetchone()
        if live:
            out["kept_live"] += 1
            continue
        try:
            mt = S.metrics(launch, [x for x in legs_all if x["ts"] <= cutoff], [x for x in swaps_all if x["ts"] <= cutoff], cutoff)
            row = (token, m, mt["at_ts_ns"], *(mt[c] for c in S.COLUMNS), S.METRIC_VERSION, utcnow_iso(), None, SOURCE)
            res = "ok"
        except Exception as e:  # noqa: BLE001 — stored on the row, like the live collector does
            row = (token, m, cutoff, *([None] * len(S.COLUMNS)), S.METRIC_VERSION, utcnow_iso(), f"{type(e).__name__}: {e}"[:500], SOURCE)
            res = "error"
        store.con.execute(sql, row)
        out[res] += 1
        if m in S.OUTCOME_MINUTES:
            store.snap_touched.add(token)
    return out


def resnap(store: Store, log=lambda m: None) -> dict[str, int]:
    """RGY-021 (F-020): recompute the snapshots of every backfilled launch from its stored `api_facts` with the metrics version
    in force, then the stored outcome labels. 0 credits. One transaction per launch; a launch the live collector covers is
    left to `rebuild-snapshots`. Idempotent."""
    out = {"launches": 0, "ok": 0, "error": 0, "kept_live": 0, "no_facts": 0}
    toks = [t for (t,) in store.con.execute(
        "SELECT DISTINCT s.token FROM launch_snapshots s WHERE s.source = 'backfill' ORDER BY s.token").fetchall()]
    for i, token in enumerate(toks):
        launch = _launch_row(store, token)
        if launch is None:
            continue
        store.begin()
        try:
            r = write_snapshots(store, launch)
            outcomes.refresh_touched(store, log)
            store.commit()
        except BaseException:
            store.rollback()
            store.snap_touched.clear()
            raise
        out["launches"] += 1
        for k in ("ok", "error", "kept_live"):
            out[k] += r[k]
        if (i + 1) % 500 == 0:
            log(f"resnap {i + 1}/{len(toks)}")
    return out


# ================================================================================================ queue
def enqueue(store: Store) -> dict[str, int]:
    """Add the historical launches to the queue (idempotent): every `launch_only` launch, and every token of the launch index
    that has no `launches` row. Own transaction."""
    now = utcnow_iso()
    store.begin()
    try:
        a = store.con.execute(
            "INSERT OR IGNORE INTO backfill_queue(kind,key,status,ts_ns,height,queued_at) "
            "SELECT 'launch', l.token, 'pending', l.ts_ns, l.height, ? FROM launches l WHERE l.coverage = 'launch_only'", (now,)).rowcount
        b = store.con.execute(
            "INSERT OR IGNORE INTO backfill_queue(kind,key,status,ts_ns,height,queued_at) "
            "SELECT 'launch', li.token, 'pending', li.create_ts_ns, li.create_height, ? FROM launch_index li "
            "WHERE li.create_ts_ns IS NOT NULL AND NOT EXISTS (SELECT 1 FROM launches l WHERE l.token = li.token)", (now,)).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"launch_only": a, "index_only": b}


def _launch_row(store: Store, token: str) -> dict | None:
    r = S._launch(store, token)
    if r is None:
        return None
    r["tx"] = r.get("tx")
    return r


def _start_height(store: Store, token: str, queue_height: int | None) -> int | None:
    hs = [queue_height]
    r = store.con.execute("SELECT create_height FROM launch_index WHERE token = ?", (token,)).fetchone()
    if r:
        hs.append(r[0])
    hs = [h for h in hs if h]
    return min(hs) - START_MARGIN if hs else None


def _set(store: Store, token: str, **cols: Any) -> None:
    sets = ",".join(f"{k} = ?" for k in cols)
    store.con.execute(f"UPDATE backfill_queue SET {sets} WHERE kind = 'launch' AND key = ?", (*cols.values(), token))


def _finish(store: Store, token: str, res: dict[str, Any]) -> dict[str, Any]:
    """The queue row of a launch that ended without a database write of its own (own short transaction)."""
    store.begin()
    try:
        _set(store, token, status=res["status"], hops=res["hops"], truncated=res.get("truncated", 0), note=res.get("note"),
             finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


def process_launch(store: Store, api: Api, token: str, now_ns: int, log: Callable[[str], None]) -> dict[str, Any]:
    """One launch, end to end. Returns the result row written to the queue."""
    q = store.con.execute("SELECT ts_ns, height FROM backfill_queue WHERE kind = 'launch' AND key = ?", (token,)).fetchone()
    launch = _launch_row(store, token)
    ts0 = launch["ts_ns"] if launch else q[0]
    end_ts = ts0 + WINDOW_MIN * NS_MIN
    h0 = _start_height(store, token, q[1])
    if h0 is None:
        return _finish(store, token, {"status": "skipped", "note": "no start height", "hops": 0})
    api.budget = MAX_HOPS_LAUNCH
    hops0 = api.hops
    note: list[str] = []
    partial = False
    try:
        w = fetch_window(api, token, h0, end_ts)
    except HopCap:
        w = None
        partial = True
        note.append(f"hop cap {MAX_HOPS_LAUNCH} of the launch reached")
    finally:
        api.budget = None
    hops = api.hops - hops0
    if w is None:
        return _finish(store, token, {"status": "partial", "note": "; ".join(note), "hops": hops, "truncated": 1})
    pool = launch["pool"] if launch else None
    facts, notes, errors, receipts = parse_txs(w["txs"], token, pool, end_ts)
    if w["truncated"]:
        partial = True
        note.append(f"index cut at {CAP_TX} transactions")
    if w["missing"]:
        partial = True
        note.append(f"{len(w['missing'])} requested transaction(s) not returned")
    if errors:
        note.append(f"{len(errors)} parse error(s): {errors[0].error[:120]}")
    note += notes[:3]
    res: dict[str, Any] = {"hops": hops, "txs": len(w["txs"]), "truncated": int(w["truncated"])}
    store.begin()
    try:
        res["facts"] = write_api_facts(store, token, facts)
        snaps = None
        if launch is None:
            res["status"] = "skipped"
            note.append("no launches row: facts kept, no snapshots")
        elif end_ts > now_ns - 5 * NS_MIN:
            res["status"] = "skipped"
            note.append("24 h window not over yet: facts kept, no snapshots")
        elif partial:
            res["status"] = "partial"
        else:
            snaps = write_snapshots(store, launch)
            outcomes.refresh_touched(store, log)
            res["status"] = "done"
        res["snapshots"] = snaps["ok"] if snaps else 0
        if snaps and (snaps["error"] or snaps["kept_live"]):
            note.append(f"snapshots: {snaps}")
        res["note"] = "; ".join(note) or None
        _set(store, token, status=res["status"], hops=hops, txs=res["txs"], facts=res["facts"], snapshots=res["snapshots"],
             truncated=res["truncated"], note=res["note"], finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        store.snap_touched.clear()
        raise
    return res


# ================================================================================================ run
def reset_running(store: Store) -> int:
    """After a crash: rows left 'running' go back to pending (one runner at a time is enforced by the caller's lock)."""
    store.begin()
    try:
        n = store.con.execute("UPDATE backfill_queue SET status = 'pending' WHERE status = 'running'").rowcount
        store.commit()
        return n
    except BaseException:
        store.rollback()
        raise


def _next(store: Store, retry_errors: bool) -> str | None:
    r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status = 'pending' "
                          "ORDER BY ts_ns DESC LIMIT 1").fetchone()
    if r is None and retry_errors:
        r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status = 'error' AND attempts < 3 "
                              "ORDER BY attempts, ts_ns DESC LIMIT 1").fetchone()
    return r[0] if r else None


def run(store: Store, api: Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None,
        only: str | None = None, log: Callable[[str], None] = print, now_ns: Callable[[], int] | None = None) -> dict[str, Any]:
    """Process pending launches, newest first, until the queue is empty, `stop()`, the credit cap or `limit` launches.
    Returns {"launches", "hops", "reason"}."""
    now_ns = now_ns or (lambda: time.time_ns())
    reset_running(store)
    n = 0
    consecutive_errors = 0
    hops0 = api.hops
    reason = "queue empty"
    while True:
        if stop():
            reason = "stopped"
            break
        if max_credits is not None and api.hops - hops0 >= max_credits:
            reason = f"credit cap {max_credits} reached"
            break
        if limit is not None and n >= limit:
            reason = f"limit {limit} reached"
            break
        token = only if (only and n == 0) else (None if only else _next(store, True))
        if token is None:
            break
        store.begin()
        try:
            store.con.execute("UPDATE backfill_queue SET status = 'running', attempts = attempts + 1, started_at = ? "
                              "WHERE kind = 'launch' AND key = ?", (utcnow_iso(), token))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        try:
            res = process_launch(store, api, token, now_ns(), log)
            consecutive_errors = 0
        except KeyLost as e:
            _fail(store, token, str(e)[:300], pending=True)
            reason = f"key: {e}"
            break
        except Exception as e:  # noqa: BLE001 — a failing launch never stops the run; stored on the row
            _fail(store, token, f"{type(e).__name__}: {api.keys.scrub(str(e))}"[:300])
            consecutive_errors += 1
            log(f"{token}: {type(e).__name__}: {e}")
            if consecutive_errors >= 10:
                reason = "10 launches in a row failed (network or API down?)"
                break
            api.sleep(min(5 * consecutive_errors, 60))
            n += 1
            continue
        n += 1
        if n % 25 == 0:
            log(f"{n} launches this run, {api.hops - hops0} credits, last {token}: {res['status']}")
        if only:
            break
    return {"launches": n, "hops": api.hops - hops0, "reason": reason}


def _fail(store: Store, token: str, error: str, pending: bool = False) -> None:
    store.begin()
    try:
        _set(store, token, status="pending" if pending else "error", note=error, finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise


def status(store: Store) -> dict[str, Any]:
    """Progress from the queue: counts per status, credits, speed over the last 10 minutes, ETA."""
    q = store.con.execute
    by = dict(q("SELECT status, COUNT(*) FROM backfill_queue WHERE kind = 'launch' GROUP BY status"))
    tot = sum(by.values())
    hops = q("SELECT COALESCE(SUM(hops), 0) FROM backfill_queue WHERE kind = 'launch'").fetchone()[0]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    recent = q("SELECT COUNT(*), COALESCE(SUM(hops), 0) FROM backfill_queue WHERE kind = 'launch' AND finished_at >= ? "
               "AND status IN ('done','partial','skipped')", (since,)).fetchone()
    pending = by.get("pending", 0) + by.get("running", 0)
    per_min = recent[0] / 10
    return {
        "total": tot, "by_status": by, "credits_spent": int(hops),
        "avg_credits_per_launch": round(hops / max(1, tot - pending - by.get("error", 0)), 2),
        "last10min": {"launches": recent[0], "credits": int(recent[1])},
        "eta_minutes": round(pending / per_min) if per_min else None,
        "api_facts": q("SELECT COUNT(*) FROM api_facts").fetchone()[0],
        "snapshots_backfill": q("SELECT COUNT(*) FROM launch_snapshots WHERE source = 'backfill'").fetchone()[0],
        "snapshots_backfill_errors": q("SELECT COUNT(*) FROM launch_snapshots WHERE source = 'backfill' AND error IS NOT NULL").fetchone()[0],
        "outcomes_backfill": q("SELECT COUNT(*) FROM launch_outcomes WHERE source = 'backfill'").fetchone()[0],
        "last_finished": q("SELECT key, status, finished_at FROM backfill_queue WHERE kind = 'launch' AND finished_at IS NOT NULL "
                           "ORDER BY finished_at DESC LIMIT 1").fetchone(),
        "partial_examples": [list(r) for r in q("SELECT key, note FROM backfill_queue WHERE kind = 'launch' AND status = 'partial' "
                                               "ORDER BY finished_at DESC LIMIT 5")],
        "skipped_examples": [list(r) for r in q("SELECT key, note FROM backfill_queue WHERE kind = 'launch' AND status = 'skipped' "
                                               "ORDER BY finished_at DESC LIMIT 8")],
        "skipped_by_reason": [list(r) for r in q("SELECT substr(note, 1, 60), COUNT(*) FROM backfill_queue WHERE kind = 'launch' "
                                                "AND status = 'skipped' GROUP BY 1")],
        "error_examples": [list(r) for r in q("SELECT key, attempts, note FROM backfill_queue WHERE kind = 'launch' AND status = 'error' "
                                             "ORDER BY finished_at DESC LIMIT 5")],
    }
