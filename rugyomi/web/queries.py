"""Read-only queries of the site. Every query is parameterized and uses an existing index. One place for the SQL."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import accounts, history, labels
from .. import dossier as dossier_mod
from .. import links as links_mod
from .. import launches as launches_mod
from ..rules.accuracy import outcome_state
from ..rules.backtest import chance_for_score
from ..rules import accuracy_summary, card_outcome_rows, card_row, outcome_row
from ..store import Store

SNAP_FIELDS = ("minute", "at_ts_ns", "n_swaps", "price_last", "max_x", "dev_acquired", "dev_sold", "dev_sold_share_bps",
               "dev_transferred_out", "dev_first_sell_after_s", "buyers_count", "buyers_spent", "buyers_received",
               "buyers_pnl", "holders_count", "top10_share_bps", "error")


V8_FIELDS = ("metric_version", "price_peak", "peak_at", "insiders_count", "insiders_acquired", "insiders_sold",
             "insiders_sold_share_bps", "insiders_pnl", "insiders_evidence")


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def feed(con: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    """The latest LIVE launches (coverage NULL) with the T+5 and T+60 snapshot metrics, newest first."""
    cov = "l.coverage IS NULL" if _has_col(con, "launches", "coverage") else "1 = 1"
    has_cards = _has(con, "cards")
    rows = con.execute(
        "SELECT l.token, l.launchpad, l.creator, l.owner, l.dev_buy, l.height, l.ts_ns, l.extra, "
        "s5.dev_sold_share_bps, s5.dev_first_sell_after_s, s5.buyers_pnl, s5.max_x, s5.holders_count, s5.error, "
        "s60.dev_sold_share_bps, s60.dev_first_sell_after_s, s60.buyers_pnl, s60.max_x, s60.holders_count, s60.error, "
        + ("c.score, c.band " if has_cards else "NULL, NULL ") +
        "FROM launches l LEFT JOIN launch_snapshots s5 ON s5.token = l.token AND s5.minute = 5 "
        "LEFT JOIN launch_snapshots s60 ON s60.token = l.token AND s60.minute = 60 "
        + ("LEFT JOIN cards c ON c.token = l.token " if has_cards else "") +
        f"WHERE {cov} ORDER BY l.ts_ns DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        extra = json.loads(r[7]) if r[7] else {}
        s5 = {"dev_sold_share_bps": r[8], "first_sell_s": r[9], "buyers_pnl": r[10], "max_x": r[11], "holders": r[12], "error": r[13]}
        s60 = {"dev_sold_share_bps": r[14], "first_sell_s": r[15], "buyers_pnl": r[16], "max_x": r[17], "holders": r[18], "error": r[19]}
        out.append({"token": r[0], "launchpad": r[1], "creator": r[2], "owner": r[3], "dev_buy": r[4], "height": r[5],
                    "ts_ns": r[6], "symbol": extra.get("symbol"), "score": r[20], "band": r[21],
                    "s5": s5 if r[8] is not None or r[13] else None,
                    "s60": s60 if r[14] is not None or r[19] else None})
    return out


def _has_col(con: sqlite3.Connection, table: str, col: str) -> bool:
    return any(r[1] == col for r in con.execute(f"PRAGMA table_info({table})"))


def launch(con: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    cov = "coverage" if _has_col(con, "launches", "coverage") else "NULL"
    r = con.execute(f"SELECT token, launchpad, creator, owner, owner_evidence, pool, dev_buy, height, ts_ns, tx, extra, {cov} "
                    "FROM launches WHERE token = ?", (token,)).fetchone()
    if r is None:
        return None
    d = dict(zip(("token", "launchpad", "creator", "owner", "owner_evidence", "pool", "dev_buy", "height", "ts_ns", "tx",
                  "extra", "coverage"), r))
    d["extra"] = json.loads(d["extra"]) if d["extra"] else {}
    d["symbol"] = d["extra"].get("symbol")
    d["coverage"] = d["coverage"] or "live"
    return d


def card(con: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    """The stored risk card of a launch, or None (history launch, or launched before cards existed)."""
    return card_row(con, token) if _has(con, "cards") else None


def tax_options(con: sqlite3.Connection, launch: dict[str, Any]) -> dict[str, Any] | None:
    return launches_mod.tax_options(con, launch)


def snapshots(con: sqlite3.Connection, token: str) -> list[dict[str, Any]]:
    """The snapshot rows of a launch; the v8 columns (insiders, peak) only when the database has them."""
    fields = SNAP_FIELDS + (V8_FIELDS if _has_col(con, "launch_snapshots", "insiders_count") else ())
    return [dict(zip(fields, r)) for r in con.execute(
        f"SELECT {','.join(fields)} FROM launch_snapshots WHERE token = ? ORDER BY minute", (token,))]


def outcome(con: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    """The stored outcome labels of a launch (written by the collector with the config in force), or None when there is
    no row yet or the database is older than v9. The site never computes a label."""
    return outcome_row(con, token) if _has(con, "launch_outcomes") else None


def accuracy(con: sqlite3.Connection) -> dict[str, Any]:
    """The /accuracy tables: the frozen cards against the stored outcomes (read-only, nothing computed from snapshots)."""
    out = accuracy_summary(card_outcome_rows(con))
    out["history"] = history_calibration(con)
    return out


def history_calibration(con: sqlite3.Connection) -> dict[str, Any] | None:
    """The precomputed backtest numbers (RGY-021 step 4: `collector_status.calibration_history`, written by a private diag after the
    holdout run), or None. HOLDOUT only; the site never computes them."""
    if not _has(con, "collector_status"):
        return None
    row = con.execute("SELECT value FROM collector_status WHERE key = 'calibration_history'").fetchone()
    if not row or not row[0]:
        return None
    try:
        v = json.loads(row[0])
    except ValueError:
        return None
    return v if isinstance(v, dict) and "profiles" in v else None


def chance(con: sqlite3.Connection, card: dict[str, Any] | None) -> dict[str, Any] | None:
    """"Rug chance ≈ X %" for a v1 card: the calibration block of its score. Only a card issued by rules v1 gets one (v0 cards stay
    uncalibrated: a v0 score is not a probability)."""
    if not card or not str(card.get("rules_version", "")).startswith("1"):
        return None
    return chance_for_score(history_calibration(con), int(card["score"]), "v1")


def _with_forecasts(con: sqlite3.Connection, launches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adds `score`, `band` (from the stored card) and `state` (rug / no rug / waiting / None) to launch rows."""
    if not launches or not _has(con, "cards"):
        return launches
    toks = [r["token"] for r in launches]
    cards: dict[str, tuple[int, str]] = {}
    outs: dict[str, dict[str, Any]] = {}
    has_o = _has(con, "launch_outcomes")
    for i in range(0, len(toks), 400):
        part = toks[i:i + 400]
        q = ",".join("?" * len(part))
        for t, sc, b in con.execute(f"SELECT token, score, band FROM cards WHERE token IN ({q})", part):
            cards[t] = (sc, b)
        if has_o:
            for t, rug, fin in con.execute(f"SELECT token, rug, final FROM launch_outcomes WHERE token IN ({q})", part):
                outs[t] = {"rug": rug, "final": fin}
    for r in launches:
        sc = cards.get(r["token"])
        r["score"], r["band"] = (sc if sc else (None, None))
        o = outs.get(r["token"])
        if o:
            r["state"] = outcome_state({"rug": o["rug"], "final": o["final"]})
        else:
            r["state"] = "waiting" if r.get("coverage") in (None, "live") else None
    return launches


