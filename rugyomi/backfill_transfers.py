"""RGY-020 phase C: the incoming NEAR / wNEAR transfers of every launch participant, full history, from the FastNear Transfers API.

Why: the link tables (`links.py`) build `funded` edges from what we hold — block facts since 30.10 and the imported history. A
creator funded before that (months ago, by a wallet that funded other creators) is invisible. The Transfers API indexes every
transfer of an account from 2021 on; one request returns 100 rows.

One unit of work = one participant (`backfill_queue`, kind 'transfers_in', key = account; participants = `links.participants`, which
already excludes the explicit service accounts). Per account and per asset (`native:near`, `nep141:wrap.near`) the pages are read
NEWEST first with `direction: receiver` and `min_amount` = MIN_YOCTO (0.1 NEAR: dust is not a funding; `links.min_near` — 1 NEAR
in the default config — stays a reader-side filter), at most CAP_PAGES pages per asset. An asset with more pages than that is cut
(the OLDEST rows are dropped) and the account is marked `truncated` (a wallet with thousands of incoming transfers is a payout
wallet or an exchange account, not a creator whose funder we can name); `partial` when the hop cap of the account was hit.
Rows go to `transfers_api` (schema v12), read by `links._incoming` as `funded` edges with source 'transfers_api'.
The write of one account is ONE short transaction; re-running an account replaces its rows. 1 request = 1 credit, every attempt
(a 429 retry too) is counted. Nothing here touches facts, blocks, cards, the config or the rules.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable

from . import links
from .backfill import Api, HopCap, KeyLost, _ns
from .store import Store
from .store.db import utcnow_iso

TR_BASE = "https://transfers.main.fastnear.com"
KIND = "transfers_in"
OUT_KIND = "transfers_out"      # RGY-023: the same participants, the API's sender direction, stored with dir 'out'
TR_PAGE = 100                    # rows per page (asked 200, got 100 — measured 06.11)
CAP_PAGES = 20                   # pages per account and asset (2 000 rows); more = truncated
MAX_HOPS_ACCOUNT = 80            # hops of one account (2 assets x CAP_PAGES = 40, plus retries)
MIN_YOCTO = 10**23               # 0.1 NEAR (wNEAR has 24 decimals too)
ASSETS = (("native:near", "NEAR"), ("nep141:wrap.near", "wNEAR"))


def fetch_incoming(api: Api, account: str, since_ms: int | None = None, until_ms: int | None = None,
                   cap_pages: int = CAP_PAGES, direction: str = "receiver") -> dict[str, Any]:
    """{rows, pages, truncated: [asset labels cut at the page cap], skipped: Counter of rows not used}. Raises ApiError / KeyLost.
    since_ms (inclusive) / until_ms (exclusive): a block-time window in ms since the epoch (`from_timestamp_ms` / `to_timestamp_ms`).
    direction "receiver" (default: what the account received) or "sender" (RGY-023: what it sent; `other` is then the receiver and a
    negative amount — the sender's point of view — is read as its absolute value)."""
    rows: list[dict[str, Any]] = []
    truncated: list[str] = []
    skipped: Counter = Counter()
    pages = 0
    for asset_id, label in ASSETS:
        rt = None
        n_pages = 0
        while True:
            body: dict[str, Any] = {"account_id": account, "direction": direction, "asset_id": asset_id,
                                    "min_amount": str(MIN_YOCTO), "desc": True, "limit": TR_PAGE}
            if since_ms is not None:
                body["from_timestamp_ms"] = since_ms
            if until_ms is not None:
                body["to_timestamp_ms"] = until_ms
            if rt:
                body["resume_token"] = rt
            r = api.post("/v0/transfers", body)
            pages += 1
            n_pages += 1
            got = (r or {}).get("transfers") or []
            for t in got:
                amount = str(t.get("amount") or "")
                if amount.startswith("-"):
                    if direction != "sender":                    # a sent amount in a "receiver" feed: not ours to use
                        skipped["negative amount"] += 1
                        continue
                    amount = amount[1:]                          # the sender's own view of an outgoing transfer
                if not amount.isdigit() or int(amount) < MIN_YOCTO:
                    skipped["below the floor / not a number"] += 1
                    continue
                ts = _ns(t.get("block_timestamp"))
                if ts is None or not t.get("transaction_id"):
                    skipped["no time / no transaction"] += 1
                    continue
                rows.append({"height": int(t.get("block_height") or 0), "ts_ns": ts, "tx": t["transaction_id"],
                             "receipt": t.get("receipt_id") or "", "tindex": t.get("transfer_index") or 0,
                             "other": t.get("other_account_id"), "asset": label, "amount": amount,
                             "method": t.get("method_name"), "ttype": t.get("transfer_type"),
                             "signer": t.get("signer_id"), "pred": t.get("predecessor_id")})
            rt = (r or {}).get("resume_token")
            if len(got) < TR_PAGE or not rt:
                break
            if n_pages >= cap_pages:
                truncated.append(label)
                break
    return {"rows": rows, "pages": pages, "truncated": truncated, "skipped": skipped}


def write_rows(store: Store, account: str, rows: list[dict[str, Any]], dir_: str = "in") -> int:
    """Inside the caller's transaction: replace the account's incoming (dir_ 'in') or outgoing ('out') rows. Returns the rows written."""
    store.con.execute("DELETE FROM transfers_api WHERE account = ? AND dir = ?", (account, dir_))
    v13 = store.user_version() >= 13
    sql = ("INSERT OR IGNORE INTO transfers_api(account,dir,height,ts_ns,tx,receipt,tindex,other,asset,amount,method,ttype"
           + (",signer,pred" if v13 else "") + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?" + (",?,?" if v13 else "") + ")")
    n = 0
    for r in rows:
        n += store.con.execute(sql, (account, dir_, r["height"], r["ts_ns"], r["tx"], r["receipt"], r["tindex"], r["other"],
                                     r["asset"], r["amount"], r["method"], r["ttype"],
                                     *((r.get("signer"), r.get("pred")) if v13 else ()))).rowcount
    return n


def enqueue(store: Store, kind: str = KIND) -> dict[str, int]:
    """Every participant (idempotent). Own transaction. The ts is the participant's last launch: the newest are done first."""
    part, _times = links.participants(store.con)
    now = utcnow_iso()
    store.begin()
    try:
        n = sum(store.con.execute("INSERT OR IGNORE INTO backfill_queue(kind,key,status,ts_ns,queued_at) VALUES(?,?,?,?,?)",
                                  (kind, a, "pending", info["last_ns"], now)).rowcount for a, info in part.items())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"participants": len(part), "added": n}


