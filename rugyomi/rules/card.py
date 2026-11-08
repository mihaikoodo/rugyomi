"""Risk cards (RGY-015): compute, issue (immutable) and read.

`compute_card(store, token)` is pure with respect to the database: it never writes (the `score` CLI uses it for research
and backtests). `issue_for_height(store, height)` is called by the collector inside the block transaction, after the
launches of the block were detected and the snapshots due at the block time were computed — in that order the card sees
exactly the snapshots that are knowable at the launch time, so a later `score --token` gives the same card. A card is
written with INSERT OR IGNORE and the table has triggers that refuse UPDATE / DELETE: a card is never changed.

Only live launches (coverage NULL) get a card. A launch whose dev account is unknown at that moment (a launch bot payment
not found yet) still gets a card; the signals that need the dev are silent and the card says so in `notes`.
"""
from __future__ import annotations

import json
import time
from typing import Any

from ..store import Store
from ..store.db import utcnow_iso
from .config import ConfigError, config_hash, load_config
from .context import LaunchContext, load_launch
from .signals import SIGNALS

RULES_VERSION = "0.1"
NOTE = "v0 — weights are provisional; calibration in progress."


def band_of(score: int, cfg: dict[str, Any]) -> str:
    b = cfg["bands"]
    return "High" if score >= b["high"] else "Elevated" if score >= b["elevated"] else "Low"


def compute_card(store: Store, token: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The card for a launch as of its launch time. Does not write anything. Raises LookupError for an unknown token."""
    cfg = config or load_config()
    launch = load_launch(store, token)
    if launch is None:
        raise LookupError(f"no such launch: {token}")
    t0 = time.perf_counter()
    ctx = LaunchContext(store, launch, cfg)
    results = []
    for sig in SIGNALS:
        c = cfg["signals"].get(sig.id)
        if c is None or not c.get("enabled", True):
            continue                                                     # a profile lists only the signals it uses (RGY-021)
        results.append(sig.compute(ctx, c))
    fired = sorted((r for r in results if r["fired"]), key=lambda r: (-r["contribution"], r["id"]))
    score = min(100, sum(r["contribution"] for r in fired))
    notes = []
    if ctx.dev is None:
        notes.append("The creator account behind this launch was not known at launch time, so the creator signals were not evaluated.")
    return {"token": token, "score": score, "band": band_of(score, cfg), "reasons": fired,
            "signals": {"results": results, "notes": notes}, "dev_account": ctx.dev,
            "config_version": cfg["version"], "config_hash": config_hash(cfg),
            "rules_version": cfg.get("rules_version", RULES_VERSION), "launch_ts_ns": launch["ts_ns"], "computed_ms": int((time.perf_counter() - t0) * 1000),
            "launch_height": launch["height"], "coverage": launch["coverage"] or "live"}


def issue_card(store: Store, token: str, height: int, config: dict[str, Any] | None = None) -> bool:
    """Compute and store the card inside the caller's transaction. False if the token already has one."""
    if store.con.execute("SELECT 1 FROM cards WHERE token = ?", (token,)).fetchone():
        return False
    c = compute_card(store, token, config)
    cur = store.con.execute(
        "INSERT OR IGNORE INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, "
        "dev_account, config_version, config_hash, rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (token, utcnow_iso(), height, c["launch_ts_ns"], c["score"], c["band"],
         json.dumps(c["reasons"], separators=(",", ":")), json.dumps(c["signals"], separators=(",", ":")),
         c["dev_account"], c["config_version"], c["config_hash"], c["rules_version"], c["computed_ms"]))
    return cur.rowcount == 1


def issue_for_height(store: Store, height: int, log=lambda m: None) -> int:
    """Cards for the live launches detected at `height` that have none yet. Each in its own savepoint: a failing card
    is logged, remembered in the status (`cards_last_error`) and never breaks the block."""
    if store.user_version() < 7:
        return 0
    n = 0
    rows = store.con.execute(
        "SELECT l.token FROM launches l WHERE l.height = ? AND l.coverage IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM cards c WHERE c.token = l.token) ORDER BY l.ts_ns", (height,)).fetchall()
    if not rows:
        return 0
    try:
        cfg = load_config()
    except ConfigError as e:
        _remember(store, "-", f"ConfigError: {e}")
        log(f"cards: {e}")
        return 0
    for (token,) in rows:
        store.con.execute("SAVEPOINT card")
        try:
            n += int(issue_card(store, token, height, cfg))
            store.con.execute("RELEASE card")
        except Exception as e:  # noqa: BLE001 — a failing card never breaks the block
            store.con.execute("ROLLBACK TO card")
            store.con.execute("RELEASE card")
            _remember(store, token, f"{type(e).__name__}: {e}"[:300])
            log(f"cards: {token}: {type(e).__name__}: {e}")
    return n


def _remember(store: Store, token: str, error: str) -> None:
    store.set_status("cards_last_error", {"token": token, "error": error, "at": utcnow_iso()})


def card_row(con: Any, token: str) -> dict[str, Any] | None:
    """The stored card (reasons / signals decoded) or None. `con` is a sqlite3 connection or a Store."""
    con = getattr(con, "con", con)
    r = con.execute("SELECT token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, "
                    "config_version, config_hash, rules_version, computed_ms FROM cards WHERE token = ?", (token,)).fetchone()
    if r is None:
        return None
    d = dict(zip(("token", "issued_at", "issued_height", "launch_ts_ns", "score", "band", "reasons", "signals",
                  "dev_account", "config_version", "config_hash", "rules_version", "computed_ms"), r))
    d["reasons"] = json.loads(d["reasons"])
    d["signals"] = json.loads(d["signals"])
    return d
