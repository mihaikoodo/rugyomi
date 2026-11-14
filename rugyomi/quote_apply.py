"""RGY-070 (B of Q-046), stage 2: what the quote-token prices change, and the rebuild that makes it so.

`plan(store)` is READ ONLY (it opens nothing for writing and works on a read-only connection): for every launch whose pool is not paired with wNEAR
(`quote.not_near_quote`) it computes in memory the snapshots with the NEAR amounts converted at the swap-time price (`snapshots.metrics` with
`snapshots.prices_for`, the same functions the rebuild uses) and from them the outcome row (`rules.stored.row_from`, the very code of `compute_row`),
and sets it beside the stored outcome:

  * counts rug / no rug / undetermined / pending before and after, for these launches and for all launches, with the transition matrix;
  * why a launch stays undetermined (no price source for its quote token / k of n trades without a price within the price age) per quote token;
  * five examples with the transactions (the launch, the price swaps the conversion rests on);
  * the issued cards: the only card signal that reads a NEAR amount of the launch pool is `fingerprint` (the dev buy against the funding); for a
    launch with another quote token the card compared RAW units of that token with NEAR. The same signal is recomputed with the dev buy converted at the
    launch time price (`cards`): which cards would score differently. Cards are NEVER changed; this is the list a note would be made from.

`apply(store, tokens)` writes DERIVED rows only, by the existing paths: a `launch_only` (history) launch -> `backfill.write_snapshots` (source 'backfill', rows of the
live collector are kept), a fully covered launch -> `snapshots.rebuild_snapshots`; then `rules.stored.rebuild_outcomes` per launch and the dossier refresh (as
`quotegate.apply`). One short transaction per launch, runs alongside the collector. Idempotent. Raw facts, cards and launches paired with wNEAR are never touched.
NOT run before the user has read the output of `plan`."""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from typing import Any, Callable

from . import backfill as BF
from . import quote_price as QP
from . import snapshots as S
from .quote import not_near_quote
from .rules import stored as O
from .store import Store

NS_MIN = 60 * 10**9
LABELS = ("rug", "no rug", "undetermined", "pending")


def category(row: dict[str, Any] | None) -> str:
    """rug / no rug / undetermined (final, the label cannot be stated) / pending (no T+24 h snapshot yet, or no outcome row)."""
    if row is None:
        return "pending"
    if row["rug"] == 1:
        return "rug"
    if row["rug"] == 0:
        return "no rug"
    return "undetermined" if row["final"] else "pending"


def _stored_outcome(con: Any, token: str) -> dict[str, Any] | None:
    r = con.execute(f"SELECT {','.join(O.ROW_COLS)} FROM launch_outcomes WHERE token = ?", (token,)).fetchone()
    return dict(zip(O.ROW_COLS, r)) if r else None


def _stored_snaps(con: Any, token: str) -> list[dict[str, Any]]:
    return O._snapshots(con, token)


def _roundtrip(store: Store, token: str, rows: list[tuple]) -> list[dict[str, Any]]:
    """The snapshot rows as the table would hold them (column types), read back with the query `rules.stored` uses: the outcome code sees the same values."""
    ddl = store.con.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'launch_snapshots'").fetchone()[0]
    mem = sqlite3.connect(":memory:")
    try:
        mem.execute(ddl)
        cols = ("token", "minute", "at_ts_ns") + S.COLUMNS + ("metric_version", "computed_at", "error", "source")
        mem.executemany(f"INSERT OR REPLACE INTO launch_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})", rows)
        return O._snapshots(mem, token)
    finally:
        mem.close()


