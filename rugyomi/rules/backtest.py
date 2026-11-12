"""The as-of backtest (RGY-021 step 2): how well would a rules profile have forecast every launch that has a final outcome?

For every such launch (history from the backfill + live) a card is computed AS OF the launch time with `compute_card` — the very
code path the live card uses (`card.py`), which reads only what was knowable then (`context.py`) — for each profile asked for, and
never written to `cards` (since RGY-050 a card is scored from the stored measures of `signal_features` when they are there: same result, seconds instead of minutes). The outcome is the stored
`launch_outcomes` row. Nothing here writes to the database.

  asof_rows(store, profiles)       one row per launch: outcome + the card of every profile (score, band, signals fired)
  split(rows, split_ns)            (train, holdout) by launch time: train = before, holdout = from
  report(rows, profile)            n, base rate, bands and score ranges with 90 % Wilson intervals and lift, coverage,
                                   AUC, per signal fire rate and rug rate fired / not fired
  isotonic(pairs) / calibrate(...) score -> rug probability fitted on TRAIN (pool adjacent violators), Brier, and the
                                   predicted-vs-observed table on any other set

A backtest is not a track record: the public site says so, and these numbers never mix with the live accuracy table.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable

from .accuracy import BANDS, SCORE_RANGES, Z90, _rate_row, counted, summarize, wilson
from .card import compute_card


def final_tokens(store: Any) -> list[tuple[str, int, str, str]]:
    """(token, launch ts, launchpad, 'history' | 'live') of every launch with a FINAL, decided outcome, oldest first."""
    cov = "l.coverage" if store.user_version() >= 6 else "NULL"
    return [(t, ts, lp, "history" if c == "launch_only" else "live") for t, ts, lp, c in store.con.execute(
        f"SELECT l.token, l.ts_ns, l.launchpad, {cov} FROM launches l JOIN launch_outcomes o ON o.token = l.token "
        f"WHERE o.final = 1 AND o.rug IN (0, 1) ORDER BY l.ts_ns, l.token")]


def _from_store(store: Any, profiles: dict[str, dict[str, Any]], tokens: list[str], fresh_only: bool) -> tuple[dict[str, dict[str, dict[str, Any]]], int]:
    """RGY-050: the cards of every profile scored from the stored measures (`signal_features`), {profile: {token: card fields}}, and how many stored rows of these profiles are older than the
    current data mark. Only the launches whose measures of the profile are all there (and, with `fresh_only`, not stale). ({}, 0) for a database without the table or when anything
    goes wrong: the caller then computes every card."""
    try:
        from . import features as FT
        if not FT.available(store):
            return {}, 0
        feats = FT.Features(store, FT.data_mark(store) if fresh_only else None)
        got = {name: FT.score_all(feats, cfg, tokens)[0] for name, cfg in profiles.items()}
        return got, sum(t["stale"] for t in FT.status(store, list(profiles.values()))["tasks"])
    except Exception:  # noqa: BLE001 - the store is an accelerator, never a reason to fail
        return {}, 0


def asof_rows(store: Any, profiles: dict[str, dict[str, Any]], tokens: Iterable[str] | None = None,
              log: Callable[[str], None] = lambda m: None, exclude_launchpads: Iterable[str] = (), use_store: bool = True, fresh_only: bool = False) -> list[dict[str, Any]]:
    """`profiles`: name -> effective config. A launch whose card fails is skipped and listed in `.errors` of the result.
    `use_store` (RGY-050): a card is scored from the stored measures when the launch has all of them for the profile (seconds for the whole set), and computed with `compute_card` otherwise;
    the two are equal while the stored rows are fresh (a test and a server diagnostic). A row measured under an older data mark (the link tables are rebuilt by the collector all the time) is
    still used, and the log says how many such rows there are (`features_fill.py --recompute-stale` brings them up to date); `fresh_only` ignores them instead (those cards are computed).
    `.from_store` / `.computed` / `.stale_rows` of the result count the cards of each kind and the stale rows."""
    want = set(tokens) if tokens is not None else None
    skip = set(exclude_launchpads)
    rows, errors = [], []
    items = [x for x in final_tokens(store) if (want is None or x[0] in want) and x[2] not in skip]
    stored, n_stale = _from_store(store, profiles, [x[0] for x in items], fresh_only) if use_store else ({}, 0)
    n_store = n_comp = 0
    for i, (tok, ts, lp, src) in enumerate(items):
        o = store.con.execute("SELECT rug, final, severity_near, dev_exit, buyers_loss FROM launch_outcomes WHERE token = ?", (tok,)).fetchone()
        row = {"token": tok, "launch_ts_ns": ts, "launchpad": lp, "source": src, "issued_at": None, "rug": o[0], "final": o[1],
               "severity_near": o[2], "dev_exit": o[3], "buyers_loss": o[4], "cards": {}}
        try:
            for name, cfg in profiles.items():
                c = stored.get(name, {}).get(tok)
                if c is None:
                    c = compute_card(store, tok, cfg)
                    n_comp += 1
                else:
                    n_store += 1
                row["cards"][name] = {"score": c["score"], "band": c["band"], "config_version": c["config_version"],
                                      "fired": {r["id"]: r["contribution"] for r in c["reasons"]},
                                      "values": {r["id"]: r.get("value") for r in c["reasons"]},       # RGY-033: the continuous input of a fired signal (the fit builds its tiers from it)
                                      "reasons": [r["id"] for r in c["reasons"]]}
        except Exception as e:  # noqa: BLE001 — one launch must not stop the report
            errors.append(f"{tok}: {type(e).__name__}: {str(e)[:120]}")
            continue
        rows.append(row)
        if (i + 1) % 500 == 0:
            log(f"as-of cards {i + 1}/{len(items)}")
    out = _Rows(rows)
    out.errors = errors
    out.from_store, out.computed, out.stale_rows = n_store, n_comp, n_stale
    log(f"as-of cards: {n_store} from the stored measures, {n_comp} computed"
        + (f"; {n_stale} stored rows are older than the data mark: run features_fill.py --recompute-stale" if n_stale else ""))
    return out


class _Rows(list):
    """The list of rows; `.errors` holds the launches whose card failed, `.from_store` / `.computed` the cards scored from the stored measures / computed, `.stale_rows` the stored rows of the profiles older than the data mark."""
    errors: list[str]
    from_store: int = 0
    computed: int = 0
    stale_rows: int = 0


def view(rows: Iterable[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
    """The rows in the shape `accuracy.summarize` wants, with the score / band / config version of `profile`."""
    out = []
    for r in rows:
        c = r["cards"].get(profile)
        if c is not None:
            out.append({**{k: v for k, v in r.items() if k != "cards"}, "score": c["score"], "band": c["band"],
                        "config_version": c["config_version"], "fired": c["fired"]})
    return out


def split(rows: Iterable[dict[str, Any]], split_ns: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = list(rows)
    return [r for r in rows if r["launch_ts_ns"] < split_ns], [r for r in rows if r["launch_ts_ns"] >= split_ns]


def auc(rows: Iterable[dict[str, Any]]) -> float | None:
    """P(score of a random rug > score of a random non-rug), ties count half. None when one class is missing."""
    pos = sorted(r["score"] for r in rows if counted(r) and r["rug"] == 1)
    neg = sorted(r["score"] for r in rows if counted(r) and r["rug"] == 0)
    if not pos or not neg:
        return None
    import bisect
    total = 0.0
    for s in pos:
        total += bisect.bisect_left(neg, s) + 0.5 * (bisect.bisect_right(neg, s) - bisect.bisect_left(neg, s))
    return total / (len(pos) * len(neg))


def signal_table(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per signal id and contribution: how often it fires and the rug rate when fired vs not fired (counted rows only)."""
    c = [r for r in rows if counted(r)]
    keys = sorted({(sid, pts) for r in c for sid, pts in r["fired"].items()})
    out = []
    for sid, pts in keys:
        fired = [r for r in c if r["fired"].get(sid) == pts]
        rest = [r for r in c if r["fired"].get(sid) != pts]
        kf, kr = sum(r["rug"] for r in fired), sum(r["rug"] for r in rest)
        ci = wilson(kf, len(fired))
        out.append({"signal": sid, "points": pts, "fired": len(fired), "fire_rate": len(fired) / len(c) if c else None,
                    "rugs_fired": kf, "rate_fired": kf / len(fired) if fired else None,
                    "lo": ci[0] if ci else None, "hi": ci[1] if ci else None,
                    "rate_not_fired": kr / len(rest) if rest else None})
    return out


