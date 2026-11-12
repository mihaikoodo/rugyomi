"""Comparing rules profiles (RGY-051): can the data tell a tie from a win?

A profile comparison used to end in "A is not better than B" - which reads as "A is worse" when it is only "the difference is smaller than the noise". Here every comparison also carries a
paired bootstrap confidence interval, so the verdict can be one of three:
    better                    the interval of (B - A) lies above 0
    worse                     ... below 0
    no detectable difference  the interval contains 0
This sits NEXT TO the one rule of Q-025 (`walkforward.beats_rule`: more than half of the weeks AND a positive mean gain), which decides what the site says; nothing here changes that rule.

  bootstrap(stat, groups)           the percentile interval of a statistic of resampled launches; the launches are resampled WITH replacement inside their stratum (the week), the same
                                    resampled launches score both sides (a paired bootstrap), so the noise of the market cancels and only the difference stays; seeded, reproducible
  compare_auc(sa, sb, ys, weeks)    delta AUC (B - A) of two scores on the same launches: pooled, or the mean of the weekly AUC differences
  compare_loss(la, lb, weeks)       the gain (mean of loss A - loss B; > 0 favours B) per launch, as the mean of the weekly means (the "mean gain" of the rule) or pooled
  head_to_head(la, lb, weeks)       the weekly wins and the mean weekly gain, and the verdict of the ONE rule
  baseline_table(rows, ...)         per launch the 2-week rug rate before its week: the pooled one, and the rate of its own launchpad (the pooled rate where the launchpad has too few)
  fixed_table(rows, profiles, ...)  per launch the probability that the points AS SHIPPED give: the score of each profile mapped to a probability by an isotonic scale fitted on the launches
                                    whose outcome was known when the week started (the past only) - no weights are refitted, so a richer profile is not penalised by a small window
  add_refit(table, result, cells)   the out-of-sample probabilities of walk-forward cells (`walkforward.evaluate`) next to the fixed ones, joined by token

Pure: rows in (the as-of rows of `backtest.asof_rows`), numbers out. No database, no clock, nothing written."""
from __future__ import annotations

import bisect
import math
import random
from typing import Any, Callable, Iterable, Sequence

from . import backtest as BT
from . import walkforward as WF

B_DEFAULT = 2000
LEVEL = 0.95
SEED = 20261111
PAD_MIN_N = 20              # a launchpad whose recent window has fewer launches than this takes the pooled rate as its baseline
CALIB_MIN_N = 30           # the smallest block of the isotonic scale (as the published calibration)
MIN_REPS = 0.5             # a bootstrap with fewer valid replicates than this share of B (a resample without both classes has no AUC) gives no interval
LABELS = ("better", "worse", "no detectable difference")


def verdict_of(lo: float | None, hi: float | None) -> str | None:
    """B against A: 'better' when the whole interval is above 0, 'worse' when below, else 'no detectable difference'."""
    if lo is None or hi is None:
        return None
    return "better" if lo > 0 else "worse" if hi < 0 else "no detectable difference"


# ---------------------------------------------------------------------------------------------- the bootstrap
def strata(keys: Sequence[Any]) -> list[list[int]]:
    """Indices grouped by key, groups in key order (so a seed gives the same resamples)."""
    by: dict[Any, list[int]] = {}
    for i, k in enumerate(keys):
        by.setdefault(k, []).append(i)
    return [by[k] for k in sorted(by, key=lambda x: (str(type(x)), x))]


def bootstrap(stat: Callable[[list[list[int]]], float | None], groups: list[list[int]], *, B: int = B_DEFAULT, seed: int = SEED, level: float = LEVEL) -> dict[str, Any]:
    """`stat` gets the groups of (resampled) indices and returns a number or None. -> {est, lo, hi, verdict, B (valid replicates)}."""
    est = stat(groups)
    rng = random.Random(seed)
    reps = []
    for _ in range(B):
        v = stat([[g[rng.randrange(len(g))] for _ in g] for g in groups])
        if v is not None:
            reps.append(v)
    reps.sort()
    out = {"est": est, "lo": None, "hi": None, "verdict": None, "B": len(reps)}
    if est is None or len(reps) < max(20, int(B * MIN_REPS)):
        return out
    a = (1 - level) / 2
    out["lo"], out["hi"] = reps[int(a * len(reps))], reps[min(len(reps) - 1, int((1 - a) * len(reps)))]
    out["verdict"] = verdict_of(out["lo"], out["hi"])
    return out


def auc(scores: Sequence[float], ys: Sequence[int]) -> float | None:
    """P(score of a random rug > score of a random non-rug), ties count half; None when a class is missing."""
    pos = [s for s, y in zip(scores, ys) if y == 1]
    neg = sorted(s for s, y in zip(scores, ys) if y == 0)
    if not pos or not neg:
        return None
    t = 0.0
    for s in pos:
        lo, hi = bisect.bisect_left(neg, s), bisect.bisect_right(neg, s)
        t += lo + 0.5 * (hi - lo)
    return t / (len(pos) * len(neg))


