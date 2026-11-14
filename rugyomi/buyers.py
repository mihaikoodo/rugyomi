"""Who buys a launch (RGY-028): the first buyers of every launch and the links between them.

Everything here is DERIVED and rebuildable (schema v16); the site only reads it; it changes no card, no signal and no rug label.
Publicly we say only what a transaction shows: a wallet "received NEAR from the dev" (never "is the dev"); a bundle or a trader group is a set of
wallets with recorded facts in common, never "one person" (master §8).

Stage 1 - `launch_buyers`: per launch, the first `buyers.top_n` buyers by their first buy within `buyers.window_min` minutes (T+60), from the
  same trade attribution the snapshots use (`snapshots.attribute`; live launches from `facts`, history launches from `api_facts`): when, how
  much they spent, their first sell, their P&L at T+60. The dev's own accounts and the service accounts are not buyers; a wallet that
  received launch tokens from the dev has role 'insider' (it stays in the list, marked). Resumable per launch (`launch_buyers_done`).
Stage 2 - `buyer_links`, `buyer_groups`, `buyer_group_members`: three link types, each with its evidence (tx):
  dev_side_buyer  a buyer that received NEAR / wNEAR (a DIRECT transfer, F-019) in [first buy - `dev_side.window_days`, first buy] from the dev
                  (owner / creator of the launch), from a member of the dev's linked group, or from the dev's funding-cluster source. The evidence
                  is the closest such transfer. Our data on the buyer's funding is a lower bound (see `funding_covered`).
  bundle          >= `bundle.min_wallets` buyers whose first buy came within `bundle.seconds` of the launch and that were all funded by ONE common
                  source, a non-service wallet, within `bundle.funding_window` HOURS before the launch; the source must be narrow (it funded at
                  most `bundle.narrow_max` distinct accounts in that window, as far as our data shows). Cluster type "bundle", key = the source.
  co_buyers       two wallets whose first buys of the same launch came within `co.seconds` of each other in >= `co.min_launches` launches form a
                  PAIR (RGY-031: stored as a pair with its evidence - `buyer_pairs`, `buyer_pair_launches` - also for frequent wallets). A wallet that is
                  an early buyer in `co.frequent_share` of the launches of its active span (and in >= `co.frequent_min_launches`) is FREQUENT: it joins no
                  group and connects nobody. A trader group is a CLIQUE of >= 3 non-frequent wallets where every two are a stored pair; a wallet is in at
                  most one group (greedy: the largest clique first). No chaining through a third wallet, no connected components. No data cap: a group
                  has all its members; the page caps what it lists (`co.page_members`). Cluster type "trader group", key = the smallest member.
Service accounts (explicit list `links.SERVICE_ACCOUNTS` + the structural lockers) are never a source or a member; no account is included or excluded
by a name pattern. Defaults live in the config section `buyers` (outside the card hash; own hash `buyers_config_hash`).
"""
from __future__ import annotations

import datetime as dt
import json
import time
from collections import Counter, defaultdict
from typing import Any, Callable

from . import backfill as BF
from . import links as K
from . import quote_price as QP
from . import snapshots as S
from .parse import LAUNCHBOT
from .quote import not_near_quote, quote_token  # noqa: F401 - RGY-066 (re-exported)
from .progress import ticker
from .rules.config import buyers_config_hash, load_config
from .store import Store
from .store.db import utcnow_iso

VERSION = "0.1"
TYPES = ("dev_side_buyer", "bundle", "co_buyers")
NS = 10**9
NS_HOUR = 3600 * NS
NS_DAY = 24 * NS_HOUR
BATCH = 25                          # launches per commit of stage 1
MAX_PER_REFRESH = 100               # launches stage 1 computes per collector refresh
REFRESH_EVERY_S = 3600.0
REFRESH_MIN_GAP_S = 120.0
BUY_KIND = "buyers_in"


def _has(con: Any, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def stage1_hash(cfg: dict[str, Any], quote: str | None = None) -> str:
    """The parameters the rows were computed with. A launch whose pool is not paired with wNEAR (`quote`) carries a suffix: its rows hold NEAR converted at the swap-time
    price (RGY-073, `:q2` = the strict 15 min; RGY-075, `:q3` = the last known price up to 24 h old) - rows of an earlier suffix are recomputed once, the other launches are not
    touched. What is STORED may add `~<age s>` (`done_hash`): the oldest price used was older than the strict age, the amounts are approximate; `pending` compares without it."""
    b = cfg["buyers"]
    return f"{VERSION}:{b['top_n']}:{b['window_min']}" + (":q3" if quote else "")


def done_hash(cfg: dict[str, Any], quote: str | None, near_ok: bool | None, age_s: int | None) -> str:
    """The hash stored with a launch's buyers: `stage1_hash`, plus `~<age s>` when the rows hold NEAR amounts computed with a price older than the strict 15 min (approximate)."""
    h = stage1_hash(cfg, quote)
    return f"{h}~{int(age_s)}" if quote and near_ok and QP.is_approx(age_s) else h


# ============================================================================================ stage 1: the buyers of a launch

NO_NEAR = ""                         # RGY-073: spent / received / pnl of a launch whose pool is not paired with wNEAR and whose trades have no provable price: the NEAR amount is unknown


def compute_launch(launch: dict[str, Any], legs: list[dict], swaps: list[dict], cfg: dict[str, Any], near_known: bool = True) -> tuple[list[dict[str, Any]], int]:
    """([the first `top_n` buyers], the number of buyers) of a launch from its legs / swaps up to T+window. Pure.
    `swaps` are in wNEAR (a pool paired with wNEAR, or a quote pool converted by `snapshots.to_near`); `near_known` False = they are raw units of another token: the
    token amounts and times stay, `spent` / `received` / `pnl` are `NO_NEAR` (RGY-073)."""
    b = cfg["buyers"]
    token = launch["token"]
    cutoff = launch["ts_ns"] + int(b["window_min"]) * S.NS_MIN
    legs = [x for x in legs if x["ts"] <= cutoff]
    swaps = [x for x in swaps if x["ts"] <= cutoff]
    trades = S.attribute(token, legs, swaps)
    ids = {i for t in trades for i in t["legs"]}
    dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != LAUNCHBOT}
    ins = S._insiders(token, dev, legs, ids)
    priced = [t for t in trades if t["price"] is not None]
    price_last = priced[-1]["price"] if priced else None
    per: dict[str, dict[str, Any]] = {}
    for t in trades:
        w = t["who"]
        if t["side"] != "buy" or not w or w in dev or S.is_service(w, token):
            continue
        r = per.get(w)
        if r is None:
            r = per[w] = {"buyer": w, "first_buy_ns": t["ts"], "first_buy_tx": t["tx"], "buys": 0, "spent": 0, "bought": 0, "sold": 0,
                          "received": 0, "first_sell_ns": None, "first_sell_tx": None, "order": len(per)}
        r["buys"] += 1
        r["spent"] += t["near"]
        r["bought"] += t["tokens"]
    for t in trades:
        w = t["who"]
        if t["side"] == "sell" and w in per:
            r = per[w]
            r["sold"] += t["tokens"]
            r["received"] += t["near"]
            if r["first_sell_ns"] is None:
                r["first_sell_ns"], r["first_sell_tx"] = t["ts"], t["tx"]
    rows = sorted(per.values(), key=lambda r: (r["first_buy_ns"], r["order"]))
    out = []
    for i, r in enumerate(rows[:int(b["top_n"])], 1):
        unsold = max(0, r["bought"] - r["sold"])
        pnl = r["received"] + (int(unsold * price_last) if price_last is not None else 0) - r["spent"]
        out.append({"token": token, "buyer": r["buyer"], "rank": i, "first_buy_ns": r["first_buy_ns"], "first_buy_tx": r["first_buy_tx"],
                    "buys": r["buys"], "spent": str(r["spent"]) if near_known else NO_NEAR, "bought": str(r["bought"]), "sold": str(r["sold"]),
                    "received": str(r["received"]) if near_known else NO_NEAR, "first_sell_ns": r["first_sell_ns"], "first_sell_tx": r["first_sell_tx"],
                    "pnl": str(pnl) if near_known else NO_NEAR, "role": "insider" if r["buyer"] in ins["recipients"] else "buyer"})
    return out, len(rows)