def _set(store: Store, account: str, kind: str = KIND, **cols: Any) -> None:
    sets = ",".join(f"{k} = ?" for k in cols)
    store.con.execute(f"UPDATE backfill_queue SET {sets} WHERE kind = ? AND key = ?", (*cols.values(), kind, account))


def process_account(store: Store, api: Api, account: str, log: Callable[[str], None], kind: str = KIND,
                    since_ms: int | None = None, until_ms: int | None = None, cap_pages: int = CAP_PAGES,
                    max_hops: int | None = None, direction: str = "receiver") -> dict[str, Any]:
    """One account: fetch (whole history, or the [since_ms, until_ms) window), write in one short transaction, update the queue row.
    direction "sender": the outgoing transfers (RGY-023), stored with dir 'out'."""
    max_hops = max_hops or MAX_HOPS_ACCOUNT
    api.budget = max_hops
    hops0 = api.hops
    note: list[str] = []
    try:
        try:
            w = fetch_incoming(api, account, since_ms, until_ms, cap_pages, direction)
        except HopCap:
            w = None
    finally:
        api.budget = None
    hops = api.hops - hops0
    if w is None:
        res = {"status": "partial", "hops": hops, "facts": 0, "truncated": 1, "note": f"hop cap {max_hops} of the account reached"}
        store.begin()
        try:
            _set(store, account, kind, status="partial", hops=hops, truncated=1, note=res["note"], finished_at=utcnow_iso())
            store.commit()
        except BaseException:
            store.rollback()
            raise
        return res
    if w["truncated"]:
        note.append(f"cut at {cap_pages} pages ({TR_PAGE * cap_pages} rows), oldest dropped: {', '.join(w['truncated'])}")
    if w["skipped"]:
        note.append(f"rows not used: {dict(w['skipped'])}")
    store.begin()
    try:
        n = write_rows(store, account, w["rows"], "out" if direction == "sender" else "in")
        res = {"status": "done", "hops": hops, "facts": n, "truncated": int(bool(w["truncated"])),
               "note": "; ".join(note) or None}
        _set(store, account, kind, status="done", hops=hops, facts=n, txs=w["pages"], truncated=res["truncated"], note=res["note"],
             finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


def _next(store: Store, retry_errors: bool, kind: str = KIND) -> str | None:
    r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns DESC LIMIT 1",
                          (kind,)).fetchone()
    if r is None and retry_errors:
        r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'error' AND attempts < 3 "
                              "ORDER BY attempts, ts_ns DESC LIMIT 1", (kind,)).fetchone()
    return r[0] if r else None


