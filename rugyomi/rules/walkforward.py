"""Walk-forward evaluation by week (RGY-038): does a model fitted on recent launches forecast next week better than one fitted on all history?

The market shifts week to week (the share of rugs, the fee signal that flips between train and holdout), so one train / holdout cut says little. Here every ISO week W
(Monday 00:00 UTC to Sunday 24:00 UTC, by launch time) with enough final launches is a test week, and the model for W is fitted ONLY on launches whose outcome was already known
when W started - the rule `train_rows` encodes:
    a launch enters the fit for W only if  known_ns(launch) <= start of W   (no lookahead)
    known_ns = launch time + `LAG_H` hours (the label is read at T+24 h, the snapshot lags a little); a row that carries `final_ns` (the time its outcome became final)
    is known at the later of the two.
Three training windows - all earlier history, the last 2 weeks, the last 4 weeks (by launch time, before W) - and two feature sets: `v1` (the fired signal tiers of the v1 card)
and `v2` (the same with the fee and name-copy signals as cumulative tiers, `fit.tier_view`; the edges come from the training rows only). Each cell is a non-negative logistic
regression (`fit.fit_nonneg`, the very fit of the v1 / v2 profiles; l2 chosen by cross-validation on the training rows alone) scored on W: AUC, log-loss, Brier, and
the predicted vs observed rate by band. Baselines: a constant - the rug rate of all earlier history, of the last 2 weeks, of the last 4 weeks. A week is evaluated once, by
the rule above; nothing is tuned on it.

Pure: rows in (the as-of rows of `backtest.asof_rows` / `backtest_features.jsonl`), numbers out. No database, no clock, nothing written.
  evaluate(rows, ...)    -> {"weeks": [...], "skipped": [...], "preds": [...], "settings": {...}}
  summarize(result)      -> per cell and window the mean metrics, the wins, the launchpad split, and the gate
"""
from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from typing import Any, Iterable

from . import fit as FIT
from .accuracy import BANDS
from .backtest import auc as _auc
from .backtest import brier as _brier

DAY_NS = 86400 * 10**9
LAG_H = 25                                   # a label is read at T+24 h; one more hour for the snapshot
WINDOWS = (("all", None), ("2w", 14), ("4w", 28))
FEATURE_SETS = ("v1", "v2")
P_MIN, P_MAX = 0.01, 0.99                    # probabilities are clipped before a log-loss: one confident miss must not make a week infinite
L2S = (0.3, 1.0, 3.0, 10.0)                  # the grid of fit_v1 / fit_v2
MIN_FIRED = 15                               # a tier is fitted only when it fired this often in the training rows (as in fit_v1 / fit_v2)
MIN_WEEK_N = 30                              # a test week needs this many launches with a final outcome
MIN_TRAIN = 30                               # a training set smaller than this makes no model
MIN_CLASS = 10                               # ... and so does one with fewer than this many rugs or non-rugs (a fit on 2 rugs is a coin; added after the first server run, see the note of RGY-038)
MIN_LAUNCHPAD_N = 60                         # the launchpad split of the pooled forecasts needs this many launches and both classes


def week_start_ns(ns: int) -> int:
    d = dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc)
    monday = (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp()) * 10**9


