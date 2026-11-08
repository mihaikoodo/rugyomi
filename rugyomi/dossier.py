"""Creator dossier and rankings over the whole history (RGY-023): derived tables, rebuilt by the collector, read by the site.

What a dossier is: the creator page at GROUP level — an account plus the accounts it is linked to (`links`: direct links, never
"one person"), with the account's own numbers shown next to the group's. Every launch of the group (history + live), what the buyers
lost, what the insiders made, where the money came from and where it went afterwards.

Storage choice — (A), precomputed (schema v14, additive): the collector rebuilds two small tables as a whole, in one transaction,
from tables it already holds (launches, cards, launch_outcomes, launch_snapshots, transfers_api, link_*):
  dossier_launch   one row per launch of a participant: group key, live/history, the live card's score, the outcome, the outside
                   buyers' loss at T+60, the insiders' result, time since the latest funding, NEAR sent on within 1 h / 24 h
  dossier_flow     per group, per direction, the top counterparties by NEAR (money in: before the launches; money out: after them),
                   each with a class — linked account, another participant, a funding source of a cluster, a listed service
                   account, a launchpad / launch-token contract, or an unknown wallet — and the largest transaction
  dossier_group    per group: size, first funding, totals, how many members have their incoming / outgoing transfers loaded
The pages and the rankings are plain indexed SQL over these tables (a few ms), so a page is fast on one vCPU and the collector stays
the only writer. Cost: the numbers are as old as the last rebuild (`dossier_built` in collector_status; at most an hour, a few minutes
after new final outcomes, at once after a link rebuild).

Group key (`gk`): the `link_groups.group_id` of the account, else the account itself. Counting rules (all stated on the page):
  loss        the negative part of the outside buyers' net result at T+60 min (as `links.RANKING_MINUTE`; the same number for live and history)
  left        NEAR sent from the launch account in the first 1 h / 24 h after the launch to wallets that are NOT linked accounts, NOT a
              launchpad / token contract and NOT a listed DEX / wrap / relayer (a purchase or a swap stays in the market); a listed bridge or
              Intents account (EXIT_ACCOUNTS) counts as leaving. The share on the page is capped per launch at the insiders' result:
              sum(min(left, insiders' result)) / sum(insiders' result) over the launches with a positive insiders' result. Measured only
              where the account's outgoing transfers are loaded and the window is not cut off.
Nothing here changes the score, the signals or the rug label, and no account is treated as a service by its name.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import time
from collections import defaultdict
from typing import Any, Callable, Iterable

from . import links as K
from .rules.config import load_config
from .store import Store
from .store.db import utcnow_iso

DOSSIER_VERSION = "0.1"
NS = 10**9
H = 3600 * NS
NEAR = 10**24
TOP = 15                                   # counterparties kept per group and direction
REFRESH_EVERY_S = 3600.0
REFRESH_AFTER_FINALS_S = 300.0
# Listed bridge / Intents accounts: money sent there leaves the chain's own world. Every OTHER listed service (DEX, wrap, launchpad, relayer)
# is not "leaving" — a swap or a purchase on a launchpad stays in the market — but it is shown with its class.
EXIT_ACCOUNTS = frozenset({"intents.near", "omft.near", "omni.bridge.near"})
LEFT_KINDS = ("exit", "participant", "cluster_source", "unknown")
KIND_LABEL = {"linked": "linked account", "participant": "another participant", "cluster_source": "funding source of a cluster",
              "service": "listed service account", "exit": "bridge / Intents (listed)", "platform": "launchpad / token contract", "unknown": "unknown wallet"}
WINDOWS = {"all": None, "30d": 30, "7d": 7}


def _has(con: Any, table: str) -> bool:
    return K._has(con, table)


def _near(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return int(v) / NEAR
    except (TypeError, ValueError):
        return None


# ============================================================================================ build

def compute(store: Store, cfg: dict[str, Any]) -> dict[str, Any]:
    """The rows of the three tables. Reads only."""
    con = store.con
    min_y = K.yocto(cfg["links"]["min_near"])
    part, _times = K.participants(con)
    gk_of: dict[str, str] = {}
    gsize: dict[str, int] = {}
    if _has(con, "link_members"):
        gk_of = dict(con.execute("SELECT account, group_id FROM link_members"))
        gsize = {g: s for g, s in con.execute("SELECT group_id, size FROM link_groups")}
    members_of: dict[str, set[str]] = defaultdict(set)
    for a in part:
        members_of[gk_of.get(a, a)].add(a)
    cluster_sources = {r[0] for r in con.execute("SELECT source FROM link_clusters")} if _has(con, "link_clusters") else set()
    contracts: set[str] = set(K.PLATFORM_ACCOUNTS)
    for token, pool in con.execute("SELECT token, pool FROM launches"):
        contracts.add(token)
        if pool:
            contracts.add(pool)

    def kind_of(peer: str, gk: str) -> str:
        if peer in EXIT_ACCOUNTS:
            return "exit"
        if peer in contracts:
            return "platform"
        if K.service(peer):
            return "service"
        if peer in members_of.get(gk, ()):
            return "linked"
        if peer in part:
            return "participant"
        if peer in cluster_sources:
            return "cluster_source"
        return "unknown"

    # ---- the launches
    has_o, has_c, has_s = _has(con, "launch_outcomes"), _has(con, "cards"), _has(con, "launch_snapshots")
    sql = ("SELECT l.token, l.launchpad, l.creator, l.owner, l.pool, l.ts_ns, l.coverage, "
           + ("c.score, c.band, " if has_c else "NULL, NULL, ")
           + ("o.rug, o.final, o.severity_near, o.insiders_pnl, " if has_o else "NULL, NULL, NULL, NULL, ")
           + ("s.buyers_pnl " if has_s else "NULL ")
           + "FROM launches l "
           + ("LEFT JOIN cards c ON c.token = l.token " if has_c else "")
           + ("LEFT JOIN launch_outcomes o ON o.token = l.token " if has_o else "")
           + ("LEFT JOIN launch_snapshots s ON s.token = l.token AND s.minute = ? AND s.error IS NULL " if has_s else ""))
    rows: dict[str, dict[str, Any]] = {}
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in con.execute(sql, (K.RANKING_MINUTE,) if has_s else ()):
        token, pad, creator, owner, _pool, ts, cov, score, band, rug, final, sev, ins, pnl = r
        account = owner if owner and not K.service(owner) else creator if creator and not K.service(creator) else None
        if account is None:
            continue
        gk = gk_of.get(account) or gk_of.get(creator or "") or gk_of.get(owner or "") or account
        pnl_near = _near(pnl)
        row = {"token": token, "gk": gk, "account": account, "launchpad": pad, "ts_ns": ts, "live": int(cov is None),
               "score": score, "band": band, "rug": rug, "final": int(bool(final)), "severity_near": sev,
               "has_snapshot": int(pnl is not None), "loss_near": (-pnl_near if pnl_near is not None and pnl_near < 0 else 0.0),
               "buyers_pnl_near": pnl_near, "insiders_pnl_near": _near(ins), "funded_before_s": None, "funder": None,
               "out_1h_near": None, "out_24h_near": None}
        rows[token] = row
        by_account[account].append(row)
    for lst in by_account.values():
        lst.sort(key=lambda x: x["ts_ns"])

    # ---- the transfers (one pass over each direction, account by account)
    loaded = {d: {} for d in ("in", "out")}
    if _has(con, "backfill_queue"):
        for kind, d in (("transfers_in", "in"), ("transfers_out", "out")):
            for key, st, tr in con.execute("SELECT key, status, truncated FROM backfill_queue WHERE kind = ? AND status IN ('done', 'partial')", (kind,)):
                loaded[d][key] = bool(tr) or st == "partial"          # value: the load of this account is incomplete (cut off)
    flows: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    first_fund: dict[str, tuple[int, str, str | None]] = {}

    def add_flow(gk: str, d: str, peer: str, kind: str, near: float, ts: int, tx: str | None) -> None:
        f = flows[(gk, d)].setdefault(peer, {"kind": kind, "near": 0.0, "n": 0, "first_ns": ts, "last_ns": ts, "tx": tx, "big": 0.0})
        f["near"] += near
        f["n"] += 1
        f["first_ns"], f["last_ns"] = min(f["first_ns"], ts), max(f["last_ns"], ts)
        if near > f["big"]:
            f["big"], f["tx"] = near, tx

    if _has(con, "transfers_api"):
        v13 = store.user_version() >= 13
        cols = "account, ts_ns, other, amount, tx" + (", signer, pred" if v13 else "")
        ge = K._ge_sql("amount", min_y)

        def process_in(acc: str, buf: list[tuple]) -> None:
            gk = gk_of.get(acc, acc)
            last_l = part[acc]["last_ns"]
            # a DIRECT transfer only: a payout or a refund of a contract is no funding (RGY-021, F-019)
            direct = [b for b in buf if (not v13) or b[4] is None or (b[4] == b[1] and b[5] == b[1])]
            d_ts = [b[0] for b in direct]
            for lr in by_account.get(acc, ()):
                i = bisect.bisect_left(d_ts, lr["ts_ns"]) - 1                 # the latest direct funding before the launch
                if i >= 0:
                    lr["funded_before_s"] = int((lr["ts_ns"] - direct[i][0]) / NS)
                    lr["funder"] = direct[i][1]
            for ts, other, amount, tx, *_ in direct:
                if other == acc or ts > last_l + 24 * H:
                    continue                                                  # money in = what arrived up to the last launch (+ a day)
                near = int(amount) / NEAR
                k = kind_of(other, gk)
                add_flow(gk, "in", other, k, near, ts, tx)
                if k != "platform":
                    totals[gk]["in"] += near
                    if gk not in first_fund or ts < first_fund[gk][0]:
                        first_fund[gk] = (ts, other, tx)

        def process_out(acc: str, buf: list[tuple]) -> None:
            gk = gk_of.get(acc, acc)
            first_l = part[acc]["first_ns"]
            kinds = [kind_of(b[1], gk) for b in buf]
            ts_list = [b[0] for b in buf]
            ok_from = buf[0][0] if loaded["out"].get(acc) else None            # an incomplete load drops the OLDEST rows
            for lr in by_account.get(acc, ()):
                if acc not in loaded["out"] or (ok_from is not None and lr["ts_ns"] < ok_from):
                    continue                                                  # not loaded / cut off: unknown, not zero
                o1 = o24 = 0.0
                for j in range(bisect.bisect_right(ts_list, lr["ts_ns"]), len(buf)):
                    after = buf[j][0] - lr["ts_ns"]
                    if after > 24 * H:
                        break
                    if kinds[j] in LEFT_KINDS:
                        near = int(buf[j][2]) / NEAR
                        o24 += near
                        if after <= H:
                            o1 += near
                lr["out_1h_near"], lr["out_24h_near"] = o1, o24
            for (ts, other, amount, tx, *_), k in zip(buf, kinds):
                if ts < first_l or other == acc:
                    continue                                                  # money out = what left after the first launch
                near = int(amount) / NEAR
                add_flow(gk, "out", other, k, near, ts, tx)
                if k in LEFT_KINDS:
                    totals[gk]["out"] += near

        for d, process in (("in", process_in), ("out", process_out)):
            cur_acc, buf = None, []
            for row in con.execute(f"SELECT {cols} FROM transfers_api WHERE dir = ? AND other IS NOT NULL AND {ge} ORDER BY account, ts_ns", (d,)):
                if row[0] != cur_acc:
                    if cur_acc in part and buf:
                        process(cur_acc, buf)
                    cur_acc, buf = row[0], []
                buf.append(row[1:])
            if cur_acc in part and buf:
                process(cur_acc, buf)
        for acc, incomplete in loaded["out"].items():                          # loaded and nothing sent in the window = zero, not unknown
            if not incomplete:
                for lr in by_account.get(acc, ()):
                    if lr["out_1h_near"] is None:
                        lr["out_1h_near"] = lr["out_24h_near"] = 0.0

    flow_rows = []
    for (gk, d), peers in flows.items():
        top = sorted(peers.items(), key=lambda kv: (-kv[1]["near"], kv[0]))[:TOP]
        for rank, (peer, f) in enumerate(top, 1):
            flow_rows.append((gk, d, peer, f["kind"], f["near"], f["n"], f["first_ns"], f["last_ns"], f["tx"], rank))
    groups = []
    for gk, mem in members_of.items():
        launches = sum(part[a]["launches"] for a in mem)
        groups.append((gk, gsize.get(gk, len(mem)), launches, sum(1 for a in mem if a in loaded["in"]), sum(1 for a in mem if a in loaded["out"]),
                       len(mem), first_fund.get(gk, (None, None, None))[0], first_fund.get(gk, (None, None, None))[1],
                       first_fund.get(gk, (None, None, None))[2], totals[gk]["in"], totals[gk]["out"]))
    return {"launches": list(rows.values()), "flows": flow_rows, "groups": groups}


LAUNCH_COLS = ("token", "gk", "account", "launchpad", "ts_ns", "live", "score", "band", "rug", "final", "severity_near", "has_snapshot",
               "loss_near", "buyers_pnl_near", "insiders_pnl_near", "funded_before_s", "funder", "out_1h_near", "out_24h_near")


def write(store: Store, built: dict[str, Any], meta: dict[str, Any]) -> None:
    con = store.con
    store.begin()
    try:
        for tab in ("dossier_launch", "dossier_flow", "dossier_group"):
            con.execute(f"DELETE FROM {tab}")
        con.executemany(f"INSERT INTO dossier_launch({','.join(LAUNCH_COLS)}) VALUES({','.join('?' * len(LAUNCH_COLS))})",
                        [tuple(r[c] for c in LAUNCH_COLS) for r in built["launches"]])
        con.executemany("INSERT INTO dossier_flow(gk, dir, peer, kind, near, n, first_ns, last_ns, tx, rank) VALUES(?,?,?,?,?,?,?,?,?,?)", built["flows"])
        con.executemany("INSERT INTO dossier_group(gk, size, launches, in_loaded, out_loaded, members, first_funded_ns, first_funder, "
                        "first_funded_tx, in_total_near, out_total_near) VALUES(?,?,?,?,?,?,?,?,?,?,?)", built["groups"])
        store.set_status("dossier_built", meta)
        store.commit()
    except BaseException:
        store.rollback()
        raise


def _signature(store: Store) -> dict[str, Any]:
    q = store.con.execute
    return {"finals": q("SELECT COUNT(*) FROM launch_outcomes WHERE final = 1").fetchone()[0] if _has(store.con, "launch_outcomes") else 0,
            "launches": q("SELECT COUNT(*) FROM launches").fetchone()[0],
            "loaded": q("SELECT COUNT(*) FROM backfill_queue WHERE kind IN ('transfers_in', 'transfers_out') AND status IN ('done', 'partial')").fetchone()[0]
            if _has(store.con, "backfill_queue") else 0}


def rebuild(store: Store, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    if store.user_version() < 14:
        raise RuntimeError("schema < 14: the collector has not migrated to v14 yet")
    cfg = cfg or load_config()
    t0 = time.perf_counter()
    built = compute(store, cfg)
    t1 = time.perf_counter()
    st = store.get_status()
    meta = {"at": utcnow_iso(), "version": DOSSIER_VERSION, "launches": len(built["launches"]), "flows": len(built["flows"]),
            "groups": len(built["groups"]), "links_at": (st.get("links_built") or {}).get("at"), "signature": _signature(store),
            "compute_s": round(t1 - t0, 2)}
    write(store, built, meta)
    meta["write_s"] = round(time.perf_counter() - t1, 2)
    return meta


def refresh(store: Store, log: Callable[[str], None], force: bool = False, now: float | None = None) -> dict[str, Any] | None:
    """Collector hook (between blocks, outside the block transaction): rebuild when never built, right after a link rebuild, a few minutes
    after new final outcomes / loaded transfers, and at least hourly. A failure is logged and remembered (`dossier_last_error`)."""
    if store.user_version() < 14 or store.user_version() < 10:
        return None
    try:
        st = store.get_status()
        last = st.get("dossier_built")
        age = None
        if last and last.get("at"):
            t = dt.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() if now is None else now - t.timestamp()
        links_at = (st.get("links_built") or {}).get("at")
        due = (force or not last or age is None or (links_at and last.get("links_at") != links_at)
               or age > REFRESH_EVERY_S or (age > REFRESH_AFTER_FINALS_S and last.get("signature") != _signature(store)))
        if not due:
            return None
        meta = rebuild(store)
        store.set_status("dossier_last_error", None)
        log(f"dossier: {meta['launches']} launches, {meta['flows']} flows, {meta['groups']} groups ({meta['compute_s']} s + {meta['write_s']} s)")
        return meta
    except Exception as e:  # noqa: BLE001 — never stops the collector
        log(f"dossier: {type(e).__name__}: {e}")
        try:
            store.set_status("dossier_last_error", {"error": f"{type(e).__name__}: {e}"[:300], "at": utcnow_iso()})
        except Exception:  # noqa: BLE001
            pass
        return None


# ============================================================================================ readers (site)

def built(con: Any) -> dict[str, Any] | None:
    con = getattr(con, "con", con)
    if not _has(con, "dossier_launch") or not _has(con, "collector_status"):
        return None
    r = con.execute("SELECT value FROM collector_status WHERE key = 'dossier_built'").fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else None
    except ValueError:
        return None


def _headline(con: Any, where: str, args: tuple) -> dict[str, Any]:
    r = con.execute(
        "SELECT COUNT(*), COALESCE(SUM(live), 0), COALESCE(SUM(final), 0), "
        "COALESCE(SUM(final = 1 AND rug IN (0, 1)), 0), COALESCE(SUM(final = 1 AND rug = 1), 0), "
        "COALESCE(SUM(loss_near), 0), COALESCE(SUM(CASE WHEN rug = 1 THEN severity_near ELSE 0 END), 0), "
        "COALESCE(SUM(insiders_pnl_near), 0), COALESCE(SUM(insiders_pnl_near IS NOT NULL), 0), MIN(ts_ns), MAX(ts_ns), "
        "GROUP_CONCAT(DISTINCT launchpad), COALESCE(SUM(has_snapshot), 0), "
        "COALESCE(SUM(CASE WHEN out_1h_near IS NOT NULL AND insiders_pnl_near > 0 THEN MIN(out_1h_near, insiders_pnl_near) END), 0), "
        "COALESCE(SUM(CASE WHEN out_1h_near IS NOT NULL AND insiders_pnl_near > 0 THEN MIN(out_24h_near, insiders_pnl_near) END), 0), "
        "COALESCE(SUM(CASE WHEN out_1h_near IS NOT NULL AND insiders_pnl_near > 0 THEN insiders_pnl_near END), 0), "
        "COALESCE(SUM(out_1h_near IS NOT NULL AND insiders_pnl_near > 0), 0) "
        f"FROM dossier_launch WHERE {where}", args).fetchone()
    vals = list(r)
    d = {"launches": vals[0], "live": vals[1], "final": vals[2], "decided": vals[3], "rugs": vals[4], "loss_near": vals[5],
         "severity_near": vals[6], "insiders_pnl_near": vals[7], "insiders_n": vals[8], "first_ns": vals[9], "last_ns": vals[10],
         "launchpads": sorted((vals[11] or "").split(",")) if vals[11] else [], "with_snapshot": vals[12],
         "out_1h": vals[13], "out_24h": vals[14], "insiders_pos": vals[15], "out_n": vals[16]}
    d["rug_share"] = d["rugs"] / d["decided"] if d["decided"] else None
    d["share_1h"] = d["out_1h"] / d["insiders_pos"] if d["insiders_pos"] else None
    d["share_24h"] = d["out_24h"] / d["insiders_pos"] if d["insiders_pos"] else None
    return d


def group_key(con: Any, account: str) -> str:
    con = getattr(con, "con", con)
    if _has(con, "link_members"):
        r = con.execute("SELECT group_id FROM link_members WHERE account = ?", (account,)).fetchone()
        if r:
            return r[0]
    return account


def dossier(con: Any, account: str, limit: int = 200) -> dict[str, Any]:
    """The dossier sections of `account`'s group; {"built": False} until the first rebuild."""
    con = getattr(con, "con", con)
    b = built(con)
    if b is None:
        return {"built": False}
    gk = group_key(con, account)
    g = con.execute("SELECT size, launches, in_loaded, out_loaded, members, first_funded_ns, first_funder, first_funded_tx, in_total_near, "
                    "out_total_near FROM dossier_group WHERE gk = ?", (gk,)).fetchone()
    grp = dict(zip(("size", "launches", "in_loaded", "out_loaded", "members", "first_funded_ns", "first_funder", "first_funded_tx",
                    "in_total_near", "out_total_near"), g)) if g else None
    tl_cols = ("token", "account", "launchpad", "ts_ns", "live", "score", "band", "rug", "final", "severity_near", "has_snapshot",
               "loss_near", "buyers_pnl_near", "insiders_pnl_near", "funded_before_s", "funder", "out_1h_near", "out_24h_near")
    timeline = [dict(zip(tl_cols, r)) for r in con.execute(
        f"SELECT {','.join(tl_cols)} FROM dossier_launch WHERE gk = ? ORDER BY ts_ns DESC, token LIMIT ?", (gk, limit))]
    flows = {"in": [], "out": []}
    for d, peer, kind, near, n, first, last, tx in con.execute(
            "SELECT dir, peer, kind, near, n, first_ns, last_ns, tx FROM dossier_flow WHERE gk = ? ORDER BY dir, rank", (gk,)):
        flows[d].append({"peer": peer, "kind": kind, "kind_label": KIND_LABEL[kind], "near": near, "n": n, "first_ns": first, "last_ns": last, "tx": tx})
    return {"built": True, "built_at": b.get("at"), "gk": gk, "group": grp, "grouped": grp is not None and (grp["size"] or 1) > 1,
            "headline_group": _headline(con, "gk = ?", (gk,)), "headline_own": _headline(con, "account = ?", (account,)),
            "timeline": timeline, "timeline_total": con.execute("SELECT COUNT(*) FROM dossier_launch WHERE gk = ?", (gk,)).fetchone()[0],
            "flows_in": flows["in"], "flows_out": flows["out"], "kind_label": KIND_LABEL}