def run(store: Store, api: Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None,
        only: str | None = None, log: Callable[[str], None] = print, kind: str = KIND, process: Any = None, retry_errors: bool = True) -> dict[str, Any]:
    """The queue loop of a transfers kind: `process(store, api, key, log)` handles one key (default: process_account)."""
    process = process or process_account
    store.begin()
    store.con.execute("UPDATE backfill_queue SET status = 'pending' WHERE kind = ? AND status = 'running'", (kind,))
    store.commit()
    n = 0
    fails = 0
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
        account = only if (only and n == 0) else (None if only else _next(store, retry_errors, kind))
        if account is None:
            break
        store.begin()
        try:
            store.con.execute("UPDATE backfill_queue SET status = 'running', attempts = attempts + 1, started_at = ? "
                              "WHERE kind = ? AND key = ?", (utcnow_iso(), kind, account))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        try:
            res = process(store, api, account, log)
            fails = 0
        except KeyLost as e:
            _fail(store, account, str(e)[:300], pending=True, kind=kind)
            reason = f"key: {e}"
            break
        except Exception as e:  # noqa: BLE001 — one failing account never stops the run
            _fail(store, account, f"{type(e).__name__}: {api.keys.scrub(str(e))}"[:300], kind=kind)
            fails += 1
            log(f"{account}: {type(e).__name__}: {e}")
            if fails >= 10:
                reason = "10 accounts in a row failed (network or API down?)"
                break
            api.sleep(min(5 * fails, 60))
            n += 1
            continue
        n += 1
        if n % 100 == 0:
            log(f"{n} accounts this run, {api.hops - hops0} credits, last {account}: {res['status']}")
        if only:
            break
    return {"accounts": n, "hops": api.hops - hops0, "reason": reason}


def _fail(store: Store, account: str, error: str, pending: bool = False, kind: str = KIND) -> None:
    store.begin()
    try:
        _set(store, account, kind, status="pending" if pending else "error", note=error, finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise


def status(store: Store, kind: str = KIND) -> dict[str, Any]:
    q = store.con.execute
    d = "out" if kind == OUT_KIND else "in"                   # rows of this direction only (the outgoing load shares the table)
    by = dict(q("SELECT status, COUNT(*) FROM backfill_queue WHERE kind = ? GROUP BY status", (kind,)))
    tot = sum(by.values())
    hops = q("SELECT COALESCE(SUM(hops), 0) FROM backfill_queue WHERE kind = ?", (kind,)).fetchone()[0]
    since = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    recent = q("SELECT COUNT(*), COALESCE(SUM(hops), 0) FROM backfill_queue WHERE kind = ? AND finished_at >= ? AND status IN "
               "('done','partial')", (kind, since)).fetchone()
    pending = by.get("pending", 0) + by.get("running", 0)
    per_min = recent[0] / 10
    finished = tot - pending - by.get("error", 0)
    return {
        "kind": kind, "total": tot, "by_status": by, "credits_spent": int(hops),
        "avg_credits_per_account": round(hops / max(1, finished), 2),
        "last10min": {"accounts": recent[0], "credits": int(recent[1])},
        "eta_minutes": round(pending / per_min) if per_min else None,
        "rows": q("SELECT COUNT(*) FROM transfers_api WHERE dir = ?", (d,)).fetchone()[0],
        "rows_by_asset": dict(q("SELECT asset, COUNT(*) FROM transfers_api WHERE dir = ? GROUP BY asset", (d,))),
        "rows_by_transfer_type": dict(q("SELECT COALESCE(ttype, '-'), COUNT(*) FROM transfers_api WHERE dir = ? GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (d,))),
        "accounts_with_rows": q("SELECT COUNT(DISTINCT account) FROM transfers_api WHERE dir = ?", (d,)).fetchone()[0],
        "oldest_row": q("SELECT MIN(ts_ns) FROM transfers_api WHERE dir = ?", (d,)).fetchone()[0],
        "truncated_accounts": q("SELECT COUNT(*) FROM backfill_queue WHERE kind = ? AND truncated = 1", (kind,)).fetchone()[0],
        "truncated_examples": [list(r) for r in q("SELECT key, facts, note FROM backfill_queue WHERE kind = ? AND truncated = 1 "
                                                  "ORDER BY facts DESC LIMIT 5", (kind,))],
        "partial_examples": [list(r) for r in q("SELECT key, note FROM backfill_queue WHERE kind = ? AND status = 'partial' LIMIT 5", (kind,))],
        "error_examples": [list(r) for r in q("SELECT key, attempts, note FROM backfill_queue WHERE kind = ? AND status = 'error' "
                                             "ORDER BY finished_at DESC LIMIT 5", (kind,))],
        "heaviest": [list(r) for r in q("SELECT key, hops, facts FROM backfill_queue WHERE kind = ? ORDER BY hops DESC LIMIT 5", (kind,))],
    }


def requeue_unsigned(store: Store, kind: str) -> dict[str, int]:
    """RGY-021 step 0 (F-019): the accounts of `kind` ('transfers_in' | 'buyers_in') whose stored rows have no signer (loaded before
    schema v13) go back to `pending`, so the next run reloads them with `signer_id` / `predecessor_id`. Accounts that have no rows
    stay `done` (nothing to enrich). Idempotent: after the reload no row is unsigned."""
    store.begin()
    try:
        n = store.con.execute(
            "UPDATE backfill_queue SET status = 'pending', attempts = 0 WHERE kind = ? AND status IN ('done', 'partial') AND key IN "
            "(SELECT DISTINCT account FROM transfers_api WHERE signer IS NULL AND dir = 'in')", (kind,)).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"requeued": n, "unsigned_rows": store.con.execute("SELECT COUNT(*) FROM transfers_api WHERE signer IS NULL AND dir = 'in'").fetchone()[0]}
