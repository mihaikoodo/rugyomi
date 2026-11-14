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

from .. import accounts
from ..store import Store
from ..store.db import utcnow_iso
from .config import ConfigError, config_hash, load_config
from .context import LaunchContext, load_launch
from .signals import SIGNALS

RULES_VERSION = "0.1"
NOTE = "The live score is not calibrated yet: the weights are provisional."          # only for a profile without its own display.note


def band_of(score: int, cfg: dict[str, Any]) -> str:
    b = cfg["bands"]
    return "High" if score >= b["high"] else "Elevated" if score >= b["elevated"] else "Low"


def assemble(results: list[dict[str, Any]], cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int, str]:
    """The fired results (strongest first), the score (capped at 100) and the band of a list of signal results: the one place that turns results into a card,
    for the live card and for a card scored from stored measures (RGY-050)."""
    fired = sorted((r for r in results if r["fired"]), key=lambda r: (-r["contribution"], r["id"]))
    score = min(100, sum(r["contribution"] for r in fired))
    return fired, score, band_of(score, cfg)


GAP_KIND = "transfers_in"


def _coverage_start(store: Store) -> int | None:
    r = store.con.execute("SELECT MIN(ts_ns) FROM facts").fetchone()
    return r[0] if r else None


def _history_loaded(store: Store, account: str, start: int | None) -> bool:
    """The account's history before the live coverage is in the database: a completed `transfers_in` load, or the account was created
    inside the live coverage (everything it ever received is in the facts)."""
    r = store.con.execute("SELECT status FROM backfill_queue WHERE kind = ? AND key = ?", (GAP_KIND, account)).fetchone()
    if r and r[0] in ("done", "partial"):
        return True
    c = accounts.account_created(store, account)
    return bool(start is not None and c and c.get("ts_ns") is not None and c["ts_ns"] >= start)


def history_gap(store: Store, ctx: LaunchContext, cfg: dict[str, Any]) -> list[dict[str, str]]:
    """RGY-024 (Q-011): for a profile that reads the history (v1), the accounts whose history before the live coverage is NOT loaded yet:
    the creator and the largest pre-launch funder. [{"account", "role"}]; [] when nothing is missing (or the profile does not use history)."""
    if not (cfg.get("context") or {}).get("read_backfill") or store.user_version() < 11 or not ctx.dev:
        return []
    start = _coverage_start(store)
    out = []
    if not _history_loaded(store, ctx.dev, start):
        out.append({"account": ctx.dev, "role": "creator"})
    top = max(ctx.funders(ctx.dev, None, 1.0, 50), key=lambda f: f["near"], default=None)
    if top and top["sender"] != ctx.dev and not _history_loaded(store, top["sender"], start):
        out.append({"account": top["sender"], "role": "largest funder"})
    return [dict(g, since=start) for g in out]


def _gap_note(g: dict[str, Any]) -> str:
    day = time.strftime("%Y-%m-%d", time.gmtime((g["since"] or 0) / 1e9))
    who = "creator's" if g["role"] == "creator" else f"largest funder's ({g['account']})"
    return f"The {who} history before {day} is not loaded yet, so the history-based signals may be incomplete."


def _gap_note_short(g: dict[str, Any]) -> str:
    """The channel post's one-line form of `_gap_note` (RGY-041): the wording lives next to the long note."""
    return "creator's history not loaded yet" if g["role"] == "creator" else "funder's history not loaded yet"


def card_notes(store: Store, ctx: LaunchContext, cfg: dict[str, Any]) -> tuple[list[str], list[str], list[dict[str, str]]]:
    """The card-level notes (long, short) and the history gaps of a launch: what the card says about what it could not see. They do not move the score and depend on the state of the
    database NOW (which histories are loaded), so they are computed for every card and are not part of the stored signal measures."""
    notes, notes_short = [], []
    if ctx.dev is None:
        notes.append("The creator account behind this launch was not known at launch time, so the creator signals were not evaluated.")
        notes_short.append("creator not known at launch")
    gaps = history_gap(store, ctx, cfg)
    notes += [_gap_note(g) for g in gaps]
    notes_short += [_gap_note_short(g) for g in gaps]
    return notes, notes_short, gaps


def compute_card(store: Store, token: str, config: dict[str, Any] | None = None, cache: Any = None, attach: bool = False) -> dict[str, Any]:
    """The card for a launch as of its launch time. Does not write anything. Raises LookupError for an unknown token.
    `cache`: a `passcache.PassCache` for a pass over many launches (RGY-050); None (the live path) reads the database every time.
    `attach` (RGY-054): the card issued at launch time - the creator (and any member the link tables do not know yet) is attached to the linked group from the data known at the launch time,
    because the tables are rebuilt only after the launch. A later `score --token` (attach False) reads the rebuilt tables and gives the same group."""
    cfg = config or load_config()
    launch = load_launch(store, token)
    if launch is None:
        raise LookupError(f"no such launch: {token}")
    t0 = time.perf_counter()
    ctx = LaunchContext(store, launch, cfg, cache, attach)
    results = []
    for sig in SIGNALS:
        c = cfg["signals"].get(sig.id)
        if c is None or not c.get("enabled", True):
            continue                                                     # a profile lists only the signals it uses (RGY-021)
        results.append(sig.compute(ctx, c))
    fired, score, band = assemble(results, cfg)
    notes, notes_short, gaps = card_notes(store, ctx, cfg)
    return {"token": token, "score": score, "band": band, "reasons": fired,
            "signals": {"results": results, "notes": notes, "notes_short": notes_short}, "dev_account": ctx.dev, "history_gap": gaps,
            "config_version": cfg["version"], "config_hash": config_hash(cfg),
            "rules_version": cfg.get("rules_version", RULES_VERSION), "launch_ts_ns": launch["ts_ns"], "computed_ms": int((time.perf_counter() - t0) * 1000),
            "launch_height": launch["height"], "coverage": launch["coverage"] or "live"}


def issue_card(store: Store, token: str, height: int, config: dict[str, Any] | None = None) -> bool:
    """Compute and store the card inside the caller's transaction. False if the token already has one."""
    if store.con.execute("SELECT 1 FROM cards WHERE token = ?", (token,)).fetchone():
        return False
    c = compute_card(store, token, config, attach=True)          # RGY-054: the group of the creator at launch time, before the link tables are rebuilt
    cur = store.con.execute(
        "INSERT OR IGNORE INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, "
        "dev_account, config_version, config_hash, rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (token, utcnow_iso(), height, c["launch_ts_ns"], c["score"], c["band"],
         json.dumps(c["reasons"], separators=(",", ":")), json.dumps(c["signals"], separators=(",", ":")),
         c["dev_account"], c["config_version"], c["config_hash"], c["rules_version"], c["computed_ms"]))
    if cur.rowcount == 1:
        _enqueue_gaps(store, c["history_gap"], c["launch_ts_ns"])
    return cur.rowcount == 1


def _enqueue_gaps(store: Store, gaps: list[dict[str, Any]], ts_ns: int) -> None:
    """Q-011: the accounts with a missing history go to the backfill queue at once (the autofill worker, when it is on, loads them
    newest first); INSERT OR IGNORE, so an account already queued keeps its status."""
    if gaps and store.user_version() >= 11:
        for g in gaps:
            store.con.execute("INSERT OR IGNORE INTO backfill_queue(kind, key, status, ts_ns, queued_at) VALUES(?,?,?,?,?)",
                              (GAP_KIND, g["account"], "pending", ts_ns, utcnow_iso()))


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