def week_label(start_ns: int) -> str:
    y, w, _ = dt.datetime.fromtimestamp(start_ns / 1e9, dt.timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def known_ns(r: dict[str, Any], lag_h: float = LAG_H) -> int:
    t = r["launch_ts_ns"] + int(lag_h * 3600 * 10**9)
    f = r.get("final_ns")
    return max(t, int(f)) if f is not None else t


def usable(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Launches with a final, definite outcome."""
    return [r for r in rows if r.get("final") == 1 and r.get("rug") in (0, 1)]


def train_rows(rows: Iterable[dict[str, Any]], start_ns: int, days: int | None, lag_h: float = LAG_H) -> list[dict[str, Any]]:
    """The launches the model for the week starting at `start_ns` may be fitted on: outcome known by then, and (for a recent window) launched within `days` before it."""
    lo = None if days is None else start_ns - days * DAY_NS
    return [r for r in rows if r["launch_ts_ns"] < start_ns and known_ns(r, lag_h) <= start_ns and (lo is None or r["launch_ts_ns"] >= lo)]


def clip(p: float) -> float:
    return min(max(p, P_MIN), P_MAX)


def logloss(pairs: Iterable[tuple[float, int]]) -> float | None:
    pairs = [(clip(p), y) for p, y in pairs]
    return -sum(y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in pairs) / len(pairs) if pairs else None


def _rate(rows: list[dict[str, Any]]) -> float | None:
    return sum(r["rug"] for r in rows) / len(rows) if rows else None


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _fit_cell(train: list[dict[str, Any]], test: list[dict[str, Any]], fs: str, l2s: tuple[float, ...], min_fired: int) -> dict[str, Any]:
    """Fit on `train`, score `test`. -> {train_n, train_rugs, features, l2, edges, p: [(row, p)]}. The edges of the v2 tiers come from `train` alone."""
    tiered = fs in ("v2", "v2r", "v2l")           # RGY-049: v2r = the v2 tiers on the cards of profile v2r (RGY-053: v2l reads the same tiers of the stored take); its expert rules (fixed points) are never a feature
    edges = FIT.choose_edges(train, fs) if tiered else {}
    if tiered:
        tr, te, prof = FIT.tier_view(train, fs, edges), FIT.tier_view(test, fs, edges), FIT.TIERS
    else:
        tr, te, prof = [r for r in train if fs in r["cards"]], [r for r in test if fs in r["cards"]], fs
    cnt: Counter[str] = Counter()
    for r in tr:
        for sid, pts in r["cards"][prof]["fired"].items():
            if sid not in FIT.EXPERT:
                cnt[f"{sid}:{pts}"] += 1
    names = sorted(nm for nm, c in cnt.items() if c >= min_fired)
    if names:
        best = min(((FIT.cv_logloss(tr, prof, names, l2), l2) for l2 in l2s), key=lambda x: (x[0] if x[0] == x[0] else float("inf"), x[1]))
        l2 = best[1]
    else:
        l2 = 1.0
    b, w = FIT.fit_nonneg(FIT.patterns(tr, prof, names), len(names), l2)
    idx = {n: i for i, n in enumerate(names)}
    return {"train_n": len(tr), "train_rugs": sum(r["rug"] for r in tr), "features": len(names), "l2": l2, "edges": edges, "prof": prof,
            "p": [(r, FIT.predict(FIT.row_key(r["cards"][prof], idx), b, w)) for r in te]}


def _metrics(rows_p: list[tuple[dict[str, Any], float]], band_profile: str) -> dict[str, Any]:
    ps = [(p, r["rug"]) for r, p in rows_p]
    out = {"n": len(ps), "auc": _auc([{"score": p, "rug": y, "final": 1} for p, y in ps]), "logloss": logloss(ps), "brier": _brier([(clip(p), y) for p, y in ps]),
           "bands": []}
    for band in BANDS:
        got = [(p, r["rug"]) for r, p in rows_p if (r["cards"].get(band_profile) or {}).get("band") == band]
        out["bands"].append({"band": band, "n": len(got), "mean_p": _mean([p for p, _ in got]), "rate": _mean([y for _, y in got])})
    return out


def evaluate(rows: Iterable[dict[str, Any]], *, windows: tuple[tuple[str, int | None], ...] = WINDOWS, feature_sets: tuple[str, ...] = FEATURE_SETS,
             l2s: tuple[float, ...] = L2S, min_fired: int = MIN_FIRED, min_week_n: int = MIN_WEEK_N, min_train: int = MIN_TRAIN,
             lag_h: float = LAG_H, only_weeks: Iterable[str] | None = None, min_class: int = MIN_CLASS) -> dict[str, Any]:
    rows = usable(rows)
    by_week: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by_week.setdefault(week_start_ns(r["launch_ts_ns"]), []).append(r)
    keep = set(only_weeks) if only_weeks is not None else None
    weeks, skipped, preds = [], [], []
    for start in sorted(by_week):
        label, test = week_label(start), by_week[start]
        if keep is not None and label not in keep:
            continue
        if len(test) < min_week_n:
            skipped.append({"week": label, "why": f"{len(test)} final launches (< {min_week_n})"})
            continue
        trains = {name: train_rows(rows, start, days, lag_h) for name, days in windows}
        def why_not(t: list[dict[str, Any]]) -> str | None:
            rugs = sum(r["rug"] for r in t)
            if len(t) < min_train:
                return f"{len(t)} training launches (< {min_train})"
            if rugs < min_class or len(t) - rugs < min_class:
                return f"{rugs} rugs and {len(t) - rugs} non-rugs among the training launches (each needs >= {min_class})"
            return None
        if all(why_not(t) for t in trains.values()):
            skipped.append({"week": label, "why": "no earlier launches with a known outcome to fit on" if not any(trains.values()) else
                            "no training set with enough launches of both classes: " + why_not(trains[windows[0][0]])})
            continue
        base = {}
        for name, _ in windows:
            t = trains[name]
            rate = _rate(t)
            base[name] = None if why_not(t) else {
                "n_train": len(t), "rate": rate, "logloss": logloss([(rate, r["rug"]) for r in test]), "brier": _brier([(clip(rate), r["rug"]) for r in test])}
        wk = {"week": label, "start": dt.datetime.fromtimestamp(start / 1e9, dt.timezone.utc).strftime("%Y-%m-%d"), "n": len(test), "rugs": sum(r["rug"] for r in test),
              "rate": _rate(test), "baselines": base,
              "reference_auc": {fs: _auc([{"score": r["cards"][fs]["score"], "rug": r["rug"], "final": 1} for r in test if fs in r["cards"]]) for fs in feature_sets},
              "cells": {}}
        for name, _ in windows:
            t = trains[name]
            for fs in feature_sets:
                key = f"{name}/{fs}"
                reason = why_not(t)
                if reason:
                    wk["cells"][key] = {"skipped": reason}
                    continue
                cell = _fit_cell(t, test, fs, l2s, min_fired)
                m = _metrics(cell["p"], fs)
                wk["cells"][key] = {"train_n": cell["train_n"], "train_rugs": cell["train_rugs"], "features": cell["features"], "l2": cell["l2"], **m}
                for r, p in cell["p"]:
                    preds.append({"week": label, "cell": key, "token": r.get("token"), "launchpad": r["launchpad"], "p": p, "y": r["rug"],
                                  "base": {n: (b["rate"] if b else None) for n, b in base.items()}})
        weeks.append(wk)
    return {"weeks": weeks, "skipped": skipped, "preds": preds,
            "settings": {"windows": [list(w) for w in windows], "feature_sets": list(feature_sets), "l2s": list(l2s), "min_fired": min_fired, "min_week_n": min_week_n,
                         "min_train": min_train, "min_class": min_class, "lag_h": lag_h, "p_clip": [P_MIN, P_MAX]}}


# ------------------------------------------------------------------------------------------------ the summary and the gate
def _cells(result: dict[str, Any]) -> list[str]:
    s = result["settings"]
    return [f"{w[0]}/{fs}" for fs in s["feature_sets"] for w in s["windows"]]


def distinct(week: dict[str, Any], name: str) -> bool:
    """Is the training set of window `name` in this week a different set from the one of all history? In the first weeks the history is shorter than the window, the two
    are the same launches, and counting that week as a 'win' or a 'loss' of the window would only add a tie (the same model twice)."""
    if name == "all":
        return True
    a, b = week["baselines"].get(name), week["baselines"].get("all")
    return bool(a and b and a["n_train"] != b["n_train"])


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    weeks, s = result["weeks"], result["settings"]
    names = [w[0] for w in s["windows"]]
    cells: dict[str, Any] = {}
    for key in _cells(result):
        got = [(w, w["cells"].get(key)) for w in weeks]
        got = [(w, c) for w, c in got if c and "skipped" not in c]
        row: dict[str, Any] = {"weeks": len(got), "of": len(weeks),
                               "mean_logloss": _mean([c["logloss"] for _, c in got]), "mean_brier": _mean([c["brier"] for _, c in got]),
                               "mean_auc": _mean([c["auc"] for _, c in got if c["auc"] is not None]), "mean_features": _mean([c["features"] for _, c in got]),
                               "gain": {}, "wins": {}}
        for b in names:
            pairs = [(w["baselines"][b]["logloss"], c["logloss"]) for w, c in got if w["baselines"].get(b) and distinct(w, b)]
            row["gain"][b] = _mean([bl - m for bl, m in pairs])                   # > 0: the model has the lower log-loss
            row["wins"][b] = {"won": sum(1 for bl, m in pairs if m < bl), "of": len(pairs)}
        cells[key] = row
    baselines = {}
    for b in names:
        got = [w["baselines"][b] for w in weeks if w["baselines"].get(b)]
        baselines[b] = {"weeks": len(got), "distinct_weeks": sum(1 for w in weeks if w["baselines"].get(b) and distinct(w, b)), "mean_logloss": _mean([x["logloss"] for x in got]), "mean_brier": _mean([x["brier"] for x in got])}
    recent = [n for n in names if n != "all"]
    window_wins: dict[str, Any] = {}
    recent_vs_all: dict[str, Any] = {}
    for fs in s["feature_sets"]:
        tally = Counter()
        counts = {n: [0, 0] for n in recent}
        used = 0
        for w in weeks:
            cs = {n: w["cells"].get(f"{n}/{fs}") for n in names if distinct(w, n)}
            if len(cs) < 2 or any(c is None or "skipped" in c for c in cs.values()):
                continue
            used += 1
            tally[min(cs, key=lambda n: cs[n]["logloss"])] += 1
            for n in recent:
                if n in cs:
                    counts[n][1] += 1
                    counts[n][0] += cs[n]["logloss"] < cs["all"]["logloss"]
        window_wins[fs] = {"weeks": used, "best_window": {n: tally.get(n, 0) for n in names}}
        recent_vs_all[fs] = {n: {"won": counts[n][0], "of": counts[n][1]} for n in recent}
    base_recent_vs_all = {}
    for n in recent:
        pairs = [(w["baselines"][n]["logloss"], w["baselines"]["all"]["logloss"]) for w in weeks if w["baselines"].get(n) and w["baselines"].get("all") and distinct(w, n)]
        base_recent_vs_all[n] = {"won": sum(1 for a, b in pairs if a < b), "of": len(pairs)}
    pooled = _pooled_by_launchpad(result)
    gate = _gate(cells, recent_vs_all, base_recent_vs_all, recent, s)
    return {"cells": cells, "baselines": baselines, "window_wins": window_wins, "recent_vs_all": recent_vs_all, "baseline_recent_vs_all": base_recent_vs_all,
            "by_launchpad": pooled, "gate": gate, "weeks": [w["week"] for w in weeks], "skipped": result["skipped"]}


SUMMARY_VERSION = 1


def site_summary(result: dict[str, Any], summ: dict[str, Any], computed_at: str, *, reference_cell: str = "all/v1") -> dict[str, Any]:
    """RGY-044: the small, stored summary the site renders its sentence from (`collector_status.walk_forward_summary`). Pure; the numbers are those of `evaluate` / `summarize`, nothing is typed.
    The only yardstick is the plain rug rate of the last 2 weeks (`VERDICT_BASELINE`); other windows stay in the diagnostic.
    best        the cell that passes `beats_rule` with the highest mean gain, else the cell with the highest mean gain: its name, weeks won of weeks compared, mean gain, `passes`
    beats_recent  the gate's verdict: some cell passes `beats_rule` against the yardstick (the same value as `summ["gate"]["any_model_beats_recent_base_rate"]`)
    auc         min / max over the tested weeks of the AUC of the v1 card score (`reference_auc`)
    worst_week  the tested week where the model of `reference_cell` (fitted on all earlier launches = the way the published calibration is fitted) UNDERSTATED the rug rate most:
                n-weighted mean of (observed - predicted) over the bands; its predicted / observed rate per band"""
    weeks = result["weeks"]
    cells = summ["cells"]
    baseline = VERDICT_BASELINE
    best, best_key = None, None
    for key, c in cells.items():
        gain = c["gain"].get(baseline)
        w = c["wins"].get(baseline) or {}
        if gain is None or not w.get("of"):
            continue
        passes = beats_rule(w["won"], w["of"], gain)
        if best is None or (passes, gain) > (best["passes"], best["mean_gain"]):
            best = {"cell": key, "mean_gain": gain, "won": w["won"], "of": w["of"], "passes": passes, "mean_auc": c["mean_auc"], "baseline": baseline}
    aucs = [w["reference_auc"].get("v1") for w in weeks if w.get("reference_auc", {}).get("v1") is not None]
    rates = [w["rate"] for w in weeks if w.get("rate") is not None]
    worst = None
    for w in weeks:
        c = w["cells"].get(reference_cell)
        if not c or "skipped" in c:
            continue
        got = [b for b in c["bands"] if b["n"] and b["mean_p"] is not None and b["rate"] is not None]
        n = sum(b["n"] for b in got)
        if not n:
            continue
        gap = sum(b["n"] * (b["rate"] - b["mean_p"]) for b in got) / n
        if worst is None or gap > worst["gap"]:
            worst = {"week": w["week"], "cell": reference_cell, "gap": gap, "n": n,
                     "bands": [{"band": b["band"], "n": b["n"], "predicted": b["mean_p"], "observed": b["rate"]} for b in c["bands"]]}
    return {"version": SUMMARY_VERSION, "computed_at": computed_at, "weeks": [w["week"] for w in weeks], "n_weeks": len(weeks),
            "first_week": weeks[0]["week"] if weeks else None, "last_week": weeks[-1]["week"] if weeks else None, "launches": sum(w["n"] for w in weeks),
            "rate_min": min(rates) if rates else None, "rate_max": max(rates) if rates else None, "best": best,
            "beats_recent": bool(summ["gate"]["any_model_beats_recent_base_rate"]),
            "auc": {"min": min(aucs), "max": max(aucs)} if aucs else {"min": None, "max": None}, "worst_week": worst,
            "settings": {"lag_h": result["settings"]["lag_h"], "min_week_n": result["settings"]["min_week_n"], "baseline": baseline}}


def _pooled_by_launchpad(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Out-of-sample forecasts pooled over the test weeks, per launchpad and cell: n, rugs, log-loss of the model and of the constant baselines, AUC. Only where n allows."""
    out = []
    pads = sorted({p["launchpad"] for p in result["preds"]})
    for key in _cells(result):
        for pad in pads:
            ps = [p for p in result["preds"] if p["cell"] == key and p["launchpad"] == pad]
            ys = {p["y"] for p in ps}
            if len(ps) < MIN_LAUNCHPAD_N or len(ys) < 2:
                continue
            row = {"cell": key, "launchpad": pad, "n": len(ps), "rugs": sum(p["y"] for p in ps), "auc": _auc([{"score": p["p"], "rug": p["y"], "final": 1} for p in ps]),
                   "logloss": logloss([(p["p"], p["y"]) for p in ps]), "baselines": {}}
            for b in [w[0] for w in result["settings"]["windows"]]:
                have = [p for p in ps if p["base"].get(b) is not None]
                row["baselines"][b] = logloss([(p["base"][b], p["y"]) for p in have]) if len(have) == len(ps) else None
            out.append(row)
    return out


VERDICT_BASELINE = "2w"                 # RGY-044 / Q-025: the one, announced yardstick of the verdict: the plain rug rate of the last 2 weeks; the other recent windows are printed, not counted


def majority(won: int, of: int) -> bool:
    """More than half of the compared weeks."""
    return of > 0 and won * 2 > of


def beats_rule(won: int, of: int, gain: float | None) -> bool:
    """The ONE rule for 'better than the plain recent rug rate': the lower log-loss in more than half of the compared weeks AND a positive mean gain. The gate and the site both call it."""
    return majority(won, of) and (gain or 0) > 0


def _gate(cells: dict[str, Any], recent_vs_all: dict[str, Any], base_recent_vs_all: dict[str, Any], recent: list[str], s: dict[str, Any]) -> dict[str, Any]:
    """The two questions of the task, as booleans plus the numbers behind them. 'Most weeks' = more than half of the weeks both sides have; only weeks where the recent
    window's training set differs from all history count (see `distinct`)."""
    def most(d: dict[str, int]) -> bool:
        return majority(d["won"], d["of"])
    q1 = {fs: {n: most(recent_vs_all[fs][n]) for n in recent} for fs in s["feature_sets"]}
    q1_base = {n: most(base_recent_vs_all[n]) for n in recent}
    q2 = {key: {n: beats_rule(c["wins"].get(n, {"won": 0})["won"], c["wins"].get(n, {"of": 0})["of"], c["gain"].get(n)) for n in recent} for key, c in cells.items()}
    return {"recent_baselines": recent, "recent_window_beats_all_history": q1, "recent_base_rate_beats_all_history_base_rate": q1_base,
            "model_beats_recent_base_rate": q2,
            "any_recent_window_beats_all": any(v for d in q1.values() for v in d.values()),
            "verdict_baseline": VERDICT_BASELINE,
            "any_model_beats_recent_base_rate": any(d.get(VERDICT_BASELINE) for d in q2.values()),
            "any_model_beats_other_recent_base_rate": any(v for d in q2.values() for n, v in d.items() if n != VERDICT_BASELINE)}
