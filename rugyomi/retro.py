"""Retroactive risk cards (RGY-027): a card AS OF THE LAUNCH TIME for a launch that never got a live card (a history launch).

A retro card is NOT a forecast that was published at the time. It is computed afterwards, by the very same `compute_card` the live cards and
the backtest (`rules.backtest.asof_rows`) use - one copy of the logic - with the v1 profile, and it sees only what was knowable at the launch
time (the no-lookahead guarantee of RGY-021 is in `compute_card`, not here). It lives in its OWN table, `retro_cards` (schema v15):
  * derived and rebuildable: `rebuild_retro_cards` / the CLI `rebuild-retro-cards`; the site never writes it;
  * never mixed into `cards` (the immutable, published forecasts), and never read by the publisher, by `rules.accuracy` or by the rules
    context (a test scans for that);
  * `in_sample` = the launch is older than the end of the data the v1 weights were fitted on (`fit.train_until` of the v1 config), so its score
    is not an out-of-sample result.

Which launches: every launch with a `launches` row and NO live card. Rebuild is resumable (a row with the same config version and hash is kept)
and it commits in small batches, so it never holds the collector's writer for long.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from .progress import ticker
from .rules import features as FT
from .rules.card import compute_card
from .rules.passcache import PassCache
from .rules.config import ConfigError, config_hash, load_config, train_until_ns
from .store import Store
from .store.db import utcnow_iso

PROFILE = "v1"
BATCH = 50
BANNER = ("This card was computed retroactively as of the launch time (rules v1) — it is not a forecast published at the time.")
TITLE = "computed retroactively as of the launch time (rules v1) \u2014 not a forecast published at the time"
COLS = ("token", "rules_version", "config_version", "config_hash", "launch_ts_ns", "score", "band", "reasons", "signals", "notes",
        "in_sample", "computed_at")


def has_table(con: Any) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'retro_cards'").fetchone() is not None


def eligible(store: Store, only: list[str] | None = None) -> list[tuple[str, int]]:
    """(token, launch ts) of every launch that has no live card, oldest first."""
    if store.user_version() < 7:
        rows = store.con.execute("SELECT l.token, l.ts_ns FROM launches l ORDER BY l.ts_ns, l.token").fetchall()
    else:
        rows = store.con.execute("SELECT l.token, l.ts_ns FROM launches l WHERE NOT EXISTS (SELECT 1 FROM cards c WHERE c.token = l.token) "
                                 "ORDER BY l.ts_ns, l.token").fetchall()
    if only is not None:
        want = set(only)
        rows = [r for r in rows if r[0] in want]
    return [(t, ts) for t, ts in rows]


def retro_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """The config retro cards are computed with: the v1 profile (+ the server override), whatever profile the live cards use."""
    return cfg or load_config(profile=PROFILE)


def _card_of(store: Store, token: str, cfg: dict[str, Any], stored: tuple[Any, Any, Any] | None) -> tuple[dict[str, Any], bool]:
    """The card of a launch and whether it was scored from the stored measures. `stored`: (Features, plan, PassCache) of the as-of feature store (RGY-050): the card is then the one
    `compute_card` gives (equal, tested), and a launch without all its measures is computed."""
    c = FT.card_from_store(store, stored[0], token, cfg, stored[1], stored[2]) if stored else None
    return (c, True) if c is not None else (compute_card(store, token, cfg), False)


def build_row(store: Store, token: str, cfg: dict[str, Any], now: str | None = None, stored: tuple[Any, Any, Any] | None = None) -> dict[str, Any]:
    """The `retro_cards` row of a launch (no write). Raises what `compute_card` raises."""
    return _row(_card_of(store, token, cfg, stored)[0], cfg, now)


def _row(c: dict[str, Any], cfg: dict[str, Any], now: str | None) -> dict[str, Any]:
    cut = train_until_ns(cfg)
    return {"token": c["token"], "rules_version": c["rules_version"], "config_version": c["config_version"], "config_hash": c["config_hash"],
            "launch_ts_ns": c["launch_ts_ns"], "score": c["score"], "band": c["band"],
            "reasons": json.dumps(c["reasons"], separators=(",", ":")), "signals": json.dumps(c["signals"], separators=(",", ":")),
            "notes": json.dumps(c["signals"].get("notes", []), separators=(",", ":")),
            "in_sample": int(cut is not None and c["launch_ts_ns"] < cut), "computed_at": now or utcnow_iso()}


def _write(store: Store, rows: list[dict[str, Any]]) -> None:
    store.begin()
    try:
        for r in rows:
            store.con.execute("DELETE FROM retro_cards WHERE token = ? AND config_version <> ?", (r["token"], r["config_version"]))
            store.con.execute(f"INSERT OR REPLACE INTO retro_cards({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
                              [r[k] for k in COLS])
        store.commit()
    except BaseException:
        store.rollback()
        raise


def rebuild_retro_cards(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None,
                        only: list[str] | None = None, limit: int | None = None, force: bool = False, use_store: bool = True, fresh_only: bool = False) -> dict[str, Any]:
    """Compute the retro card of every launch without a live card. Resumable: a row with the same config version and hash is kept
    (`force` recomputes it). A launch whose card fails is listed in `failed_tokens` and left without a row. Returns the counts."""
    t0 = time.time()
    cfg = retro_config(cfg)
    if store.user_version() < 15:
        raise RuntimeError("retro_cards: the database is not migrated to schema v15 yet")
    ver, h = cfg["version"], config_hash(cfg)
    have = {t: (v, hh) for t, v, hh in store.con.execute("SELECT token, config_version, config_hash FROM retro_cards")}
    items = eligible(store, only)
    out: dict[str, Any] = {"launches_without_live_card": len(items), "written": 0, "kept": 0, "failed": 0, "failed_tokens": [],
                           "pruned": 0, "in_sample": 0, "config_version": ver, "config_hash": h}
    todo = []
    for t, ts in items:
        if not force and have.get(t) == (ver, h):
            out["kept"] += 1
        else:
            todo.append(t)
    if limit is not None:
        todo = todo[:limit]
    batch: list[dict[str, Any]] = []
    tick = ticker(log, "retro cards", len(todo))
    stored = None
    if use_store and FT.available(store):
        try:
            stored = (FT.Features(store, FT.data_mark(store) if fresh_only else None), FT.profile_plan(cfg), PassCache(store))
            out["stale_rows"] = sum(t["stale"] for t in FT.status(store, [cfg])["tasks"])
        except Exception:  # noqa: BLE001 - the store only speeds this up
            stored = None
    out["from_store"] = 0
    for i, t in enumerate(todo):
        try:
            c, fast = _card_of(store, t, cfg, stored)
            batch.append(_row(c, cfg, None))
            out["from_store"] += int(fast)
        except Exception as e:  # noqa: BLE001 - one launch must not stop the rebuild
            out["failed"] += 1
            out["failed_tokens"].append(f"{t}: {type(e).__name__}: {str(e)[:120]}")
        if len(batch) >= BATCH or (i == len(todo) - 1 and batch):
            _write(store, batch)
            out["written"] += len(batch)
            out["in_sample"] += sum(r["in_sample"] for r in batch)
            batch = []
            tick(i + 1)
    # a token that has a live card (or no launch) must not keep a retro row
    store.begin()
    try:
        cur = store.con.execute("DELETE FROM retro_cards WHERE token IN (SELECT token FROM cards) "
                                "OR token NOT IN (SELECT token FROM launches)")
        out["pruned"] = cur.rowcount
        store.commit()
    except BaseException:
        store.rollback()
        raise
    out["total_rows"] = store.con.execute("SELECT COUNT(*) FROM retro_cards").fetchone()[0]
    out["seconds"] = round(time.time() - t0, 1)
    return out


# ---------------------------------------------------------------- reading (the site)

def _decode(r: tuple) -> dict[str, Any]:
    d = dict(zip(COLS, r))
    d["reasons"] = json.loads(d["reasons"])
    d["signals"] = json.loads(d["signals"])
    d["notes"] = json.loads(d["notes"])
    d["in_sample"] = bool(d["in_sample"])
    d["retro"] = True
    return d


def retro_row(con: Any, token: str) -> dict[str, Any] | None:
    """The retro card of a launch (the newest row), decoded, or None. `con` is a sqlite3 connection or a Store."""
    con = getattr(con, "con", con)
    if not has_table(con):
        return None
    r = con.execute(f"SELECT {','.join(COLS)} FROM retro_cards WHERE token = ? ORDER BY computed_at DESC LIMIT 1", (token,)).fetchone()
    return _decode(r) if r else None


def retro_scores(con: Any, tokens: list[str]) -> dict[str, tuple[int, str, bool]]:
    """token -> (score, band, in_sample) for the tokens that have a retro card."""
    con = getattr(con, "con", con)
    out: dict[str, tuple[int, str, bool]] = {}
    if not tokens or not has_table(con):
        return out
    for i in range(0, len(tokens), 400):
        part = tokens[i:i + 400]
        q = ",".join("?" * len(part))
        for t, sc, b, ins in con.execute(f"SELECT token, score, band, in_sample FROM retro_cards WHERE token IN ({q}) ORDER BY computed_at", part):
            out[t] = (sc, b, bool(ins))
    return out


def attach(con: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For the rows with NO live score (`score` is None): the retro score and band, marked `retro` = True (and `retro_in_sample`). A row
    that has a live card is left alone. Used by every list that shows a score, so a retro score is never shown without its mark."""
    need = [r["token"] for r in rows if r.get("score") is None and r.get("token")]
    got = retro_scores(con, need)
    for r in rows:
        r.setdefault("retro", False)
        g = got.get(r.get("token")) if r.get("score") is None else None
        if g:
            r["score"], r["band"], r["retro"], r["retro_in_sample"] = g[0], g[1], True, g[2]
    return rows


__all__ = ["rebuild_retro_cards", "retro_row", "retro_scores", "attach", "eligible", "build_row", "BANNER", "ConfigError"]