def _history_done(store: Store) -> set[str]:
    return {r[0] for r in store.con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status IN ('done', 'partial')")}


def pending(store: Store, cfg: dict[str, Any], only: list[str] | None = None, force: bool = False) -> list[tuple[str, str | None, int]]:
    """(token, source 'live' | 'history' | None, launch ts) of the launches whose buyers are not computed (or not final, or computed with
    other parameters), oldest first. source None = no trades available (a history launch that is not backfilled)."""
    con = store.con
    cov = "l.coverage" if store.user_version() >= 6 else "NULL"
    hist = _history_done(store) if _has(con, "backfill_queue") else set()
    done = {t: (f, hh, src) for t, f, hh, src in con.execute("SELECT token, final, config_hash, source FROM launch_buyers_done")}
    want = set(only) if only is not None else None
    out = []
    for token, ts, c, pool in con.execute(f"SELECT l.token, l.ts_ns, {cov}, l.pool FROM launches l ORDER BY l.ts_ns, l.token").fetchall():
        if want is not None and token not in want:
            continue
        h = stage1_hash(cfg, not_near_quote(token, pool))
        src = "live" if c is None else "history" if token in hist else None
        d = done.get(token)
        if not force and d and d[0] == 1 and str(d[1]).split("~")[0] == h and d[2] == (src or "none"):
            continue
        out.append((token, src, ts))
    return out


def compute_one(store: Store, token: str, src: str, cfg: dict[str, Any]) -> tuple[dict[str, Any], int, list[dict[str, Any]], int, bool | None, int | None]:
    """(launch, cutoff_ns, rows, buyers_total, near_ok, price_age_s) of one launch, nothing written. near_ok: None = a pool paired with wNEAR, True = a quote pool converted to NEAR
    (every trade has a price at most 24 h old; the age is the oldest price used), False = a quote pool with an unpriced trade (the NEAR amounts stay unknown)."""
    launch = S._launch(store, token)
    cutoff = launch["ts_ns"] + int(cfg["buyers"]["window_min"]) * S.NS_MIN
    legs, swaps = S._rows(store, launch, cutoff) if src == "live" else BF.rows_from_api(store, launch, cutoff)
    swaps, near_ok, age = S.to_near_age(launch, swaps, S.quote_series(store, launch, cutoff))      # a quote pool's trades in NEAR at the swap-time price, all or nothing
    rows, total = compute_launch(launch, legs, swaps, cfg, near_ok is not False)
    return launch, cutoff, rows, total, near_ok, age


def build_launch_buyers(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None,
                        only: list[str] | None = None, limit: int | None = None, force: bool = False) -> dict[str, Any]:
    """Stage 1, resumable: the buyers of every launch that has none computed yet. Commits every BATCH launches."""
    t0 = time.time()
    cfg = cfg or load_config()
    if store.user_version() < 17:
        raise RuntimeError("buyers: the database is not migrated to schema v17 yet")
    con = store.con
    todo = pending(store, cfg, only, force)
    total_launches = con.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    pools = dict(con.execute("SELECT token, pool FROM launches"))
    out: dict[str, Any] = {"quote_converted": 0, "quote_approx": 0, "quote_unpriced": 0, "launches": total_launches, "to_compute": len(todo), "computable": sum(1 for _, s_, _ in todo if s_), "computed": 0, "no_trades": 0, "failed": 0, "failed_tokens": [],
                           "buyer_rows": 0}
    if limit is not None:
        todo = todo[:limit]
    last_fact = con.execute("SELECT MAX(ts_ns) FROM facts").fetchone()[0] or 0
    batch: list[tuple[str, str | None, list[dict[str, Any]], int, bool, str]] = []

    def flush() -> None:
        if not batch:
            return
        store.begin()
        try:
            for token, src, rows, total, final, h in batch:
                con.execute("DELETE FROM launch_buyers WHERE token = ?", (token,))
                con.executemany(
                    "INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, first_sell_ns, "
                    "first_sell_tx, pnl, role) VALUES(:token,:buyer,:rank,:first_buy_ns,:first_buy_tx,:buys,:spent,:bought,:sold,:received,"
                    ":first_sell_ns,:first_sell_tx,:pnl,:role)", rows)
                con.execute("INSERT OR REPLACE INTO launch_buyers_done(token, buyers_total, kept, source, final, config_hash, computed_at) "
                            "VALUES(?,?,?,?,?,?,?)", (token, total, len(rows), src or "none", int(final), h, utcnow_iso()))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        batch.clear()

    tick = ticker(log, "buyers", len(todo))
    for i, (token, src, ts) in enumerate(todo):
        if src is None:
            batch.append((token, None, [], 0, False, stage1_hash(cfg, not_near_quote(token, pools.get(token)))))                     # retried later: the history may be backfilled meanwhile
            out["no_trades"] += 1
        else:
            try:
                launch, cutoff, rows, total, near_ok, age = compute_one(store, token, src, cfg)
                if near_ok is not None:
                    out["quote_converted" if near_ok else "quote_unpriced"] += 1
                batch.append((token, src, rows, total, src == "history" or last_fact >= cutoff, done_hash(cfg, not_near_quote(token, launch.get("pool")), near_ok, age)))
                out["computed"] += 1
                out["buyer_rows"] += len(rows)
                out["quote_approx"] += bool(near_ok) and QP.is_approx(age)
            except Exception as e:  # noqa: BLE001 - one launch must not stop the rebuild
                out["failed"] += 1
                out["failed_tokens"].append(f"{token}: {type(e).__name__}: {str(e)[:120]}")
        if len(batch) >= BATCH:
            flush()
            tick(i + 1)
    flush()
    out["seconds"] = round(time.time() - t0, 1)
    return out


# ============================================================================================ stage 2: the links

def load_rows(store: Store) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """({token: launch}, {token: [early-buyer rows by rank]}) of the launches with early buyers computed - all `detect_co` needs. Read only."""
    con = store.con
    launches: dict[str, dict[str, Any]] = {}
    cov = "l.coverage" if store.user_version() >= 6 else "NULL"
    for token, pad, creator, owner, ts, c in con.execute(
            f"SELECT l.token, l.launchpad, l.creator, l.owner, l.ts_ns, {cov} FROM launches l JOIN launch_buyers_done d ON d.token = l.token WHERE d.kept > 0"):
        launches[token] = {"token": token, "launchpad": pad, "creator": creator, "owner": owner, "ts_ns": ts, "live": c is None}
    rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in con.execute("SELECT token, buyer, rank, first_buy_ns, first_buy_tx, spent, role FROM launch_buyers ORDER BY token, rank"):
        if r[0] in launches:
            rows[r[0]].append(dict(zip(("token", "buyer", "rank", "first_buy_ns", "first_buy_tx", "spent", "role"), r)))
    return launches, dict(rows)


def load_inputs(store: Store, cfg: dict[str, Any]) -> dict[str, Any]:
    """Everything `detect` needs, read from the database (no write)."""
    con = store.con
    b = cfg["buyers"]
    min_y = K.yocto(b["min_funding_near"])
    launches, rows = load_rows(store)
    group_of: dict[str, str] = {}
    members_of: dict[str, list[str]] = defaultdict(list)
    sources_of: dict[str, list[str]] = defaultdict(list)
    if _has(con, "link_members"):
        for account, gid in con.execute("SELECT account, group_id FROM link_members"):
            group_of[account] = gid
            members_of[gid].append(account)
        for account, source in con.execute("SELECT account, source FROM link_cluster_members"):
            sources_of[account].append(source)
    buyers = sorted({r["buyer"] for rs in rows.values() for r in rs})
    fundings: dict[str, list[dict[str, Any]]] = {}
    for a in buyers:
        seen, got = set(), []
        for e in K._incoming(store, a, min_y):
            k = (e["src"], e["tx"] or e["ts_ns"])
            if k not in seen and e["ts_ns"] is not None:
                seen.add(k)
                got.append(e)
        got.sort(key=lambda e: e["ts_ns"])
        fundings[a] = got
    cover: dict[str, int | None] = {}                       # account -> the end of its loaded funding window (None = whole history)
    if _has(con, "backfill_queue"):
        for key, kind, ts in con.execute("SELECT key, kind, ts_ns FROM backfill_queue WHERE kind IN ('transfers_in', ?) AND status IN ('done', 'partial')", (BUY_KIND,)):
            if kind == "transfers_in":
                cover[key] = None
            elif key not in cover:
                cover[key] = ts
    first_fact = con.execute("SELECT MIN(ts_ns) FROM facts").fetchone()[0]
    return {"launches": launches, "rows": rows, "group_of": group_of, "members_of": dict(members_of), "sources_of": dict(sources_of),
            "fundings": fundings, "cover": cover, "first_fact_ns": first_fact,
            "recipients": lambda src, lo, hi, limit: K._outgoing_in(store, src, min_y, limit, lo, hi)}


def dev_set(l: dict[str, Any], inputs: dict[str, Any]) -> dict[str, str]:
    """{account: how it is tied to the dev}: the dev itself, the members of its linked group, the source of its funding cluster."""
    dev = [x for x in (l.get("owner"), l.get("creator")) if x and x != LAUNCHBOT and not K.service(x)]
    out: dict[str, str] = {}
    for d in dev:
        out[d] = "the dev"
    for d in dev:
        gid = inputs["group_of"].get(d)
        for m in inputs["members_of"].get(gid, ()) if gid else ():
            if not K.service(m):
                out.setdefault(m, "a member of the dev's linked group")
    for d in dev:
        for src in inputs["sources_of"].get(d, ()):
            if not K.service(src):
                out.setdefault(src, "the funding source of the dev's cluster")
    return out


def funding_covered(inputs: dict[str, Any], account: str, first_buy_ns: int, window_ns: int) -> bool:
    """Does our transfer data reach the whole window [first buy - window, first buy] of this wallet?"""
    cov = inputs["cover"]
    if account in cov and (cov[account] is None or cov[account] >= first_buy_ns):
        return True
    ff = inputs.get("first_fact_ns")
    return bool(ff is not None and first_buy_ns - window_ns >= ff)


def detect(inputs: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """The links, groups and members for the settings of `cfg["buyers"]`. Pure with respect to the database."""
    b = cfg["buyers"]
    links: list[dict[str, Any]] = []
    stats: dict[str, Any] = {}

    # ---- dev_side_buyer
    window = int(float(b["dev_side"]["window_days"]) * NS_DAY)
    dev_launches, dev_wallets, covered_launches = set(), set(), set()
    for token, l in inputs["launches"].items():
        ds = dev_set(l, inputs)
        for r in inputs["rows"].get(token, ()):
            if K.service(r["buyer"]):
                continue
            if funding_covered(inputs, r["buyer"], r["first_buy_ns"], window):
                covered_launches.add(token)
            hits = [f for f in inputs["fundings"].get(r["buyer"], ()) if r["first_buy_ns"] - window <= f["ts_ns"] <= r["first_buy_ns"] and f["src"] in ds]
            if not hits:
                continue
            best = max(hits, key=lambda f: f["ts_ns"])
            links.append({"token": token, "buyer": r["buyer"], "type": "dev_side_buyer", "group_key": "", "ref": best["src"], "tx": best["tx"],
                          "ts_ns": best["ts_ns"], "amount": best["amount"], "relation": ds[best["src"]],
                          "detail": {"seconds_before_buy": max(0, (r["first_buy_ns"] - best["ts_ns"]) // NS), "transfers": len(hits),
                                     "asset": best.get("asset")}})
            dev_launches.add(token)
            dev_wallets.add(r["buyer"])
    stats["dev_side_buyer"] = {"launches": len(dev_launches), "wallets": len(dev_wallets), "groups": 0, "launches_with_covered_funding": len(covered_launches)}

    # ---- bundle
    fw = int(float(b["bundle"]["funding_window"]) * NS_HOUR)
    sec = int(float(b["bundle"]["seconds"]) * NS)
    min_w, narrow = int(b["bundle"]["min_wallets"]), int(b["bundle"]["narrow_max"])
    by_src: dict[str, dict[str, Any]] = {}
    cache: dict[tuple, int] = {}
    bundle_launches, bundle_wallets = set(), set()
    for token, l in inputs["launches"].items():
        ts = l["ts_ns"]
        cand: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for r in inputs["rows"].get(token, ()):
            if r["first_buy_ns"] - ts > sec or K.service(r["buyer"]):
                continue
            for f in inputs["fundings"].get(r["buyer"], ()):
                if ts - fw <= f["ts_ns"] <= ts and not K.service(f["src"]) and f["src"] != r["buyer"]:
                    cand[f["src"]][r["buyer"]] = {"f": f, "row": r}          # the latest funding before the launch wins (sorted by time)
        for src, m in cand.items():
            if len(m) < min_w:
                continue
            if narrow:
                key = (src, ts - fw, ts)
                if key not in cache:
                    cache[key] = len(inputs["recipients"](src, ts - fw, ts, narrow))
                if cache[key] > narrow:
                    continue
            g = by_src.setdefault(src, {"members": defaultdict(int), "launches": set(), "first": ts, "last": ts})
            g["launches"].add(token)
            g["first"], g["last"] = min(g["first"], ts), max(g["last"], ts)
            for buyer, x in m.items():
                g["members"][buyer] += 1
                links.append({"token": token, "buyer": buyer, "type": "bundle", "group_key": src, "ref": src, "tx": x["f"]["tx"],
                              "ts_ns": x["f"]["ts_ns"], "amount": x["f"]["amount"], "relation": "funded by the common source",
                              "detail": {"seconds_after_launch": max(0, (x["row"]["first_buy_ns"] - ts) // NS), "wallets": len(m),
                                         "seconds_funded_before_launch": max(0, (ts - x["f"]["ts_ns"]) // NS), "buy_tx": x["row"]["first_buy_tx"]}})
                bundle_wallets.add(buyer)
            bundle_launches.add(token)
    groups: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    for src, g in sorted(by_src.items()):
        groups.append({"type": "bundle", "group_key": src, "size": len(g["members"]), "launches": len(g["launches"]), "first_ns": g["first"],
                       "last_ns": g["last"], "truncated": 0})
        members += [{"type": "bundle", "group_key": src, "account": a, "launches": n} for a, n in sorted(g["members"].items())]
    stats["bundle"] = {"launches": len(bundle_launches), "wallets": len(bundle_wallets), "groups": len(by_src)}

    # ---- co_buyers (RGY-031): wallet frequency, pairs with evidence, groups = cliques of non-frequent wallets
    co = detect_co(inputs, cfg)
    links += co["links"]
    groups += co["groups"]
    members += co["members"]
    stats["co_buyers"] = co["stats"]
    return {"links": links, "groups": groups, "members": members, "stats": stats, "pairs": co["pairs"],
            "pair_launches": co["pair_launches"], "wallets": co["wallets"]}


def wallet_frequency(inputs: dict[str, Any], cfg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{wallet: {launches, span_launches, first_ns, last_ns, share_bps, frequent}} for every early buyer (service accounts left out). `launches` = the
    launches (with early buyers computed) where it is an early buyer; the span runs from the first to the last of those launches and
    `span_launches` counts every launch of the data inside it. FREQUENT = in >= `co.frequent_min_launches` launches AND in >= `co.frequent_share`
    of the launches of its span. Pure."""
    import bisect
    co = cfg["buyers"]["co"]
    share_min, n_min = float(co["frequent_share"]), int(co["frequent_min_launches"])
    ts_all = sorted(l["ts_ns"] for l in inputs["launches"].values())
    seen: dict[str, set[str]] = defaultdict(set)
    for token, rs in inputs["rows"].items():
        if token in inputs["launches"]:
            for r in rs:
                if not K.service(r["buyer"]):
                    seen[r["buyer"]].add(token)
    out: dict[str, dict[str, Any]] = {}
    for w, toks in seen.items():
        ts = [inputs["launches"][t]["ts_ns"] for t in toks]
        lo, hi = min(ts), max(ts)
        span = bisect.bisect_right(ts_all, hi) - bisect.bisect_left(ts_all, lo)
        n = len(toks)
        share = n / span if span else 0.0
        out[w] = {"launches": n, "span_launches": span, "first_ns": lo, "last_ns": hi, "share_bps": int(round(share * 10000)),
                  "frequent": int(n >= n_min and share >= share_min)}
    return out


CLIQUE_MAX_STEPS = 400_000          # a safety valve for the clique search; hitting it is reported (stats.clique_search_truncated), never silent


def _maximal_cliques(adj: dict[str, set[str]], min_size: int, max_steps: int | None = None) -> tuple[list[tuple[str, ...]], bool]:
    """Maximal cliques of >= `min_size` nodes (Bron-Kerbosch with a pivot) and whether the step limit cut the search. Deterministic."""
    cliques: list[tuple[str, ...]] = []
    steps = 0
    cut = False
    limit = CLIQUE_MAX_STEPS if max_steps is None else max_steps

    def bk(r: set[str], p: set[str], x: set[str]) -> None:
        nonlocal steps, cut
        steps += 1
        if steps > limit:
            cut = True
            return
        if not p and not x:
            if len(r) >= min_size:
                cliques.append(tuple(sorted(r)))
            return
        if not p:
            return
        u = max(sorted(p | x), key=lambda v: len(p & adj[v]))
        for v in sorted(p - adj[u]):
            bk(r | {v}, p & adj[v], x & adj[v])
            if cut:
                return
            p = p - {v}
            x = x | {v}

    nodes = {n for n, nb in adj.items() if len(nb) >= min_size - 1}
    while True:                                           # a node with fewer than min_size - 1 neighbours cannot be in a clique: prune (a search aid only)
        small = {n for n in nodes if len(adj[n] & nodes) < min_size - 1}
        if not small:
            break
        nodes -= small
    sub = {n: adj[n] & nodes for n in nodes}
    bk(set(), set(sub), set())
    return cliques, cut


def pick_groups(adj: dict[str, set[str]], weight: dict[tuple[str, str], int], min_size: int = 3) -> tuple[list[list[str]], bool]:
    """Disjoint groups: the largest clique first (ties: more shared launches, then the names), then the same on what is left. A clique that lost
    members to an earlier group stays a group while >= `min_size` of its members are left (a subset of a clique is a clique). -> (groups, cut)"""
    cliques, cut = _maximal_cliques(adj, min_size)
    used: set[str] = set()
    out: list[list[str]] = []
    pool = [set(c) for c in cliques]
    while True:
        best = None
        for c in pool:
            e = sorted(c - used)
            if len(e) < min_size:
                continue
            w = sum(weight[(x, y)] for i, x in enumerate(e) for y in e[i + 1:])
            k = (-len(e), -w, tuple(e))
            if best is None or k < best[0]:
                best = (k, e)
        if best is None:
            return out, cut
        out.append(best[1])
        used.update(best[1])


def detect_co(inputs: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """The co-buyer part of the links: wallet frequency, pairs with evidence, groups (cliques) and the group links. Needs only `inputs["launches"]`
    and `inputs["rows"]`. Pure with respect to the database."""
    b = cfg["buyers"]
    csec = int(float(b["co"]["seconds"]) * NS)
    min_l = int(b["co"]["min_launches"])
    wallets = wallet_frequency(inputs, cfg)
    pair_hits: dict[tuple[str, str], list[tuple[str, dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for token, rs in inputs["rows"].items():
        if token not in inputs["launches"]:
            continue
        rs = [r for r in rs if not K.service(r["buyer"])]
        rs.sort(key=lambda r: (r["first_buy_ns"], r["rank"]))
        for i, a in enumerate(rs):
            for c in rs[i + 1:]:
                if c["first_buy_ns"] - a["first_buy_ns"] > csec:
                    break
                if a["buyer"] != c["buyer"]:
                    x, y = (a, c) if a["buyer"] < c["buyer"] else (c, a)
                    pair_hits[(x["buyer"], y["buyer"])].append((token, x, y))
    edges = {p: h for p, h in pair_hits.items() if len({t for t, _, _ in h}) >= min_l}
    adj: dict[str, set[str]] = defaultdict(set)
    weight: dict[tuple[str, str], int] = {}
    for (x, y), h in edges.items():
        weight[(x, y)] = len({t for t, _, _ in h})
        if not wallets[x]["frequent"] and not wallets[y]["frequent"]:
            adj[x].add(y)
            adj[y].add(x)
    chosen, cut = pick_groups(adj, weight)
    group_of = {a: min(g) for g in chosen for a in g}
    groups: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    co_launches, co_wallets = set(), set()
    for g in sorted(chosen, key=min):
        key = min(g)
        gs = set(g)
        toks: dict[str, int] = {}
        mem_launches: dict[str, set[str]] = defaultdict(set)
        partners: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for i, x in enumerate(g):
            for y in g[i + 1:]:
                for token, rx, ry in edges[(x, y)]:
                    toks[token] = inputs["launches"][token]["ts_ns"]
                    mem_launches[x].add(token)
                    mem_launches[y].add(token)
                    gap = abs(ry["first_buy_ns"] - rx["first_buy_ns"]) // NS
                    partners[(token, x)].append({"account": y, "seconds_apart": gap, "tx": ry["first_buy_tx"]})
                    partners[(token, y)].append({"account": x, "seconds_apart": gap, "tx": rx["first_buy_tx"]})
        groups.append({"type": "trader_group", "group_key": key, "size": len(g), "launches": len(toks), "first_ns": min(toks.values()),
                       "last_ns": max(toks.values()), "truncated": 0})
        members += [{"type": "trader_group", "group_key": key, "account": a, "launches": len(mem_launches[a])} for a in sorted(gs)]
        for (token, who), ps in sorted(partners.items()):
            rr = next(r for r in inputs["rows"][token] if r["buyer"] == who)
            seen, uniq = set(), []
            for p in sorted(ps, key=lambda p: (p["seconds_apart"], p["account"])):
                if p["account"] not in seen:
                    seen.add(p["account"])
                    uniq.append(p)
            links.append({"token": token, "buyer": who, "type": "co_buyers", "group_key": key, "ref": uniq[0]["account"], "tx": rr["first_buy_tx"],
                          "ts_ns": rr["first_buy_ns"], "amount": rr["spent"] or None, "relation": "bought together with the group",
                          "detail": {"partners": uniq[:8], "shared_launches": len(mem_launches[who]), "group_size": len(g)}})
            co_launches.add(token)
            co_wallets.add(who)
    pairs: list[dict[str, Any]] = []
    pair_launches: list[dict[str, Any]] = []
    for (x, y), h in sorted(edges.items()):
        gaps = [abs(ry["first_buy_ns"] - rx["first_buy_ns"]) // 10**6 for _, rx, ry in h]
        times = [inputs["launches"][t]["ts_ns"] for t, _, _ in h]
        pairs.append({"a": x, "b": y, "launches": len({t for t, _, _ in h}), "first_ns": min(times), "last_ns": max(times), "min_gap_ms": min(gaps),
                      "group_key": group_of[x] if group_of.get(x) is not None and group_of.get(x) == group_of.get(y) else ""})
        for token, rx, ry in h:
            pair_launches.append({"a": x, "b": y, "token": token, "gap_ms": abs(ry["first_buy_ns"] - rx["first_buy_ns"]) // 10**6,
                                  "a_ns": rx["first_buy_ns"], "a_tx": rx["first_buy_tx"], "b_ns": ry["first_buy_ns"], "b_tx": ry["first_buy_tx"]})
    sizes = Counter(len(g) for g in chosen)
    stats = {"launches": len(co_launches), "wallets": len(co_wallets), "groups": len(chosen), "pairs": len(edges),
             "pairs_with_a_frequent_wallet": sum(1 for (x, y) in edges if wallets[x]["frequent"] or wallets[y]["frequent"]),
             "wallets_early_buyers": len(wallets), "frequent_wallets": sum(w["frequent"] for w in wallets.values()),
             "wallets_in_groups_total": len(group_of), "largest_group": max((len(g) for g in chosen), default=0),
             "group_sizes": {str(k): v for k, v in sorted(sizes.items())}, "clique_search_truncated": bool(cut)}
    return {"links": links, "groups": groups, "members": members, "stats": stats, "pairs": pairs, "pair_launches": pair_launches,
            "wallets": [{"account": a, **w} for a, w in sorted(wallets.items())]}


def write_links(store: Store, built: dict[str, Any], meta: dict[str, Any]) -> None:
    """Replace the six derived link tables and the status key in one short transaction."""
    con = store.con
    store.begin()
    try:
        for tbl in ("buyer_links", "buyer_groups", "buyer_group_members", "buyer_wallets", "buyer_pairs", "buyer_pair_launches"):
            con.execute(f"DELETE FROM {tbl}")
        con.executemany(
            "INSERT OR REPLACE INTO buyer_links(token, buyer, type, group_key, ref, tx, ts_ns, amount, relation, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(x["token"], x["buyer"], x["type"], x["group_key"], x["ref"], x["tx"], x["ts_ns"], x["amount"], x["relation"],
              json.dumps(x["detail"], separators=(",", ":"))) for x in built["links"]])
        con.executemany("INSERT INTO buyer_groups(type, group_key, size, launches, first_ns, last_ns, truncated) VALUES(:type,:group_key,:size,:launches,:first_ns,:last_ns,:truncated)",
                        built["groups"])
        con.executemany("INSERT INTO buyer_group_members(type, group_key, account, launches) VALUES(:type,:group_key,:account,:launches)", built["members"])
        con.executemany("INSERT INTO buyer_wallets(account, launches, span_launches, first_ns, last_ns, share_bps, frequent) "
                        "VALUES(:account,:launches,:span_launches,:first_ns,:last_ns,:share_bps,:frequent)", built["wallets"])
        con.executemany("INSERT INTO buyer_pairs(a, b, launches, first_ns, last_ns, min_gap_ms, group_key) VALUES(:a,:b,:launches,:first_ns,:last_ns,:min_gap_ms,:group_key)",
                        built["pairs"])
        con.executemany("INSERT INTO buyer_pair_launches(a, b, token, gap_ms, a_ns, a_tx, b_ns, b_tx) VALUES(:a,:b,:token,:gap_ms,:a_ns,:a_tx,:b_ns,:b_tx)",
                        built["pair_launches"])
        store.set_status("buyer_links_built", meta)
        store.commit()
    except BaseException:
        store.rollback()
        raise


def build_links(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Stage 2: load, detect, write. Returns the counts per type."""
    cfg = cfg or load_config()
    if store.user_version() < 17:
        raise RuntimeError("buyers: the database is not migrated to schema v17 yet")
    t0 = time.perf_counter()
    inputs = load_inputs(store, cfg)
    t1 = time.perf_counter()
    built = detect(inputs, cfg)
    t2 = time.perf_counter()
    meta = {"at": utcnow_iso(), "version": VERSION, "config_version": cfg["version"], "config_hash": buyers_config_hash(cfg),
            "launches_with_buyers": len(inputs["launches"]), "buyer_wallets": len(inputs["fundings"]), "links": len(built["links"]),
            "stats": built["stats"], "load_s": round(t1 - t0, 2), "detect_s": round(t2 - t1, 2)}
    write_links(store, built, meta)
    meta["write_s"] = round(time.perf_counter() - t2, 2)
    return meta


def rebuild(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None, only: list[str] | None = None,
            limit: int | None = None, force: bool = False, links: bool = True) -> dict[str, Any]:
    """Both stages (the CLI). Stage 1 is resumable (`limit` / run again); stage 2 is a whole rebuild and runs when stage 1 has nothing left."""
    cfg = cfg or load_config()
    out = {"stage1": build_launch_buyers(store, cfg, log, only, limit, force)}
    left = len(pending(store, cfg))
    out["stage1"]["left"] = left
    if links and (limit is None or out["stage1"]["computable"] <= limit):
        out["stage2"] = build_links(store, cfg, log)
    return out


def refresh(store: Store, log: Callable[[str], None], force: bool = False, now: float | None = None) -> dict[str, Any] | None:
    """Collector hook, between blocks, after `links.refresh`: compute the buyers of new launches (at most MAX_PER_REFRESH) and rebuild the links when
    due. It starts only after the first manual build (`rebuild-buyer-links`), so the initial backlog never runs inside the collector. A failure is
    logged and remembered (`buyers_last_error`); it never stops the collector."""
    if store.user_version() < 17:
        return None
    try:
        st = store.get_status()
        last = st.get("buyer_links_built")
        if not last:
            return None
        cfg = load_config()
        todo = pending(store, cfg)
        age = None
        if last.get("at"):
            t = dt.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() if now is None else now - t.timestamp()
        due = (force or last.get("config_hash") != buyers_config_hash(cfg)
               or (age is not None and (age > REFRESH_EVERY_S or (any(s for _, s, _ in todo) and age > REFRESH_MIN_GAP_S))))
        if not due:
            return None
        s1 = build_launch_buyers(store, cfg, log, limit=MAX_PER_REFRESH)
        meta = build_links(store, cfg, log)
        store.set_status("buyers_last_error", None)
        log(f"buyers: {s1['computed']} launches, links {meta['links']} ({meta['load_s']} s + {meta['detect_s']} s + {meta['write_s']} s)")
        return meta
    except Exception as e:  # noqa: BLE001 - never stops the collector
        log(f"buyers: {type(e).__name__}: {e}")
        try:
            store.set_status("buyers_last_error", {"error": f"{type(e).__name__}: {e}"[:300], "at": utcnow_iso()})
        except Exception:  # noqa: BLE001
            pass
        return None


# ============================================================================================ readers (site, diag)

LINK_LABEL = {"dev_side_buyer": "dev side", "bundle": "bundle", "co_buyers": "group"}
PAIRS_ON_LAUNCH_PAGE = 3            # pairs listed per early buyer on a launch page ("and N more")
EVIDENCE_PER_PAIR = 25              # launches listed per pair on a wallet page
PAIRS_ON_WALLET_PAGE = 60
GROUP_LAUNCHES_ON_PAGE = 50


def _dec(row: tuple, cols: tuple) -> dict[str, Any]:
    d = dict(zip(cols, row))
    if "detail" in d:
        d["detail"] = json.loads(d["detail"]) if d["detail"] else {}
    return d


def page_settings() -> dict[str, int]:
    """What the pages show: how many early buyers, the "sold within" line and how many group members are listed (a display cap only - the data has
    no cap), from the config section `buyers` (defaults if it is broken)."""
    try:
        b = load_config()["buyers"]
        return {"page_n": int(b["page_n"]), "sold_within_min": int(b["sold_within_min"]), "page_members": int(b["co"]["page_members"])}
    except Exception:  # noqa: BLE001 - a broken override must not break the page
        return {"page_n": 20, "sold_within_min": 15, "page_members": 50}


def _converted_hash(h: str | None) -> bool:
    """RGY-073 / 075: rows of a launch not paired with wNEAR hold NEAR only when they were computed by the conversion (hash suffix `:q2` = the strict 15 min, `:q3` = the last known
    price up to 24 h, then maybe `~<age s>`); earlier rows hold raw units of the quote token."""
    h = str(h or "").split("~")[0]
    return h.endswith(":q2") or h.endswith(":q3")


def _hash_age(h: str | None) -> int | None:
    """The oldest price age (s) of rows stored with an approximate conversion (`~<age s>` after the hash), else None (exact: every price is within the strict 15 min)."""
    _, _, a = str(h or "").partition("~")
    return int(a) if a.isdigit() else None


def _amount_view(row: dict[str, Any], quote: str | None, done_hash: str | None, keys: tuple[str, ...]) -> None:
    """The NEAR amounts of one stored row as the pages show them: unchanged for a pool paired with wNEAR; for a quote pool the converted amounts when the row was computed by the
    conversion, else None («—») with `n/a (quote: TOKEN)` - a price that cannot be proven is never guessed. RGY-075: `approx` True + `price_age_s` + `price_age_text` when the amounts
    rest on a price older than the strict 15 min (the page shows «≈» and the age); `approx` False = exact. A pool paired with wNEAR has none of these keys."""
    row["quote_token"] = quote
    if not quote:
        return
    if _converted_hash(done_hash) and all(row.get(k) not in (None, NO_NEAR) for k in keys if k in row):
        age = _hash_age(done_hash)
        row["approx"] = age is not None
        row["price_age_s"] = age
        row["price_age_text"] = QP.age_text(age)
        row["amount_note"] = f"≈ at the last known price of {quote}, {QP.age_text(age)} old" if age is not None else f"converted from {quote} at the swap-time price"
        return
    for k in keys:
        if k in row:
            row[k] = None
    row["approx"], row["price_age_s"], row["price_age_text"] = False, None, None
    row["amount_note"] = f"n/a (quote: {quote})"


def early_buyers(con: Any, token: str, limit: int = 20) -> list[dict[str, Any]]:
    """The first buyers of a launch with their links (evidence) and manual labels. [] when none are stored."""
    con = getattr(con, "con", con)
    if not _has(con, "launch_buyers"):
        return []
    cols = ("buyer", "rank", "first_buy_ns", "first_buy_tx", "buys", "spent", "bought", "sold", "received", "first_sell_ns", "first_sell_tx", "pnl", "role")
    ts = con.execute("SELECT ts_ns, pool FROM launches WHERE token = ?", (token,)).fetchone()
    other_quote = not_near_quote(token, ts[1]) if ts else None
    dh = con.execute("SELECT config_hash FROM launch_buyers_done WHERE token = ?", (token,)).fetchone() if _has(con, "launch_buyers_done") else None
    rows = [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {','.join(cols)} FROM launch_buyers WHERE token = ? ORDER BY rank LIMIT ?", (token, limit))]
    if not rows:
        return []
    links: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if _has(con, "buyer_links"):
        for r in con.execute("SELECT buyer, type, group_key, ref, tx, ts_ns, amount, relation, detail FROM buyer_links WHERE token = ? ORDER BY type, group_key", (token,)):
            links[r[0]].append(_dec(r[1:], ("type", "group_key", "ref", "tx", "ts_ns", "amount", "relation", "detail")))
    labs: dict[str, list[dict[str, Any]]] = {}
    if _has(con, "manual_labels"):
        from . import labels as L
        for r in rows:
            labs[r["buyer"]] = L.active_labels(con, r["buyer"])
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if _has(con, "buyer_pair_launches"):               # RGY-031: a pair is shown as a pair, with the evidence of this launch
        for a, b, gap, a_tx, b_tx, n in con.execute(
                "SELECT l.a, l.b, l.gap_ms, l.a_tx, l.b_tx, p.launches FROM buyer_pair_launches l JOIN buyer_pairs p ON p.a = l.a AND p.b = l.b "
                "WHERE l.token = ?", (token,)):
            pairs[a].append({"partner": b, "gap_ms": gap, "launches": n, "partner_tx": b_tx})
            pairs[b].append({"partner": a, "gap_ms": gap, "launches": n, "partner_tx": a_tx})
    wallets: dict[str, dict[str, Any]] = {}
    if _has(con, "buyer_wallets"):
        for w in rows:
            x = con.execute("SELECT launches, span_launches, share_bps, frequent FROM buyer_wallets WHERE account = ?", (w["buyer"],)).fetchone()
            if x:
                wallets[w["buyer"]] = {"launches": x[0], "span_launches": x[1], "share_bps": x[2], "frequent": bool(x[3])}
    for r in rows:
        _amount_view(r, other_quote, dh[0] if dh else None, ("spent", "received", "pnl"))      # RGY-066 / RGY-073: a quote pool shows NEAR only when converted at the swap-time price
        r["after_s"] = max(0, (r["first_buy_ns"] - ts[0]) // NS) if ts else None
        r["links"] = links.get(r["buyer"], [])
        r["labels"] = labs.get(r["buyer"], [])
        r["sold_after_s"] = (r["first_sell_ns"] - r["first_buy_ns"]) // NS if r["first_sell_ns"] is not None else None
        ps = sorted(pairs.get(r["buyer"], []), key=lambda p: (-p["launches"], p["gap_ms"], p["partner"]))
        r["pairs"], r["pairs_more"] = ps[:PAIRS_ON_LAUNCH_PAGE], max(0, len(ps) - PAIRS_ON_LAUNCH_PAGE)
        r["wallet"] = wallets.get(r["buyer"])
    return rows


def launch_summary(con: Any, tokens: list[str]) -> dict[str, dict[str, int]]:
    """{token: {dev_side, bundle, co}} = wallets with a link of that type among the first buyers; tokens without a link are absent."""
    con = getattr(con, "con", con)
    out: dict[str, dict[str, int]] = {}
    if not tokens or not _has(con, "buyer_links"):
        return out
    key = {"dev_side_buyer": "dev_side", "bundle": "bundle", "co_buyers": "co"}
    for i in range(0, len(tokens), 400):
        part = tokens[i:i + 400]
        q = ",".join("?" * len(part))
        for token, typ, n in con.execute(f"SELECT token, type, COUNT(DISTINCT buyer) FROM buyer_links WHERE token IN ({q}) GROUP BY token, type", part):
            out.setdefault(token, {})[key[typ]] = n
    return out


def group_page(con: Any, typ: str, key: str) -> dict[str, Any] | None:
    """A bundle (key = the source) or a trader group (key = any member account; the group it belongs to) with its members and the launches
    where the links were recorded, each with the timing evidence."""
    con = getattr(con, "con", con)
    if typ not in ("bundle", "trader_group") or not _has(con, "buyer_groups"):
        return None
    if typ == "trader_group":
        r = con.execute("SELECT group_key FROM buyer_group_members WHERE type = 'trader_group' AND account = ? LIMIT 1", (key,)).fetchone()
        if r is None:
            r = con.execute("SELECT group_key FROM buyer_groups WHERE type = 'trader_group' AND group_key = ?", (key,)).fetchone()
        if r is None:
            return None
        key = r[0]
    g = con.execute("SELECT size, launches, first_ns, last_ns, truncated FROM buyer_groups WHERE type = ? AND group_key = ?", (typ, key)).fetchone()
    if g is None:
        return None
    ltype = "bundle" if typ == "bundle" else "co_buyers"
    cap = page_settings()["page_members"]
    mem = [{"account": a, "launches": n} for a, n in con.execute(
        "SELECT account, launches FROM buyer_group_members WHERE type = ? AND group_key = ? ORDER BY launches DESC, account LIMIT ?", (typ, key, cap))]
    labs: dict[str, list[dict[str, Any]]] = {}
    if _has(con, "manual_labels"):
        from . import labels as L
        for m in mem[:200]:
            labs[m["account"]] = L.active_labels(con, m["account"])
    for m in mem:
        m["labels"] = labs.get(m["account"], [])
    ev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for token, buyer, ref, tx, ts, amount, detail in con.execute(
            "SELECT token, buyer, ref, tx, ts_ns, amount, detail FROM buyer_links WHERE type = ? AND group_key = ? ORDER BY token, ts_ns", (ltype, key)):
        ev[token].append({"buyer": buyer, "ref": ref, "tx": tx, "ts_ns": ts, "amount": amount, "detail": json.loads(detail) if detail else {}})
    launches = []
    for token, es in ev.items():
        l = con.execute("SELECT launchpad, ts_ns, extra FROM launches WHERE token = ?", (token,)).fetchone()
        if l is None:
            continue
        first = {r[0]: (r[1], r[2]) for r in con.execute("SELECT buyer, first_buy_ns, first_buy_tx FROM launch_buyers WHERE token = ?", (token,))}
        for e in es:
            fb = first.get(e["buyer"])
            e["after_s"] = max(0, (fb[0] - l[1]) // NS) if fb else None
            e["buy_tx"] = fb[1] if fb else None
        launches.append({"token": token, "launchpad": l[0], "ts_ns": l[1], "symbol": (json.loads(l[2]) if l[2] else {}).get("symbol"), "members": es})
    launches.sort(key=lambda x: x["ts_ns"], reverse=True)
    more = max(0, len(launches) - GROUP_LAUNCHES_ON_PAGE)
    return {"type": typ, "key": key, "size": g[0], "launches_n": g[1], "first_ns": g[2], "last_ns": g[3], "truncated": bool(g[4]),
            "members": mem, "members_more": max(0, g[0] - len(mem)), "launches": launches[:GROUP_LAUNCHES_ON_PAGE], "launches_more": more}


def trader_page(con: Any, account: str) -> dict[str, Any] | None:
    """RGY-031: a wallet as an early buyer - in how many launches, its pairs with the evidence of each (launch, gap, both transactions) and its
    trader group, if it is in one. None when the account is no early buyer we know. Never raises for a wallet that is in no group."""
    con = getattr(con, "con", con)
    if not _has(con, "launch_buyers"):
        return None
    w = con.execute("SELECT launches, span_launches, first_ns, last_ns, share_bps, frequent FROM buyer_wallets WHERE account = ?", (account,)).fetchone() \
        if _has(con, "buyer_wallets") else None
    n_early = con.execute("SELECT COUNT(*) FROM launch_buyers WHERE buyer = ?", (account,)).fetchone()[0]
    group = group_page(con, "trader_group", account)
    prs = con.execute("SELECT a, b, launches, first_ns, last_ns, min_gap_ms, group_key FROM buyer_pairs WHERE a = ? OR b = ? ORDER BY launches DESC, a, b",
                      (account, account)).fetchall() if _has(con, "buyer_pairs") else []
    if w is None and not n_early and group is None and not prs:
        return None
    freq = {r[0] for r in con.execute(f"SELECT account FROM buyer_wallets WHERE frequent = 1 AND account IN ({','.join('?' * len(prs))})",
                                      [(x[1] if x[0] == account else x[0]) for x in prs])} if prs else set()
    pairs = []
    for a, b, n, first, last, gap, gkey in prs[:PAIRS_ON_WALLET_PAGE]:
        mine_is_a = a == account
        partner = b if mine_is_a else a
        ev_rows = con.execute("SELECT l.token, l.gap_ms, l.a_ns, l.a_tx, l.b_tx, k.launchpad, k.ts_ns, k.extra FROM buyer_pair_launches l "
                              "LEFT JOIN launches k ON k.token = l.token WHERE l.a = ? AND l.b = ? ORDER BY l.a_ns DESC LIMIT ?", (a, b, EVIDENCE_PER_PAIR)).fetchall()
        ev = [{"token": t, "gap_ms": g_, "ts_ns": a_ns, "tx_own": a_tx if mine_is_a else b_tx, "tx_partner": b_tx if mine_is_a else a_tx,
               "launchpad": pad, "symbol": (json.loads(extra) if extra else {}).get("symbol")} for t, g_, a_ns, a_tx, b_tx, pad, _ts, extra in ev_rows]
        pairs.append({"partner": partner, "launches": n, "first_ns": first, "last_ns": last, "min_gap_ms": gap,
                      "same_group": bool(gkey), "partner_frequent": partner in freq, "evidence": ev, "evidence_more": max(0, n - len(ev))})
    wallet = {"launches": w[0] if w else n_early, "span_launches": w[1] if w else None, "share_bps": w[4] if w else None, "frequent": bool(w[5]) if w else False}
    return {"account": account, "wallet": wallet, "group": group, "pairs": pairs, "pairs_more": max(0, len(prs) - len(pairs))}


TRADES_ON_ACCOUNT_PAGE = 100          # launches listed on the account page before "all" (the dossier does the same)
OUTCOME_TEXT = {"rug": "rug", "no rug": "no rug", "undefined": "undecided", "waiting": "undecided"}


def has_trades(con: Any, account: str) -> bool:
    """True when the account is one of the stored early buyers of some launch or a wallet of the early-buyer tables (RGY-065: its page is not a 404)."""
    con = getattr(con, "con", con)
    if _has(con, "launch_buyers") and con.execute("SELECT 1 FROM launch_buyers WHERE buyer = ? LIMIT 1", (account,)).fetchone():
        return True
    return _has(con, "buyer_wallets") and con.execute("SELECT 1 FROM buyer_wallets WHERE account = ? LIMIT 1", (account,)).fetchone() is not None


def account_trades(con: Any, account: str, limit: int = TRADES_ON_ACCOUNT_PAGE) -> dict[str, Any] | None:
    """RGY-065: "As a trader" on the account page - the launches where `account` is among the stored early buyers (`launch_buyers`: the first
    `buyers.top_n` buyers inside T+60 min of each launch), newest first, with what the stored rows say: first buy (time, transaction), spent, the share
    of the bought tokens it sold, its P&L at T+60 min (sold + unsold at the last price - spent), the launch outcome (the stored one: rug / no rug /
    undecided) and the role (buyer / insider = received launch tokens from the dev). No new metric, no rank, no score of the wallet; facts only.
    None when the account has no such row. The summary counts ALL rows, the list shows `limit` of them (`more` = the rest)."""
    con = getattr(con, "con", con)
    if not _has(con, "launch_buyers"):
        return None
    has_o = _has(con, "launch_outcomes")
    allrows = con.execute(
        "SELECT b.pnl, b.role, " + ("o.rug, o.final" if has_o else "NULL, NULL") + ", b.token, k.pool, d.config_hash FROM launch_buyers b LEFT JOIN launches k ON k.token = b.token "
        "LEFT JOIN launch_buyers_done d ON d.token = b.token "
        + ("LEFT JOIN launch_outcomes o ON o.token = b.token " if has_o else "") + "WHERE b.buyer = ?", (account,)).fetchall()
    if not allrows:
        return None
    from .rules.accuracy import outcome_state          # imported here: the rules package imports this module's neighbours
    pnl_total, rugs, noruns, undec, insiders, pnl_launches, other_rows, approx_rows, approx_age = 0, 0, 0, 0, 0, 0, 0, 0, 0
    for pnl, role, rug, final, tok, pool, dhash in allrows:
        q = not_near_quote(tok, pool)
        if q and not (_converted_hash(dhash) and pnl not in (None, NO_NEAR)):       # RGY-066 / RGY-073: no provable NEAR amount: out of the NEAR sum (counted apart)
            other_rows += 1
        else:
            pnl_total += int(pnl or 0)
            pnl_launches += 1
            age = _hash_age(dhash) if q else None
            if age is not None:                                                     # RGY-075: a sum with an approximate part is marked as approximate, never presented as exact
                approx_rows += 1
                approx_age = max(approx_age, age)
        st = outcome_state({"rug": rug, "final": final})
        rugs += st == "rug"
        noruns += st == "no rug"
        undec += st in ("undefined", "waiting")
        insiders += role == "insider"
    cols = ("token", "rank", "first_buy_ns", "first_buy_tx", "buys", "spent", "bought", "sold", "first_sell_ns", "first_sell_tx", "pnl", "role")
    rows = []
    for r in con.execute(
            "SELECT " + ",".join("b." + c for c in cols) + ", k.launchpad, k.ts_ns, k.extra, k.pool, d.config_hash, " + ("o.rug, o.final" if has_o else "NULL, NULL")
            + " FROM launch_buyers b LEFT JOIN launches k ON k.token = b.token LEFT JOIN launch_buyers_done d ON d.token = b.token " + ("LEFT JOIN launch_outcomes o ON o.token = b.token " if has_o else "")
            + "WHERE b.buyer = ? ORDER BY b.first_buy_ns DESC, b.token LIMIT ?", (account, limit)):
        d = dict(zip(cols, r[:len(cols)]))
        pad, lts, extra, pool, dhash, rug, final = r[len(cols):]
        bought, sold = int(d["bought"] or 0), int(d["sold"] or 0)
        state = outcome_state({"rug": rug, "final": final})
        _amount_view(d, not_near_quote(d["token"], pool), dhash, ("spent", "pnl"))            # RGY-066 / RGY-073
        d.setdefault("amount_note", None)
        d.setdefault("approx", False)
        d.setdefault("price_age_s", None)
        d.update({"launchpad": pad, "launch_ts_ns": lts, "symbol": (json.loads(extra) if extra else {}).get("symbol"),
                  "after_s": max(0, (d["first_buy_ns"] - lts) // NS) if lts else None,
                  "sold_bps": min(10000, sold * 10000 // bought) if bought > 0 else None,        # share of the bought tokens sold, capped at 100 %
                  "sold_over_bought": bought > 0 and sold > bought,
                  "outcome": OUTCOME_TEXT[state], "outcome_raw": state,
                  "role_text": "got tokens from the dev" if d["role"] == "insider" else None})
        rows.append(d)
    n_pairs = con.execute("SELECT COUNT(*) FROM buyer_pairs WHERE a = ? OR b = ?", (account, account)).fetchone()[0] if _has(con, "buyer_pairs") else 0
    grp = None
    if _has(con, "buyer_group_members") and _has(con, "buyer_groups"):
        g = con.execute("SELECT m.group_key, g.size FROM buyer_group_members m JOIN buyer_groups g ON g.type = m.type AND g.group_key = m.group_key "
                        "WHERE m.account = ? AND m.type = 'trader_group'", (account,)).fetchone()
        grp = {"key": g[0], "size": g[1]} if g else None
    w = con.execute("SELECT launches, span_launches, share_bps, frequent FROM buyer_wallets WHERE account = ?", (account,)).fetchone() \
        if _has(con, "buyer_wallets") else None
    return {"account": account, "trader_url": f"/traders/{account}",
            "summary": {"launches": len(allrows), "pnl_t60": str(pnl_total), "pnl_launches": pnl_launches, "other_quote_rows": other_rows,
                       "approx": approx_rows > 0, "approx_rows": approx_rows, "price_age_s": approx_age or None, "price_age_text": QP.age_text(approx_age or None), "rugs": rugs, "no_rugs": noruns, "undecided": undec, "insider_rows": insiders},
            "rows": rows, "more": max(0, len(allrows) - len(rows)), "limit": limit,
            "pairs": n_pairs, "group": grp,
            "wallet": {"launches": w[0], "span_launches": w[1], "share_bps": w[2], "frequent": bool(w[3])} if w else None,
            "top_n": load_config().get("buyers", {}).get("top_n"), "window_min": load_config().get("buyers", {}).get("window_min")}


def account_groups(con: Any, account: str) -> list[dict[str, Any]]:
    """The bundles and trader groups an account is a member of."""
    con = getattr(con, "con", con)
    if not _has(con, "buyer_group_members"):
        return []
    return [{"type": t, "key": k, "launches": n} for t, k, n in con.execute(
        "SELECT type, group_key, launches FROM buyer_group_members WHERE account = ? ORDER BY type, group_key", (account,))]


def status(con: Any) -> dict[str, Any] | None:
    con = getattr(con, "con", con)
    if not _has(con, "collector_status"):
        return None
    r = con.execute("SELECT value FROM collector_status WHERE key = 'buyer_links_built'").fetchone()
    return json.loads(r[0]) if r and r[0] else None
