"""Who holds the supply of a launch (RGY-036): the shares of the dev, the wallets the dev sent tokens to, the early buyers (by what links them) and
the top-10 holders, at T+5 / T+15 / T+60 - and whether they still hold.

Everything here is DERIVED and rebuildable (schema v18); the site only reads it; it changes no card, no signal and no rug label. Publicly we say only
what the trades show: a wallet "bought" and "sold" (never "is the dev", never "one person", master §8). 0 credits: it reads `facts` (live launches) or
`api_facts` (history launches) through the same trade attribution the snapshots use (`snapshots.attribute`), plus the early buyers and their links
that `buyers` already stored.

STORAGE FORK (RGY-036): (A) a derived table, not (B) computing on page render. Each launch needs the trade attribution of three cutoffs and the account age
of up to `buyers.top_n` wallets; the server has one vCPU, so the page reads two small rows. Recomputed by `rebuild-holdings` (resumable, run again) and by the
collector between blocks once the first manual build has run.

Supply. Not stored anywhere and never assumed: it is the net of the token's own `ft_mint` minus `ft_burn` events up to the cutoff (the launch data). No mint
event in the data -> `supply` is NULL, note `no_mint`; the rows hold more than was minted -> note `below_holdings`. In both cases the page says so and shows no
shares. A token that mints on every buy has a growing supply: each minute's share is of the supply minted by then, and the page prints that supply.

Held (per wallet, never negative) = bought - sold up to the cutoff, from the trades; the dev: what the dev acquired (buys and the mint to the dev in the
launch transaction) minus what the dev sold minus what the dev sent to other wallets (shown separately as `dev_transferred_out`; those tokens are in the
next row if the receiver is tracked); a wallet that got tokens from the dev: what it received from the dev + bought - sold.

One wallet is counted ONCE, in the first row that applies (the rows add up):
  dev -> insiders (got tokens from the dev) -> dev_side (received NEAR from the dev side) -> bundle -> group (a pair or a trader group on this launch)
  -> fresh (account younger than `holdings.fresh_hours` at its first buy) -> unknown_age (no creation record in our data; never guessed) -> other.
The early buyers are the first `buyers.top_n` wallets that bought within `buyers.window_min` (the `launch_buyers` rows): wallets that bought later, the pool and
service accounts are in none of the rows (the page shows the remainder as one line).

Top-10 holders: of the tokens held by non-service wallets (the `top10_share_bps` basis). "Bundle-adjusted" counts the wallets of one bundle or of one trader
group (as stored for this launch) as ONE holder; it is never lower than the raw share. Pairs are not merged.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from collections import defaultdict
from typing import Any, Callable

from . import backfill as BF
from . import snapshots as S
from .accounts import account_created
from .parse import LAUNCHBOT
from .progress import ticker
from .rules.config import holdings_config_hash, load_config
from .store import Store
from .store.db import utcnow_iso

VERSION = "0.1"
BATCH = 25
MAX_PER_REFRESH = 100
REFRESH_EVERY_S = 3600.0
NS_HOUR = 3600 * 10**9

# (key, label) in priority order = the order of the rows on the page.
ROWS: tuple[tuple[str, str], ...] = (
    ("dev", "Dev account"),
    ("insiders", "Wallets that got tokens from the dev"),
    ("dev_side", "Early buyers that received NEAR from the dev side"),
    ("bundle", "Early buyers in a bundle"),
    ("group", "Early buyers in a pair or a trader group"),
    ("fresh", "Early buyers with a fresh account"),
    ("unknown_age", "Early buyers, account age unknown"),
    ("other", "Other early buyers"),
)
LABELS = dict(ROWS)
EARLY_KEYS = tuple(k for k, _ in ROWS[2:])
# Q-022 (user, 11.11; data/deploy/buyer_ages.txt 12.11): a creation time is stored for about 2 % of the early buyers (0.8 % from the sources read today, 2.0 % with every stored
# source), so the pages do not split the early buyers by account age. The computation and the stored rows keep `fresh` / `unknown_age`; the READERS (the holdings table and the
# block of facts "now") fold them into `other`, and the methodology does not describe them. Switch back to True when the creation times are there.
AGE_ROWS = ("fresh", "unknown_age")
SHOW_AGE_ROWS = False


def page_keys() -> tuple[str, ...]:
    """The row keys the pages show, in order."""
    return tuple(k for k, _ in ROWS if SHOW_AGE_ROWS or k not in AGE_ROWS)


def fold_age_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{key: row} of stored rows as the pages show them: with SHOW_AGE_ROWS off, the age rows are added into `other` (the same wallets and tokens, so the rows still add up)."""
    out = {r["key"]: dict(r) for r in rows}
    if SHOW_AGE_ROWS:
        return out
    o = out.setdefault("other", {"key": "other", "wallets": 0, "holding": 0, "held": "0"})
    for k in AGE_ROWS:
        r = out.pop(k, None)
        if r:
            o["wallets"] += r["wallets"]
            o["holding"] += r["holding"]
            o["held"] = str(int(o["held"]) + int(r["held"]))
    return out


