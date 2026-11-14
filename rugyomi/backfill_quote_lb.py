"""RGY-075: the LOOKBACK of the quote prices - for a launch whose pool is not paired with wNEAR, the last swap of the pool Q / wNEAR BEFORE its first trade that has no price.

WHY. RGY-070 collected the swaps of the pools Q / wNEAR only in the 30 minutes before each trade without a 15-minute price. A quote token that trades with wNEAR rarely (PRL, RHEA, ...)
has no swap in those 30 minutes, so the trader pages stay «n/a». The pages may show an approximate amount with the last KNOWN price up to 24 h old (`quote_price.DISPLAY_MAX_AGE_MIN`, shown with
«≈» and the age); outcomes, labels and cards keep the strict 15 minutes. This module collects exactly that price: the latest swap of the pair BEFORE the first trade of the launch that has no price
within 24 h (the later trades of the launch are priced by that swap while it is at most 24 h old; a trade that is still without a price after it is queued again by the next `plan`).

HOW. ONE UNIT = (Q, t0) in `backfill_queue` (kind `quote_lb`, key "<Q>:<t0 ns>", ts_ns = t0, height = the block height of that trade). The collector walks the account index of the token contract
Q BACKWARDS from t0 (`desc`, 200 transactions a page, the transactions of a page by hash), parses every page with the same parser as the quote windows (only the swaps of the pools `Q|wrap.near|fee`
are kept) and STOPS at the first page that holds a usable price (so a token that trades with wNEAR often costs one page) or when the page is older than 24 h, the index is exhausted, the unit's
transaction cap or its credit cap is reached. Same route, same raw store as RGY-070: the swaps go to `api_facts` under their OWN scope "quote:<Q>:lb<t0>" (so `quote_price.load` reads them like the
quote windows) - NEVER `facts`, `cards`, `launch_snapshots`, `launch_outcomes` or the buyer tables. Idempotent (a key is queued once; a re-run replaces the scope), resumable (a unit cut by the run's
credit cap goes back to pending), and every credit is counted (1 HTTP request = 1 credit; a page costs 1 + 10 credits: the index page and its 200 transactions in batches of 20).
Before a unit calls the API it looks at what is stored: a price that an earlier unit (or a stored pool) already gives for t0 makes it `skipped` (0 credits), so units of one token cost once.

Statuses: done (a price found, or 24 h / the whole index scanned with none), partial (a cap cut the scan: the rows are kept), skipped, error (pending again, attempts < 3)."""
from __future__ import annotations

import time
from typing import Any, Callable

from . import backfill as BF
from . import backfill_quote as BQ
from . import quote_price as QP
from .store.db import Store, utcnow_iso

KIND = "quote_lb"
NS_MIN = QP.NS_MIN
LB_NS = QP.DISPLAY_MAX_AGE_MIN * NS_MIN        # how far back a unit looks: the age limit of the display price
UNIT_CAP_TX = 3000                             # transactions of one unit (15 index pages + 150 batches), beyond it the unit is `partial`
MAX_HOPS_UNIT = 400                            # credits of one unit
END_MARGIN_BLOCKS = 5                          # blocks above the trade's height for the index query (an index row is the INCLUSION block, before the execution)
BLOCKS_PER_S = BQ.BLOCKS_PER_S


def scope_of(key: str) -> str:
    q, t0 = BQ.split_key(key)
    return f"quote:{q}:lb{t0}"


def key_of(quote: str, t0: int) -> str:
    return f"{quote}:{t0}"


def _note_field(note: str | None, name: str) -> int | None:
    return BQ._field(note, name)


# ---------------------------------------------------------------- what is needed (0 credits)

def _covered(store: Store) -> list[tuple[str, int, int]]:
    """[(quote, lo, t0)] of the finished units that FOUND a price: any trade time in [lo, t0] has the price the unit stored (lo = the swap's time)."""
    out = []
    for key, note in store.con.execute("SELECT key, note FROM backfill_queue WHERE kind = ? AND status IN ('done', 'partial')", (KIND,)):
        lo = _note_field(note, "price")
        if lo:
            q, t0 = BQ.split_key(key)
            out.append((q, lo, t0))
    return out


def needed(store: Store, now_ns: int) -> list[dict[str, Any]]:
    """Per launch not paired with wNEAR (window ended an hour ago) whose trades have no price within 24 h from what is stored: {quote, ts, height, token, trades, missing} - `ts` is the FIRST such trade."""
    covered = _covered(store)
    out: list[dict[str, Any]] = []
    for d in BQ.dependents(store, now_ns):
        swaps = BQ.launch_swaps(store, d)
        r = QP.convert_swaps(d["token"], d["quote"], swaps, QP.series_for(store, d, d["quote"], QP.DISPLAY_MAX_AGE_MIN))
        if r["total"] == 0 or r["ok"]:
            continue
        t0 = min(r["missing_ts"])
        if any(q == d["quote"] and lo <= t0 <= hi for q, lo, hi in covered):
            continue
        out.append({"quote": d["quote"], "ts": t0, "height": BQ.trade_heights(store, d).get(t0, d["height"]), "token": d["token"], "trades": r["total"], "missing": r["missing"]})
    return out