def report(rows: Iterable[dict[str, Any]], profile: str) -> dict[str, Any]:
    v = view(rows, profile)
    s = summarize(v)
    return {"profile": profile, "summary": s["summary"], "by_band": s["by_band"], "by_range": s["by_range"],
            "coverage": s["coverage"], "auc": auc(v), "signals": signal_table(v)}


def by_launchpad(rows: Iterable[dict[str, Any]], profile: str) -> dict[str, dict[str, Any]]:
    rows = list(rows)
    return {lp: report([r for r in rows if r["launchpad"] == lp], profile) for lp in sorted({r["launchpad"] for r in rows})}


# ---------------------------------------------------------------- calibration: score -> probability

def isotonic(rows: Iterable[dict[str, Any]], min_n: int = 30) -> list[dict[str, Any]]:
    """Monotone (non-decreasing) rug probability by score, fitted by pool-adjacent-violators on the counted rows. Returns
    blocks {lo_score, hi_score, p, n, rugs} ordered by score; the blocks tile the scores seen. Blocks with fewer than `min_n` launches
    are merged into a neighbour."""
    c = [r for r in rows if counted(r)]
    by: dict[int, list[int]] = {}
    for r in c:
        by.setdefault(r["score"], [0, 0])
        by[r["score"]][0] += r["rug"]
        by[r["score"]][1] += 1
    blocks = [{"lo_score": s, "hi_score": s, "rugs": k, "n": n} for s, (k, n) in sorted(by.items())]
    out: list[dict[str, Any]] = []
    for b in blocks:
        out.append(dict(b))
        while len(out) >= 2 and out[-2]["rugs"] / out[-2]["n"] > out[-1]["rugs"] / out[-1]["n"]:
            hi = out.pop()
            lo = out.pop()
            out.append({"lo_score": lo["lo_score"], "hi_score": hi["hi_score"], "rugs": lo["rugs"] + hi["rugs"], "n": lo["n"] + hi["n"]})
    # a block of a handful of launches is noise (a single launch would claim 0 % or 100 %): fold it into its neighbour, then restore
    # the order with pool-adjacent-violators again
    while len(out) > 1:
        small = [i for i, b in enumerate(out) if b["n"] < min_n]
        if not small:
            break
        i = min(small, key=lambda j: out[j]["n"])
        nb = i - 1 if i == len(out) - 1 else i + 1 if i == 0 else (
            i - 1 if abs(out[i - 1]["rugs"] / out[i - 1]["n"] - out[i]["rugs"] / out[i]["n"])
            <= abs(out[i + 1]["rugs"] / out[i + 1]["n"] - out[i]["rugs"] / out[i]["n"]) else i + 1)
        a, b = sorted((i, nb))
        out[a:b + 1] = [{"lo_score": out[a]["lo_score"], "hi_score": out[b]["hi_score"], "rugs": out[a]["rugs"] + out[b]["rugs"],
                         "n": out[a]["n"] + out[b]["n"]}]
        j = 0
        while j < len(out) - 1:
            if out[j]["rugs"] / out[j]["n"] > out[j + 1]["rugs"] / out[j + 1]["n"]:
                out[j:j + 2] = [{"lo_score": out[j]["lo_score"], "hi_score": out[j + 1]["hi_score"],
                                 "rugs": out[j]["rugs"] + out[j + 1]["rugs"], "n": out[j]["n"] + out[j + 1]["n"]}]
                j = max(0, j - 1)
            else:
                j += 1
    for b in out:
        b["p"] = b["rugs"] / b["n"]
    return out


