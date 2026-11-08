"""RGY-020 phase D: the buyers of the launches, and the NEAR / wNEAR they received in the 7 days before their first buy.

Who: the accounts with at least one attributed BUY (`snapshots.attribute`, the same attribution the snapshots use) on a launch in
its first 24 hours, other than the launch's dev (owner / creator) and the service accounts (`snapshots.is_service`). The trades come
from the live `facts` of the launches the collector saw (`launches.coverage` NULL) and from `api_facts` of the backfilled history
launches (phase B). The dev's transfer targets (insiders) are NOT excluded: they are buyers of the same kind for a bundle analysis.
One unit of work = one wallet (`backfill_queue`, kind 'buyers_in', key = account, ts_ns = the time of its FIRST buy anywhere): the
incoming transfers of the two assets (`native:near`, `nep141:wrap.near`; >= 0.1 NEAR) in [first buy - 7 days, first buy], newest
first, at most CAP_PAGES pages per asset (more = `truncated`, the oldest rows of the window are dropped). Rows go to `transfers_api`
(the table of phase C). A wallet that is also a launch participant (queue kind 'transfers_in' done) is `skipped`: its whole
history is already there. Newest first-buys are fetched first. Credits: 1 request = 1 credit, 2 assets x 1-2 pages: ~2-4 per wallet.
Nothing here touches facts, blocks, snapshots, the config or the rules; `links` does not read buyer rows (only the rows of the
participants), so the link tables are the same before and after this phase.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from . import backfill_transfers as T
from . import snapshots as S
from .backfill import Api, rows_from_api
from .parse import LAUNCHBOT
from .store import Store
from .store.db import utcnow_iso

KIND = "buyers_in"
WINDOW_DAYS = 7
CAP_PAGES = 2                    # per asset (200 rows)
MAX_HOPS_BUYER = 12              # 2 assets x 2 pages = 4, plus retries
NS_DAY = 24 * 3600 * 10**9


def collect(store: Store, since_ns: int | None = None, until_ns: int | None = None) -> tuple[dict[str, dict[str, int]], Counter]:
    """({buyer: {first_ns, launches, buys}}, counters of launches by the way they were read).
    since_ns (exclusive) / until_ns (inclusive): only the launches with a launch time in that range (phase E: the new ones)."""
    con = store.con
    got: dict[str, dict[str, int]] = {}
    seen: Counter = Counter()
    hist_done = {r[0] for r in con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status IN ('done', 'partial')")}
    for (token,) in con.execute("SELECT token FROM launches WHERE ts_ns > ? AND ts_ns <= ? ORDER BY ts_ns",
                                (since_ns if since_ns is not None else -1, until_ns if until_ns is not None else 2**62)).fetchall():
        launch = S._launch(store, token)
        cov = con.execute("SELECT coverage FROM launches WHERE token = ?", (token,)).fetchone()[0]
        cutoff = launch["ts_ns"] + S.MINUTES[-1] * S.NS_MIN
        if cov is None:
            legs, swaps = S._rows(store, launch, cutoff)
            seen["launches read from live facts"] += 1
        elif token in hist_done:
            legs, swaps = rows_from_api(store, launch, cutoff)
            seen["launches read from the backfill (api_facts)"] += 1
        else:
            seen["launches without trades (not backfilled)"] += 1
            continue
        dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != LAUNCHBOT}
        mine: dict[str, list[int]] = {}
        for t in S.attribute(token, legs, swaps):
            if t["side"] == "buy" and t["who"] and t["who"] not in dev and not S.is_service(t["who"], token):
                mine.setdefault(t["who"], []).append(t["ts"])
        for who, tss in mine.items():
            b = got.setdefault(who, {"first_ns": tss[0], "launches": 0, "buys": 0})
            b["first_ns"] = min(b["first_ns"], min(tss))
            b["launches"] += 1
            b["buys"] += len(tss)
    return got, seen


def enqueue(store: Store, since_ns: int | None = None, until_ns: int | None = None) -> dict[str, Any]:
    """Every buyer into the queue (idempotent; a wallet already there keeps its status and its first buy).
    Own transaction. Returns the counts and the credit estimate. since_ns / until_ns: see `collect`."""
    got, seen = collect(store, since_ns, until_ns)
    now = utcnow_iso()
    covered = {r[0] for r in store.con.execute("SELECT key FROM backfill_queue WHERE kind = ? AND status IN ('done', 'partial')", (T.KIND,))}
    store.begin()
    try:
        added = 0
        for who, b in got.items():
            added += store.con.execute(
                "INSERT OR IGNORE INTO backfill_queue(kind,key,status,ts_ns,note,queued_at) VALUES(?,?,?,?,?,?)",
                (KIND, who, "skipped" if who in covered else "pending", b["first_ns"],
                 "covered by transfers_in (whole history already stored)" if who in covered else None, now)).rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    todo = [w for w in got if w not in covered]
    multi = sum(1 for b in got.values() if b["launches"] >= 2)
    return {"buyers": len(got), "added": added, "already_covered_by_participants": len(got) - len(todo),
            "to_fetch": len(todo), "buying_in_2_or_more_launches": multi, "launches": dict(seen),
            "credits_estimate": {"low": 2 * len(todo), "typical": round(2.3 * len(todo)), "high": 4 * len(todo)}}


def process_buyer(store: Store, api: Api, account: str, log: Callable[[str], None]) -> dict[str, Any]:
    q = store.con.execute("SELECT ts_ns FROM backfill_queue WHERE kind = ? AND key = ?", (KIND, account)).fetchone()
    covered = store.con.execute("SELECT 1 FROM backfill_queue WHERE kind = ? AND key = ? AND status IN ('done', 'partial')",
                                (T.KIND, account)).fetchone()
    if covered:                                                  # became a participant since the plan: its whole history is stored
        store.begin()
        try:
            T._set(store, account, KIND, status="skipped", note="covered by transfers_in (whole history already stored)",
                   finished_at=utcnow_iso())
            store.commit()
        except BaseException:
            store.rollback()
            raise
        return {"status": "skipped", "hops": 0}
    first = q[0]
    return T.process_account(store, api, account, log, kind=KIND, since_ms=(first - WINDOW_DAYS * NS_DAY) // 10**6,
                             until_ms=first // 10**6 + 1, cap_pages=CAP_PAGES, max_hops=MAX_HOPS_BUYER)


def run(store: Store, api: Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None,
        only: str | None = None, log: Callable[[str], None] = print, retry_errors: bool = True) -> dict[str, Any]:
    return T.run(store, api, stop, max_credits, limit, only, log, kind=KIND, process=process_buyer, retry_errors=retry_errors)


def status(store: Store) -> dict[str, Any]:
    out = T.status(store, KIND)
    q = store.con.execute
    mine = "dir = 'in' AND account IN (SELECT key FROM backfill_queue WHERE kind = ? AND status IN ('done', 'partial'))"
    out["rows"] = q(f"SELECT COUNT(*) FROM transfers_api WHERE {mine}", (KIND,)).fetchone()[0]
    out["rows_by_asset"] = dict(q(f"SELECT asset, COUNT(*) FROM transfers_api WHERE {mine} GROUP BY 1", (KIND,)))
    out["rows_by_transfer_type"] = dict(q(f"SELECT COALESCE(ttype, '-'), COUNT(*) FROM transfers_api WHERE {mine} GROUP BY 1 ORDER BY 2 DESC LIMIT 8", (KIND,)))
    out["accounts_with_rows"] = q(f"SELECT COUNT(DISTINCT account) FROM transfers_api WHERE {mine}", (KIND,)).fetchone()[0]
    out["oldest_row"] = q(f"SELECT MIN(ts_ns) FROM transfers_api WHERE {mine}", (KIND,)).fetchone()[0]
    out["buyers_with_no_incoming_in_the_window"] = q(
        "SELECT COUNT(*) FROM backfill_queue WHERE kind = ? AND status = 'done' AND COALESCE(facts, 0) = 0", (KIND,)).fetchone()[0]
    return out