def _has(con: Any, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    h = cfg["holdings"]
    return {"minutes": [int(m) for m in h["minutes"]], "fresh_hours": float(h["fresh_hours"])}


def page_settings() -> dict[str, Any]:
    """What the page says about the thresholds (defaults if the config is broken: a broken override must not break the page)."""
    try:
        cfg = load_config()
        return {"fresh_hours": float(cfg["holdings"]["fresh_hours"]), "top_n": int(cfg["buyers"]["top_n"]), "window_min": int(cfg["buyers"]["window_min"])}
    except Exception:  # noqa: BLE001
        return {"fresh_hours": 24.0, "top_n": 30, "window_min": 60}


def build_hash(cfg: dict[str, Any]) -> str:
    """What a stored row depends on: the holdings parameters and the early-buyer set (top_n / window) it was computed from."""
    b = cfg["buyers"]
    return f"{VERSION}:{holdings_config_hash(cfg)}:{b['top_n']}:{b['window_min']}"


def describe(cfg: dict[str, Any]) -> dict[str, Any]:
    """The methodology text, from the same constants the computation uses."""
    s = settings(cfg)
    mins = ", ".join(f"T+{m}" for m in s["minutes"])
    return {
        "intro": f"The launch page shows who holds the supply at {mins} minutes after the launch, from the trades and the token's own mint events. "
                 "It is a fact table: it is not part of the risk score, and it does not say who controls a wallet.",
        "supply": "Supply is the net of the token's mint minus burn events up to that minute, read from the launch data; it is never assumed. When the "
                  "data shows no mint, or shows fewer tokens than the rows hold, the page says so and shows no shares.",
        "held": "A wallet holds what it bought minus what it sold up to that minute (never below zero). The dev's line is what the dev acquired minus "
                "what the dev sold minus what the dev sent to other wallets; the sent amount is shown on its own line.",
        "rows": [{"id": k, "description": d} for k, d in (
            ("dev", "The dev account: the owner behind the launch and the creator."),
            ("insiders", "Every other wallet the dev sent launch tokens to."),
            ("dev_side", "An early buyer that received NEAR from the dev, from a wallet linked to the dev or from the source that funded the dev."),
            ("bundle", "An early buyer in a bundle: several wallets that bought within seconds of the launch, funded by the same narrow source."),
            ("group", "An early buyer that bought within seconds of another wallet in several launches (a pair), or is one of a trader group."),
            ("fresh", f"An early buyer whose account was created less than {s['fresh_hours']:g} hours before its first buy, by our creation records."),
            ("unknown_age", "An early buyer whose creation we have no record of; its age is not guessed."),
            ("other", "Every other early buyer." if SHOW_AGE_ROWS else
                      "Every other early buyer. They are not split by account age: our data has the creation time of too few of these wallets."))
            if SHOW_AGE_ROWS or k not in AGE_ROWS],
        "overlap": "A wallet is counted once, in the first row that applies, in the order of the list above, so the rows add up.",
        "scope": "The early buyers are the first wallets that bought within the first hour (the number is a configuration value); wallets that "
                 "bought later, the pool and the service accounts are in none of the rows. The links are the ones recorded when the table was computed.",
        "top10": "The top-10 line is the share of the ten largest holders among the tokens held by non-service wallets. In the bundle-adjusted line the "
                 "wallets of one bundle or of one trader group count as one holder; the adjusted share is never lower than the raw one.",
    }


# ============================================================================================ the computation (pure)

def compute_minute(launch: dict[str, Any], legs: list[dict], swaps: list[dict], minute: int, early: list[dict[str, Any]],
                   tags: dict[str, dict[str, Any]], ages: dict[str, int | None], fresh_s: float, cutoff: int | None = None) -> dict[str, Any]:
    """One snapshot of the holdings at launch + `minute`. `legs` / `swaps` may be longer: they are cut at the cutoff. Pure.
    early: [{buyer, first_buy_ns}] (the stored early buyers); tags: {buyer: {dev_side, bundle, group, pair}}; ages: {buyer: seconds at first buy | None}.
    `cutoff` (RGY-043): an explicit cut-off in UTC ns instead of launch + minute (the live state's "now"); `minute` is then only the label of the
    row. The ONE code path for the fixed minutes and for "now"."""
    token = launch["token"]
    if cutoff is None:
        cutoff = launch["ts_ns"] + minute * S.NS_MIN
    lg = [x for x in legs if x["ts"] <= cutoff]
    sw = [x for x in swaps if x["ts"] <= cutoff]
    trades = S.attribute(token, lg, sw)
    leg_ids = {i for t in trades for i in t["legs"]}
    dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != LAUNCHBOT}
    ins = S._insiders(token, dev, lg, leg_ids)

    bought: dict[str, int] = defaultdict(int)
    sold: dict[str, int] = defaultdict(int)
    for t in trades:
        if t["who"]:
            (bought if t["side"] == "buy" else sold)[t["who"]] += t["tokens"]
    minted = {a: 0 for a in dev}
    out = {a: 0 for a in dev}
    supply = 0
    saw_mint = False
    for leg in lg:
        if leg["kind"] == "ft_mint":
            supply += leg["amount"]
            saw_mint = True
            if leg["a"] in dev and leg["tx"] == launch.get("tx"):
                minted[leg["a"]] += leg["amount"]
        elif leg["kind"] == "ft_burn":
            supply -= leg["amount"]
        elif leg["kind"] == "ft_transfer" and leg["a"] in dev and leg["id"] not in leg_ids:
            out[leg["a"]] += leg["amount"]

    held: dict[str, int] = defaultdict(int)
    wallets: dict[str, int] = defaultdict(int)
    holding: dict[str, int] = defaultdict(int)

    def add(key: str, amount: int) -> None:
        held[key] += amount
        wallets[key] += 1
        holding[key] += 1 if amount > 0 else 0

    for a in sorted(dev):
        add("dev", max(0, bought[a] + minted[a] - sold[a] - out[a]))
    for a in sorted(ins["recipients"]):
        add("insiders", max(0, ins["recipients"][a] + bought[a] - sold[a]))
    tracked = dev | set(ins["recipients"])
    n_early = ages_known = 0
    for e in sorted(early, key=lambda r: (r["first_buy_ns"], r["buyer"])):
        w = e["buyer"]
        if e["first_buy_ns"] > cutoff or w in tracked:
            continue
        tg = tags.get(w) or {}
        age = ages.get(w)
        n_early += 1
        ages_known += age is not None
        if tg.get("dev_side"):
            key = "dev_side"
        elif tg.get("bundle"):
            key = "bundle"
        elif tg.get("group") or tg.get("pair"):
            key = "group"
        elif age is None:
            key = "unknown_age"
        elif age < fresh_s:
            key = "fresh"
        else:
            key = "other"
        add(key, max(0, bought[w] - sold[w]))

    bal: dict[str, int] = defaultdict(int)
    for leg in lg:
        if leg["kind"] == "ft_mint":
            bal[leg["a"]] += leg["amount"]
        elif leg["kind"] == "ft_burn":
            bal[leg["a"]] -= leg["amount"]
        else:
            bal[leg["a"]] -= leg["amount"]
            bal[leg["b"]] += leg["amount"]
    pos = {k: v for k, v in bal.items() if v > 0 and not S.is_service(k, token)}
    total = sum(pos.values())
    entity: dict[str, int] = defaultdict(int)
    merged = set()
    for w, v in pos.items():
        tg = tags.get(w) or {}
        if tg.get("bundle"):
            ent = "bundle:" + tg["bundle"]
        elif tg.get("trader_group"):
            ent = "group:" + tg["trader_group"]
        else:
            ent = "w:" + w
        entity[ent] += v
        if ent[0] != "w":
            merged.add(ent)
    raw = sorted(pos.values(), reverse=True)
    adj = sorted(entity.values(), reverse=True)

    rows = [{"key": k, "wallets": wallets[k], "holding": holding[k], "held": str(held[k])} for k, _ in ROWS]
    held_sum = sum(held.values())
    if not saw_mint:
        note, sup = "no_mint", None
    else:
        sup = supply
        note = "ok" if 0 < sup and held_sum <= sup else "below_holdings"
    return {
        "minute": minute, "supply": None if sup is None else str(sup), "supply_note": note, "rows": rows,
        "extra": {"dev_transferred_out": str(sum(out.values())), "early_wallets": n_early, "ages_known": ages_known,
                  "holders": len(pos), "top10_bps": sum(raw[:10]) * 10_000 // total if total else None,
                  "holders_adjusted": len(entity), "top10_adjusted_bps": sum(adj[:10]) * 10_000 // total if total else None,
                  "merged_holders": len(merged)},
    }