def _after_rows(store: Store, launch: dict[str, Any], history: bool, prices: Any, have: dict[int, str | None]) -> tuple[list[tuple], dict[int, dict]]:
    """The snapshot rows the rebuild would write (as table rows) and, per minute, the metrics dict. `have`: minute -> source of the stored row."""
    rows: list[tuple] = []
    mets: dict[int, dict] = {}
    token, ts0 = launch["token"], launch["ts_ns"]
    legs_all = swaps_all = None
    if history:
        legs_all, swaps_all = BF.rows_from_api(store, launch, ts0 + QP.WINDOW_MIN * NS_MIN)
    for m in S.MINUTES:
        if history:
            if have.get(m, "-") is None:                 # a row of the live collector (source NULL): never replaced
                continue
        elif m not in have:                              # a fully covered launch: only the minutes already computed
            continue
        cutoff = ts0 + m * NS_MIN
        try:
            if history:
                mt = S.metrics(launch, [x for x in legs_all if x["ts"] <= cutoff], [x for x in swaps_all if x["ts"] <= cutoff], cutoff, prices)
            else:
                mt = S.compute(store, token, m)
            rows.append((token, m, mt["at_ts_ns"], *(mt[c] for c in S.COLUMNS), S.METRIC_VERSION, "", None, BF.SOURCE if history else None))
            mets[m] = mt
        except Exception as e:  # noqa: BLE001 - what the rebuild stores as the row's error
            rows.append((token, m, cutoff, *([None] * len(S.COLUMNS)), S.METRIC_VERSION, "", f"{type(e).__name__}: {e}"[:500], BF.SOURCE if history else None))
    return rows, mets


def _why(store: Store, launch: dict[str, Any], quote: str, prices: Any, history: bool) -> dict[str, Any]:
    """Why the 24 h window of a launch is not convertible, from the same trades the snapshots read (api rows for a history launch, live facts otherwise): the trades
    without a price within the price age, split into 'no earlier price at all' and 'the last earlier price is older than the age' (with its age, so the sensitivity is visible)."""
    cutoff = launch["ts_ns"] + QP.WINDOW_MIN * NS_MIN
    swaps = BF.rows_from_api(store, launch, cutoff)[1] if history else S._swaps(store, launch, cutoff)
    conv = QP.convert_swaps(launch["token"], quote, swaps, prices)
    out = {"ok": conv["ok"], "trades": conv["total"], "missing": conv["missing"], "price_points": len(prices.points) if prices is not None else 0,
           "no_earlier_price": 0, "stale": 0, "max_stale_min": None}
    if prices is not None and conv["missing_ts"]:
        wide = QP.with_age(prices, 10**9)               # any age: the nearest earlier price, whatever its age
        ages = []
        for ts in conv["missing_ts"]:
            p = wide.at(ts)
            if p is None:
                out["no_earlier_price"] += 1
            else:
                out["stale"] += 1
                ages.append((ts - p.ts) / NS_MIN)
        out["max_stale_min"] = round(max(ages), 1) if ages else None
    return out


def _partial_quotes(con: Any) -> set[str]:
    """Quote tokens with a unit the collector stopped at its transaction cap (status partial): trades near the cut may stay without a price."""
    try:
        rows = con.execute("SELECT key FROM backfill_queue WHERE kind = 'quote' AND status = 'partial'").fetchall()
    except sqlite3.Error:
        return set()
    return {str(k).split(":")[0] for (k,) in rows}


def card_impact(store: Store, launches: dict[str, dict[str, Any]], series: dict[str, Any]) -> dict[str, Any]:
    """The issued cards of these launches and what the `fingerprint` signal would say with the dev buy converted to NEAR at the launch time. Pure read."""
    from . import cardfix as CFX
    from . import rules
    from .rules.card import band_of, card_row
    out: dict[str, Any] = {"cards": 0, "changed": [], "same": 0, "no_dev_buy": 0, "no_price": [], "no_fingerprint": 0}
    for token, launch in sorted(launches.items()):
        card = card_row(store.con, token)
        if card is None:
            continue
        out["cards"] += 1
        res = next((r for r in card["signals"]["results"] if r["id"] == "fingerprint"), None)
        v = (res or {}).get("value") or {}
        if "conditions_met" not in v:
            out["no_fingerprint"] += 1
            continue
        cfg, _ = CFX._card_config(card, rules.profile_of(card["rules_version"]))
        fc = cfg["signals"]["fingerprint"]
        quote = not_near_quote(token, launch.get("pool"))
        raw = int(launch.get("dev_buy") or 0)
        p = series[token].at(launch["ts_ns"]) if token in series else None
        row = {"token": token, "quote": quote, "dev_buy_raw": raw, "issued_score": card["score"], "issued_band": card["band"], "issued_dev_buy_near": v["dev_buy_near"],
               "funding_near": v["funding_near"], "fresh": v["fresh_account"], "funded": v["funded"], "issued_spends": v["dev_buy_vs_funding"],
               "issued_points": res["contribution"], "launch_tx": launch.get("tx")}
        if not raw:                                       # no dev buy recorded: 0 in raw units is 0 in NEAR, the signal reads the same
            out["same"] += 1
            out["no_dev_buy"] += 1
            continue
        if p is None:
            row["why"] = "no price at the launch time"
            out["no_price"].append(row)
            continue
        with localcontext() as c:
            c.prec = QP.PREC
            spent = float(Decimal(raw) * p.price / Decimal(10**24))
        spends = bool(v["funded"]) and spent >= fc["dev_buy_share"] * v["funding_near"]
        n = int(bool(v["fresh_account"])) + int(bool(v["funded"])) + int(spends)
        pts = 0 if n < 2 else fc["points_3_of_3"] if n == 3 else fc["points_2_of_3"]
        row.update({"dev_buy_near": round(spent, 6), "spends": spends, "conditions_met": n, "points": pts, "price_tx": p.tx, "price_pool": p.pool})
        if pts == res["contribution"] and spends == v["dev_buy_vs_funding"]:
            out["same"] += 1
            continue
        score = max(0, min(100, card["score"] - res["contribution"] + pts))
        row.update({"score": score, "band": band_of(score, cfg)})
        out["changed"].append(row)
    return out


