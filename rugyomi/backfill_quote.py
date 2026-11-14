"""RGY-070 (B of Q-046): collect the swaps of the pools Q / wNEAR for the quote tokens Q of the launches that are not paired with wNEAR, so `quote_price` has a PROVABLE
NEAR price at the time of their swaps.

WHAT IS NEEDED. A launch paired with a quote token Q (`quote.not_near_quote`) needs the price of Q in NEAR at the time of EACH of its trades in the 24 h window its
snapshots read. `quote_price.convert_swaps` converts all or nothing, so only the trades that have no price from what is stored (0 credits) matter: the wNEAR pool of a
memetoken that is our own launch is in `facts` / `api_facts` already and costs nothing. For the trades without a price the collector asks for the time just BEFORE each
of them (PRE = twice the price age: 30 min), not for the whole 24 h of the launch - the cost is the transactions of the token contract in that time, and most trades
of a launch happen in its first hours. The intervals of one Q are merged when they are closer than MERGE_GAP and cut at UNIT_MAX_H.

ONE UNIT = (Q, interval) in `backfill_queue` (kind 'quote', key = "<Q>:<interval start ns>", ts_ns = the start, height = the start height, note "span=<ns>;h1=<end height>;n=<trades>"; newest first like the other kinds). Same route as the
launch windows: the FastNear Transactions API (account index of the token contract Q in time order + the transactions by hash, `backfill.fetch_window`), the same parser,
the swaps of the pools `Q|wrap.near|fee` kept in `api_facts` under their OWN scope "quote:<key>" (`quote_price.scope_prefix`). Statuses: done (index finished inside the
window, every transaction back, no cap hit), partial (index cut at UNIT_CAP_TX, the unit's hop cap or a missing transaction: rows kept, the window may have holes - a
launch whose trade falls into a hole simply stays undetermined), skipped (no Q any more / nothing to fetch), error (pending again, attempts < 3). 1 HTTP request =
1 credit; the run stops at its credit cap BEFORE a unit could pass it, a unit cut by the cap goes back to pending (resumable).

Writes: `api_facts` (scope "quote:<key>") and the queue row. NEVER `facts`, `cards`, `launch_snapshots`, `launch_outcomes` (they are rebuilt later, after the user has read
the before / after, by the existing paths: `rebuild-snapshots` + `rebuild_outcomes`)."""
from __future__ import annotations

import time
from typing import Any, Callable

from . import backfill as BF
from . import quote_price as QP
from . import snapshots as S
from .parse import Fact
from .quote import not_near_quote
from .store.db import Store, utcnow_iso

KIND = "quote"
NS_MIN = S.NS_MIN
WIN_NS = QP.WINDOW_MIN * NS_MIN
PRE_NS = 2 * QP.MAX_AGE_MIN * NS_MIN     # an interval starts this long before the trade it prices (twice the default price age)
MERGE_GAP_NS = 30 * NS_MIN       # intervals closer than this are one unit (a unit costs at least 2 credits, the transactions between two intervals cost more)
UNIT_MAX_H = 48                  # a merged interval longer than this is cut at a trade boundary
UNIT_CAP_TX = 6000               # transactions of one unit (30 index pages + 300 batches); beyond it the unit is `partial`
MAX_HOPS_UNIT = 700              # credits of one unit
BLOCKS_PER_S = 1.5               # upper bound of the block rate (24 h is 86-110 k blocks): the margin before the first trade of a unit
END_MARGIN_BLOCKS = 100          # blocks after the last trade of a unit for the index query (the time check stops earlier)
SAFETY_NS = 60 * NS_MIN          # a launch is collected only when its window ended this long ago


def scope_of(key: str) -> str:
    return "quote:" + key


def key_of(quote: str, start_ns: int) -> str:
    return f"{quote}:{start_ns}"


def split_key(key: str) -> tuple[str, int]:
    q, _, ts = key.rpartition(":")
    return q, int(ts)


def is_pair_pool(f: Fact, quote: str) -> bool:
    """The facts a quote window keeps: the swaps of the pools of the pair Q / wNEAR (any fee)."""
    return f.kind == "dcl_swap" and (f.b or "").startswith(QP.pool_prefix(quote))


# ---------------------------------------------------------------- what is needed (0 credits)

def dependents(store: Store, now_ns: int | None = None) -> list[dict[str, Any]]:
    """Launches whose pool is not paired with wNEAR: {token, pool, quote, ts_ns, height, coverage}, oldest first. `now_ns`: only launches whose window ended before it."""
    out = []
    for token, pool, ts, height, cov in store.con.execute("SELECT token, pool, ts_ns, height, coverage FROM launches WHERE pool IS NOT NULL ORDER BY ts_ns"):
        q = not_near_quote(token, pool)
        if q and (now_ns is None or ts + WIN_NS + SAFETY_NS <= now_ns):
            out.append({"token": token, "pool": pool, "quote": q, "ts_ns": ts, "height": height, "coverage": cov})
    return out