def creator_page(store: Store, account: str) -> dict[str, Any]:
    con = store.con
    created = accounts.account_created(con, account)
    lab = labels.active_labels(con, account) if _has(con, "manual_labels") else []
    return {"account": account, "created": created, "labels": lab,
            "launches": _with_forecasts(con, history.creator_launches(store, account))}


MEMBER_LAUNCHES = 8          # launches listed per linked account on the creator page
MEMBERS_WITH_LAUNCHES = 30


def creator_links(store: Store, account: str) -> dict[str, Any]:
    """The linked accounts and funding clusters of an account for the creator page (derived tables, read only)."""
    con = store.con
    ln = links_mod.account_links(con, account)
    ln["coverage"] = links_mod.coverage(con)
    g = ln.get("group")
    if g:
        for m in g["members"][:MEMBERS_WITH_LAUNCHES]:
            rows = history.creator_launches(store, m["account"])
            m["launch_rows"] = _with_forecasts(con, rows[-MEMBER_LAUNCHES:][::-1])
            m["launch_rows_total"] = len(rows)
    return ln


def cluster(con: sqlite3.Connection, source: str) -> dict[str, Any] | None:
    """A funding cluster with its members, their launches (score / outcome / the buyers' loss at T+60) and totals."""
    c = links_mod.cluster_page(con, source)
    if c is None:
        return None
    rows = [{"token": m["launch_token"], "coverage": "live"} for m in c["members"] if m["launch_token"]]
    fc = {r["token"]: r for r in _with_forecasts(con, rows)}
    loss = {}
    if _has(con, "launch_snapshots"):
        for t, pnl in con.execute("SELECT token, buyers_pnl FROM launch_snapshots WHERE minute = ? AND error IS NULL "
                                  "AND token IN (%s)" % ",".join("?" * len(fc)), (links_mod.RANKING_MINUTE, *fc)) if fc else []:
            loss[t] = pnl
    for m in c["members"]:
        f = fc.get(m["launch_token"], {})
        m["score"], m["band"], m["state"] = f.get("score"), f.get("band"), f.get("state")
        m["buyers_pnl"] = loss.get(m["launch_token"])
    return c