def plan(store: Store, log: Callable[[str], None] = lambda m: None, cfg: dict[str, Any] | None = None, only: list[str] | None = None) -> dict[str, Any]:
    """Read only. See the module docstring."""
    from .quotegate import affected
    con = store.con
    cfg = cfg or O.load_config()
    pairs = affected(con)
    if only is not None:
        pairs = [(t, q) for t, q in pairs if t in set(only)]
    partial_q = _partial_quotes(con)
    has_cov = store.user_version() >= 6
    trans: Counter = Counter()
    before_aff: Counter = Counter()
    after_aff: Counter = Counter()
    per_quote: dict[str, Counter] = defaultdict(Counter)
    undet: dict[str, dict[str, Any]] = {}
    examples_pool: list[dict[str, Any]] = []
    changed_tokens: list[str] = []
    launches: dict[str, dict[str, Any]] = {}
    series: dict[str, Any] = {}
    n_hist = n_full = 0
    for i, (token, quote) in enumerate(pairs):
        launch = S._launch(store, token)
        if launch is None:
            continue
        launch["dev_buy"] = con.execute("SELECT dev_buy FROM launches WHERE token = ?", (token,)).fetchone()[0]
        cov = con.execute("SELECT coverage FROM launches WHERE token = ?", (token,)).fetchone()[0] if has_cov else None
        history = cov is not None
        launches[token] = launch
        prices = S.prices_for(store, launch)
        series[token] = prices
        old = _stored_outcome(con, token)
        have = {m: src for m, src in con.execute("SELECT minute, source FROM launch_snapshots WHERE token = ?", (token,))}
        b = category(old)
        before_aff[b] += 1
        if not have:                                      # no snapshot at all (no trades in the window, or not due yet): nothing changes
            trans[(b, b)] += 1
            after_aff[b] += 1
            per_quote[quote]["no snapshots"] += 1
            continue
        rows, mets = _after_rows(store, launch, history, prices, have)
        n_hist += history
        n_full += not history
        stored = _stored_snaps(con, token)
        after_snaps = _roundtrip(store, token, rows) if rows else []
        by_min = {s["minute"]: s for s in stored}
        by_min.update({s["minute"]: s for s in after_snaps})
        new = O.row_from(token, launch["pool"], sorted(by_min.values(), key=lambda s: s["minute"]), cfg)
        a = category(new)
        trans[(b, a)] += 1
        after_aff[a] += 1
        per_quote[quote][f"{b} -> {a}"] += 1
        conv_rows = [m for m, mt in mets.items() if "quote_conversion" in (json.loads(mt["insiders_evidence"]) if mt.get("insiders_evidence") else {})]
        if conv_rows:
            changed_tokens.append(token)
        if a in ("rug", "no rug") and b != a:
            ev = json.loads(mets[max(conv_rows)]["insiders_evidence"])["quote_conversion"] if conv_rows else {}
            examples_pool.append({"token": token, "quote": quote, "before": b, "after": a, "launch_tx": launch.get("tx"), "pool": launch["pool"],
                                  "trades": ev.get("trades"), "price_pools": ev.get("pools"), "max_price_age_s": ev.get("max_price_age_s"),
                                  "price_txs": ev.get("price_txs"), "buyers_pnl_near": (new or {}).get("insiders_pnl"),
                                  "severity_near": (new or {}).get("severity_near"), "history": history})
        merged = sorted(by_min.values(), key=lambda s: s["minute"])
        if new is not None and not O._converted(merged, cfg):         # the labels still rest on a gated snapshot: name why
            undet[token] = {"quote": quote, **_why(store, launch, quote, prices, history), "partial_unit": quote in partial_q}
        if (i + 1) % 100 == 0:
            log(f"quote_apply: {i + 1}/{len(pairs)}")
    all_before = Counter()
    for t, rug, fin in con.execute("SELECT token, rug, final FROM launch_outcomes"):
        all_before["rug" if rug == 1 else "no rug" if rug == 0 else "undetermined" if fin else "pending"] += 1
    all_after = Counter(all_before)
    for k in LABELS:
        all_after[k] += after_aff[k] - before_aff[k]
    by_reason: Counter = Counter()
    for t, u in undet.items():
        by_reason[(u["quote"], "no price source (no swap of the pair in the window)" if not u["price_points"] else
                   "trades before the first price" if u["no_earlier_price"] and not u["stale"] else "last price older than the age" if u["stale"] and not u["no_earlier_price"]
                   else "trades before the first price and stale prices" if u["missing"] else "other")] += 1
    return {
        "launches": len(pairs), "history": n_hist, "full": n_full, "quote_tokens": len({q for _, q in pairs}),
        "before_affected": {k: v for k, v in before_aff.items() if v}, "after_affected": {k: v for k, v in after_aff.items() if v},
        "before_all": {k: all_before[k] for k in LABELS}, "after_all": {k: all_after[k] for k in LABELS},
        "transitions": {f"{a} -> {b}": n for (a, b), n in sorted(trans.items())},
        "per_quote": {q: dict(c) for q, c in sorted(per_quote.items())},
        "undetermined_after": {t: u for t, u in sorted(undet.items())},
        "undetermined_by_reason": {f"{q} | {r}": n for (q, r), n in sorted(by_reason.items())},
        "partial_quotes": sorted(partial_q),
        "changed_tokens": changed_tokens, "examples_pool": examples_pool,
        "cards": card_impact(store, launches, series),
    }


