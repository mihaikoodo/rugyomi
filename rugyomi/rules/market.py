"""The market's own rug rate and what the score is worth (RGY-044).

Two readers' needs, one place, no numbers typed into a template:

`market_rate(con, asof_ns)`  the rug rate of the launches of the last 14 days AS OF a moment. The only function that computes it; the launch page (as of the card's issue
    time), the daily summary (as of the end of its day) and `/accuracy` (as of now) all call it. No lookahead: a launch counts only when its outcome was already KNOWN at `asof_ns`, i.e.
    it started at least `LAG_H` = 25 hours earlier (the rule of `walkforward.py`: the label is read at T+24 h, one more hour for the snapshot) and has a final, definite label (rug 0 / 1).
    A launch after that moment, or a label that became known later, cannot change the answer.
`market_text(m)`             the one line that shows it.
`walk_forward_text(summary)` the plain-words paragraph and the calibration caveat, made from the stored walk-forward summary (`collector_status.walk_forward_summary`,
    written by the server's walk-forward diagnostic run with `--write`; `walkforward.site_summary` builds it). With no summary: a neutral line.

Read-only: nothing here writes. The score, the weights, the bands, the labels and the cards are not touched."""
from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from .accuracy import wilson

NS = 10**9
DAYS = 14
LAG_H = 25                         # same as rules.walkforward.LAG_H
MIN_N = 30                         # fewer launches than this are shown with "too few to judge"
SUMMARY_KEY = "walk_forward_summary"
BANDS = ("Low", "Elevated", "High")


def window(asof_ns: int, days: int = DAYS, lag_h: float = LAG_H) -> tuple[int, int]:
    """(from_ns, to_ns]: launches that started after `asof - days` and at least `lag_h` hours before `asof`."""
    return asof_ns - days * 86400 * NS, asof_ns - int(lag_h * 3600) * NS


def market_rate(con: Any, asof_ns: int | None = None, days: int = DAYS, lag_h: float = LAG_H) -> dict[str, Any]:
    """{"asof_ns", "days", "from_ns", "to_ns", "n", "rugs", "rate", "lo", "hi", "too_few"}; rate / lo / hi are None for n = 0. `asof_ns` None = now.
    `con` is a sqlite3 connection or a Store. An old database without the tables gives n = 0."""
    con = getattr(con, "con", con)
    if asof_ns is None:
        asof_ns = int(dt.datetime.now(dt.timezone.utc).timestamp() * NS)
    lo_ns, hi_ns = window(asof_ns, days, lag_h)
    n = k = 0
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "launches" in have and "launch_outcomes" in have:
        row = con.execute("SELECT COUNT(*), COALESCE(SUM(o.rug), 0) FROM launches l JOIN launch_outcomes o ON o.token = l.token "
                          "WHERE o.final = 1 AND o.rug IN (0, 1) AND l.ts_ns > ? AND l.ts_ns <= ?", (lo_ns, hi_ns)).fetchone()
        n, k = int(row[0]), int(row[1])
    ci = wilson(k, n)
    return {"asof_ns": asof_ns, "days": days, "from_ns": lo_ns, "to_ns": hi_ns, "n": n, "rugs": k, "rate": (k / n) if n else None,
            "lo": ci[0] if ci else None, "hi": ci[1] if ci else None, "too_few": n < MIN_N}


def market_text(m: dict[str, Any], *, as_of: str | None = None) -> str:
    """'Rug rate of launches, last 14 days: 41 % (812 launches)'. `as_of` is a short phrase appended for a past moment ('as of the card's issue time')."""
    head = f"Rug rate, last {m['days']} days"
    tail = f" ({as_of})" if as_of else ""
    if not m["n"]:
        return f"{head}: no decided launches yet{tail}"
    s = f"{head}: {m['rate'] * 100:.0f} % ({m['n']} launches)"
    if m["too_few"]:
        s += " — too few to judge"
    return s + tail


