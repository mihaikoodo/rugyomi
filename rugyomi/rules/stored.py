"""Stored outcome labels (RGY-017): the `launch_outcomes` table.

The collector — the only writer — computes the labels of a launch (`rug_fast`, `rug_late`, `rug`, `severity_near`, and
the RGY-015 facts `dev_exit` / `buyers_loss`) from its snapshots with the config in force, and stores them with the
config version. Everything else (the site, the diag, the future accuracy table) READS the rows; nothing recomputes
labels from snapshots elsewhere. The rows are derived, not predictions: they are rewritten whenever a T+60 / T+24 h
snapshot is written (`refresh_touched`, called by the collector in the block transaction) and by `rebuild_outcomes`
(after a config change or `rebuild-snapshots`).
"""
from __future__ import annotations

import json
from typing import Any

from ..store import Store
from ..store.db import utcnow_iso
from .config import ConfigError, config_hash, load_config
from .outcomes import outcome_labels
from ..quote import not_near_quote
from .rug import LABEL_VERSION, rug_labels

SNAP_COLS = ("minute", "at_ts_ns", "error", "metric_version", "dev_sold_share_bps", "buyers_count", "buyers_pnl",
             "price_last", "price_peak", "peak_at", "insiders_count", "insiders_acquired", "insiders_sold",
             "insiders_sold_share_bps", "insiders_pnl", "insiders_evidence")
ROW_COLS = ("token", "rug_fast", "rug_late", "rug", "severity_near", "insiders_pnl", "dev_exit", "buyers_loss",
            "evidence", "config_version", "config_hash", "computed_at", "final")


def _snapshots(con: Any, token: str) -> list[dict[str, Any]]:
    return [dict(zip(SNAP_COLS, r)) for r in con.execute(
        f"SELECT {','.join(SNAP_COLS)} FROM launch_snapshots WHERE token = ? ORDER BY minute", (token,))]


def _b(v: bool | None) -> int | None:
    return None if v is None else int(v)


def _converted(snaps: list[dict[str, Any]], cfg: dict[str, Any]) -> bool:
    """RGY-070: a launch not paired with wNEAR has real NEAR numbers when its T+60 and T+24 h snapshots (the ones the labels read) both exist without an error and
    hold a NEAR result (`buyers_pnl` is NULL in a gated snapshot, `snapshots.metrics`). One of them gated -> the whole launch stays undefined."""
    need = [s for s in snaps if s["minute"] in (cfg["rug"]["fast_minute"], cfg["rug"]["late_minute"])]
    return bool(need) and all(not s["error"] and s["buyers_pnl"] is not None for s in need)