def apply_calibration(blocks: list[dict[str, Any]], score: int) -> float | None:
    """The probability of the block that holds `score`; a score between two blocks takes the one below it; below the first block
    the first block's value. None when there are no blocks."""
    if not blocks:
        return None
    p = blocks[0]["p"]
    for b in blocks:
        if score >= b["lo_score"]:
            p = b["p"]
    return p


def brier(pairs: Iterable[tuple[float, int]]) -> float | None:
    pairs = list(pairs)
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def calibration_check(train: Iterable[dict[str, Any]], other: Iterable[dict[str, Any]], min_n: int = 30) -> dict[str, Any]:
    """Fit the scale on `train`, judge it on `other`: Brier of the fitted scale vs the constant base rate, and per block the
    predicted probability against the observed rate with a 90 % interval."""
    train = [r for r in train if counted(r)]
    other = [r for r in other if counted(r)]
    blocks = isotonic(train, min_n)
    base = sum(r["rug"] for r in train) / len(train) if train else None
    rows = []
    for b in blocks:
        got = [r for r in other if b["lo_score"] <= r["score"] <= b["hi_score"]]
        k = sum(r["rug"] for r in got)
        ci = wilson(k, len(got))
        rows.append({"lo_score": b["lo_score"], "hi_score": b["hi_score"], "predicted": b["p"], "train_n": b["n"], "n": len(got),
                     "rugs": k, "observed": k / len(got) if got else None, "lo": ci[0] if ci else None, "hi": ci[1] if ci else None,
                     "inside": (ci[0] <= b["p"] <= ci[1]) if ci else None})
    return {"base_rate_train": base, "blocks": rows,
            "brier_scale": brier((apply_calibration(blocks, r["score"]), r["rug"]) for r in other) if blocks else None,
            "brier_constant": brier((base, r["rug"]) for r in other) if base is not None else None, "n": len(other)}