def _mean(xs: Iterable[float]) -> float | None:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def compare_auc(sa: Sequence[float], sb: Sequence[float], ys: Sequence[int], weeks: Sequence[Any], *, mode: str = "pooled", B: int = B_DEFAULT, seed: int = SEED,
                level: float = LEVEL) -> dict[str, Any]:
    """Delta AUC = AUC(B) - AUC(A) on the same launches. `mode` 'pooled': one AUC over all launches; 'weekly': the mean of the weekly AUC differences (weeks without both classes are left out).
    -> {n, auc_a, auc_b, delta, lo, hi, verdict, B}. The interval resamples launches inside their week."""
    if mode not in ("pooled", "weekly"):
        raise ValueError(mode)
    n = len(ys)

    def pick(idx: list[int]) -> tuple[list[float], list[float], list[int]]:
        return [sa[i] for i in idx], [sb[i] for i in idx], [ys[i] for i in idx]

    def pair(idx: list[int]) -> tuple[float | None, float | None]:
        a, b, y = pick(idx)
        return auc(a, y), auc(b, y)

    def stat(groups: list[list[int]]) -> float | None:
        if mode == "pooled":
            xa, xb = pair([i for g in groups for i in g])
            return None if xa is None or xb is None else xb - xa
        diffs = []
        for g in groups:
            xa, xb = pair(g)
            if xa is not None and xb is not None:
                diffs.append(xb - xa)
        return _mean(diffs)

    out = bootstrap(stat, strata(weeks), B=B, seed=seed, level=level)
    aa, ab = pair(list(range(n)))
    return {"n": n, "mode": mode, "auc_a": aa, "auc_b": ab, "delta": out["est"], "lo": out["lo"], "hi": out["hi"], "verdict": out["verdict"], "B": out["B"]}


def compare_loss(la: Sequence[float], lb: Sequence[float], weeks: Sequence[Any], *, mode: str = "weekly", B: int = B_DEFAULT, seed: int = SEED, level: float = LEVEL) -> dict[str, Any]:
    """The gain of B over A: mean of (loss A - loss B), > 0 means B has the lower loss. `mode` 'weekly' (the default, the 'mean gain' of the rule): the mean of the weekly mean differences;
    'pooled': the mean over launches. -> {n, loss_a, loss_b, gain, lo, hi, verdict, B}."""
    if mode not in ("pooled", "weekly"):
        raise ValueError(mode)
    d = [x - y for x, y in zip(la, lb)]

    def stat(groups: list[list[int]]) -> float | None:
        if mode == "pooled":
            return _mean(d[i] for g in groups for i in g)
        return _mean(m for m in (_mean(d[i] for i in g) for g in groups) if m is not None)

    out = bootstrap(stat, strata(weeks), B=B, seed=seed, level=level)
    return {"n": len(d), "mode": mode, "loss_a": _mean(la), "loss_b": _mean(lb), "gain": out["est"], "lo": out["lo"], "hi": out["hi"], "verdict": out["verdict"], "B": out["B"]}


def head_to_head(la: Sequence[float], lb: Sequence[float], weeks: Sequence[Any]) -> dict[str, Any]:
    """The rule of Q-025 for B against A: weeks where B has the lower mean loss, the mean weekly gain, and `better` = `walkforward.beats_rule`."""
    per: dict[Any, list[float]] = {}
    for x, y, w in zip(la, lb, weeks):
        per.setdefault(w, []).append(x - y)
    gains = [(w, _mean(v)) for w, v in sorted(per.items())]
    won = sum(1 for _, g in gains if g > 0)
    mean_gain = _mean(g for _, g in gains)
    return {"won": won, "of": len(gains), "gain": mean_gain, "better": WF.beats_rule(won, len(gains), mean_gain), "weekly": gains}


# ---------------------------------------------------------------------------------------------- the tables
def logloss_row(p: float, y: int) -> float:
    p = WF.clip(p)
    return -(y * math.log(p) + (1 - y) * math.log(1 - p))