def enqueue(store: Store, now_ns: int | None = None) -> dict[str, int]:
    """Queue the units the launches need (idempotent: a key already there keeps its status). Own transaction. Reads only the stored rows: 0 credits."""
    now_ns = now_ns if now_ns is not None else time.time_ns()
    items = needed(store, now_ns)
    units: dict[tuple[str, int], dict[str, Any]] = {}
    for i in items:
        u = units.setdefault((i["quote"], i["ts"]), {"height": i["height"], "launches": set()})
        u["launches"].add(i["token"])
    now = utcnow_iso()
    store.begin()
    try:
        added = 0
        for (q, t0), u in units.items():
            added += store.con.execute("INSERT OR IGNORE INTO backfill_queue(kind, key, status, ts_ns, height, queued_at, note) VALUES(?,?,?,?,?,?,?)",
                                       (KIND, key_of(q, t0), "pending", t0, u["height"], now, f"n={len(u['launches'])}")).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"needed_launches": len({i["token"] for i in items}), "units": len(units), "added": added}


# ---------------------------------------------------------------- one unit

def _set(store: Store, key: str, **cols: Any) -> None:
    sets = ",".join(f"{k} = ?" for k in cols)
    store.con.execute(f"UPDATE backfill_queue SET {sets} WHERE kind = ? AND key = ?", (*cols.values(), KIND, key))


def _finish(store: Store, key: str, **cols: Any) -> dict[str, Any]:
    store.begin()
    try:
        _set(store, key, finished_at=utcnow_iso(), **cols)
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"status": cols.get("status"), "hops": cols.get("hops", 0), "facts": cols.get("facts", 0)}


def fetch_back(api: BF.Api, quote: str, h_hi: int, t0: int, cap_tx: int = UNIT_CAP_TX) -> dict[str, Any]:
    """Walk the account index of `quote` backwards from `h_hi` (the trade at `t0`) and parse the pages until one holds a usable price of the pair.
    {facts, price_ts, pages, txs, scanned_to, truncated, missing, errors, notes, hop_cap}. A HopCap in the middle returns what was collected with `hop_cap` True."""
    lo_ts = t0 - LB_NS
    h_lo = max(1, h_hi - int(LB_NS / 1e9 * BLOCKS_PER_S) - BF.START_MARGIN)
    res: dict[str, Any] = {"facts": [], "price_ts": None, "pages": 0, "txs": 0, "scanned_to": t0, "truncated": False, "missing": [], "errors": [], "notes": [], "hop_cap": False}
    seen: set[str] = set()
    rt = None
    prev_h: int | None = None
    try:
        while True:
            body: dict[str, Any] = {"account_id": quote, "from_tx_block_height": h_lo, "to_tx_block_height": h_hi + END_MARGIN_BLOCKS, "limit": BF.PAGE, "desc": True}
            if rt:
                body["resume_token"] = rt
            r = api.post("/v0/account", body)
            rows = (r or {}).get("account_txs") or []
            res["pages"] += 1
            hashes: list[str] = []
            past = False
            for row in rows:
                h = row.get("tx_block_height")
                if h is not None and str(h).isdigit():
                    if prev_h is not None and int(h) > prev_h:
                        raise BF.ApiError("the account index did not come back in descending order (desc is not honoured)")
                    prev_h = int(h)
                ts = BF._ns(row.get("tx_block_timestamp"))
                if ts is not None and ts < lo_ts:
                    past = True
                    break
                if ts is not None:
                    res["scanned_to"] = min(res["scanned_to"], ts)
                    if ts > t0:
                        continue                                  # a transaction after the trade is not a price BEFORE it
                th = row.get("transaction_hash")
                if th and th not in seen:
                    seen.add(th)
                    hashes.append(th)
            txs: list[Any] = []
            for i in range(0, len(hashes), BF.TX_BATCH):
                txs += ((api.post("/v0/transactions", {"tx_hashes": hashes[i:i + BF.TX_BATCH]}) or {}).get("transactions")) or []
            got = {((t.get("transaction") or {}).get("hash")) for t in txs if isinstance(t, dict)}
            res["missing"] += [x for x in hashes if x not in got]
            res["txs"] += len(txs)
            facts, notes, errors, _rc = BF.parse_txs(txs, quote, None, t0, keep=lambda f: BQ.is_pair_pool(f, quote))
            facts = [x for x in facts if lo_ts <= x[0].ts_ns <= t0]
            res["facts"] += facts
            res["errors"] += errors
            res["notes"] += notes[:2]
            usable = [x[0].ts_ns for x in facts if QP.price_of(x[0].extra or {}, quote) is not None]
            if usable:
                res["price_ts"] = max(usable)
                break
            rt = (r or {}).get("resume_token")
            if past or len(rows) < BF.PAGE or not rt:
                break
            if res["txs"] >= cap_tx:
                res["truncated"] = True
                break
    except BF.HopCap:
        res["hop_cap"] = True
    return res