def feature_names(rows: Iterable[dict[str, Any]], profile: str) -> list[str]:
    """`signal:points` for every signal tier that fired at least once."""
    return sorted({f"{sid}:{pts}" for r in rows for sid, pts in r["cards"].get(profile, {}).get("fired", {}).items()})


# ---------------------------------------------------------------- the public summary (RGY-021 step 4)

HISTORY_NOTE = ("A backtest, not a track record: every launch is scored as of its launch time with rules that were fitted on earlier "
                "launches only, but the rules were chosen after the fact. The history is approximate (about 1 swap in 450 is lost "
                "when the history was loaded).")


SOURCES = ("all", "history", "live")
SHIFT_NOTE = ("The share of rugs is not the same in the two parts of the holdout. Launches loaded afterwards from the history are almost all "
              "from before 30 October; launches watched live start on 30 October. Time and source coincide, so the shift can be a real "
              "change in the market, a difference in how history launches are labelled, or both. We checked the labelling on live launches "
              "(the history route gave the same label for each of them), and activity has indeed fallen, so a real change is the likelier "
              "reading - but the two cannot be fully separated. That is why every band is compared with the base rate of its own "
              "source, and why this table is a ranking (are the higher bands worse than the lower ones?), not a probability.")
LABEL_NOTE = ("The rug labels were corrected after the holdout was first read (data quality, not tuning): a launch whose insiders' sold share "
              "does not exist is now called undefined only if the insiders actually sold something - if they sold nothing there was nothing to "
              "dump, and the launch is a plain no rug; undefined launches are left out of every number here. The weights of the rules were "
              "not refitted after the correction.")


