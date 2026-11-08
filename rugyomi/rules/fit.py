"""Fitting the weights of a rules profile on TRAIN launches (RGY-021 step 3), standard library only.

Fork (A) of the task: a logistic regression with NON-NEGATIVE coefficients on the signal tiers (a signal only ever adds risk),
rounded to points. Fork (B), hand-tuned weights, is done by a person reading `backtest.signal_table`; this module gives the data
for both and the mechanics for (A). Nothing here reads the database or the holdout: it gets rows in and returns numbers.

  patterns(rows, profile, names)             rows -> {feature tuple: [n, rugs]} (identical launches pooled)
  fit_nonneg(patterns, p, l2)                projected Newton: (intercept, [coef >= 0])
  cv_logloss(rows, profile, names, l2, k)    k-fold log-loss on TRAIN rows (the fold of a launch is a hash of its token)
  to_points(coefs, scale_max, step)          coefficients -> integer points, the largest = scale_max, multiples of `step`
  tier_keys(cfg, signal_id, points)          which `points*` config keys of a signal hold that value (must be exactly one)
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable


def patterns(rows: Iterable[dict[str, Any]], profile: str, names: list[str]) -> dict[tuple[int, ...], list[int]]:
    idx = {n: i for i, n in enumerate(names)}
    out: dict[tuple[int, ...], list[int]] = {}
    for r in rows:
        if r.get("final") != 1 or r.get("rug") not in (0, 1):
            continue
        card = r["cards"].get(profile)
        if card is None:
            continue
        key = tuple(sorted(idx[f"{sid}:{pts}"] for sid, pts in card["fired"].items() if f"{sid}:{pts}" in idx))
        cell = out.setdefault(key, [0, 0])
        cell[0] += 1
        cell[1] += r["rug"]
    return out


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))


def _loss(pats: dict, w: list[float], b: float, l2: float) -> float:
    tot = 0.0
    for key, (n, k) in pats.items():
        z = b + sum(w[j] for j in key)
        tot += n * (max(z, 0) + math.log1p(math.exp(-abs(z)))) - k * z
    return tot + 0.5 * l2 * sum(x * x for x in w)


def _solve(a: list[list[float]], y: list[float]) -> list[float]:
    n = len(y)
    m = [row[:] + [y[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            m[piv][c] = 1e-12
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                if f:
                    for k in range(c, n + 1):
                        m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_nonneg(pats: dict[tuple[int, ...], list[int]], p: int, l2: float = 1.0, iters: int = 200) -> tuple[float, list[float]]:
    """Minimise the logistic loss + l2/2 |w|^2 over (b, w >= 0) by projected Newton steps. Deterministic."""
    N = sum(n for n, _ in pats.values())
    K = sum(k for _, k in pats.values())
    if not N:
        return 0.0, [0.0] * p
    base = min(max(K / N, 1e-3), 1 - 1e-3)
    b, w = math.log(base / (1 - base)), [0.0] * p
    cur = _loss(pats, w, b, l2)
    for _ in range(iters):
        g = [0.0] * (p + 1)                                     # [intercept, w...]
        H = [[0.0] * (p + 1) for _ in range(p + 1)]
        for key, (n, k) in pats.items():
            pr = _sigmoid(b + sum(w[j] for j in key))
            act = [0] + [j + 1 for j in key]
            r, wt = n * pr - k, n * pr * (1 - pr)
            for i in act:
                g[i] += r
                for j in act:
                    H[i][j] += wt
        for j in range(p):
            g[j + 1] += l2 * w[j]
            H[j + 1][j + 1] += l2
        free = [0] + [j + 1 for j in range(p) if not (w[j] <= 0.0 and g[j + 1] > 0)]
        d = [0.0] * (p + 1)
        sub = _solve([[H[i][j] for j in free] for i in free], [-g[i] for i in free])
        for i, v in zip(free, sub):
            d[i] = v
        t, improved = 1.0, False
        while t > 1e-6:
            nb = b + t * d[0]
            nw = [max(0.0, w[j] + t * d[j + 1]) for j in range(p)]
            new = _loss(pats, nw, nb, l2)
            if new <= cur - 1e-12:
                improved = True
                break
            t /= 2
        if not improved:
            break
        step = max(abs(nb - b), *(abs(nw[j] - w[j]) for j in range(p))) if p else abs(nb - b)
        b, w, cur = nb, nw, new
        if step < 1e-9:
            break
    return b, w


def predict(pats_key: tuple[int, ...], b: float, w: list[float]) -> float:
    return _sigmoid(b + sum(w[j] for j in pats_key))


def cv_logloss(rows: list[dict[str, Any]], profile: str, names: list[str], l2: float, folds: int = 5) -> float:
    """Mean log-loss over `folds` folds of the given (TRAIN) rows; the fold of a launch is a hash of its token."""
    def fold(r: dict[str, Any]) -> int:
        return int(hashlib.sha256(r["token"].encode()).hexdigest(), 16) % folds
    tot, cnt = 0.0, 0
    for f in range(folds):
        tr = patterns([r for r in rows if fold(r) != f], profile, names)
        te = patterns([r for r in rows if fold(r) == f], profile, names)
        b, w = fit_nonneg(tr, len(names), l2)
        for key, (n, k) in te.items():
            p = min(max(predict(key, b, w), 1e-6), 1 - 1e-6)
            tot += -(k * math.log(p) + (n - k) * math.log(1 - p))
            cnt += n
    return tot / cnt if cnt else float("nan")


def to_points(coefs: list[float], scale_max: int = 50, step: int = 5) -> list[int]:
    """The largest coefficient becomes `scale_max`, the others in proportion, rounded to multiples of `step` (a coefficient that
    rounds to 0 stays 0: the signal is dropped)."""
    top = max(coefs) if coefs else 0.0
    if top <= 0:
        return [0] * len(coefs)
    return [int(round(c / top * scale_max / step)) * step for c in coefs]


def tier_keys(cfg: dict[str, Any], signal_id: str, points: int) -> list[str]:
    c = cfg["signals"].get(signal_id, {})
    return [k for k, v in c.items() if k.startswith("points") and isinstance(v, (int, float)) and not isinstance(v, bool) and v == points]