def compute_row(store: Store, token: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The row for `token` from its snapshots, or None when it has no T+60 / T+24 h snapshot at all. No writes."""
    row = store.con.execute("SELECT pool FROM launches WHERE token = ?", (token,)).fetchone()
    return row_from(token, row[0] if row else None, _snapshots(store.con, token), cfg)


def row_from(token: str, pool: str | None, snaps: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any] | None:
    """`compute_row` from the snapshot rows themselves (RGY-070: the before / after tool computes the row of snapshots that are not stored yet). Pure."""
    late_min = cfg["rug"]["late_minute"]
    if not any(s["minute"] in (cfg["rug"]["fast_minute"], late_min) for s in snaps):
        return None
    quote = not_near_quote(token, pool)                             # RGY-066 (Q-046): not paired with wNEAR -> undefined, no NEAR amounts
    converted = None
    if quote and _converted(snaps, cfg):                            # RGY-070: the NEAR amounts of the T+60 and T+24 h snapshots were converted at the swap-time price
        converted, quote = quote, None
    labels = rug_labels(snaps, cfg, quote=quote)
    facts = outcome_labels(snaps, cfg)
    if quote:
        facts["buyers_loss"] = {**facts["buyers_loss"], "value": None, "buyers_pnl_near": None}
    usable = [s for s in snaps if not s["error"] and s["insiders_pnl"] is not None]
    last24 = next((s for s in snaps if s["minute"] == late_min and not s["error"]), None)
    late_ev = labels["rug_late"]["evidence"]
    ev = {**{k: labels[k] for k in ("rug_fast", "rug_late", "rug")}, "severity_near": labels["severity_near"], "outcomes": facts, "label_version": LABEL_VERSION}
    if converted:
        ev["quote_conversion"] = {"quote": converted}
    return {
        # RGY-026: not stored; counted by `rebuild_outcomes` (a T+24 h label with outside buyers but no price)
        "_price_missing": int(bool(last24) and bool(last24["buyers_count"]) and late_ev.get("price_vs_peak", 0) is None),
        "_price_undefined": int("price" in (labels["rug"].get("undefined_kind") or "")),
        "token": token, "rug_fast": _b(labels["rug_fast"]["value"]), "rug_late": _b(labels["rug_late"]["value"]),
        "rug": _b(labels["rug"]["value"]), "severity_near": labels["severity_near"],
        "insiders_pnl": usable[-1]["insiders_pnl"] if usable else None,
        "dev_exit": _b(facts["dev_exit"]["value"]), "buyers_loss": _b(facts["buyers_loss"]["value"]),
        "evidence": json.dumps(ev, separators=(",", ":")),
        "config_version": cfg["version"], "config_hash": config_hash(cfg), "computed_at": utcnow_iso(),
        "final": int(last24 is not None),
    }


def _store(store: Store, row: dict[str, Any]) -> None:
    store.con.execute(f"INSERT OR REPLACE INTO launch_outcomes({','.join(ROW_COLS)}) "
                      f"VALUES({','.join('?' * len(ROW_COLS))})", [row[c] for c in ROW_COLS])
    if store.user_version() >= 11:               # RGY-020: a launch whose snapshots come from the backfill says so
        store.con.execute("UPDATE launch_outcomes SET source = CASE WHEN EXISTS (SELECT 1 FROM launch_snapshots s "
                          "WHERE s.token = ? AND s.source = 'backfill') THEN 'backfill' END WHERE token = ?",
                          (row["token"], row["token"]))


def _remember(store: Store, token: str, error: str) -> None:
    store.set_status("outcomes_last_error", {"token": token, "error": error, "at": utcnow_iso()})


def refresh_touched(store: Store, log=lambda m: None) -> int:
    """Inside the caller's transaction: recompute the rows of the launches whose T+60 / T+24 h snapshot was written
    since the last call (`store.snap_touched`). Each in its own savepoint: a failure is logged, remembered in the
    status (`outcomes_last_error`) and never breaks the block. Returns the number of rows written."""
    tokens = sorted(store.snap_touched)
    store.snap_touched.clear()
    if not tokens or store.user_version() < 9:
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        _remember(store, "-", f"ConfigError: {e}")
        log(f"outcomes: {e}")
        return 0
    n = 0
    for token in tokens:
        store.con.execute("SAVEPOINT outcome")
        try:
            row = compute_row(store, token, cfg)
            if row is not None:
                _store(store, row)
                n += 1
            store.con.execute("RELEASE outcome")
        except Exception as e:  # noqa: BLE001 — a failing label never breaks the block
            store.con.execute("ROLLBACK TO outcome")
            store.con.execute("RELEASE outcome")
            _remember(store, token, f"{type(e).__name__}: {e}"[:300])
            log(f"outcomes: {token}: {type(e).__name__}: {e}")
    return n


def refresh_touched_committed(store: Store, log=lambda m: None) -> int:
    """`refresh_touched` in a transaction of its own — for callers outside the block transaction (the snapshot
    rebuilds after a recovered block)."""
    if not store.snap_touched:
        return 0
    store.begin()
    try:
        n = refresh_touched(store, log)
        store.commit()
        return n
    except BaseException:
        store.rollback()
        raise


def rebuild_outcomes(store: Store, token: str | None = None, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    """Recompute the rows of all launches that have snapshots (or of one), with the config in force. Idempotent apart
    from `computed_at`. One transaction. Raises ConfigError for a broken override."""
    cfg = cfg or load_config()
    q = "SELECT DISTINCT token FROM launch_snapshots" + (" WHERE token = ?" if token else "") + " ORDER BY token"
    out = {"launches": 0, "rows": 0, "final": 0, "rug_fast": 0, "rug_late": 0, "no_rug": 0, "pending": 0, "undefined": 0,
           "price_missing": 0, "price_missing_undefined": 0}
    store.begin()
    try:
        for (t,) in store.con.execute(q, (token,) if token else ()).fetchall():
            out["launches"] += 1
            row = compute_row(store, t, cfg)
            if row is None:
                continue
            _store(store, row)
            out["rows"] += 1
            out["final"] += row["final"]
            out["price_missing"] += row["_price_missing"]
            out["price_missing_undefined"] += row["_price_undefined"]
            if row["rug_fast"]:
                out["rug_fast"] += 1
            elif row["rug_late"]:
                out["rug_late"] += 1
            elif row["rug"] == 0:
                out["no_rug"] += 1
            elif row["final"]:
                out["undefined"] += 1             # final, but rug / no rug cannot be stated (RGY-024)
            else:
                out["pending"] += 1
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return out


def outcome_row(con: Any, token: str) -> dict[str, Any] | None:
    """The stored labels in the shape of `rug_labels` (+ `outcomes`, `config_version`, `computed_at`, `final`), or None
    when nothing is stored. `con` is a sqlite3 connection or a Store."""
    con = getattr(con, "con", con)
    r = con.execute(f"SELECT {','.join(ROW_COLS)} FROM launch_outcomes WHERE token = ?", (token,)).fetchone()
    if r is None:
        return None
    d = dict(zip(ROW_COLS, r))
    ev = json.loads(d.pop("evidence"))
    return {**ev, "config_version": d["config_version"], "config_hash": d["config_hash"],
            "computed_at": d["computed_at"], "final": bool(d["final"]), "insiders_pnl": d["insiders_pnl"]}