def rankings(con: Any, window: str = "all", launchpad: str | None = None, limit: int = 50, clean_min: int = 3,
             now_ns: int | None = None) -> dict[str, Any]:
    """Groups (and accounts outside any group) and funding clusters over the whole history: by the outside buyers' loss and by the number
    of rugs; the clean ones (>= `clean_min` launches with a final outcome, none a rug). `window`: all | 30d | 7d (by launch time)."""
    con = getattr(con, "con", con)
    out: dict[str, Any] = {"built": built(con) is not None, "window": window, "launchpad": launchpad, "minute": K.RANKING_MINUTE,
                           "by_loss": [], "by_rugs": [], "clusters": [], "clean": [], "launchpads": []}
    if not out["built"]:
        return out
    days = WINDOWS.get(window)
    lo = (now_ns if now_ns is not None else time.time_ns()) - days * 86400 * NS if days else 0
    out["launchpads"] = [r[0] for r in con.execute("SELECT DISTINCT launchpad FROM dossier_launch ORDER BY 1")]
    where, args = ["ts_ns >= ?"], [lo]
    if launchpad:
        where.append("launchpad = ?")
        args.append(launchpad)
    rows = con.execute("SELECT gk, account, loss_near, rug, final, severity_near, has_snapshot, ts_ns, live FROM dossier_launch WHERE "
                       + " AND ".join(where), args).fetchall()
    sizes = {g: s for g, s in con.execute("SELECT gk, size FROM dossier_group")}

    def agg(items: Iterable[tuple[str, tuple]]) -> dict[str, dict[str, Any]]:
        acc: dict[str, dict[str, Any]] = {}
        for key, (_gk, _a, loss, rug, final, sev, has_s, ts, live) in items:
            x = acc.setdefault(key, {"key": key, "launches": 0, "final": 0, "decided": 0, "rugs": 0, "loss": 0.0, "severity": 0.0,
                                     "clean_ok": True, "last_ns": 0, "live": 0})
            x["launches"] += 1
            x["live"] += live
            x["last_ns"] = max(x["last_ns"], ts)
            if has_s:
                x["loss"] += loss or 0.0
            if final:
                x["final"] += 1
            if final and rug in (0, 1):
                x["decided"] += 1
                if rug == 1:
                    x["rugs"] += 1
                    x["severity"] += sev or 0.0
                    x["clean_ok"] = False
        return acc

    def finish(x: dict[str, Any]) -> None:
        x["rug_share"] = x["rugs"] / x["decided"] if x["decided"] else None

    groups = agg((r[0], r) for r in rows)
    for x in groups.values():
        x["size"] = sizes.get(x["key"], 1)
        finish(x)
    vals = list(groups.values())
    out["by_loss"] = sorted((x for x in vals if x["loss"] > 0), key=lambda x: (-x["loss"], x["key"]))[:limit]
    out["by_rugs"] = sorted((x for x in vals if x["rugs"] > 0), key=lambda x: (-x["rugs"], -x["loss"], x["key"]))[:limit]
    out["clean"] = sorted(({"key": x["key"], "size": x["size"], "decided": x["decided"], "last_ns": x["last_ns"]}
                           for x in vals if x["decided"] >= clean_min and x["clean_ok"]),
                          key=lambda x: (-x["decided"], x["key"]))[:limit]
    if _has(con, "link_cluster_members"):
        by_acc: dict[str, list[tuple]] = defaultdict(list)
        for r in rows:
            by_acc[r[1]].append(r)
        csz = {s: (c, rc) for s, c, rc in con.execute("SELECT source, creators, recipients FROM link_clusters")}
        members: dict[str, set[str]] = defaultdict(set)
        for src, a in con.execute("SELECT source, account FROM link_cluster_members"):
            members[src].add(a)
        cl = agg((src, r) for src, accs in members.items() for a in accs for r in by_acc.get(a, ()))
        for x in cl.values():
            x["creators"], x["recipients"] = csz.get(x["key"], (None, None))
            finish(x)
        out["clusters"] = sorted((x for x in cl.values() if x["loss"] > 0), key=lambda x: (-x["loss"], x["key"]))[:limit]
    return out
