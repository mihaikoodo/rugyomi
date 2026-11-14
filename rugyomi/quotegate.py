"""RGY-066 (Q-046, answer A): apply the quote gate to what is already stored. Idempotent, derived data only (raw facts and cards are never touched).

For every launch whose pool is NOT paired with wNEAR (`quote.not_near_quote`):
  1. the NEAR amounts of its stored snapshots (`snapshots.NEAR_COLUMNS`) are set to NULL - they are raw units of another token (new snapshots are written that way by
     `snapshots.metrics`);
  2. its stored outcome row is recomputed (`rules.stored.rebuild_outcomes`, label version 5): every label undefined, `undefined_kind` "quote", no severity, no insiders' NEAR result;
  3. the dossier tables follow (`dossier.refresh(force=True)`), they read the outcomes and the snapshots.
`dry_run=True` (default) only counts. Returns the counts before and after for the affected launches and for all launches (rug 1 / 0 / NULL)."""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable

from . import snapshots as S
from .quote import not_near_quote
from .rules import stored as O
from .store import Store

BATCH = 50


def affected(con: Any) -> list[tuple[str, str]]:
    """[(token, quote token)] of the launches whose pool is not paired with wNEAR."""
    return [(t, q) for t, p in con.execute("SELECT token, pool FROM launches ORDER BY token") if (q := not_near_quote(t, p))]


def _has(con: Any, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def counts(con: Any, tokens: list[str]) -> dict[str, Any]:
    """The stored state: snapshot rows that still hold a NEAR amount, the outcome labels (rug 1 / 0 / NULL, final) of these launches and of all launches."""
    mine = set(tokens)
    out: dict[str, Any] = {"launches": len(tokens), "snapshot_rows_with_near": 0, "snapshot_rows": 0}
    if _has(con, "launch_snapshots"):
        cond = " OR ".join(f"{c} IS NOT NULL" for c in S.NEAR_COLUMNS)
        for t, n in con.execute(f"SELECT token, {cond} FROM launch_snapshots"):
            if t in mine:
                out["snapshot_rows"] += 1
                out["snapshot_rows_with_near"] += bool(n)
    mine_o, all_o = Counter(), Counter()
    if _has(con, "launch_outcomes"):
        for t, rug, fin in con.execute("SELECT token, rug, final FROM launch_outcomes"):
            k = "rug=1" if rug == 1 else "rug=0" if rug == 0 else "rug NULL"
            all_o[k] += 1
            all_o["final"] += bool(fin)
            if t in mine:
                mine_o[k] += 1
                mine_o["final"] += bool(fin)
    out["outcomes_affected"], out["outcomes_all"] = dict(mine_o), dict(all_o)
    return out


def apply(store: Store, dry_run: bool = True, log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    con = store.con
    pairs = affected(con)
    tokens = [t for t, _ in pairs]
    res: dict[str, Any] = {"dry_run": dry_run, "quote_tokens": len({q for _, q in pairs}), "before": counts(con, tokens)}
    if dry_run:
        return res
    cols = ", ".join(f"{c} = NULL" for c in S.NEAR_COLUMNS)
    cleared = 0
    for i in range(0, len(tokens), BATCH):
        part = tokens[i:i + BATCH]
        store.begin()
        try:
            for t in part:
                cleared += con.execute(f"UPDATE launch_snapshots SET {cols} WHERE token = ?", (t,)).rowcount
            store.commit()
        except BaseException:
            store.rollback()
            raise
    res["snapshot_rows_updated"] = cleared
    log(f"quote gate: {cleared} snapshot rows cleared of NEAR amounts ({len(tokens)} launches)")
    rb = Counter()
    for t in tokens:
        for k, v in O.rebuild_outcomes(store, token=t).items():
            rb[k] += v
    res["outcomes_rebuilt"] = dict(rb)
    log(f"quote gate: outcomes rebuilt {dict(rb)}")
    try:
        from . import dossier as DS
        meta = DS.refresh(store, log, force=True)
        res["dossier"] = {"rebuilt": meta is not None}
    except Exception as e:  # noqa: BLE001 - the dossier is derived: the collector's own refresh repeats it
        res["dossier"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
    res["after"] = counts(con, tokens)
    return res