def rankings(con: sqlite3.Connection, window: str = "all", launchpad: str | None = None) -> dict[str, Any]:
    """RGY-023: groups, clusters and clean creators over the whole history (the dossier tables); until they are built, the older
    live-only ranking of the link tables."""
    r = dossier_mod.rankings(con, window, launchpad)
    if r["built"]:
        return r
    old = links_mod.rankings(con)
    old["legacy"] = True
    return {**r, "legacy": True, "old": old}


def dossier(con: sqlite3.Connection, account: str, limit: int = 100) -> dict[str, Any]:
    return dossier_mod.dossier(con, account, limit)


def known_account(con: sqlite3.Connection, account: str) -> bool:
    """An account page exists when we know anything about it: a launch, an index row, a creation, a label."""
    if con.execute("SELECT 1 FROM launches WHERE creator = ? OR owner = ? LIMIT 1", (account, account)).fetchone():
        return True
    if _has(con, "launch_index") and con.execute("SELECT 1 FROM launch_index WHERE creator = ? LIMIT 1", (account,)).fetchone():
        return True
    if _has(con, "hist_launches") and con.execute("SELECT 1 FROM hist_launches WHERE creator = ? LIMIT 1", (account,)).fetchone():
        return True
    if _has(con, "manual_labels") and con.execute("SELECT 1 FROM manual_labels WHERE account = ? LIMIT 1", (account,)).fetchone():
        return True
    if _has(con, "link_clusters") and con.execute("SELECT 1 FROM link_clusters WHERE source = ? LIMIT 1", (account,)).fetchone():
        return True                                        # a funding source: the cluster page links here
    return accounts.account_created(con, account) is not None


def health(con: sqlite3.Connection) -> dict[str, Any]:
    st: dict[str, Any] = {}
    for k, v in con.execute("SELECT key, value FROM collector_status WHERE key IN ('last_height','last_block_ts_ns')"):
        try:
            st[k] = json.loads(v)
        except (TypeError, ValueError):
            st[k] = None
    return {"schema": con.execute("PRAGMA user_version").fetchone()[0], "last_height": st.get("last_height"),
            "last_block_ts_ns": st.get("last_block_ts_ns")}
