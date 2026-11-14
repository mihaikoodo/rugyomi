"""RGY-055: fill the HISTORY GAP of the launches that still trade, so their Live state can give a number.

A history launch (`launches.coverage` set) has its trades for its first 24 h in `api_facts` (scope = the token, `backfill`) and every swap from the start of the
complete live data (`live_state.history_until`) in `facts`. What lies between - the GAP - is missing, so the Live state shows `history_gap` and no number
(`livestate.compute_gap`). This module loads the gap through the same route as the 24 h window (FastNear Transactions API: the account index of the token
contract, the transactions by hash, the same parser, `backfill.fetch_window` / `parse_txs` / `write_api_facts`) into `api_facts` with its OWN scope
(`scope_of(token)` = "gap:" + token), so the window rows are never replaced (the RGY-024 trap: `backfill.process_launch` rewrites the window and the outcome; it is
never called here).

WHICH LAUNCHES (user decision 12.11: groups A + B, not C). A history launch with a finished window (its launch backfill done / partial), a pool, and a window that
ends before the live data:
  A  a swap on its pool in `facts` after the window ends (it traded since the live data starts);
  B  none, but its last swap inside the window is within TAIL_MIN of the window's end (it was still trading when the window closed).
The same rules as the RGY-043 step-0 diagnostic of the gap.

ONE UNIT = one launch (`backfill_queue`, kind 'gap', key = the token, ts_ns = the launch time; newest first, like the other kinds). Statuses:
  done      the fill reached the live data with every transaction back and no cap hit: the GAP IS CLOSED (`closed()`), the only state in which the Live state
            computes a number over the whole history of the launch;
  partial   the index was cut (GAP_CAP_TX), the launch's hop cap (MAX_HOPS_GAP) was hit or a transaction did not come back: rows kept, the gap stays OPEN;
  skipped   nothing to fill (no pool, no gap);
  error     an exception (stored on the row); pending again on a later run (attempts < 3).
Writes: `api_facts` (scope "gap:<token>", replaced as a whole), the queue row, and DELETES the launch's `launch_live_state` row (derived; the collector recomputes
it first, now with the number). Never `facts`, `launch_outcomes`, `launch_snapshots`, `cards`. 1 HTTP request = 1 credit; the run stops at its credit cap BEFORE a
launch could pass it (the launch's own hop budget is the cap that is left), and a launch cut by the run cap goes back to pending (resumable).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from . import backfill as BF
from . import snapshots as S
from .store.db import Store, utcnow_iso

KIND = "gap"
PREFIX = "gap:"
TAIL_MIN = 60                   # group B: the last swap of the window within this many minutes of its end (live_state_gap.py --tail-min)
GAP_CAP_TX = 20_000             # transactions of one gap (100 index pages + 1000 batches); beyond it the launch is `partial`
MAX_HOPS_GAP = 1_500            # hops of one launch; beyond it the launch is `partial`
H_TO_MARGIN = 2_000             # blocks after the first live fact, for the upper bound of the index query (the time check stops earlier)
NS_DAY = 86400 * 10**9
WIN_NS = S.MINUTES[-1] * S.NS_MIN


def scope_of(token: str) -> str:
    return PREFIX + token


def _live_from(cfg: dict[str, Any] | None = None) -> int:
    from .livestate import settings
    from .rules.config import load_config
    return settings(cfg or load_config())["live_from_ns"]


def candidates(store: Store, live_from: int) -> dict[str, list[tuple[str, int]]]:
    """{"A": [(token, launch ts)], "B": [...]} by the module docstring's rules. Read only."""
    con = store.con
    swap = store.lookup_kind("dcl_swap")
    out: dict[str, list[tuple[str, int]]] = {"A": [], "B": []}
    if swap is None or store.user_version() < 11:
        return out
    rows = con.execute("SELECT l.token, l.pool, l.ts_ns FROM launches l JOIN backfill_queue q ON q.kind = 'launch' AND q.key = l.token "
                       "WHERE l.coverage IS NOT NULL AND q.status IN ('done', 'partial') AND l.pool IS NOT NULL ORDER BY l.ts_ns").fetchall()
    for token, pool, ts in rows:
        end = ts + WIN_NS
        if end >= live_from:
            continue                                                    # the window reaches the live data: no gap
        pid = store.lookup_account(pool)
        if pid is None:
            continue
        if con.execute("SELECT 1 FROM facts WHERE kind = ? AND b = ? AND ts_ns > ? LIMIT 1", (swap, pid, end)).fetchone():
            out["A"].append((token, ts))
            continue
        last = con.execute("SELECT MAX(ts_ns) FROM api_facts WHERE scope = ? AND kind = ? AND b = ?", (token, swap, pid)).fetchone()[0]
        if last is not None and last >= end - TAIL_MIN * S.NS_MIN:
            out["B"].append((token, ts))
    return out