def compute_launch(launch: dict[str, Any], legs: list[dict], swaps: list[dict], early: list[dict[str, Any]], tags: dict[str, dict[str, Any]],
                   ages: dict[str, int | None], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    s = settings(cfg)
    return [compute_minute(launch, legs, swaps, m, early, tags, ages, s["fresh_hours"] * 3600) for m in s["minutes"]]


# ============================================================================================ reading the stored inputs

def load_tags(con: Any, token: str) -> dict[str, dict[str, Any]]:
    """{buyer: {dev_side, bundle: key, group: key, trader_group: key, pair}} of this launch from the stored links and pairs."""
    tags: dict[str, dict[str, Any]] = defaultdict(dict)
    if _has(con, "buyer_links"):
        for buyer, typ, key in con.execute("SELECT buyer, type, group_key FROM buyer_links WHERE token = ?", (token,)):
            if typ == "dev_side_buyer":
                tags[buyer]["dev_side"] = True
            elif typ == "bundle":
                tags[buyer]["bundle"] = key
            elif typ == "co_buyers":
                tags[buyer]["group"] = key
                tags[buyer]["trader_group"] = key
    if _has(con, "buyer_pair_launches"):
        for a, b in con.execute("SELECT a, b FROM buyer_pair_launches WHERE token = ?", (token,)):
            tags[a]["pair"] = True
            tags[b]["pair"] = True
    return dict(tags)


def load_ages(store: Store, early: list[dict[str, Any]], cache: dict[str, int | None]) -> dict[str, int | None]:
    """{buyer: seconds between the account's creation record and its first buy | None}. Stored creation data only (`accounts.account_created`); a record
    that is not before the first buy is not an age (None). `cache`: account -> creation ts, shared by the launches of one run."""
    out: dict[str, int | None] = {}
    for e in early:
        w = e["buyer"]
        if w not in cache:
            c = account_created(store, w)
            cache[w] = c["ts_ns"] if c and c.get("ts_ns") is not None else None
        ts = cache[w]
        out[w] = (e["first_buy_ns"] - ts) // 10**9 if ts is not None and ts <= e["first_buy_ns"] else None
    return out


def _history_done(store: Store) -> set[str]:
    return {r[0] for r in store.con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status IN ('done', 'partial')")}


def pending(store: Store, cfg: dict[str, Any], only: list[str] | None = None, force: bool = False) -> list[tuple[str, str]]:
    """(token, source 'live' | 'history') of the launches whose holdings are not computed (or computed with other parameters): only launches whose
    early buyers are FINAL (the T+60 window is complete) and have trades; oldest first."""
    con = store.con
    h = build_hash(cfg)
    done = {t: (f, hh, src) for t, f, hh, src in con.execute("SELECT token, final, config_hash, source FROM launch_holdings_done")}
    want = set(only) if only is not None else None
    out = []
    for token, src, final in con.execute("SELECT d.token, d.source, d.final FROM launch_buyers_done d JOIN launches l ON l.token = d.token "
                                         "ORDER BY l.ts_ns, l.token").fetchall():
        if (want is not None and token not in want) or src not in ("live", "history") or not final:
            continue
        d = done.get(token)
        if not force and d and d[0] == 1 and d[1] == h and d[2] == src:
            continue
        out.append((token, src))
    return out


def build(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None, only: list[str] | None = None,
          limit: int | None = None, force: bool = False) -> dict[str, Any]:
    """Resumable: the holdings of every launch that has none computed yet. Commits every BATCH launches."""
    t0 = time.time()
    cfg = cfg or load_config()
    if store.user_version() < 18:
        raise RuntimeError("holdings: the database is not migrated to schema v18 yet")
    con = store.con
    todo = pending(store, cfg, only, force)
    out: dict[str, Any] = {"to_compute": len(todo), "computed": 0, "failed": 0, "failed_tokens": [], "rows": 0, "supply_unknown": 0,
                           "supply_below_holdings": 0}
    if limit is not None:
        todo = todo[:limit]
    h = build_hash(cfg)
    max_min = max(settings(cfg)["minutes"])
    cache: dict[str, int | None] = {}
    batch: list[tuple[str, str, list[dict[str, Any]]]] = []

    def flush() -> None:
        if not batch:
            return
        store.begin()
        try:
            for token, src, res in batch:
                con.execute("DELETE FROM launch_holdings WHERE token = ?", (token,))
                now = utcnow_iso()
                con.executemany(
                    "INSERT INTO launch_holdings(token, minute, supply, supply_note, rows, extra, computed_at) VALUES(?,?,?,?,?,?,?)",
                    [(token, r["minute"], r["supply"], r["supply_note"], json.dumps(r["rows"], separators=(",", ":")),
                      json.dumps(r["extra"], separators=(",", ":")), now) for r in res])
                con.execute("INSERT OR REPLACE INTO launch_holdings_done(token, source, final, config_hash, computed_at) VALUES(?,?,?,?,?)",
                            (token, src, 1, h, now))
            store.commit()
        except BaseException:
            store.rollback()
            raise
        batch.clear()

    tick = ticker(log, "holdings", len(todo))
    for i, (token, src) in enumerate(todo):
        try:
            launch = S._launch(store, token)
            cutoff = launch["ts_ns"] + max_min * S.NS_MIN
            legs, swaps = S._rows(store, launch, cutoff) if src == "live" else BF.rows_from_api(store, launch, cutoff)
            early = [{"buyer": b, "first_buy_ns": ts} for b, ts in
                     con.execute("SELECT buyer, first_buy_ns FROM launch_buyers WHERE token = ? ORDER BY rank", (token,))]
            res = compute_launch(launch, legs, swaps, early, load_tags(con, token), load_ages(store, early, cache), cfg)
            batch.append((token, src, res))
            out["computed"] += 1
            out["rows"] += len(res)
            out["supply_unknown"] += sum(1 for r in res if r["supply_note"] == "no_mint")
            out["supply_below_holdings"] += sum(1 for r in res if r["supply_note"] == "below_holdings")
        except Exception as e:  # noqa: BLE001 - one launch must not stop the rebuild
            out["failed"] += 1
            out["failed_tokens"].append(f"{token}: {type(e).__name__}: {str(e)[:120]}")
        if len(batch) >= BATCH:
            flush()
            tick(i + 1)
    flush()
    store.set_status("holdings_built", {"at": utcnow_iso(), "version": VERSION, "config_hash": build_hash(cfg), "computed": out["computed"]})
    out["left"] = len(pending(store, cfg))
    out["seconds"] = round(time.time() - t0, 1)
    return out


def refresh(store: Store, log: Callable[[str], None], force: bool = False, now: float | None = None) -> dict[str, Any] | None:
    """Collector hook, between blocks, after `buyers.refresh`: the holdings of new launches (at most MAX_PER_REFRESH) once the early buyers are final. It
    starts only after the first manual build (`rebuild-holdings`), so the initial backlog never runs inside the collector. A failure is logged and
    remembered (`holdings_last_error`); it never stops the collector."""
    if store.user_version() < 18:
        return None
    try:
        last = store.get_status().get("holdings_built")
        if not last:
            return None
        cfg = load_config()
        age = None
        if last.get("at"):
            t = dt.datetime.fromisoformat(last["at"].replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() if now is None else now - t.timestamp()
        if not (force or last.get("config_hash") != build_hash(cfg) or (age is not None and age > REFRESH_EVERY_S)):
            return None
        res = build(store, cfg, log, limit=MAX_PER_REFRESH)
        store.set_status("holdings_last_error", None)
        if res["computed"] or res["failed"]:
            log(f"holdings: {res['computed']} launches, {res['failed']} failed ({res['seconds']} s)")
        return res
    except Exception as e:  # noqa: BLE001 - never stops the collector
        log(f"holdings: {type(e).__name__}: {e}")
        try:
            store.set_status("holdings_last_error", {"error": f"{type(e).__name__}: {e}"[:300], "at": utcnow_iso()})
        except Exception:  # noqa: BLE001
            pass
        return None


# ============================================================================================ reader (site, diag)

def share_bps(supply: str | int | None, note: str, held: int) -> int | None:
    """A share of the supply in bps, only when the supply is known and consistent (`supply_note` ok): held * 10000 // supply, else None. The one copy of the rule."""
    if note != "ok" or supply is None or int(supply) <= 0:
        return None
    return int(held) * 10_000 // int(supply)


def launch_holdings(con: Any, token: str) -> dict[str, Any] | None:
    """What the launch page shows, or None when nothing is stored: {minutes: [{minute, supply, supply_note, shares_ok, rest_bps, extra, cells}],
    rows: [{key, label, cells: [{wallets, holding, bps}]}]}. A share is held * 10000 // supply, only when the supply is known and consistent."""
    con = getattr(con, "con", con)
    if not _has(con, "launch_holdings"):
        return None
    got = con.execute("SELECT minute, supply, supply_note, rows, extra FROM launch_holdings WHERE token = ? ORDER BY minute", (token,)).fetchall()
    if not got:
        return None
    minutes, by_key = [], {k: [] for k in page_keys()}
    for minute, supply, note, rows, extra in got:
        rows = fold_age_rows(json.loads(rows))
        ok = share_bps(supply, note, 0) is not None
        total = 0
        for k in page_keys():
            r = rows.get(k) or {"wallets": 0, "holding": 0, "held": "0"}
            held = int(r["held"])
            total += held
            by_key[k].append({"wallets": r["wallets"], "holding": r["holding"], "bps": share_bps(supply, note, held)})
        ex = json.loads(extra) if extra else {}
        minutes.append({"minute": minute, "supply": supply, "supply_note": note, "shares_ok": ok, "extra": ex,
                        "rest_bps": max(0, 10_000 - total * 10_000 // int(supply)) if ok else None,
                        "early_bps": sum(by_key[k][-1]["bps"] for k in EARLY_KEYS if k in by_key) if ok else None})
    return {"minutes": minutes, "rows": [{"key": k, "label": LABELS[k], "cells": by_key[k]} for k in page_keys()], "settings": page_settings()}