def market_short(m: dict[str, Any]) -> str:
    """The same fact in a few words for a short post: 'market 14 d: 41 % (812)', a star when too few launches to judge, 'no data' when none."""
    if not m["n"]:
        return f"market {m['days']} d: no data"
    return f"market {m['days']} d: {m['rate'] * 100:.0f} % ({m['n']})" + (" *" if m["too_few"] else "")


def issued_ns(issued_at: str | None) -> int | None:
    """The card's `issued_at` (ISO, UTC, 'Z' optional) in ns."""
    if not issued_at:
        return None
    try:
        d = dt.datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp()) * NS + d.microsecond * 1000


def day_end_ns(day: str) -> int:
    """The end (00:00 UTC of the next day) of a UTC day 'YYYY-MM-DD'."""
    return int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()) * NS + 86400 * NS


def read_summary(con: Any) -> dict[str, Any] | None:
    """The stored walk-forward summary or None (no table, no key, bad JSON, not a dict with a version)."""
    import json
    con = getattr(con, "con", con)
    try:
        row = con.execute("SELECT value FROM collector_status WHERE key = ?", (SUMMARY_KEY,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        v = json.loads(row[0])
    except ValueError:
        return None
    return v if isinstance(v, dict) and v.get("version") == 1 and v.get("weeks") else None


def _pc(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}"


def _date(iso: str | None) -> str:
    return (iso or "")[:10]


NEUTRAL = "No walk-forward check of the score against the recent rug rate of the market has been stored yet."


def walk_forward_text(s: dict[str, Any] | None) -> dict[str, Any]:
    """{"paragraph", "caveat", "beats", "date"}: facts from the stored summary, no advice, no log-loss. One yardstick (the plain rug rate of the last 2 weeks), one rule ("better" = more than
    half of the compared weeks AND better on average), named in the text. The plain sentence about the score depends on the stored verdict (`beats_recent`)."""
    if not s or not s.get("n_weeks"):
        return {"paragraph": NEUTRAL, "caveat": None, "beats": None, "date": None}
    best, auc = s.get("best") or {}, s.get("auc") or {}
    weeks = f"{s['n_weeks']} weeks ({s['first_week']} to {s['last_week']}, {s['launches']} launches)"
    parts = [f"Walk-forward check of {_date(s.get('computed_at'))}: each of {weeks} was scored by models fitted only on launches whose outcome was already known when the week began, "
             "and compared with one fixed yardstick, the plain rug rate of the previous 2 weeks. A model counts as better only if it beat that rate in more than half of the compared weeks and on average."]
    if best and best.get("of"):
        head = f"The best model, {best['cell']}, beat that rate in {best['won']} of {best['of']} weeks"
        if best.get("passes"):
            parts.append(head + " and on average.")
        elif best["won"] * 2 > best["of"]:
            parts.append(head + " but lost on average; we do not count that as better.")
        else:
            parts.append(head + ".")
    if auc.get("min") is not None:
        parts.append(f"The AUC of the launch-time score ranged from {auc['min']:.2f} to {auc['max']:.2f} by week (0.50 = a coin flip).")
    if s.get("rate_min") is not None:
        parts.append(f"The weekly rug rate in those weeks ran from {_pc(s['rate_min'])} % to {_pc(s['rate_max'])} %.")
    beats = bool(s.get("beats_recent"))
    parts.append("In this check a model did beat the recent rug rate of the market by that rule." if beats
                 else "The launch-time score does not yet beat the recent rug rate of the market.")
    caveat = None
    w = s.get("worst_week")
    if w and w.get("gap") is not None and w["gap"] > 0:
        pred = " / ".join(_pc(b["predicted"]) for b in w["bands"])
        obs = " / ".join(_pc(b["observed"]) for b in w["bands"])
        caveat = (f"Calibration on history understates risk when the market shifts: in {w['week']} a model fitted on all earlier launches predicted rug rates of {pred} % "
                  f"for Low / Elevated / High, and {obs} % happened.")
    return {"paragraph": " ".join(parts), "caveat": caveat, "beats": beats, "date": _date(s.get("computed_at"))}