def enqueue(store: Store, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """Add the A + B launches to the queue (idempotent; a row already there keeps its status). Own transaction."""
    lf = _live_from(cfg)
    c = candidates(store, lf)
    now = utcnow_iso()
    store.begin()
    try:
        n = 0
        for g in ("A", "B"):
            for token, ts in c[g]:
                n += store.con.execute("INSERT OR IGNORE INTO backfill_queue(kind, key, status, ts_ns, queued_at, note) VALUES(?,?,?,?,?,?)",
                                       (KIND, token, "pending", ts, now, f"group {g}")).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"A": len(c["A"]), "B": len(c["B"]), "added": n}


def closed(con: Any, token: str) -> bool:
    """The gap of a history launch is closed: its fill finished (status done). The Live state gives a number only then."""
    con = getattr(con, "con", con)
    try:
        r = con.execute("SELECT status FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, token)).fetchone()
    except Exception:  # noqa: BLE001 - a database without the queue
        return False
    return bool(r and r[0] == "done")


def bounds(store: Store, launch: dict[str, Any], live_from: int) -> tuple[int, int] | None:
    """(start height, upper height) of the index query of the gap: from the last height of the window's rows (a transaction included before the window's end
    can execute after it) to a little past the first fact of the live data; None when the live data has no fact yet."""
    h0 = store.con.execute("SELECT MAX(height) FROM api_facts WHERE scope = ?", (launch["token"],)).fetchone()[0]
    h1 = store.con.execute("SELECT height FROM facts INDEXED BY facts_ts WHERE ts_ns >= ? ORDER BY ts_ns LIMIT 1", (live_from,)).fetchone()
    if h1 is None:
        return None
    return int(h0 if h0 is not None else launch["height"]), int(h1[0]) + H_TO_MARGIN


def _set(store: Store, token: str, **cols: Any) -> None:
    sets = ",".join(f"{k} = ?" for k in cols)
    store.con.execute(f"UPDATE backfill_queue SET {sets} WHERE kind = ? AND key = ?", (*cols.values(), KIND, token))


def _finish(store: Store, token: str, res: dict[str, Any]) -> dict[str, Any]:
    store.begin()
    try:
        _set(store, token, status=res["status"], hops=res["hops"], truncated=res.get("truncated", 0), note=res.get("note"), finished_at=utcnow_iso())
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


class RunCap(Exception):
    """The run's credit cap ended this launch: it goes back to pending."""


def process_gap(store: Store, api: BF.Api, token: str, live_from: int, budget: int, log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """One launch: fetch the gap (window end < ts < live_from), write it under its own scope, close or not. `budget`: hops this launch may use
    (min of MAX_HOPS_GAP and what the run has left)."""
    launch = S._launch(store, token)
    if launch is None or not launch.get("pool"):
        return _finish(store, token, {"status": "skipped", "note": "no launches row or no pool", "hops": 0})
    launch["height"] = store.con.execute("SELECT height FROM launches WHERE token = ?", (token,)).fetchone()[0]
    end = launch["ts_ns"] + WIN_NS
    if end >= live_from:
        return _finish(store, token, {"status": "skipped", "note": "the window reaches the live data: no gap", "hops": 0})
    b = bounds(store, launch, live_from)
    if b is None:
        return _finish(store, token, {"status": "skipped", "note": "no live fact yet", "hops": 0})
    api.budget = min(budget, MAX_HOPS_GAP)
    run_cut = budget < MAX_HOPS_GAP
    hops0 = api.hops
    try:
        w = BF.fetch_window(api, token, b[0], live_from - 1, cap_tx=GAP_CAP_TX, h_to=b[1])
    except BF.HopCap:
        if run_cut:
            raise RunCap(token) from None
        return _finish(store, token, {"status": "partial", "note": f"hop cap {MAX_HOPS_GAP} of the launch reached", "hops": api.hops - hops0, "truncated": 1})
    finally:
        api.budget = None
    hops = api.hops - hops0
    facts, notes, errors, receipts = BF.parse_txs(w["txs"], token, launch["pool"], live_from - 1)
    facts = [x for x in facts if x[0].ts_ns > end]                    # the window's own rows stay in the window's scope
    note: list[str] = []
    partial = False
    if w["truncated"]:
        partial = True
        note.append(f"index cut at {GAP_CAP_TX} transactions")
    if w["missing"]:
        partial = True
        note.append(f"{len(w['missing'])} requested transaction(s) not returned")
    if errors:
        note.append(f"{len(errors)} parse error(s): {errors[0].error[:120]}")
    note += notes[:3]
    res: dict[str, Any] = {"hops": hops, "txs": len(w["txs"]), "truncated": int(w["truncated"]), "status": "partial" if partial else "done"}
    store.begin()
    try:
        res["facts"] = BF.write_api_facts(store, scope_of(token), facts)
        if res["status"] == "done":
            store.con.execute("DELETE FROM launch_live_state WHERE token = ?", (token,))      # derived: the collector recomputes it first, now with the whole history
        res["note"] = "; ".join(note) or None
        _set(store, token, status=res["status"], hops=hops, txs=res["txs"], facts=res["facts"], truncated=res["truncated"], note=res["note"], finished_at=utcnow_iso())
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
    """`n` pending launches spread evenly over the launch time (the oldest, the newest and between): a pilot that sees every gap length, so the projection is not
    measured on the shortest gaps only."""
    pend = [k for (k,) in store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status = 'pending' ORDER BY ts_ns", (KIND,))]
    if n <= 0 or not pend:
        return []
    if n >= len(pend):
        return pend
    return [pend[round(i * (len(pend) - 1) / (n - 1))] if n > 1 else pend[0] for i in range(n)]


def run(store: Store, api: BF.Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None, only: str | None = None,
        log: Callable[[str], None] = print, cfg: dict[str, Any] | None = None, order: str = "newest") -> dict[str, Any]:
    """Process pending gaps until the queue is empty, `stop()`, the credit cap (never passed) or `limit` launches. `order`: "newest" (the newest launch first) or
    "spread" (`limit` launches spread over the launch time - a pilot for the projection; needs `limit`)."""
    lf = _live_from(cfg)
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
        left = None if max_credits is None else max_credits - (api.hops - hops0)
        if left is not None and left <= 0:
            reason = f"credit cap {max_credits} reached"
            break
        if limit is not None and n >= limit:
            reason = f"limit {limit} reached"
            break
        token = only if (only and n == 0) else (None if only else (picks.pop(0) if picks else None) if picks is not None else _next(store))
        if token is None:
            break
        store.begin()
        try:
            store.con.execute("UPDATE backfill_queue SET status = 'running', attempts = attempts + 1, started_at = ? WHERE kind = ? AND key = ?", (utcnow_iso(), KIND, token))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        try:
            res = process_gap(store, api, token, lf, MAX_HOPS_GAP if left is None else left, log)
            errors_in_row = 0
        except RunCap:
            _fail(store, token, f"the run's credit cap ended this launch after {api.hops - hops0} credits of the run; pending again", pending=True)
            reason = f"credit cap {max_credits} reached"
            break
        except BF.KeyLost as e:
            _fail(store, token, str(e)[:300], pending=True)
            reason = f"key: {e}"
            break
        except Exception as e:  # noqa: BLE001 - a failing launch never stops the run; stored on the row
            _fail(store, token, f"{type(e).__name__}: {api.keys.scrub(str(e))}"[:300])
            errors_in_row += 1
            log(f"{token}: {type(e).__name__}: {e}")
            if errors_in_row >= 10:
                reason = "10 launches in a row failed (network or API down?)"
                break
            api.sleep(min(5 * errors_in_row, 60))
            n += 1
            continue
        n += 1
        if n % 10 == 0 or limit is not None:
            log(f"{n} gaps this run, {api.hops - hops0} credits, last {token}: {res['status']} ({res['hops']} credits, {res.get('facts', 0)} rows)")
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


def status(store: Store, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Progress and the PROJECTION: credits spent, credits per launch-day of gap measured on the finished launches, the gap-days left and the credits they would take."""
    lf = _live_from(cfg)
    q = store.con.execute
    rows = q("SELECT key, status, ts_ns, hops, facts, note FROM backfill_queue WHERE kind = ?", (KIND,)).fetchall()
    by: dict[str, int] = {}
    for _k, st, *_ in rows:
        by[st] = by.get(st, 0) + 1
    days = lambda ts: max(0.0, (lf - (ts + WIN_NS)) / NS_DAY)  # noqa: E731
    fin = [r for r in rows if r[1] in ("done", "partial")]
    spent = int(sum(r[3] or 0 for r in rows))
    fin_days = sum(days(r[2]) for r in fin)
    fin_hops = sum(r[3] or 0 for r in fin)
    rate = fin_hops / fin_days if fin_days else None
    left = [r for r in rows if r[1] in ("pending", "running", "error")]
    left_days = sum(days(r[2]) for r in left)
    per_launch = fin_hops / len(fin) if fin else None
    return {"total": len(rows), "by_status": by, "credits_spent": spent, "finished": len(fin), "finished_gap_days": round(fin_days, 1),
            "credits_per_gap_day": round(rate, 3) if rate is not None else None, "credits_per_launch": round(fin_hops / len(fin), 1) if fin else None,
            "gap_days_left": round(left_days, 1), "projection_left": round(left_days * rate) if rate is not None else None,
            "projection_total": round(spent + left_days * rate) if rate is not None else None,
            "launches_left": len(left),
            "projection_total_by_launch": round(spent + len(left) * per_launch) if per_launch is not None else None,
            "closed": by.get("done", 0), "rows_written": int(sum(r[4] or 0 for r in fin)),
            "partial_examples": [[r[0], r[5]] for r in rows if r[1] == "partial"][:5], "error_examples": [[r[0], r[5]] for r in rows if r[1] == "error"][:5],
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ---------------------------------------------------------------- reading the whole history of a launch whose gap is closed

def full_rows(store: Store, launch: dict[str, Any], now_ns: int, live_from: int) -> tuple[list[dict], list[dict]]:
    """(legs, swaps) of a history launch with a CLOSED gap, up to `now_ns`: the window (api_facts, scope = token), the gap (scope "gap:<token>", after the window,
    before the live data) and the live data (`facts` from `live_from` on), in time order, with unique ids. The same readers as everywhere (`rows_from_api`,
    `snapshots._rows`); the facts before `live_from` (the sparse early collection) are not used."""
    end = launch["ts_ns"] + WIN_NS
    wl, ws = BF.rows_from_api(store, launch, min(end, now_ns))
    gl, gs = BF.rows_from_api(store, launch, min(live_from - 1, now_ns), scope=scope_of(launch["token"]), after=end)
    ll, ls = S._rows(store, launch, now_ns)
    ll = [x for x in ll if x["ts"] >= live_from]
    ls = [x for x in ls if x["ts"] >= live_from]
    legs, swaps, i = [], [], 0
    for part_l, part_s in ((wl, ws), (gl, gs), (ll, ls)):
        merged = sorted([("l", x) for x in part_l] + [("s", x) for x in part_s], key=lambda t: t[1]["id"])       # the order inside a part, as its reader gave it
        for kind, x in merged:
            y = dict(x, id=i)
            i += 1
            (legs if kind == "l" else swaps).append(y)
    return legs, swaps