def launch_swaps(store: Store, d: dict[str, Any]) -> list[dict]:
    """The swaps of the launch pool in its 24 h window, from the same place the snapshot of that launch reads them."""
    end = d["ts_ns"] + WIN_NS
    if d["coverage"] is not None:                                  # a history launch: its window is in api_facts
        from .backfill import rows_from_api
        return rows_from_api(store, d, end)[1]
    return S._swaps(store, d, end)


def check(store: Store, d: dict[str, Any], series: QP.Series | None = None) -> dict[str, Any]:
    """The conversion of one launch with what is stored: {trades, missing, ok, pools, max_age_s}. `series` given = reused (the diag loads one per Q)."""
    swaps = launch_swaps(store, d)
    ser = series if series is not None else QP.series_for(store, d, d["quote"])
    return QP.convert_swaps(d["token"], d["quote"], swaps, ser) | {"series_points": len(ser.points)}


def trade_heights(store: Store, d: dict[str, Any]) -> dict[int, int]:
    """{swap time: block height} of the launch pool's swaps in the launch's 24 h window (the height is where the index query of a unit starts)."""
    kind, pid = store.lookup_kind("dcl_swap"), store.lookup_account(d["pool"])
    end = d["ts_ns"] + WIN_NS
    out: dict[int, int] = {}
    if kind is None or pid is None:
        return out
    q = "SELECT ts_ns, height FROM facts WHERE kind = ? AND b = ? AND ts_ns <= ?"
    args: tuple = (kind, pid, end)
    if d["coverage"] is not None:
        q, args = "SELECT ts_ns, height FROM api_facts WHERE scope = ? AND kind = ? AND b = ? AND ts_ns <= ?", (d["token"], kind, pid, end)
    for ts, h in store.con.execute(q, args):
        out[int(ts)] = min(int(h), out.get(int(ts), int(h)))
    return out


def _span(note: str | None) -> int:
    """The interval length (ns) a unit's note starts with ("span=<ns>;h1=<height>;n=<trades>;...")."""
    n = (note or "").split(";")[0].removeprefix("span=")
    return int(n) if n.isdigit() else 0


def _field(note: str | None, name: str) -> int | None:
    for part in (note or "").split(";"):
        if part.startswith(name + "="):
            v = part.split("=", 1)[1]
            return int(v) if v.isdigit() else None
    return None


def _done_units(store: Store) -> list[tuple[str, int, int]]:
    """[(quote, interval start, interval end)] of the units already collected (done / partial)."""
    out = []
    for key, note in store.con.execute("SELECT key, note FROM backfill_queue WHERE kind = ? AND status IN ('done', 'partial')", (KIND,)):
        q, w0 = split_key(key)
        out.append((q, w0, w0 + _span(note)))
    return out


def needed(store: Store, now_ns: int) -> list[dict[str, Any]]:
    """The trades that have no price from the stored swaps: [{quote, ts, height, token}] over the dependents whose window ended an hour ago. A trade inside an interval that
    was already collected is left out (collecting it again changes nothing)."""
    done = _done_units(store)
    out: list[dict[str, Any]] = []
    for d in dependents(store, now_ns):
        r = check(store, d)
        if r["total"] == 0 or r["ok"]:
            continue
        heights = trade_heights(store, d)
        for ts in r["missing_ts"]:
            if any(q == d["quote"] and w0 <= ts <= w1 for q, w0, w1 in done):
                continue
            out.append({"quote": d["quote"], "ts": ts, "height": heights.get(ts, d["height"]), "token": d["token"]})
    return out