def shift_note(hold_rows: list[dict[str, Any]]) -> str:
    """SHIFT_NOTE preceded by the numbers it talks about: the share of rugs among the definite holdout launches, history vs live."""
    parts = []
    for src, label in (("history", "loaded from the history"), ("live", "watched live")):
        part = [r for r in hold_rows if r["rug"] in (0, 1) and r.get("final", 1) and r["source"] == src]
        if part:
            parts.append(f"{label}: {sum(r['rug'] for r in part) / len(part):.0%} rugs of {len(part)}")
    return ("Holdout launches " + "; ".join(parts) + ". " if parts else "") + SHIFT_NOTE


def ranking_tables(hold_rows: list[dict[str, Any]], profile: str) -> dict[str, Any]:
    """Per source (all / history / live): the base rate of that source and, per band, n / rugs / rate / interval / lift over the base rate
    of the SAME source. Counted rows only (a final, definite yes / no: an undefined label is excluded). Plain data."""
    v = [r for r in view(hold_rows, profile) if counted(r)]
    out: dict[str, Any] = {}
    for src in SOURCES:
        part = [r for r in v if src == "all" or r["source"] == src]
        k = sum(r["rug"] for r in part)
        base = k / len(part) if part else None
        bands = []
        for b in BANDS:
            row = _rate_row([r for r in part if r["band"] == b], base)
            bands.append({"band": b, **{key: row[key] for key in ("n", "rugs", "rate", "lo", "hi", "base", "lift", "too_few")}})
        out[src] = {"n": len(part), "rugs": k, "base_rate": base, "bands": bands}
    return out


def history_summary(rows: Iterable[dict[str, Any]], split_ns: int, split_label: str, profiles: Iterable[str] = ("v0", "v1"),
                    calibrated: str = "v1", generated_at: str | None = None) -> dict[str, Any]:
    """The JSON the public /accuracy page shows under "Calibration on history (backtest)": HOLDOUT numbers only (plus the sizes of the
    train part), v0 and v1 side by side, and the score -> probability scale fitted on the train part and checked on the holdout.
    Everything is plain data (no rows, no tokens)."""
    train, hold = split(list(rows), split_ns)
    train_c = [r for r in train if r["rug"] in (0, 1)]
    hold_c = [r for r in hold if r["rug"] in (0, 1)]

    def size(part: list[dict[str, Any]]) -> dict[str, Any]:
        k = sum(r["rug"] for r in part)
        return {"n": len(part), "rugs": k, "base_rate": k / len(part) if part else None}

    out: dict[str, Any] = {"split": split_label, "generated_at": generated_at, "note": HISTORY_NOTE,
                           "train": size(train_c), "holdout": {**size(hold_c), "live": sum(1 for r in hold_c if r["source"] == "live")},
                           "profiles": {}, "calibration": {}}
    for prof in profiles:
        rep = report(hold, prof)
        out["profiles"][prof] = {"auc": rep["auc"], "by_band": rep["by_band"], "by_range": rep["by_range"], "coverage": rep["coverage"]}
        cal = calibration_check(view(train, prof), view(hold, prof))
        out["calibration"][prof] = {**cal, "fitted_blocks": len(cal["blocks"])}
        out.setdefault("ranking", {})[prof] = ranking_tables(hold, prof)
    out["calibrated"] = calibrated
    out["shift_note"] = shift_note(hold)
    out["label_note"] = LABEL_NOTE
    return out


def chance_for_score(history: dict[str, Any] | None, score: int, profile: str = "v1") -> dict[str, Any] | None:
    """The calibration row (predicted probability and the holdout interval of its score block) that holds `score`, or None."""
    if not history:
        return None
    blocks = (history.get("calibration", {}).get(profile) or {}).get("blocks") or []
    hit = None
    for b in blocks:
        if score >= b["lo_score"]:
            hit = b
    return hit if hit is not None else (blocks[0] if blocks else None)