def process_unit(store: Store, api: BF.Api, key: str, budget: int, log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    q, t0 = BQ.split_key(key)
    row = store.con.execute("SELECT height, note FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, key)).fetchone()
    if not row or row[0] is None:
        return _finish(store, key, status="skipped", note="no height", hops=0)
    head = (row[1] or "").split(";")[0]
    have = QP.load(store, q, t0 - LB_NS, t0, QP.DISPLAY_MAX_AGE_MIN).at(t0)
    if have is not None:                                              # an earlier unit, a stored pool or a quote window already gives the price: nothing to ask
        return _finish(store, key, status="skipped", note=f"{head};price already stored at {have.ts}", hops=0)
    api.budget = min(budget, MAX_HOPS_UNIT)
    run_cut = budget < MAX_HOPS_UNIT
    hops0 = api.hops
    try:
        w = fetch_back(api, q, int(row[0]), t0)
    finally:
        api.budget = None
    hops = api.hops - hops0
    if w["hop_cap"] and run_cut:
        raise BQ.RunCap(key)
    note = [head, f"price={w['price_ts'] or 0}", f"pages={w['pages']}", f"to={w['scanned_to']}"]
    partial = w["truncated"] or w["hop_cap"] or bool(w["missing"])
    if w["truncated"]:
        note.append(f"cut at {UNIT_CAP_TX} transactions")
    if w["hop_cap"]:
        note.append(f"hop cap {MAX_HOPS_UNIT} of the unit reached")
    if w["missing"]:
        note.append(f"{len(w['missing'])} requested transaction(s) not returned")
    if w["errors"]:
        note.append(f"{len(w['errors'])} parse error(s): {w['errors'][0].error[:120]}")
    note += w["notes"][:2]
    status = "partial" if partial and not w["price_ts"] else "done"
    store.begin()
    try:
        n = BF.write_api_facts(store, scope_of(key), w["facts"])
        _set(store, key, status=status, hops=hops, txs=w["txs"], facts=n, truncated=int(partial), note=";".join(note), finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"status": status, "hops": hops, "facts": n, "txs": w["txs"]}


def _next(store: Store) -> str | None:
    r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns DESC LIMIT 1", (KIND,)).fetchone()
    if r is None:
        r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'error' AND attempts < 3 ORDER BY attempts, ts_ns DESC LIMIT 1", (KIND,)).fetchone()
    return r[0] if r else None


def spread(store: Store, n: int) -> list[str]:
    """`n` pending units spread over the quote tokens (round robin, the busiest token first) and, inside a token, evenly over time: a pilot that sees every kind of token."""
    by: dict[str, list[str]] = {}
    for k, in store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns", (KIND,)):
        by.setdefault(BQ.split_key(k)[0], []).append(k)
    if n <= 0 or not by:
        return []
    order = sorted(by, key=lambda q: (-len(by[q]), q))
    share = -(-n // len(order))                                          # units per token, rounded up
    queues = []
    for q in order:
        lst, m = by[q], min(len(by[q]), share)
        queues.append([lst[round(i * (len(lst) - 1) / (m - 1))] if m > 1 else lst[0] for i in range(m)])
    out: list[str] = []
    for i in range(share):
        for qu in queues:
            if i < len(qu) and qu[i] not in out and len(out) < n:
                out.append(qu[i])
    return out


def _fail(store: Store, key: str, error: str, pending: bool = False) -> None:
    store.begin()
    try:
        r = store.con.execute("SELECT note FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, key)).fetchone()
        head = ((r[0] if r else None) or "").split(";")[0]
        _set(store, key, status="pending" if pending else "error", note=f"{head};{error}" if head else error, finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise


def run(store: Store, api: BF.Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None, only: str | None = None,
        log: Callable[[str], None] = print, order: str = "newest") -> dict[str, Any]:
    """Process pending units until the queue is empty, `stop()`, the credit cap (never passed) or `limit` units. `max_credits` is REQUIRED (the approved budget);
    `order` "spread" = `limit` units spread over the quote tokens (the pilot for the projection)."""
    if max_credits is None:
        raise ValueError("backfill_quote_lb.run needs an explicit credit cap (the user's approved budget)")
    picks = spread(store, limit or 0) if order == "spread" else None
    store.begin()
    try:
        store.con.execute("UPDATE backfill_queue SET status = 'pending' WHERE kind = ? AND status = 'running'", (KIND,))
        store.commit()
    except BaseException:
        store.rollback()
        raise
    n, errors_in_row, hops0, reason = 0, 0, api.hops, "queue empty"
    while True:
        if stop():
            reason = "stopped"
            break
        left = max_credits - (api.hops - hops0)
        if left <= 0:
            reason = f"credit cap {max_credits} reached"
            break
        if limit is not None and n >= limit:
            reason = f"limit {limit} reached"
            break
        key = only if (only and n == 0) else (None if only else (picks.pop(0) if picks else None) if picks is not None else _next(store))
        if key is None:
            break
        store.begin()
        try:
            store.con.execute("UPDATE backfill_queue SET status = 'running', attempts = attempts + 1, started_at = ? WHERE kind = ? AND key = ?", (utcnow_iso(), KIND, key))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        try:
            res = process_unit(store, api, key, left, log)
            errors_in_row = 0
        except BQ.RunCap:
            _fail(store, key, f"the run's credit cap ended this unit after {api.hops - hops0} credits of the run; pending again", pending=True)
            reason = f"credit cap {max_credits} reached"
            break
        except BF.KeyLost as e:
            _fail(store, key, str(e)[:300], pending=True)
            reason = f"key: {e}"
            break
        except Exception as e:  # noqa: BLE001 - a failing unit never stops the run; stored on the row
            _fail(store, key, f"{type(e).__name__}: {api.keys.scrub(str(e))}"[:300])
            errors_in_row += 1
            log(f"{key}: {type(e).__name__}: {e}")
            if errors_in_row >= 10:
                reason = "10 units in a row failed (network or API down?)"
                break
            api.sleep(min(5 * errors_in_row, 60))
            n += 1
            continue
        n += 1
        if n % 10 == 0 or limit is not None:
            log(f"{n} units this run, {api.hops - hops0} credits, last {key}: {res['status']} ({res['hops']} credits, {res.get('facts', 0)} swaps)")
        if only:
            break
    return {"launches": n, "units": n, "hops": api.hops - hops0, "reason": reason}


# ---------------------------------------------------------------- status and projection

def status(store: Store) -> dict[str, Any]:
    """Progress and the PROJECTION: credits spent, credits per finished unit (all, and per quote token), units left and the credits they would take (a unit that is `skipped` costs nothing and
    is left out of the average; a token without a finished unit uses the overall average)."""
    rows = store.con.execute("SELECT key, status, hops, facts, note FROM backfill_queue WHERE kind = ?", (KIND,)).fetchall()
    by: dict[str, int] = {}
    for r in rows:
        by[r[1]] = by.get(r[1], 0) + 1
    paid = [r for r in rows if r[1] in ("done", "partial") and (r[2] or 0) > 0]
    left = [r for r in rows if r[1] in ("pending", "running", "error")]
    spent = int(sum(r[2] or 0 for r in rows))
    avg = sum(r[2] for r in paid) / len(paid) if paid else None
    pq: dict[str, dict[str, float]] = {}
    for key, st, hops, _f, note in rows:
        q = BQ.split_key(key)[0]
        d = pq.setdefault(q, {"units": 0, "paid": 0, "credits": 0.0, "left": 0, "found": 0})
        d["units"] += 1
        if st in ("done", "partial") and (hops or 0) > 0:
            d["paid"] += 1
            d["credits"] += hops or 0
            d["found"] += bool(_note_field(note, "price"))
        elif st in ("pending", "running", "error"):
            d["left"] += 1
    by_quote, proj, unmeasured = [], 0.0, 0
    for q, d in pq.items():
        a = d["credits"] / d["paid"] if d["paid"] else None
        if d["left"]:
            if a is None:
                unmeasured += int(d["left"])
            proj += d["left"] * (a if a is not None else (avg or 0))
        by_quote.append({"quote": q, "units": int(d["units"]), "paid": int(d["paid"]), "price_found": int(d["found"]), "credits_per_unit": round(a, 1) if a is not None else None, "left": int(d["left"])})
    by_quote.sort(key=lambda x: -x["left"])
    return {"total": len(rows), "by_status": by, "credits_spent": spent, "paid_units": len(paid), "credits_per_unit": round(avg, 1) if avg is not None else None,
            "units_left": len(left), "projection_left": round(len(left) * avg) if avg is not None else None, "projection_left_by_quote": round(proj) if avg is not None else None,
            "units_without_own_rate": unmeasured, "by_quote": by_quote[:12], "swaps_written": int(sum(r[3] or 0 for r in rows if r[1] in ("done", "partial"))),
            "partial_examples": [[r[0], r[4]] for r in rows if r[1] == "partial"][:5], "error_examples": [[r[0], r[4]] for r in rows if r[1] == "error"][:5],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
