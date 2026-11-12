"""The as-of feature store (RGY-050): measure once, score any profile in seconds.

A signal is `measure` (reads the database as of the launch, returns a plain JSON dict) + `score` (points, reason, evidence; no database) - see `signals.py`. The measure depends on the
launch and on a few settings, not on the points: so it is stored once per (launch, signal, `feature_key`) in `signal_features` (schema v19) and any rules profile - other points, tiers,
thresholds that belong to the score, bands, a signal switched on or off - is scored from the stored values with the signals' own `score`, the same code the live card runs.

  feature_key(sig, cfg)        what the stored value depends on: the signal's `measure_key` (versions, declared settings, lists' versions) + the context settings (`context`, `rug`, `links.group_max`)
  data_mark(store)             how far the appended data reached (MAX ids of the source tables); a row measured earlier is `stale` when the mark moved
  fill(store, cfgs, ...)       measure what is missing for the profiles `cfgs` (a PassCache serves the big reads once per pass); resumable; the live path never calls this
  Features(store, mark=None)   the stored measures, read once and kept (json parsed once); with a `mark` only the rows measured under it (stale rows count as missing); without it every stored row is used
  card_from_store(...)         the whole card dict of `compute_card` from the stored measures, notes computed as there
  score_launch(feats, token, cfg)  the card fields (score, band, reasons, results) of a launch under `cfg`, scored from the store - or None when a measure is missing

The as-of rule is NOT here: it is in `LaunchContext`, which is the only thing a measure reads. A stored value is therefore what `compute_card` would have measured at that time on
that data; the equality with `compute_card` is checked launch by launch (a test and a server diagnostic). Card-level notes (the history gap) are not part of the score and not stored here.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Iterable

from ..progress import ticker
from ..store import Store
from ..store.db import utcnow_iso
from .card import RULES_VERSION, assemble, card_notes
from .config import config_hash
from .context import LaunchContext, load_launch
from .passcache import PassCache
from .signals import SIGNALS, Signal

BATCH = 400
MARK_TABLES = ("hist_launches", "hist_fundings", "launch_index", "link_edges", "link_cluster_members")      # tables that get OLD-dated rows in bulk (a history import, a links rebuild)


def profile_config(name: str) -> dict[str, Any]:
    """The effective config of a profile name: `v1`, `v1r`, ... as shipped, or `<profile>+draft` (the profile with the draft points of the fits, `fit.V2_DRAFT`: every signal on, one feature
    per tier) and `<profile>+v2rdraft` (`fit.v2r_draft()`). The studies score the draft profiles, which switch on signals the shipped profile has off - their measures are filled by name."""
    from . import fit as FIT
    from .config import default_config, effective
    base, _, over = name.strip().partition("+")
    if not over:
        return default_config(base)
    drafts = {"draft": lambda: FIT.V2_DRAFT, "v2rdraft": FIT.v2r_draft}
    if over not in drafts:
        raise ValueError(f"unknown profile overlay {over!r} in {name!r}")
    return effective(drafts[over](), base)


def available(store: Store) -> bool:
    return store.con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_features'").fetchone() is not None


def ctx_key(cfg: dict[str, Any]) -> str:
    """The context settings every measure reads through `LaunchContext`: whether backfilled snapshots count, the rug label thresholds, the size of a linked group."""
    raw = json.dumps({"context": cfg.get("context") or {}, "rug": cfg.get("rug"), "group_max": (cfg.get("links") or {}).get("group_max")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode()).hexdigest()[:6]


def feature_key(sig: Signal, cfg: dict[str, Any]) -> str:
    return sig.measure_key(cfg["signals"][sig.id]) + ctx_key(cfg)


def enabled(cfg: dict[str, Any]) -> list[tuple[Signal, dict[str, Any]]]:
    """The signals a profile uses (the very predicate `compute_card` applies), with their config."""
    out = []
    for sig in SIGNALS:
        c = cfg["signals"].get(sig.id)
        if c is not None and c.get("enabled", True):
            out.append((sig, c))
    return out


def data_mark(store: Store) -> str:
    """A short fingerprint of the bulk-loaded tables that can hold rows OLDER than the launches already measured (the imported history, the link tables). It does NOT follow the
    tables the live collector appends to all the time (facts, launches, snapshots) or the account histories the autofill loads one by one (transfers_api, backfilled snapshots): a mark
    that moved with every block would call every row stale at once. What those can change for an old launch is caught by the equality check; `recompute_all` re-measures everything."""
    parts = []
    for t in MARK_TABLES:
        try:
            n = store.con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except Exception:  # noqa: BLE001 - a table an old database does not have
            n = 0
        parts.append(f"{t}={n}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def _tasks(cfgs: Iterable[dict[str, Any]]) -> dict[tuple[str, str], tuple[Signal, dict[str, Any], dict[str, Any]]]:
    """(signal id, feature key) -> (signal, its config, the profile config it comes from): what has to exist for these profiles; profiles that agree on a key share one task."""
    out: dict[tuple[str, str], tuple[Signal, dict[str, Any], dict[str, Any]]] = {}
    for cfg in cfgs:
        for sig, c in enabled(cfg):
            out.setdefault((sig.id, feature_key(sig, cfg)), (sig, c, cfg))
    return out


def fill(store: Store, cfgs: list[dict[str, Any]], tokens: list[str] | None = None, log: Callable[[str], None] = lambda m: None, limit: int | None = None,
         recompute_stale: bool = False, recompute_all: bool = False, cache: PassCache | None = None) -> dict[str, Any]:
    """Measure what is missing for the profiles `cfgs` and store it. `tokens`: only these launches (default: every launch). `recompute_stale`: also re-measure rows whose
    mark is not the current one; `recompute_all`: re-measure every row. A measure that fails is listed in `failed` and left without a row. Writes only `signal_features`; one transaction per batch."""
    t0 = time.time()
    if not available(store):
        raise RuntimeError("signal_features: the database is not migrated to schema v19 yet")
    tasks = _tasks(cfgs)
    mark = data_mark(store)
    all_tokens = [r[0] for r in store.con.execute("SELECT token FROM launches ORDER BY ts_ns, token")] if tokens is None else list(tokens)
    have: dict[tuple[str, str], set[str]] = {}
    stale: dict[tuple[str, str], set[str]] = {}
    for (sid, key) in tasks:
        have[(sid, key)], stale[(sid, key)] = set(), set()
        for tok, mk in store.con.execute("SELECT token, mark FROM signal_features WHERE signal = ? AND mkey = ?", (sid, key)):
            (have if (mk == mark or not recompute_stale) and not recompute_all else stale)[(sid, key)].add(tok)
    out: dict[str, Any] = {"launches": len(all_tokens), "tasks": len(tasks), "mark": mark, "measured": 0, "kept": 0, "failed": 0, "failed_items": [], "stale_recomputed": 0,
                           "stale_rows": sum(len(v) for v in stale.values())}
    todo = []
    for tok in all_tokens:
        need = [k for k in tasks if tok not in have[k]]
        out["kept"] += len(tasks) - len(need)
        if need:
            todo.append((tok, need))
    if limit is not None:
        todo = todo[:limit]
    cache = cache or PassCache(store)
    rows: list[tuple[str, str, str, str, str, str]] = []
    tick = ticker(log, "features", len(todo))
    for i, (tok, need) in enumerate(todo):
        tick(i)
        launch = load_launch(store, tok)
        if launch is None:
            continue
        ctxs: dict[str, LaunchContext] = {}
        for (sid, key) in need:
            sig, c, cfg = tasks[(sid, key)]
            ck = ctx_key(cfg)
            ctx = ctxs.get(ck) or ctxs.setdefault(ck, LaunchContext(store, launch, cfg, cache))
            try:
                m = sig.measured(ctx, c)
            except Exception as e:  # noqa: BLE001 - one launch must not stop the fill
                out["failed"] += 1
                if len(out["failed_items"]) < 30:
                    out["failed_items"].append(f"{tok} {sid}: {type(e).__name__}: {str(e)[:100]}")
                continue
            rows.append((tok, sid, key, json.dumps(m, separators=(",", ":")), mark, utcnow_iso()))
            out["stale_recomputed"] += int(tok in stale[(sid, key)])
        if len(rows) >= BATCH or (i == len(todo) - 1 and rows):
            _write(store, rows)
            out["measured"] += len(rows)
            rows = []
    if rows:
        _write(store, rows)
        out["measured"] += len(rows)
    out["rows_total"] = store.con.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    out["cache"] = dict(cache.stats)
    out["seconds"] = round(time.time() - t0, 1)
    return out


def _write(store: Store, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    store.begin()
    try:
        store.con.executemany("INSERT OR REPLACE INTO signal_features(token, signal, mkey, measure, mark, computed_at) VALUES(?,?,?,?,?,?)", rows)
        store.commit()
    except BaseException:
        store.rollback()
        raise


class Features:
    """The stored measures, read lazily and kept: `get(signal id, key)` -> {token: measure} (each JSON parsed once). Valid while the table is not written to."""
    def __init__(self, store: Store, mark: str | None = None) -> None:
        """`mark`: when given, only the rows measured under this data mark are read (a stale row counts as missing); None reads every row."""
        self.store = store
        self.mark = mark
        self._by: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}

    def get(self, sid: str, key: str) -> dict[str, dict[str, Any]]:
        k = (sid, key)
        if k not in self._by:
            q, args = "SELECT token, measure FROM signal_features WHERE signal = ? AND mkey = ?", [sid, key]
            if self.mark is not None:
                q, args = q + " AND mark = ?", args + [self.mark]
            self._by[k] = {t: json.loads(m) for t, m in self.store.con.execute(q, args)}
        return self._by[k]

    def count(self, sid: str, key: str) -> int:
        return len(self.get(sid, key))


def score_launch(feats: Features, token: str, cfg: dict[str, Any], plan: list[tuple[Signal, dict[str, Any], str]] | None = None) -> dict[str, Any] | None:
    """The card fields of `token` under `cfg` from the stored measures, or None when one of the profile's measures is missing. Same assembly as `compute_card` (`card.assemble`);
    no notes (the history gap is card-level and does not move the score). `plan`: `profile_plan(cfg)`, to avoid recomputing the keys per launch."""
    plan = plan or profile_plan(cfg)
    results, ts = [], None
    for sig, c, key in plan:
        m = feats.get(sig.id, key).get(token)
        if m is None:
            return None
        ts = m.get("_t")
        results.append(sig.score(m, c))
    fired, score, band = assemble(results, cfg)
    return {"token": token, "score": score, "band": band, "reasons": fired, "signals": {"results": results},
            "config_version": cfg["version"], "config_hash": config_hash(cfg), "rules_version": cfg.get("rules_version", RULES_VERSION), "launch_ts_ns": ts}


def card_from_store(store: Store, feats: "Features", token: str, cfg: dict[str, Any], plan: list[tuple[Signal, dict[str, Any], str]] | None = None,
                    cache: PassCache | None = None) -> dict[str, Any] | None:
    """The whole card dict `compute_card` returns, with the signal results scored from the stored measures and the card-level notes computed as `compute_card` does (they depend on
    what is loaded now, not on the launch time); None when a measure is missing or the launch is unknown. `computed_ms` is 0 (nothing was measured)."""
    c = score_launch(feats, token, cfg, plan)
    launch = load_launch(store, token) if c is not None else None
    if c is None or launch is None:
        return None
    ctx = LaunchContext(store, launch, cfg, cache)
    notes, notes_short, gaps = card_notes(store, ctx, cfg)
    c["signals"].update(notes=notes, notes_short=notes_short)
    c.update(dev_account=ctx.dev, history_gap=gaps, launch_ts_ns=launch["ts_ns"], computed_ms=0, launch_height=launch["height"], coverage=launch["coverage"] or "live")
    return c


def profile_plan(cfg: dict[str, Any]) -> list[tuple[Signal, dict[str, Any], str]]:
    return [(sig, c, feature_key(sig, cfg)) for sig, c in enabled(cfg)]


def score_all(feats: Features, cfg: dict[str, Any], tokens: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """{token: card fields} for the tokens that have every measure of the profile, and the tokens that do not."""
    plan = profile_plan(cfg)
    out, missing = {}, []
    for t in tokens:
        c = score_launch(feats, t, cfg, plan)
        if c is None:
            missing.append(t)
        else:
            out[t] = c
    return out, missing


def status(store: Store, cfgs: list[dict[str, Any]]) -> dict[str, Any]:
    """What the store holds for these profiles: rows per task, launches covered, stale rows (a mark that is not the current one)."""
    mark = data_mark(store)
    n_launch = store.con.execute("SELECT COUNT(*) FROM launches").fetchone()[0]
    rows = []
    for (sid, key) in sorted(_tasks(cfgs)):
        n, st = store.con.execute("SELECT COUNT(*), SUM(mark <> ?) FROM signal_features WHERE signal = ? AND mkey = ?", (mark, sid, key)).fetchone()
        rows.append({"signal": sid, "key": key, "rows": n, "stale": st or 0})
    return {"mark": mark, "launches": n_launch, "tasks": rows}
