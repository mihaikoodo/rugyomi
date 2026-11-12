"""Fitting the weights of a rules profile on TRAIN launches (RGY-021 step 3), standard library only.

Fork (A) of the task: a logistic regression with NON-NEGATIVE coefficients on the signal tiers (a signal only ever adds risk),
rounded to points. Fork (B), hand-tuned weights, is done by a person reading `backtest.signal_table`; this module gives the data
for both and the mechanics for (A). Nothing here reads the database or the holdout: it gets rows in and returns numbers.

  patterns(rows, profile, names)             rows -> {feature tuple: [n, rugs]} (identical launches pooled)
  fit_nonneg(patterns, p, l2)                projected Newton: (intercept, [coef >= 0])
  cv_logloss(rows, profile, names, l2, k)    k-fold log-loss on TRAIN rows (the fold of a launch is a hash of its token)
  to_points(coefs, scale_max, step)          coefficients -> integer points, the largest = scale_max, multiples of `step`
  tier_keys(cfg, signal_id, points)          which `points*` config keys of a signal hold that value (must be exactly one)

RGY-033 (profile v2: smooth points). The fee and the name-copy signals are fitted as CUMULATIVE tiers: the features are "take >= the
first edge" (= the signal fires), "take >= tercile 1", "take >= tercile 2" (and "a copy", "3 or more uses", "the latest use is recent"),
all with a coefficient >= 0, so the points of a launch are the sum of the increments of the tiers it reaches: they can only grow with the
take / the uses / the recency, by construction. The edges come from the TRAIN distribution of the inputs alone (never from the outcome).
  choose_edges(rows, profile)                the tercile edges, `many_uses` and `recent_h` from the stored values of fired signals
  tier_view(rows, profile, edges, flat)      copies of the rows whose card of `TIERS` has the tier features (or one flat feature per signal)
"""
from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable


def row_key(card: dict[str, Any], idx: dict[str, int]) -> tuple[int, ...]:
    """The feature pattern of one card: the sorted indices of its fired `signal:points` features that are in `idx` (RGY-038: also used to score unseen rows)."""
    return tuple(sorted(idx[f"{sid}:{pts}"] for sid, pts in card["fired"].items() if f"{sid}:{pts}" in idx))


def patterns(rows: Iterable[dict[str, Any]], profile: str, names: list[str]) -> dict[tuple[int, ...], list[int]]:
    idx = {n: i for i, n in enumerate(names)}
    out: dict[tuple[int, ...], list[int]] = {}
    for r in rows:
        if r.get("final") != 1 or r.get("rug") not in (0, 1):
            continue
        card = r["cards"].get(profile)
        if card is None:
            continue
        key = row_key(card, idx)
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


# The draft points the v1 fit started from (fit_v1.txt): distinct per tier so a tier maps to one config key; every signal on. The stored features of the
# v2 fit (fit_v2.py step 1) and of the walk-forward (RGY-038) are made with this draft, so both read the same features.
V2_DRAFT = {"signals": {
    "creator_track_record": {"points_mid": 25, "points_high": 40},
    "funded_by_rugger": {"points": 30, "points_multi": 40},
    **{k: {"enabled": True} for k in ("cluster_rugs", "funded_by_dumper", "prior_fast_dump", "serial_launcher")},
}}

ZEROED_IN_V2 = ("serial_launcher", "prior_fast_dump", "funded_by_dumper", "cluster_rugs")      # v2 has their points at 0 (disabled); a refit needs them as candidates again


def v2r_draft() -> dict[str, Any]:
    """RGY-049: the draft of the v2r fit = `V2_DRAFT` plus, for the signals whose points are 0 in the v2 config, the v1 points (distinct per tier, enabled): in the v2 config the tiers of
    such a signal all read 0, one feature, which no fit can split back into its config keys. The expert rules are not in the draft: their points stay as configured (40 / 10)."""
    from .config import effective
    v1 = effective(None, "v1")["signals"]
    d = {"signals": {k: dict(v) for k, v in V2_DRAFT["signals"].items()}}
    for sid in ZEROED_IN_V2:
        d["signals"][sid] = {"enabled": True, **{k: v for k, v in v1[sid].items() if k.startswith("points")}}
    return d