def pick_examples(pool: list[dict[str, Any]], n: int = 5) -> list[dict[str, Any]]:
    """Up to n examples: both kinds of outcome, different quote tokens first, deterministic."""
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    for want in ("rug", "no rug", "rug", "no rug", "rug", "no rug"):
        for e in sorted(pool, key=lambda x: (x["quote"] in used, x["token"])):
            if e["after"] == want and e not in out and e.get("price_txs"):
                out.append(e)
                used.add(e["quote"])
                break
        if len(out) >= n:
            break
    return out


def apply(store: Store, tokens: list[str], log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Write the converted snapshots and the outcomes of `tokens` by the existing paths (see the module docstring). Short transactions, derived rows only."""
    has_cov = store.user_version() >= 6
    res: dict[str, Any] = {"launches": 0, "snapshots": Counter(), "outcomes": Counter()}
    for token in tokens:
        launch = BF._launch_row(store, token)
        if launch is None:
            continue
        cov = store.con.execute("SELECT coverage FROM launches WHERE token = ?", (token,)).fetchone()[0] if has_cov else None
        if cov is not None:
            store.begin()
            try:
                r = BF.write_snapshots(store, launch)
                store.commit()
            except BaseException:
                store.rollback()
                store.snap_touched.clear()
                raise
        else:
            r = S.rebuild_snapshots(store, token=token)
        for k in ("ok", "error", "kept_live", "not_due", "not_covered"):
            res["snapshots"][k] += r.get(k, 0)
        for k, v in O.rebuild_outcomes(store, token=token).items():
            res["outcomes"][k] += v
        store.snap_touched.clear()
        res["launches"] += 1
        if res["launches"] % 50 == 0:
            log(f"quote_apply: {res['launches']}/{len(tokens)}")
    res["snapshots"], res["outcomes"] = dict(res["snapshots"]), dict(res["outcomes"])
    try:
        from . import dossier as DS
        meta = DS.refresh(store, log, force=True)
        res["dossier"] = {"rebuilt": meta is not None}
    except Exception as e:  # noqa: BLE001 - derived: the collector's own refresh repeats it
        res["dossier"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    return res
