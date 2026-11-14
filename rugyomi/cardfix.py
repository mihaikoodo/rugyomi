"""Cards issued before a fix (RGY-056): which issued cards a later code fix would have scored differently, and by how much.

An issued card is never changed (`cards` refuses UPDATE / DELETE) and a channel post is never edited. When a bug in the card path is found and fixed, the cards
issued before the fix keep their score - that is the track record. This module computes, for each fix in `FIXES`, the list of those cards that the fixed code
scores higher on the signals the fix touched, and the site shows it as a note under the card. The list is DERIVED and rebuildable (`card_fixes`, schema v21;
the CLI `rebuild-card-fixes`), never typed by hand:

  * the cards issued before the fix went live (`deployed_at`, from the deploy log);
  * the as-of recomputation of each of them comes from the as-of feature store (RGY-050: `rules.features`, filled for these launches first, so it is the very
    `compute_card` of today's data as of the launch time) under the rules profile of the card's rules version - the config whose hash the card carries
    (with the server override, else the shipped defaults; a card whose config is neither is reported, scored with the profile's effective config);
  * only the fix's signals are compared: N = (their as-of points) - (their issued points); a card is listed when N > 0. A card where they are lower is reported
    by the CLI and not listed (the fix can only add what the old path missed). The would-be score is the ISSUED score + N (capped at 100) with the band of the card's
    config: other signals that moved with data loaded later are not the fix and are not counted.

Readers: the launch page (a note under the issued card) and /accuracy (one line with the list). NOT the channel, NOT `rules.accuracy`, NOT the rules context: the
accuracy table keeps every card as issued (a test scans for that).
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable

from .store import Store
from .store.db import utcnow_iso

# The fixes of the card path whose earlier cards are marked. `deployed_at`: the restart of the collector with the fix (data/deploy/push.txt), UTC.
FIXES: dict[str, dict[str, Any]] = {
    "creator_links": {
        "task": "RGY-054",
        "deployed_at": "2026-11-11T21:39:26Z",
        # the signals that read the creator's linked group (`LaunchContext.group`), which the card did not see before the fix
        "signals": ("creator_track_record", "funded_by_rugger", "serial_launcher"),
        "what": "creator links",
        "added": "the creator's links would have added",
        "text": "A card issued at launch time did not yet see the links of a new creator to an existing network of accounts: the link tables are rebuilt "
                "only after a launch. Since the fix the card attaches the creator to that network from the data known at launch time.",
    },
}
COLS = ("token", "fix", "deployed_at", "issued_at", "issued_score", "issued_band", "points", "score", "band", "signals", "profile",
        "config_version", "config_match", "computed_at")


def has_table(con: Any) -> bool:
    con = getattr(con, "con", con)
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'card_fixes'").fetchone() is not None


def _ns(iso: str) -> int:
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 10**9)


def _card_config(card: dict[str, Any], profile: str) -> tuple[dict[str, Any], bool]:
    """The config a card was issued with: the profile with the server override (what the collector loads), else the shipped defaults; matched by the card's hash.
    (cfg, matched): when neither matches, the effective config of the profile and False."""
    from .rules.config import ConfigError, config_hash, default_config, load_config
    try:
        cands = [load_config(profile=profile)]
    except ConfigError:
        cands = []
    cands.append(default_config(profile))
    for c in cands:
        if config_hash(c) == card["config_hash"]:
            return c, True
    return cands[0], False


def compute(store: Store, fix: str = "creator_links", log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """The rows of `fix` (nothing written to `card_fixes`; the feature store is filled for the cards issued before the fix) and what was checked.
    {"rows": [...], "checked": n cards issued before the fix, "lower": [(token, N)], "unmatched_config": [tokens], "missing": [tokens without a stored measure]}."""
    from . import rules
    from .rules import features as FT
    from .rules.card import band_of, card_row

    f = FIXES[fix]
    cut = _ns(f["deployed_at"])
    tokens = [t for t, at in store.con.execute("SELECT token, issued_at FROM cards ORDER BY launch_ts_ns, token") if _ns(at) < cut]
    cards = {t: card_row(store.con, t) for t in tokens}
    by_prof: dict[tuple[str, str], tuple[dict[str, Any], bool, list[str]]] = {}
    for t, c in cards.items():
        prof = rules.profile_of(c["rules_version"])
        cfg, ok = _card_config(c, prof)
        key = (prof, c["config_hash"])
        by_prof.setdefault(key, (cfg, ok, []))[2].append(t)
    out: dict[str, Any] = {"fix": fix, "deployed_at": f["deployed_at"], "checked": len(tokens), "rows": [], "lower": [], "unmatched_config": [], "missing": [],
                           "profiles": {}}
    now = utcnow_iso()
    for (prof, h), (cfg, ok, toks) in sorted(by_prof.items()):
        out["profiles"][f"{prof} {cfg['version']}"] = len(toks)
        if not ok:
            out["unmatched_config"] += toks
        # RGY-050: the store is filled (re-measured) for exactly these launches, so the as-of values are today's data as of the launch time
        FT.fill(store, [cfg], tokens=toks, recompute_all=True, log=log)
        feats = FT.Features(store)
        plan = FT.profile_plan(cfg)
        for t in toks:
            c = cards[t]
            asof = FT.score_launch(feats, t, cfg, plan)
            if asof is None:
                out["missing"].append(t)
                continue
            mine = {r["id"]: r for r in c["signals"]["results"]}
            now_r = {r["id"]: r for r in asof["signals"]["results"]}
            sigs = []
            for sid in f["signals"]:
                a, b = mine.get(sid), now_r.get(sid)
                if a is None or b is None:
                    continue
                if a["contribution"] != b["contribution"]:
                    sigs.append({"id": sid, "issued": a["contribution"], "asof": b["contribution"], "reason": b.get("reason") or "",
                                 "evidence": b.get("evidence") or []})
            n = sum(s["asof"] - s["issued"] for s in sigs)
            if n < 0:
                out["lower"].append((t, n))
            if n <= 0:
                continue
            score = min(100, c["score"] + n)
            out["rows"].append({"token": t, "fix": fix, "deployed_at": f["deployed_at"], "issued_at": c["issued_at"], "issued_score": c["score"],
                                "issued_band": c["band"], "points": n, "score": score, "band": band_of(score, cfg), "signals": sigs, "profile": prof,
                                "config_version": c["config_version"], "config_match": int(ok), "computed_at": now, "asof_score": asof["score"]})
    out["rows"].sort(key=lambda r: (r["issued_at"], r["token"]))
    return out


def rebuild(store: Store, fix: str = "creator_links", log: Callable[[str], None] = lambda m: None) -> dict[str, Any]:
    """Compute the list of `fix` and replace its rows in `card_fixes` in one transaction. Writes only `card_fixes` (and the feature store, via `compute`)."""
    if not has_table(store):
        raise RuntimeError("card_fixes: the database is not migrated to schema v21 yet")
    res = compute(store, fix, log)
    store.begin()
    try:
        store.con.execute("DELETE FROM card_fixes WHERE fix = ?", (fix,))
        store.con.executemany(f"INSERT INTO card_fixes({','.join(COLS)}) VALUES({','.join('?' * len(COLS))})",
                              [[json.dumps(r[k], separators=(",", ":")) if k == "signals" else r[k] for k in COLS] for r in res["rows"]])
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return res


# ---------------------------------------------------------------- readers (the site; nothing computed)

def _row(r: tuple[Any, ...]) -> dict[str, Any]:
    d = dict(zip(COLS, r))
    d["signals"] = json.loads(d["signals"])
    f = FIXES.get(d["fix"], {})
    d["what"] = f.get("what", d["fix"])
    d["date"] = d["deployed_at"][:10]
    pts = f"{d['points']} point{'s' if d['points'] != 1 else ''}"
    d["text"] = (f"Issued before a fix of {d['what']} ({d['date']}): {f.get('added', 'the fixed code would have added')} {pts} "
                 f"(score {d['score']}, band {d['band']}).")
    d["explain"] = f.get("text", "")
    d["kept"] = "The card above stays as it was issued; the accuracy table counts it as issued."
    return d


def card_fix(con: Any, token: str) -> dict[str, Any] | None:
    """The note of an issued card (the first fix by date), or None. Read only."""
    con = getattr(con, "con", con)
    if not has_table(con):
        return None
    r = con.execute(f"SELECT {','.join(COLS)} FROM card_fixes WHERE token = ? ORDER BY deployed_at LIMIT 1", (token,)).fetchone()
    return _row(r) if r else None


def summary(con: Any) -> dict[str, Any] | None:
    """The /accuracy line: {"n", "text", "rows"} over every listed card, or None when there are none. Read only; the accuracy numbers are not touched."""
    con = getattr(con, "con", con)
    if not has_table(con):
        return None
    rows = [_row(r) for r in con.execute(f"SELECT {','.join(COLS)} FROM card_fixes ORDER BY issued_at, token")]
    if not rows:
        return None
    what = sorted({r["what"] for r in rows})
    n = len(rows)
    return {"n": n, "rows": rows,
            "text": f"{n} card{'s were' if n != 1 else ' was'} issued before a fix of {' and '.join(what)}; they are counted as issued. "
                    "Their launch pages say what the fixed code would have scored."}