TIERS = "tiers"        # the key of the derived card in a `tier_view` row
FEE_NAME = ("creator_tax", "name_copy")
EXPERT = ("name_imitation", "fresh_obfuscated_funding")        # RGY-049: points set by hand (profile v2r / v1r): never a feature of a fit, whatever the training rows say


def _quantile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def choose_edges(rows: Iterable[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Edges of the tiers from the distribution of the fired signals' stored `values` (pass TRAIN rows only):
    take2_bps / take3_bps = the 33rd / 67th percentile of the known takes (rounded to 0.5 bps, take3 > take2); many_uses = the smallest
    count >= 2 that the top third of the copies reaches; recent_h = the 33rd percentile of the gap to the latest use (rounded to 0.25 h, >= 0.25)."""
    take, uses, gaps = [], [], []
    for r in rows:
        card = (r.get("cards") or {}).get(profile) or {}
        vals = card.get("values") or {}
        t, n = vals.get("creator_tax") or {}, vals.get("name_copy") or {}
        if "creator_tax" in card.get("fired", {}) and t.get("take_bps") is not None:
            take.append(float(t["take_bps"]))
        if "name_copy" in card.get("fired", {}) and n.get("uses"):
            uses.append(int(n["uses"]))
            gaps.append(float(n["gap_h"]))
    out: dict[str, Any] = {"n_take": len(take), "n_copies": len(uses)}
    if take:
        e2 = round(_quantile(take, 0.33) * 2) / 2
        e3 = round(_quantile(take, 0.67) * 2) / 2
        out["take2_bps"], out["take3_bps"] = e2, (e3 if e3 > e2 else e2 + 0.5)
    if uses:
        out["many_uses"] = max(2, int(_quantile(uses, 0.67)))
        out["recent_h"] = max(0.25, round(_quantile(gaps, 0.33) * 4) / 4)
    return out


def tier_view(rows: Iterable[dict[str, Any]], profile: str, edges: dict[str, Any], flat: bool = False) -> list[dict[str, Any]]:
    """Copies of the rows with `cards[TIERS]["fired"]`: every other signal as it was stored; the fee and name-copy signals as cumulative tier
    features ids `creator_tax` (fires), `creator_tax@2`, `creator_tax@3`, `name_copy` (a copy), `name_copy@many`, `name_copy@recent`, each with
    points 1; with `flat` only the first of each (the v1 shape, for the comparison). A take that is not known (unknown platform / burn)
    reaches the first tier only."""
    out = []
    for r in rows:
        card = (r.get("cards") or {}).get(profile)
        if card is None:
            continue
        fired = {k: v for k, v in card["fired"].items() if k not in FEE_NAME and k not in EXPERT}
        vals = card.get("values") or {}
        if "creator_tax" in card["fired"]:
            fired["creator_tax"] = 1
            take = (vals.get("creator_tax") or {}).get("take_bps")
            if not flat and take is not None:
                if "take2_bps" in edges and take >= edges["take2_bps"]:
                    fired["creator_tax@2"] = 1
                if "take3_bps" in edges and take >= edges["take3_bps"]:
                    fired["creator_tax@3"] = 1
        if "name_copy" in card["fired"]:
            fired["name_copy"] = 1
            n = vals.get("name_copy") or {}
            if not flat and n.get("uses"):
                if "many_uses" in edges and n["uses"] >= edges["many_uses"]:
                    fired["name_copy@many"] = 1
                if "recent_h" in edges and n["gap_h"] <= edges["recent_h"]:
                    fired["name_copy@recent"] = 1
        out.append({**r, "cards": {**r["cards"], TIERS: {"fired": fired}}})
    return out


def tier_keys(cfg: dict[str, Any], signal_id: str, points: int) -> list[str]:
    c = cfg["signals"].get(signal_id, {})
    return [k for k, v in c.items() if k.startswith("points") and isinstance(v, (int, float)) and not isinstance(v, bool) and v == points]
