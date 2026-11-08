"""The accuracy table (RGY-018): how well did the frozen risk cards forecast the stored outcomes?

`summarize(rows, cfg)` is pure (no database, no clock): rows in, tables out, so the public `/accuracy` page and the private
backtest diag print the very same numbers. `card_outcome_rows(con)` is the only reader (cards JOIN launch_outcomes); it
never writes.

Rules fixed by the planner (master §1, §5, §9):
  - only launches that have a CARD are counted (our live forecasts; a backtest is not a track record);
  - the target is `rug` (rug within 24 h); `dev_exit` / `buyers_loss` are secondary columns;
  - only `final = 1` outcomes are counted; the rest are shown as "waiting", never as "no rug";
  - a rate is always shown next to the base rate (rugs / final launches) and as a lift over it, with a 90 % Wilson interval;
  - a row with fewer than `MIN_N` launches is kept but marked "too few to judge".
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Iterable

MIN_N = 10
Z90 = 1.6448536269514722                      # two-sided 90 % interval
BANDS = ("Low", "Elevated", "High")
SCORE_RANGES = ((0, 19), (20, 39), (40, 59), (60, 79), (80, 100))
ROW_FIELDS = ("token", "launch_ts_ns", "issued_at", "score", "band", "config_version", "rug", "final", "severity_near",
              "dev_exit", "buyers_loss")


def wilson(k: int, n: int, z: float = Z90) -> tuple[float, float] | None:
    """The Wilson score interval of k successes in n trials; None for n = 0."""
    if n <= 0:
        return None
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def card_outcome_rows(con: Any) -> list[dict[str, Any]]:
    """Every card with its stored outcome (None fields when the launch has no outcome row yet). `con` is a sqlite3
    connection or a Store. Tables missing (an old database) → []."""
    con = getattr(con, "con", con)
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "cards" not in have:
        return []
    if "launch_outcomes" in have:
        sql = ("SELECT c.token, c.launch_ts_ns, c.issued_at, c.score, c.band, c.config_version, o.rug, o.final, "
               "o.severity_near, o.dev_exit, o.buyers_loss FROM cards c LEFT JOIN launch_outcomes o ON o.token = c.token "
               "ORDER BY c.launch_ts_ns DESC, c.token")
    else:
        sql = ("SELECT c.token, c.launch_ts_ns, c.issued_at, c.score, c.band, c.config_version, NULL, NULL, NULL, NULL, NULL "
               "FROM cards c ORDER BY c.launch_ts_ns DESC, c.token")
    return [dict(zip(ROW_FIELDS, r)) for r in con.execute(sql)]


def counted(r: dict[str, Any]) -> bool:
    """A row enters the rates only with a FINAL outcome that is a definite yes / no."""
    return bool(r.get("final")) and r.get("rug") in (0, 1)


def outcome_state(r: dict[str, Any]) -> str:
    """'rug' / 'no rug' / 'waiting' — the text of one launch. A rug found at T+60 is already a rug; a 'no' is only a
    'no rug' once final."""
    if r.get("rug") == 1:
        return "rug"
    if counted(r):
        return "no rug"
    return "waiting"


def _rate_row(rows: list[dict[str, Any]], base: float | None) -> dict[str, Any]:
    """n / rugs / rate / interval / lift / mean score / secondary shares over the COUNTED rows among `rows`."""
    c = [r for r in rows if counted(r)]
    n, k = len(c), sum(1 for r in c if r["rug"] == 1)
    rate = k / n if n else None
    ci = wilson(k, n)
    sec = {}
    for key in ("dev_exit", "buyers_loss"):
        known = [r[key] for r in c if r.get(key) in (0, 1)]
        sec[key] = (sum(known) / len(known)) if known else None
    return {"cards": len(rows), "n": n, "rugs": k, "rate": rate,
            "lo": ci[0] if ci else None, "hi": ci[1] if ci else None, "base": base,
            "lift": (rate / base) if (rate is not None and base) else None,
            "mean_score": (sum(r["score"] for r in rows) / len(rows)) if rows else None,
            "dev_exit": sec["dev_exit"], "buyers_loss": sec["buyers_loss"], "too_few": n < MIN_N}


def _day(ns: int) -> str:
    return dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).strftime("%Y-%m-%d")


def range_label(lo: int, hi: int) -> str:
    return f"{lo}–{hi}"


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """All the tables of the accuracy page from card/outcome rows (see `ROW_FIELDS`)."""
    rows = list(rows)
    cnt = [r for r in rows if counted(r)]
    rugs = [r for r in cnt if r["rug"] == 1]
    base = (len(rugs) / len(cnt)) if cnt else None
    ts = [r["launch_ts_ns"] for r in rows if r["launch_ts_ns"] is not None]
    summary = {"cards": len(rows), "final": len(cnt), "waiting": len(rows) - len(cnt), "rugs": len(rugs), "base_rate": base,
               "first_launch_ns": min(ts) if ts else None, "last_launch_ns": max(ts) if ts else None}

    by_band = [{"band": b, **_rate_row([r for r in rows if r["band"] == b], base)} for b in BANDS]
    by_range = [{"range": range_label(lo, hi), "lo_score": lo, "hi_score": hi,
                 **_rate_row([r for r in rows if lo <= r["score"] <= hi], base)} for lo, hi in SCORE_RANGES]

    # coverage: of all rugs, how many had an Elevated or High card (by count and by the NEAR the outside buyers lost)
    flagged = [r for r in rugs if r["band"] in ("Elevated", "High")]      # the band the card itself carries
    sev = lambda xs: sum(float(r["severity_near"] or 0) for r in xs)  # noqa: E731
    coverage = {"rugs": len(rugs), "flagged": len(flagged),
                "share": (len(flagged) / len(rugs)) if rugs else None,
                "severity_total": sev(rugs), "severity_flagged": sev(flagged),
                "severity_share": (sev(flagged) / sev(rugs)) if rugs and sev(rugs) > 0 else None}

    days: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["launch_ts_ns"] is not None:
            days.setdefault(_day(r["launch_ts_ns"]), []).append(r)
    by_day = []
    for d in sorted(days, reverse=True):
        x = _rate_row(days[d], base)
        by_day.append({"day": d, "cards": x["cards"], "final": x["n"], "rugs": x["rugs"], "rate": x["rate"],
                       "mean_score": x["mean_score"]})

    versions: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        versions.setdefault(r["config_version"], []).append(r)
    by_config = []
    for v in sorted(versions):
        rs = versions[v]
        by_config.append({"config_version": v, "cards": len(rs), "final": sum(1 for r in rs if counted(r)),
                          "bands": [{"band": b, **{k: x[k] for k in ("n", "rugs", "rate", "too_few")}}
                                    for b in BANDS for x in [_rate_row([r for r in rs if r["band"] == b], base)]]})

    launches = [{"token": r["token"], "launch_ts_ns": r["launch_ts_ns"], "score": r["score"], "band": r["band"],
                 "state": outcome_state(r), "severity_near": r["severity_near"]} for r in rows]
    return {"summary": summary, "by_band": by_band, "by_range": by_range, "coverage": coverage, "by_day": by_day,
            "by_config": by_config, "launches": launches, "min_n": MIN_N}