def windows(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The units: per quote token the intervals [trade - PRE, trade] of the trades without a price, merged when they are closer than MERGE_GAP, cut at UNIT_MAX_H.
    {quote, start, end, h0, h1, trades, launches}. Newest first."""
    by: dict[str, list[dict]] = {}
    for p in points:
        by.setdefault(p["quote"], []).append(p)
    out = []
    for q, ps in by.items():
        ps.sort(key=lambda p: p["ts"])
        cur: dict[str, Any] | None = None
        for p in ps:
            lo = p["ts"] - PRE_NS
            if cur is not None and lo <= cur["end"] + MERGE_GAP_NS and p["ts"] - cur["start"] <= UNIT_MAX_H * 3600 * 10**9:
                cur["end"] = max(cur["end"], p["ts"])
                cur["trades"] += 1
                cur["h1"] = max(cur["h1"], p["height"])
                cur["launches"].add(p["token"])
                continue
            cur = {"quote": q, "start": lo, "end": p["ts"], "h0": p["height"], "h1": p["height"], "trades": 1, "launches": {p["token"]}, "first": p["ts"]}
            out.append(cur)
        # the start height of a unit is the height of its FIRST trade minus a block margin for PRE (done below, once the unit is closed)
    for u in out:
        u["h0"] = max(1, u["h0"] - int(PRE_NS / 1e9 * BLOCKS_PER_S) - BF.START_MARGIN)
        u["h1"] = u["h1"] + END_MARGIN_BLOCKS
    out.sort(key=lambda u: u["start"], reverse=True)
    return out


def enqueue(store: Store, now_ns: int | None = None) -> dict[str, int]:
    """Add the units the trades without a price require (idempotent: a row already there keeps its status). Own transaction. Reads only the stored facts: 0 credits."""
    now_ns = now_ns if now_ns is not None else time.time_ns()
    items = needed(store, now_ns)
    units = windows(items)
    now = utcnow_iso()
    store.begin()
    try:
        n = 0
        for u in units:
            n += store.con.execute("INSERT OR IGNORE INTO backfill_queue(kind, key, status, ts_ns, height, queued_at, note) VALUES(?,?,?,?,?,?,?)",
                                   (KIND, key_of(u["quote"], u["start"]), "pending", u["start"], u["h0"], now,
                                    f"span={u['end'] - u['start']};h1={u['h1']};n={u['trades']};{len(u['launches'])} launch(es)")).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"needed_trades": len(items), "needed_launches": len({i["token"] for i in items}), "units": len(units), "added": n}


# ---------------------------------------------------------------- one unit

class RunCap(Exception):
    """The run's credit cap ended this unit: it goes back to pending."""


def _set(store: Store, key: str, **cols: Any) -> None:
    sets = ",".join(f"{k} = ?" for k in cols)
    store.con.execute(f"UPDATE backfill_queue SET {sets} WHERE kind = ? AND key = ?", (*cols.values(), KIND, key))


def _finish(store: Store, key: str, res: dict[str, Any]) -> dict[str, Any]:
    store.begin()
    try:
        _set(store, key, status=res["status"], hops=res["hops"], truncated=res.get("truncated", 0), note=res.get("note"), finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


def process_unit(store: Store, api: BF.Api, key: str, budget: int, log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    q, w0 = split_key(key)
    row = store.con.execute("SELECT height, note FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, key)).fetchone()
    note0 = (row[1] or "") if row else ""
    span, h1 = _span(note0), _field(note0, "h1")
    if not row or row[0] is None or not span or h1 is None:
        return _finish(store, key, {"status": "skipped", "note": "no interval or height", "hops": 0})
    end_ts = w0 + span
    h0, h_to = int(row[0]), h1
    head = ";".join(note0.split(";")[:3])                                  # span=..;h1=..;n=..
    api.budget = min(budget, MAX_HOPS_UNIT)
    run_cut = budget < MAX_HOPS_UNIT
    hops0 = api.hops
    try:
        w = BF.fetch_window(api, q, h0, end_ts, cap_tx=UNIT_CAP_TX, h_to=h_to)
    except BF.HopCap:
        if run_cut:
            raise RunCap(key) from None
        return _finish(store, key, {"status": "partial", "note": f"{head};hop cap {MAX_HOPS_UNIT} of the unit reached", "hops": api.hops - hops0, "truncated": 1})
    finally:
        api.budget = None
    hops = api.hops - hops0
    facts, notes, errors, _receipts = BF.parse_txs(w["txs"], q, None, end_ts, keep=lambda f: is_pair_pool(f, q))
    facts = [x for x in facts if x[0].ts_ns >= w0]
    note: list[str] = [head]
    partial = False
    if w["truncated"]:
        partial = True
        note.append(f"index cut at {UNIT_CAP_TX} transactions")
    if w["missing"]:
        partial = True
        note.append(f"{len(w['missing'])} requested transaction(s) not returned")
    if errors:
        note.append(f"{len(errors)} parse error(s): {errors[0].error[:120]}")
    note += notes[:3]
    res: dict[str, Any] = {"hops": hops, "txs": len(w["txs"]), "truncated": int(w["truncated"]), "status": "partial" if partial else "done"}
    store.begin()
    try:
        res["facts"] = BF.write_api_facts(store, scope_of(key), facts)
        res["note"] = "; ".join(note)
        _set(store, key, status=res["status"], hops=hops, txs=res["txs"], facts=res["facts"], truncated=res["truncated"], note=res["note"], finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


def _next(store: Store) -> str | None:
    r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns DESC LIMIT 1", (KIND,)).fetchone()
    if r is None:
        r = store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'error' AND attempts < 3 ORDER BY attempts, ts_ns DESC LIMIT 1", (KIND,)).fetchone()
    return r[0] if r else None


def spread(store: Store, n: int) -> list[str]:
    """`n` pending units spread evenly over time (the oldest, the newest and between): a pilot that sees every kind of window, so the projection is not measured on one."""
    pend = [k for (k,) in store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns", (KIND,))]
    if n <= 0 or not pend:
        return []
    if n >= len(pend):
        return pend
    return [pend[round(i * (len(pend) - 1) / (n - 1))] if n > 1 else pend[0] for i in range(n)]


def _fail(store: Store, key: str, error: str, pending: bool = False) -> None:
    """The note keeps its `span=` head (the window length is needed when the unit is tried again)."""
    store.begin()
    try:
        r = store.con.execute("SELECT note FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, key)).fetchone()
        head = ";".join(((r[0] if r else None) or "").split(";")[:3]) if _span(r[0] if r else None) else ""
        _set(store, key, status="pending" if pending else "error", note=f"{head};{error}" if head else error, finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise


def run(store: Store, api: BF.Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None, only: str | None = None,
        log: Callable[[str], None] = print, order: str = "newest") -> dict[str, Any]:
    """Process pending units until the queue is empty, `stop()`, the credit cap (never passed) or `limit` units. `max_credits` is REQUIRED (the approved budget);
    `order` "spread" = `limit` units spread over time (the pilot for the projection)."""
    if max_credits is None:
        raise ValueError("backfill_quote.run needs an explicit credit cap (the user's approved budget)")
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
        except RunCap:
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
    """Progress and the PROJECTION: credits spent, credits per window-hour measured on the finished units, the window-hours left and the credits they would take."""
    rows = store.con.execute("SELECT key, status, hops, facts, note FROM backfill_queue WHERE kind = ?", (KIND,)).fetchall()
    by: dict[str, int] = {}

    def hours(note: str | None) -> float:
        return _span(note) / (3600 * 10**9)

    for r in rows:
        by[r[1]] = by.get(r[1], 0) + 1
    fin = [r for r in rows if r[1] in ("done", "partial")]
    left = [r for r in rows if r[1] in ("pending", "running", "error")]
    spent = int(sum(r[2] or 0 for r in rows))
    fin_h, fin_hops = sum(hours(r[4]) for r in fin), sum(r[2] or 0 for r in fin)
    left_h = sum(hours(r[4]) for r in left)
    rate = fin_hops / fin_h if fin_h else None
    per_unit = fin_hops / len(fin) if fin else None
    # per quote token: a busy token costs more per hour than a quiet one, so the projection also sums (hours left x the token's own measured rate); a token with no finished unit uses the overall rate
    pq: dict[str, dict[str, float]] = {}
    for key, st, hops, _f, note in rows:
        q = split_key(key)[0]
        d = pq.setdefault(q, {"units": 0, "done": 0, "credits": 0.0, "hours_done": 0.0, "hours_left": 0.0})
        d["units"] += 1
        if st in ("done", "partial"):
            d["done"] += 1
            d["credits"] += hops or 0
            d["hours_done"] += hours(note)
        elif st in ("pending", "running", "error"):
            d["hours_left"] += hours(note)
    by_quote, proj_q, unmeasured = [], 0.0, 0.0
    for q, d in pq.items():
        r = d["credits"] / d["hours_done"] if d["hours_done"] else None
        if d["hours_left"]:
            if r is None:
                unmeasured += d["hours_left"]
            proj_q += d["hours_left"] * (r if r is not None else (rate or 0))
        by_quote.append({"quote": q, "units": int(d["units"]), "done": int(d["done"]), "credits_per_hour": round(r, 2) if r is not None else None,
                         "hours_left": round(d["hours_left"], 1)})
    by_quote.sort(key=lambda x: -x["hours_left"])
    return {"total": len(rows), "by_status": by, "credits_spent": spent, "finished": len(fin), "finished_window_hours": round(fin_h, 1),
            "credits_per_window_hour": round(rate, 2) if rate is not None else None, "credits_per_unit": round(per_unit, 1) if per_unit is not None else None,
            "window_hours_left": round(left_h, 1), "units_left": len(left),
            "projection_left": round(left_h * rate) if rate is not None else None,
            "projection_total": round(spent + left_h * rate) if rate is not None else None,
            "projection_left_by_unit": round(len(left) * per_unit) if per_unit is not None else None,
            "swaps_written": int(sum(r[3] or 0 for r in fin)),
            "projection_left_by_quote": round(proj_q) if rate is not None else None, "hours_left_without_own_rate": round(unmeasured, 1), "by_quote": by_quote[:12],
            "partial_examples": [[r[0], r[4]] for r in rows if r[1] == "partial"][:5], "error_examples": [[r[0], r[4]] for r in rows if r[1] == "error"][:5],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
