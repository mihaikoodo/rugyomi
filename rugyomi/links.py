"""Who is behind a launch (RGY-019): links between launch participants, linked accounts, funding clusters, rankings.

Terms (master §8 — do not merge them):
  participant     an account that created or owns a launch (`launches` creator / real owner, `launch_index` creator,
                  `hist_launches` creator). A launch through the launch bot belongs to its real owner; the bot is a
                  service account, never a participant.
  link (edge)     a fact between two accounts with a transaction: `funded` (NEAR / wNEAR sent), `created` (the account
                  was created by the other), `launchbot_owner` (a recorded creator and the real owner of the same launch
                  differ), `token_transfer` (the dev sent launch tokens to the other). Each edge keeps its type, the
                  transaction, the time, the amount and where the fact came from (`facts` | `history`).
  linked accounts the connected component of participants joined by direct edges (no depth limit; a size cap marks a
                  group `truncated`). Hops through wallets that are not participants are NOT followed.
                  Linked accounts are NOT a cluster, and nothing here says "one person": we show the facts.
  cluster         ONE kind for now: a common narrow funding source — a wallet that is not a service account and not itself
                  a participant, that funded at least `clusters.min_creators` different participants within
                  `clusters.window_days` BEFORE each one's launch, and that funded at most `clusters.narrow_max` distinct
                  accounts within `clusters.narrow_window_days` of those fundings (an exchange or a payout wallet fans
                  out to far more). A funding is a DIRECT transfer (signer = predecessor = sender; RGY-021).
Service accounts (launchpads, the launch bot, DCL, wrap, registrars, relayers, bridges, exchanges) are excluded by the
EXPLICIT list `SERVICE_ACCOUNTS` (plus the structural `lock_N.nearlytrade.near` lockers of snapshots.is_service) — never by a
name pattern: flippers name wallets like services (master §8.8).

Storage: derived tables (schema v10), rebuilt as a whole from facts and history by `rebuild_links`: everything is computed
first (read only), then written in one short transaction. The collector rebuilds them between blocks (`refresh`) after a
new launch and at least every `REFRESH_EVERY_S`; the `rebuild-links` CLI does the same by hand. Readers (`account_links`,
`cluster_page`, `rankings`, `coverage`) never write.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import time
from collections import defaultdict
from decimal import Decimal
from typing import Any, Callable

from . import accounts as accounts_mod
from .parse import LAUNCHBOT
from .rules.config import ConfigError, links_config_hash, load_config
from .snapshots import SERVICE, is_service
from .store import Store
from .store.db import hash_str, utcnow_iso

LINKS_VERSION = "0.1"
FUNDING_KINDS = ("near_transfer", "wnear_transfer")
NS_DAY = 24 * 3600 * 10**9
REFRESH_EVERY_S = 3600.0                  # a rebuild at least this often even without a new launch
REFRESH_MIN_GAP_S = 120.0                 # ... and never more often than this (a rebuild takes seconds on a big database)
MAX_FUNDINGS_PER_ACCOUNT = 5000           # the newest incoming fundings read per participant
RANKING_MINUTE = 60                       # the snapshot whose outside-buyers' result the rankings sum (as `buyers_loss`)
# The explicit list. Launchpads, the bot, DCL, wrap, nearly's fee seller come from snapshots.SERVICE; the rest are the
# registrar, the Intents contract and its relayers / bridge. Exchange hot wallets are mostly implicit accounts and are NOT
# listed (we have no verified list yet — Q-008): the narrowness rule keeps a fanning-out wallet from becoming a cluster.
# RGY-021 step 0 (F-019 / Q-008): platform contracts and payout accounts that remain after the funded-edge rule "a direct
# transfer signed by the sender" (signer = predecessor = sender) — FULL account names only, never patterns (master §8.8).
# Filled (07.11) from `diag/platform_candidates.py` section 1 on the server data: contracts whose payouts always came from a
# call signed by somebody else (swap pools and routers, launchpad / market / bridge / lending contracts, refund payers). Only
# accounts that are plainly contracts; unclear ones (a Telegram-wallet family account, an account that also launches) stay out.
PLATFORM_ACCOUNTS: frozenset[str] = frozenset({
    "v2.ref-finance.near", "v1.jumbo_exchange.near", "aggregatedex.near", "dex.intear.near", "a.mitte-orderbook.near",
    "contract.main.burrow.near", "omni.bridge.near", "trade.unrlzd.near", "marketplace.paras.near", "nftmarket.cookon.near",
    "nearpadfamily.near", "meme-cooking.near", "nearbyonear.nearbyon.near", "nearrr-fun.near",
    "ucat.umbrafun.near", "umbra.umbrafun.near", "router2.latedata9580.near", "curve10.latedata9580.near",
    "here.poolv1.near", "meteor.poolv1.near", "npro.poolv1.near",
    "l000000.factory.shardsmarket.near", "l000002.factory.shardsmarket.near", "l000004.factory.shardsmarket.near",
    "l000005.factory.shardsmarket.near", "l000008.factory.shardsmarket.near", "l000009.factory.shardsmarket.near",
    "l000040.factory.shardsmarket.near", "l000052.factory.shardsmarket.near", "l000130.factory.shardsmarket.near",
    "l000143.factory.shardsmarket.near", "l000169.factory.shardsmarket.near",
})
SERVICE_ACCOUNTS = frozenset(SERVICE) | frozenset({
    "near", "intents.near", "omft.near", "meteor-relayer.near", "system", "nearlytrade.near", "launchbot.nearlytrade.near",
}) | PLATFORM_ACCOUNTS
EDGE_TYPES = ("funded", "created", "launchbot_owner", "token_transfer")


def service(account: str | None, extra: frozenset[str] = frozenset()) -> bool:
    return account is None or account in SERVICE_ACCOUNTS or account in extra or is_service(account, "")


def yocto(near: float | int | str) -> int:
    return int(Decimal(str(near)) * 10**24)


def _ge_sql(col: str, min_yocto: int) -> str:
    """`col >= min_yocto` for a decimal string of base units (the integer does not fit SQLite's INTEGER): compare the
    length first, then the digits."""
    t = str(max(min_yocto, 1))
    return f"(length({col}) > {len(t)} OR (length({col}) = {len(t)} AND {col} >= '{t}'))"


# ============================================================================================ reading the data
# INDEXED BY below: with bound parameters SQLite picks `facts_kind_height` (every fact of the kind) over the account's own
# index — milliseconds per query instead of microseconds (the same trap as in accounts.account_created).

def _has(con: Any, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def participants(con: Any) -> tuple[dict[str, dict[str, Any]], dict[str, list[tuple[int, str]]]]:
    """({account: {launches, first_ns, last_ns}}, {account: sorted [(launch ts_ns, token)]}) over every source."""
    by: dict[str, dict[str, int]] = defaultdict(dict)                 # account -> {token: ts}

    def add(account: str | None, token: str, ts: int | None) -> None:
        if not account or service(account) or ts is None:
            return
        old = by[account].get(token)
        by[account][token] = ts if old is None else min(old, ts)

    for token, creator, owner, ts in con.execute("SELECT token, creator, owner, ts_ns FROM launches"):
        add(owner, token, ts)
        add(creator, token, ts)
    if _has(con, "launch_index"):
        for token, creator, ts in con.execute("SELECT token, creator, create_ts_ns FROM launch_index"):
            add(creator, token, ts)
    if _has(con, "hist_launches"):
        for token, creator, ts in con.execute("SELECT token, creator, ts_ns FROM hist_launches"):
            add(creator, token, ts)
    info, times = {}, {}
    for a, toks in by.items():
        ts = sorted((t, k) for k, t in toks.items())
        info[a] = {"launches": len(ts), "first_ns": ts[0][0], "last_ns": ts[-1][0]}
        times[a] = ts
    return info, times


def _incoming(store: Store, account: str, min_y: int) -> list[dict[str, Any]]:
    """Incoming NEAR / wNEAR fundings of `account` >= min_y from facts and the imported history, as edge dicts."""
    out: list[dict[str, Any]] = []
    con = store.con
    b = store.lookup_account(account)
    kinds = {store.lookup_kind(n): n for n in FUNDING_KINDS if store.lookup_kind(n) is not None}
    if b is not None and kinds:
        for ts, sender, amount, tx, kid in con.execute(
                f"SELECT f.ts_ns, a.name, f.amount, f.tx, f.kind FROM facts f INDEXED BY facts_b_ts JOIN accounts a ON a.id = f.a "
                f"WHERE f.b = ? AND f.kind IN ({','.join('?' * len(kinds))}) AND {_ge_sql('f.amount', min_y)} "
                f"ORDER BY f.ts_ns DESC LIMIT ?", (b, *kinds, MAX_FUNDINGS_PER_ACCOUNT)):
            if sender != account and not service(sender):
                out.append({"type": "funded", "src": sender, "dst": account, "ts_ns": ts, "tx": hash_str(tx),
                            "amount": amount, "asset": "wNEAR" if kinds[kid] == "wnear_transfer" else "NEAR",
                            "token": None, "source": "facts"})
    if _has(con, "hist_fundings"):
        for ts, funder, token, amount, tx in con.execute(
                "SELECT ts_ns, funder, token, amount, tx_hash FROM hist_fundings WHERE account = ? "
                "ORDER BY ts_ns DESC LIMIT ?", (account, MAX_FUNDINGS_PER_ACCOUNT)):
            if not funder or funder == account or service(funder) or str(token or "near").lower() not in ("near", "wnear", "wrap.near"):
                continue
            try:
                if int(amount or 0) < min_y:
                    continue
            except ValueError:
                continue
            out.append({"type": "funded", "src": funder, "dst": account, "ts_ns": ts, "tx": tx, "amount": str(amount),
                        "asset": "wNEAR" if str(token or "").lower() in ("wnear", "wrap.near") else "NEAR", "token": None,
                        "source": "history"})
    if _has(con, "transfers_api"):
        for ts, other, asset, amount, tx in con.execute(
                f"SELECT ts_ns, other, asset, amount, tx FROM transfers_api WHERE account = ? AND dir = 'in' AND {_ge_sql('amount', min_y)} "
                f"AND {_direct_sql(store)} ORDER BY ts_ns DESC LIMIT ?", (account, MAX_FUNDINGS_PER_ACCOUNT)):
            if other and other != account and not service(other):
                out.append({"type": "funded", "src": other, "dst": account, "ts_ns": ts, "tx": tx, "amount": amount,
                            "asset": asset, "token": None, "source": "transfers_api"})
    return out


def _direct_sql(store: Store) -> str:
    """A `funded` link is a DIRECT transfer: signed by the sender, delivered by the sender (signer = predecessor = sender). A payout
    or a refund of a contract has the contract as predecessor and is no funding (RGY-021, F-019). Rows loaded before schema v13 have
    no signer: they stay (NULL) until the reload replaces them."""
    if store.user_version() < 13:
        return "1"
    return "(signer IS NULL OR (signer = other AND pred = other))"


def _merge_windows(stamps: list[int], half: int) -> list[tuple[int, int]]:
    """The union of [t - half, t + half] over `stamps`, as sorted disjoint intervals."""
    out: list[list[int]] = []
    for t in sorted(stamps):
        if out and t - half <= out[-1][1]:
            out[-1][1] = max(out[-1][1], t + half)
        else:
            out.append([t - half, t + half])
    return [(a, b) for a, b in out]


def _outgoing_recipients(store: Store, source: str, min_y: int, limit: int, lo: int | None = None, hi: int | None = None,
                         windows: list[tuple[int, int]] | None = None) -> set[str]:
    """Distinct accounts `source` funded (>= min_y) in facts + history + the transfers API; stops after `limit` + 1 (wide = not
    narrow). lo / hi: only transfers in that time range (ns); `windows`: only transfers inside one of these intervals (the narrowness
    windows of Q-009: around each funding of a creator, so a source that funded creators months apart is not judged on the quiet time
    between them)."""
    seen: set[str] = set()
    if windows is None:
        windows = [(-1 if lo is None else lo, 2**62 if hi is None else hi)]
    for w_lo, w_hi in windows:
        if len(seen) > limit:
            break
        seen |= _outgoing_in(store, source, min_y, limit, w_lo, w_hi)
    return seen


def _outgoing_in(store: Store, source: str, min_y: int, limit: int, t_lo: int, t_hi: int) -> set[str]:
    con = store.con
    seen: set[str] = set()
    a = store.lookup_account(source)
    kinds = [k for k in (store.lookup_kind(n) for n in FUNDING_KINDS) if k is not None]
    if a is not None and kinds:
        for (name,) in con.execute(
                f"SELECT DISTINCT b.name FROM facts f INDEXED BY facts_a_ts JOIN accounts b ON b.id = f.b WHERE f.a = ? "
                f"AND f.kind IN ({','.join('?' * len(kinds))}) AND {_ge_sql('f.amount', min_y)} AND f.ts_ns BETWEEN ? AND ? LIMIT ?",
                (a, *kinds, t_lo, t_hi, limit + 1)):
            seen.add(name)
    if len(seen) <= limit and _has(con, "hist_fundings"):
        for (name,) in con.execute("SELECT DISTINCT account FROM hist_fundings WHERE funder = ? AND ts_ns BETWEEN ? AND ? LIMIT ?",
                                   (source, t_lo, t_hi, limit + 1)):
            seen.add(name)
    if len(seen) <= limit and _has(con, "transfers_api"):
        # every wallet the API route knows `source` funded directly: the participants (full history) and the buyers (7 days before
        # their first buy). A lower bound of the truth, so it can only make a source look wider.
        for (name,) in con.execute(
                f"SELECT DISTINCT account FROM transfers_api WHERE other = ? AND dir = 'in' AND ts_ns BETWEEN ? AND ? "
                f"AND {_ge_sql('amount', min_y)} AND {_direct_sql(store)} LIMIT ?", (source, t_lo, t_hi, limit + 1)):
            seen.add(name)
    return seen


def _token_transfers(store: Store, part: dict[str, Any]) -> list[dict[str, Any]]:
    """The dev's transfers of the launch token to other participants (insider recipients), from facts and history."""
    con = store.con
    out: list[dict[str, Any]] = []
    kid = store.lookup_kind("ft_transfer")
    if kid is not None:
        for token, creator, owner, pool in con.execute("SELECT token, creator, owner, pool FROM launches"):
            devs = {x for x in (owner, creator) if x and x in part and not service(x)}   # the launch contract / bot can be the recorded creator
            tok_id = store.lookup_account(token)
            if tok_id is None:
                continue
            for dev in devs:
                a = store.lookup_account(dev)
                if a is None:
                    continue
                for ts, to, amount, tx in con.execute(
                        "SELECT f.ts_ns, b.name, f.amount, f.tx FROM facts f INDEXED BY facts_a_ts JOIN accounts b ON b.id = f.b "
                        "WHERE f.a = ? AND f.kind = ? AND f.token = ? ORDER BY f.id", (a, kid, tok_id)):
                    if to in part and to not in devs and to != pool and not service(to) and to != token:
                        out.append({"type": "token_transfer", "src": dev, "dst": to, "ts_ns": ts, "tx": hash_str(tx),
                                    "amount": amount, "asset": "token", "token": token, "source": "facts"})
    if _has(con, "hist_dev_moves"):
        for token, ts, dev, to, amount in con.execute("SELECT token, ts_ns, dev, to_account, amount FROM hist_dev_moves"):
            if dev and to and dev != to and to in part and not service(to) and dev in part:
                out.append({"type": "token_transfer", "src": dev, "dst": to, "ts_ns": ts, "tx": None, "amount": amount,
                            "asset": "token", "token": token, "source": "history"})
    return out


def _created_edges(con: Any, part: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for account in part:
        c = accounts_mod.account_created(con, account)
        if not c:
            continue
        payer = c.get("signer") or c.get("creator")
        if payer and payer != account and payer in part and not service(payer):
            out.append({"type": "created", "src": payer, "dst": account, "ts_ns": c["ts_ns"], "tx": c["tx"], "amount": None,
                        "asset": None, "token": None, "source": c["source"]})
    return out


def _launchbot_owner_edges(con: Any, part: dict[str, Any]) -> list[dict[str, Any]]:
    """The same launch recorded with a real creator by one source and a different real owner by another (the owner behind
    the launch bot is the launches.owner; a launch the bot made has no real creator elsewhere, so this is rare)."""
    out: list[dict[str, Any]] = []
    owners = {t: (o, None, ts) for t, o, ts in con.execute("SELECT token, owner, ts_ns FROM launches WHERE owner IS NOT NULL")}
    sources = []
    if _has(con, "launch_index"):
        sources.append(("history", "SELECT token, creator FROM launch_index"))
    if _has(con, "hist_launches"):
        sources.append(("history", "SELECT token, creator FROM hist_launches"))
    for src, sql in sources:
        for token, creator in con.execute(sql):
            o = owners.get(token)
            if o and creator and creator != o[0] and creator in part and o[0] in part and not service(creator) and not service(o[0]):
                out.append({"type": "launchbot_owner", "src": creator, "dst": o[0], "ts_ns": o[2], "tx": None,   # the evidence is a receipt id, not a transaction
                            "amount": None, "asset": None, "token": token, "source": src})
    return out


# ============================================================================================ building

class _UF:
    def __init__(self) -> None:
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def compute_links(store: Store, cfg: dict[str, Any]) -> dict[str, Any]:
    """Everything the tables hold, computed from facts + history. Reads only."""
    con = store.con
    min_y = yocto(cfg["links"]["min_near"])
    group_max = int(cfg["links"]["group_max"])
    part, times = participants(con)
    incoming: dict[str, list[dict[str, Any]]] = {a: _incoming(store, a, min_y) for a in part}

    edges: list[dict[str, Any]] = []
    for a, rows in incoming.items():
        edges += [e for e in rows if e["src"] in part]
    edges += _created_edges(con, part)
    edges += _launchbot_owner_edges(con, part)
    edges += _token_transfers(store, part)
    edges = [e for e in edges if e["src"] in part and e["dst"] in part]          # both ends are participants, always
    seen, uniq = set(), []
    for e in edges:
        k = (e["type"], e["src"], e["dst"], e["tx"] or e["ts_ns"], e["token"], e["amount"])
        if k not in seen and e["src"] != e["dst"]:
            seen.add(k)
            uniq.append(e)
    # a fact present in both sources (same transaction, same pair): keep the facts row
    best: dict[tuple, dict[str, Any]] = {}
    for e in uniq:
        k = (e["type"], e["src"], e["dst"], e["tx"]) if e["tx"] else None
        if k is None:
            best[("n", id(e))] = e
        elif k not in best or e["source"] == "facts":
            best[k] = e
    edges = sorted(best.values(), key=lambda e: (e["ts_ns"] or 0, e["type"], e["src"], e["dst"]))

    uf = _UF()
    for e in edges:
        uf.union(e["src"], e["dst"])
    comps: dict[str, list[str]] = defaultdict(list)
    for a in {x for e in edges for x in (e["src"], e["dst"])}:
        comps[uf.find(a)].append(a)
    groups, members = [], []
    for gid, accs in sorted(comps.items()):
        ordered = sorted(accs, key=lambda a: (part[a]["first_ns"], a))
        kept = ordered[:group_max]
        groups.append({"group_id": gid, "size": len(accs), "truncated": int(len(accs) > group_max),
                       "launches": sum(part[a]["launches"] for a in accs),
                       "first_launch_ns": min(part[a]["first_ns"] for a in accs),
                       "last_launch_ns": max(part[a]["last_ns"] for a in accs)})
        members += [{"account": a, "group_id": gid, "launches": part[a]["launches"], "first_launch_ns": part[a]["first_ns"]}
                    for a in kept]
    for e in edges:
        e["group_id"] = uf.find(e["src"])

    clusters = _clusters(store, cfg, part, times, incoming, min_y)
    return {"participants": len(part), "edges": edges, "groups": groups, "members": members, "clusters": clusters,
            "incoming_funded": sum(len(v) for v in incoming.values())}


def _clusters(store: Store, cfg: dict[str, Any], part: dict[str, Any], times: dict[str, list[tuple[int, str]]],
              incoming: dict[str, list[dict[str, Any]]], min_y: int) -> list[dict[str, Any]]:
    c = cfg["clusters"]
    window = int(float(c["window_days"]) * NS_DAY)
    by_src: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for account, rows in incoming.items():
        launch_ts = [t for t, _ in times[account]]
        for e in rows:
            if e["src"] in part or e["ts_ns"] is None:
                continue
            i = bisect.bisect_left(launch_ts, e["ts_ns"])           # the first launch at or after the funding
            if i >= len(launch_ts) or launch_ts[i] - e["ts_ns"] > window:
                continue
            by_src[e["src"]][account].append({**e, "launch_ns": launch_ts[i], "launch_token": times[account][i][1]})
    out = []
    for src in sorted(by_src):
        funded = by_src[src]
        if len(funded) < int(c["min_creators"]):
            continue
        nw = c.get("narrow_window_days")
        stamps = [e["ts_ns"] for es in funded.values() for e in es]
        wins = _merge_windows(stamps, int(float(nw) * NS_DAY)) if nw else None
        recipients = _outgoing_recipients(store, src, min_y, int(c["narrow_max"]), windows=wins)
        recipients |= set(funded)                                      # what we saw funded counts, whatever the index says
        if len(recipients) > int(c["narrow_max"]):
            continue
        mem = []
        for account, es in funded.items():
            first = max(es, key=lambda e: e["ts_ns"])                    # the funding closest before the launch
            mem.append({"account": account, "funded_ns": first["ts_ns"], "tx": first["tx"], "amount": first["amount"],
                        "amount_total": str(sum(int(e["amount"]) for e in es)), "fundings": len(es), "asset": first["asset"],
                        "edge_source": first["source"], "launch_token": first["launch_token"], "launch_ns": first["launch_ns"],
                        "seconds_before": max(0, (first["launch_ns"] - first["ts_ns"]) // 10**9)})
        mem.sort(key=lambda m: (m["funded_ns"], m["account"]))
        out.append({"source": src, "creators": len(mem), "recipients": len(recipients),
                    "first_funded_ns": mem[0]["funded_ns"], "last_funded_ns": mem[-1]["funded_ns"],
                    "total_near": str(sum(int(m["amount_total"]) for m in mem)), "members": mem})
    return out


def write_links(store: Store, built: dict[str, Any], meta: dict[str, Any]) -> None:
    """Replace the five tables and the status key in one transaction."""
    con = store.con
    store.begin()
    try:
        for t in ("link_edges", "link_groups", "link_members", "link_clusters", "link_cluster_members"):
            con.execute(f"DELETE FROM {t}")
        con.executemany("INSERT INTO link_edges(type, src, dst, ts_ns, tx, amount, asset, token, source, group_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        [(e["type"], e["src"], e["dst"], e["ts_ns"], e["tx"], e["amount"], e["asset"], e["token"], e["source"],
                          e["group_id"]) for e in built["edges"]])
        con.executemany("INSERT INTO link_groups(group_id, size, truncated, launches, first_launch_ns, last_launch_ns) "
                        "VALUES(:group_id,:size,:truncated,:launches,:first_launch_ns,:last_launch_ns)", built["groups"])
        con.executemany("INSERT INTO link_members(account, group_id, launches, first_launch_ns) "
                        "VALUES(:account,:group_id,:launches,:first_launch_ns)", built["members"])
        for c in built["clusters"]:
            con.execute("INSERT INTO link_clusters(source, creators, recipients, first_funded_ns, last_funded_ns, total_near) "
                        "VALUES(?,?,?,?,?,?)", (c["source"], c["creators"], c["recipients"], c["first_funded_ns"],
                                                c["last_funded_ns"], c["total_near"]))
            con.executemany(
                "INSERT INTO link_cluster_members(source, account, funded_ns, tx, amount, amount_total, fundings, asset, "
                "edge_source, launch_token, launch_ns, seconds_before) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [(c["source"], m["account"], m["funded_ns"], m["tx"], m["amount"], m["amount_total"], m["fundings"], m["asset"],
                  m["edge_source"], m["launch_token"], m["launch_ns"], m["seconds_before"]) for m in c["members"]])
        store.set_status("links_built", meta)
        store.commit()
    except BaseException:
        store.rollback()
        raise


def rebuild_links(store: Store, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recompute and replace the link tables (idempotent: the same data gives the same rows). Returns the counts."""
    if store.user_version() < 10:
        raise RuntimeError("schema < 10: the collector has not migrated to v10 yet")
    cfg = cfg or load_config()
    t0 = time.perf_counter()
    built = compute_links(store, cfg)
    t1 = time.perf_counter()
    by_type: dict[str, int] = defaultdict(int)
    for e in built["edges"]:
        by_type[e["type"]] += 1
    meta = {"at": utcnow_iso(), "version": LINKS_VERSION, "config_version": cfg["version"],
            "config_hash": links_config_hash(cfg), "participants": built["participants"], "edges": len(built["edges"]),
            "edges_by_type": dict(by_type), "groups": len(built["groups"]), "grouped_accounts": len(built["members"]),
            "truncated_groups": sum(g["truncated"] for g in built["groups"]), "clusters": len(built["clusters"]),
            "compute_s": round(t1 - t0, 2)}
    write_links(store, built, meta)
    meta["write_s"] = round(time.perf_counter() - t1, 2)
    return meta


def refresh(store: Store, log: Callable[[str], None], force: bool = False, now: float | None = None) -> dict[str, Any] | None:
    """Collector hook (between blocks, outside the block transaction): rebuild when a launch is newer than the last build,
    the config changed, or the last build is older than REFRESH_EVERY_S. A failure is logged and remembered
    (`links_last_error`); it never stops the collector."""
    if store.user_version() < 10:
        return None
    try:
        cfg = load_config()
        st = store.get_status()
        last = st.get("links_built")
        newest = store.con.execute("SELECT MAX(detected_at) FROM launches").fetchone()[0]
        age = None
        if last and last.get("at"):
            t = dt.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() if now is None else now - t.timestamp()
        due = (force or not last or last.get("config_hash") != links_config_hash(cfg)
               or (age is not None and (age > REFRESH_EVERY_S or (newest and newest > last["at"] and age > REFRESH_MIN_GAP_S))))
        if not due:
            return None
        meta = rebuild_links(store, cfg)
        store.set_status("links_last_error", None)
        log(f"links: {meta['participants']} participants, {meta['edges']} edges, {meta['groups']} groups, "
            f"{meta['clusters']} clusters ({meta['compute_s']} s + {meta['write_s']} s)")
        return meta
    except Exception as e:  # noqa: BLE001 — never stops the collector
        log(f"links: {type(e).__name__}: {e}")
        try:
            store.set_status("links_last_error", {"error": f"{type(e).__name__}: {e}"[:300], "at": utcnow_iso()})
        except Exception:  # noqa: BLE001
            pass
        return None


# ============================================================================================ readers (site, diag)

def coverage(con: Any) -> dict[str, Any]:
    """Since when our data can show links: the first stored fact, the first imported funding, the last build."""
    con = getattr(con, "con", con)
    facts_from = con.execute("SELECT MIN(ts_ns) FROM facts").fetchone()[0]
    hist_from = con.execute("SELECT MIN(ts_ns) FROM hist_fundings").fetchone()[0] if _has(con, "hist_fundings") else None
    built = None
    if _has(con, "collector_status"):
        r = con.execute("SELECT value FROM collector_status WHERE key = 'links_built'").fetchone()
        built = json.loads(r[0]) if r and r[0] else None
    return {"facts_from_ns": facts_from, "history_from_ns": hist_from, "built": built}


def _edge_rows(con: Any, where: str, args: tuple) -> list[dict[str, Any]]:
    cols = ("id", "type", "src", "dst", "ts_ns", "tx", "amount", "asset", "token", "source", "group_id")
    return [dict(zip(cols, r)) for r in con.execute(
        f"SELECT {','.join(cols)} FROM link_edges WHERE {where} ORDER BY ts_ns, id", args)]


def account_links(con: Any, account: str, max_members: int = 60, edges_per_member: int = 4) -> dict[str, Any]:
    """For the creator page: the linked group of `account` (members with the edges that join them) and the clusters it
    belongs to. {"built": bool, "group": ... | None, "clusters": [...], "source_of": [...]}"""
    con = getattr(con, "con", con)
    out: dict[str, Any] = {"built": _has(con, "link_members"), "group": None, "clusters": [], "source_of": []}
    if not out["built"]:
        return out
    m = con.execute("SELECT group_id FROM link_members WHERE account = ?", (account,)).fetchone()
    edges_of = lambda a: _edge_rows(con, "src = ? OR dst = ?", (a, a))  # noqa: E731
    if m is None:                                   # a truncated group may have dropped this account: look at its edges
        es = edges_of(account)
        gid = es[0]["group_id"] if es else None
    else:
        gid = m[0]
    if gid:
        g = con.execute("SELECT group_id, size, truncated, launches, first_launch_ns, last_launch_ns FROM link_groups "
                        "WHERE group_id = ?", (gid,)).fetchone()
        if g:
            members = con.execute("SELECT account, launches, first_launch_ns FROM link_members WHERE group_id = ? "
                                  "ORDER BY first_launch_ns, account", (gid,)).fetchall()
            all_edges = _edge_rows(con, "group_id = ?", (gid,))
            by_acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for e in all_edges:
                by_acc[e["src"]].append(e)
                by_acc[e["dst"]].append(e)
            shown = []
            for acct, n, first in members:
                if acct == account:
                    continue
                es = by_acc[acct]
                es = sorted(es, key=lambda e: (0 if account in (e["src"], e["dst"]) else 1, e["ts_ns"] or 0))
                shown.append({"account": acct, "launches": n, "first_launch_ns": first, "edges": es[:edges_per_member],
                              "edges_total": len(es)})
            out["group"] = {"group_id": g[0], "size": g[1], "truncated": bool(g[2]), "launches": g[3], "first_launch_ns": g[4],
                            "last_launch_ns": g[5], "members": shown[:max_members], "members_total": len(shown),
                            "edges_total": len(all_edges)}
    for src, creators, recipients in con.execute(
            "SELECT c.source, c.creators, c.recipients FROM link_cluster_members m JOIN link_clusters c ON c.source = m.source "
            "WHERE m.account = ? ORDER BY c.source", (account,)):
        out["clusters"].append({"source": src, "creators": creators, "recipients": recipients})
    for src, creators, recipients in con.execute(
            "SELECT source, creators, recipients FROM link_clusters WHERE source = ?", (account,)):
        out["source_of"].append({"source": src, "creators": creators, "recipients": recipients})
    return out


def cluster_page(con: Any, source: str) -> dict[str, Any] | None:
    con = getattr(con, "con", con)
    if not _has(con, "link_clusters"):
        return None
    c = con.execute("SELECT source, creators, recipients, first_funded_ns, last_funded_ns, total_near FROM link_clusters "
                    "WHERE source = ?", (source,)).fetchone()
    if c is None:
        return None
    d = dict(zip(("source", "creators", "recipients", "first_funded_ns", "last_funded_ns", "total_near"), c))
    d["members"] = [dict(zip(("account", "funded_ns", "tx", "amount", "amount_total", "fundings", "asset", "edge_source",
                              "launch_token", "launch_ns", "seconds_before"), r)) for r in con.execute(
        "SELECT account, funded_ns, tx, amount, amount_total, fundings, asset, edge_source, launch_token, launch_ns, "
        "seconds_before FROM link_cluster_members WHERE source = ? ORDER BY funded_ns, account", (source,))]
    return d


def _near(x: Any) -> Decimal:
    return Decimal(str(x or 0)) / Decimal(10**24)


def _launch_results(con: Any) -> dict[str, dict[str, Any]]:
    """token -> {accounts (the participants of the launch), loss (NEAR, >= 0), rug, severity, final, ts_ns} for live launches
    with a snapshot; the loss is the outside buyers' net result at RANKING_MINUTE when it is negative."""
    has_o = _has(con, "launch_outcomes")
    sql = ("SELECT l.token, l.creator, l.owner, l.ts_ns, s.buyers_pnl, "
           + ("o.rug, o.severity_near, o.final " if has_o else "NULL, NULL, NULL ") +
           "FROM launches l LEFT JOIN launch_snapshots s ON s.token = l.token AND s.minute = ? AND s.error IS NULL "
           + ("LEFT JOIN launch_outcomes o ON o.token = l.token " if has_o else "") +
           "WHERE l.coverage IS NULL")
    out = {}
    for token, creator, owner, ts, pnl, rug, sev, final in con.execute(sql, (RANKING_MINUTE,)):
        accs = {a for a in (owner, creator) if a and not service(a)}
        loss = Decimal(0)
        if pnl is not None:
            try:
                v = _near(pnl)
                loss = -v if v < 0 else Decimal(0)
            except Exception:  # noqa: BLE001
                loss = Decimal(0)
        out[token] = {"accounts": accs, "loss": loss, "has_snapshot": pnl is not None, "rug": rug, "severity": sev,
                      "final": final, "ts_ns": ts}
    return out


def rankings(con: Any, limit: int = 50, clean_min: int = 3) -> dict[str, Any]:
    """Groups of linked accounts and clusters by the outside buyers' losses of their launches (live launches with a snapshot,
    the loss at T+60: the negative part of the buyers' net result), the stored severity of their rugs as a separate
    column, and the creators with at least `clean_min` launches that have a final outcome and no rug."""
    con = getattr(con, "con", con)
    res = _launch_results(con)
    out: dict[str, Any] = {"minute": RANKING_MINUTE, "groups": [], "clusters": [], "clean": [], "built": _has(con, "link_members")}
    acc_group: dict[str, str] = {}
    sizes: dict[str, tuple[int, int]] = {}
    if out["built"]:
        acc_group = dict(con.execute("SELECT account, group_id FROM link_members"))
        sizes = {g: (s, t) for g, s, t in con.execute("SELECT group_id, size, truncated FROM link_groups")}

    def agg(tokens_by_key: dict[str, set[str]]) -> list[dict[str, Any]]:
        rows = []
        for key, toks in tokens_by_key.items():
            rs = [res[t] for t in toks if t in res]
            snap = [r for r in rs if r["has_snapshot"]]
            if not snap:
                continue
            rugs = [r for r in rs if r["rug"] == 1]
            rows.append({"key": key, "launches": len(snap), "loss": sum((r["loss"] for r in snap), Decimal(0)),
                         "rugs": len(rugs), "severity": sum((Decimal(str(r["severity"] or 0)) for r in rugs), Decimal(0)),
                         "final": sum(1 for r in rs if r["final"])})
        rows.sort(key=lambda r: (-r["loss"], r["key"]))
        return rows[:limit]

    by_group: dict[str, set[str]] = defaultdict(set)
    by_acc: dict[str, set[str]] = defaultdict(set)
    for tok, r in res.items():
        for a in r["accounts"]:
            by_acc[a].add(tok)
            if a in acc_group:
                by_group[acc_group[a]].add(tok)
    for r in agg(by_group):
        r["size"], r["truncated"] = sizes.get(r["key"], (None, 0))
        out["groups"].append(r)
    if _has(con, "link_cluster_members"):
        by_cluster: dict[str, set[str]] = defaultdict(set)
        sizes_c = {s: (c, rc) for s, c, rc in con.execute("SELECT source, creators, recipients FROM link_clusters")}
        for src, acct in con.execute("SELECT source, account FROM link_cluster_members"):
            by_cluster[src] |= by_acc.get(acct, set())
        for r in agg(by_cluster):
            r["creators"], r["recipients"] = sizes_c.get(r["key"], (None, None))
            out["clusters"].append(r)
    clean = []
    for a, toks in by_acc.items():
        final = [res[t] for t in toks if res[t]["final"] and res[t]["rug"] in (0, 1)]
        if len(final) >= clean_min and all(r["rug"] == 0 for r in final):
            clean.append({"account": a, "launches": len(final), "last_ns": max(r["ts_ns"] or 0 for r in final)})
    clean.sort(key=lambda r: (-r["launches"], r["account"]))
    out["clean"] = clean[:limit]
    return out