def _weeks_of(rows: list[dict[str, Any]], only_weeks: Iterable[str] | None) -> list[tuple[int, list[dict[str, Any]]]]:
    by: dict[int, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(WF.week_start_ns(r["launch_ts_ns"]), []).append(r)
    keep = set(only_weeks) if only_weeks is not None else None
    return [(s, by[s]) for s in sorted(by) if keep is None or WF.week_label(s) in keep]


def _enough(train: list[dict[str, Any]], min_train: int, min_class: int) -> bool:
    rugs = sum(r["rug"] for r in train)
    return len(train) >= min_train and rugs >= min_class and len(train) - rugs >= min_class


def baseline_probs(rows: list[dict[str, Any]], start_ns: int, test: list[dict[str, Any]], *, days: int = 14, lag_h: float = WF.LAG_H, pad_min_n: int = PAD_MIN_N) -> list[dict[str, Any]]:
    """For each launch of the week starting at `start_ns`: the plain rug rate of the `days` before the week (outcome known by the week start, `walkforward.train_rows`) - pooled, and of the
    launch's own launchpad (the pooled rate when the launchpad has fewer than `pad_min_n` launches in that window; `pad_fallback`). Nothing from the week itself or later."""
    recent = WF.train_rows(rows, start_ns, days, lag_h)
    pooled = sum(r["rug"] for r in recent) / len(recent)
    pads: dict[str, list[int]] = {}
    for r in recent:
        pads.setdefault(r["launchpad"], []).append(r["rug"])
    out = []
    for r in test:
        ys = pads.get(r["launchpad"], [])
        fb = len(ys) < pad_min_n
        out.append({"pooled": pooled, "pad": pooled if fb else sum(ys) / len(ys), "fallback": fb})
    return out


def fixed_table(rows: Iterable[dict[str, Any]], profiles: Sequence[str], *, only_weeks: Iterable[str] | None = None, min_week_n: int = WF.MIN_WEEK_N, min_train: int = WF.MIN_TRAIN,
                min_class: int = WF.MIN_CLASS, lag_h: float = WF.LAG_H, base_days: int = 14, pad_min_n: int = PAD_MIN_N, calib_min_n: int = CALIB_MIN_N) -> dict[str, Any]:
    """The launches of the test weeks with the probability that the points AS SHIPPED give. For each week W: the score of each profile -> probability by an isotonic scale (`backtest.isotonic`)
    fitted on the launches whose outcome was known when W started (all earlier history; `walkforward.train_rows`), so nothing of W or later enters. A week needs `min_week_n` launches and
    enough training launches of both classes in all history and in the baseline window. -> {"token", "week", "launchpad", "source", "y", "distinct", "score", "p", "base", "fallback", "skipped"}
    (parallel lists; `p` / `score` by profile name, `base` by 'pooled' / 'pad'; `distinct`: the baseline window holds other launches than all history, the weeks the rule counts)."""
    rows = [r for r in WF.usable(rows) if all(p in r["cards"] for p in profiles)]
    t: dict[str, Any] = {"token": [], "week": [], "launchpad": [], "source": [], "y": [], "distinct": [], "score": {p: [] for p in profiles}, "p": {p: [] for p in profiles},
                         "base": {"pooled": [], "pad": []}, "fallback": [], "skipped": []}
    for start, test in _weeks_of(rows, only_weeks):
        label = WF.week_label(start)
        if len(test) < min_week_n:
            t["skipped"].append({"week": label, "why": f"{len(test)} final launches (< {min_week_n})"})
            continue
        allt = WF.train_rows(rows, start, None, lag_h)
        recent = WF.train_rows(rows, start, base_days, lag_h)
        if not (_enough(allt, min_train, min_class) and _enough(recent, min_train, min_class)):
            t["skipped"].append({"week": label, "why": "too few earlier launches with a known outcome of both classes"})
            continue
        scales = {p: BT.isotonic([{"score": r["cards"][p]["score"], "rug": r["rug"], "final": 1} for r in allt], calib_min_n) for p in profiles}
        base = baseline_probs(rows, start, test, days=base_days, lag_h=lag_h, pad_min_n=pad_min_n)
        for r, b in zip(test, base):
            t["token"].append(r["token"])
            t["week"].append(label)
            t["launchpad"].append(r["launchpad"])
            t["source"].append(r.get("source"))
            t["y"].append(r["rug"])
            t["distinct"].append(len(recent) != len(allt))
            t["base"]["pooled"].append(b["pooled"])
            t["base"]["pad"].append(b["pad"])
            t["fallback"].append(b["fallback"])
            for p in profiles:
                sc = r["cards"][p]["score"]
                t["score"][p].append(sc)
                t["p"][p].append(BT.apply_calibration(scales[p], sc))
    return t


def add_refit(table: dict[str, Any], result: dict[str, Any], cells: Sequence[str]) -> None:
    """Put the out-of-sample probabilities of walk-forward cells (`walkforward.evaluate`, 'all/v1r' ...) into `table["p"]` under the cell name, joined by token; None where the fit made no
    forecast for that launch."""
    for cell in cells:
        got = {p["token"]: p["p"] for p in result["preds"] if p["cell"] == cell and "token" in p}
        table["p"][f"refit:{cell}"] = [got.get(tok) for tok in table["token"]]


def column(table: dict[str, Any], name: str) -> list[float | None]:
    """The probability column `name`: a profile, 'refit:<cell>', or the baselines 'base:pooled' / 'base:pad'."""
    if name.startswith("base:"):
        return list(table["base"][name[5:]])
    return list(table["p"][name])


def losses(table: dict[str, Any], a: str, b: str, *, only: Callable[[int], bool] | None = None) -> tuple[list[float], list[float], list[Any], list[int]]:
    """The per-launch log-loss of columns `a` and `b` on the launches where both exist (and `only(i)` holds): (loss a, loss b, weeks, indices)."""
    ca, cb = column(table, a), column(table, b)
    idx = [i for i in range(len(table["y"])) if ca[i] is not None and cb[i] is not None and (only is None or only(i))]
    return ([logloss_row(ca[i], table["y"][i]) for i in idx], [logloss_row(cb[i], table["y"][i]) for i in idx], [table["week"][i] for i in idx], idx)
