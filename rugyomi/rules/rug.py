"""The rug label (RGY-016) — the target the risk score is calibrated against: "a rug within 24 h".

On a locked-liquidity launchpad nobody can pull the pool, so a rug is a SOFT rug: the insiders (the dev and every
account the dev sent launch tokens to, see `rugyomi.snapshots`) sell into the outside buyers. "Any dump" is not a label
(almost every token has one) and neither is the dev exit alone: a label ties WHO sold to what the OTHER buyers lost.

  rug_fast   at T+60 min: the insiders had sold at least `insiders_sold_bps` of the tokens they acquired, AND the
             outside buyers' net result is at or below -`buyers_loss_near` NEAR.
  rug_late   not rug_fast, and at T+24 h all three hold: the insiders had sold at least `insiders_sold_bps`, the price is
             at most `price_max_share` of the post-launch peak, and the outside buyers' net result is at or below
             -`buyers_loss_near` NEAR (outside buyers present). When the insiders crossed the threshold is evidence
             (`crossed_after_s`), not a condition: the damage may show only later (RGY-017, variant B).
  rug        rug_fast or rug_late. No outside buyers -> nobody was hurt -> not a rug.
  severity_near   the outside buyers' loss in NEAR at the snapshot that made it a rug (None when not a rug).

The insiders' own result is reported as a fact and is never required (REFRAX: the dev made +0.14 NEAR, the buyers lost
2.53 — a rug). A label is True / False, or None while it cannot be decided (the snapshot is not there yet, has an
error, or was computed before schema v8 and lacks the insider metrics: run `rebuild-snapshots`).
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .config import load_config

NS_MIN = 60 * 10**9
YOCTO = 10**24
LABELS = ("rug_fast", "rug_late", "rug")


def _usable(s: dict[str, Any] | None) -> str | None:
    """None when the snapshot can be used, else why not."""
    if s is None:
        return "pending"
    if s.get("error"):
        return "snapshot error"
    if s.get("insiders_acquired") is None or s.get("buyers_pnl") is None:
        return "snapshot lacks insider metrics (rebuild-snapshots)"
    return None


def _loss_line(cfg: dict[str, Any]) -> int:
    return -int(Decimal(str(cfg["buyers_loss_near"])) * YOCTO)


def _near(x: Any) -> float | None:
    return round(int(x) / YOCTO, 4) if x is not None else None


def _evidence(s: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    raw = s.get("insiders_evidence")
    ev = json.loads(raw) if isinstance(raw, str) and raw else (raw or {})
    launch_ts = s["at_ts_ns"] - s["minute"] * NS_MIN
    key = next((k for k in range(500, 10_001, 500) if k >= cfg["insiders_sold_bps"]), None)
    cross = ev.get("crossings", {}).get(str(key)) if key else None
    return {
        "minute": s["minute"], "insiders_sold_bps": s.get("insiders_sold_share_bps"),
        "insiders_count": s.get("insiders_count"), "sellers": ev.get("sellers", []),
        "recipients": ev.get("recipients", []), "first_sell_tx": ev.get("first_sell_tx"),
        "crossed_at_ns": cross[0] if cross else None, "crossed_tx": cross[1] if cross else None,
        "crossed_after_s": round((cross[0] - launch_ts) / 1e9, 1) if cross else None,
        "buyers_count": s.get("buyers_count"), "buyers_pnl_near": _near(s["buyers_pnl"]),
        "insiders_pnl_near": _near(s.get("insiders_pnl")),
    }


def _sold(s: dict[str, Any], cfg: dict[str, Any]) -> bool:
    return s.get("insiders_sold_share_bps") is not None and s["insiders_sold_share_bps"] >= cfg["insiders_sold_bps"]


def _label(value: bool | None, reason: str, evidence: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"value": value, "reason": reason, "evidence": evidence or {}, **extra}


def rug_labels(snapshots: dict[int, dict[str, Any]] | list[dict[str, Any]],
               config: dict[str, Any] | None = None) -> dict[str, Any]:
    """`snapshots`: the launch_snapshots rows of ONE launch (list or {minute: row}) with the v8 columns. Returns
    {"rug_fast": label, "rug_late": label, "rug": label, "severity_near": float | None}; a label is
    {"value": True | False | None, "reason": str, "evidence": {...}}."""
    cfg = (config or load_config())["rug"]
    by_min = snapshots if isinstance(snapshots, dict) else {s["minute"]: s for s in snapshots}
    line = _loss_line(cfg)

    # ---- rug_fast: the state at T+60
    s = by_min.get(cfg["fast_minute"])
    why = _usable(s)
    if why:
        fast = _label(None, why)
    else:
        ev = _evidence(s, cfg)
        pnl = int(s["buyers_pnl"])
        if not s["buyers_count"]:
            fast = _label(False, "no outside buyers", ev)
        elif not _sold(s, cfg):
            fast = _label(False, "insiders had not sold enough", ev)
        elif pnl > line:
            fast = _label(False, "outside buyers were not hurt", ev)
        else:
            fast = _label(True, "insiders sold and the outside buyers lost", ev)

    # ---- rug_late: the state at T+24 h, given the state at T+60
    s = by_min.get(cfg["late_minute"])
    if fast["value"] is True:
        late = _label(False, "already rug_fast")
    elif fast["value"] is None:
        late = _label(None, "waiting for the T+60 snapshot" if fast["reason"] == "pending" else fast["reason"])
    elif (why := _usable(s)):
        late = _label(None, why)
    else:
        ev = _evidence(s, cfg)
        pnl = int(s["buyers_pnl"])
        peak, last = s.get("price_peak"), s.get("price_last")
        ratio = float(Decimal(last) / Decimal(peak)) if peak and last and Decimal(peak) > 0 else None
        ev["price_vs_peak"] = round(ratio, 4) if ratio is not None else None
        if not s["buyers_count"]:
            late = _label(False, "no outside buyers", ev)
        elif not _sold(s, cfg):
            late = _label(False, "insiders had not sold enough by T+24 h", ev)
        elif ratio is None or ratio > cfg["price_max_share"]:
            late = _label(False, "the price held above the cut-off share of its peak", ev)
        elif pnl > line:
            late = _label(False, "outside buyers were not hurt", ev)
        else:
            late = _label(True, "by T+24 h the insiders had sold, the price collapsed and the outside buyers lost", ev)

    # ---- rug and severity
    if fast["value"] is True or late["value"] is True:
        rug = _label(True, "rug_fast" if fast["value"] else "rug_late")
    elif fast["value"] is False and late["value"] is False:
        rug = _label(False, "neither rug_fast nor rug_late")
    else:
        rug = _label(None, "pending")
    # the evidence shown with `rug`: the deciding snapshot, else the latest one that exists
    src = fast if fast["value"] else late if (late["value"] or late["evidence"]) else fast
    rug["evidence"] = src["evidence"]
    sev = -src["evidence"]["buyers_pnl_near"] if rug["value"] else None
    return {"rug_fast": fast, "rug_late": late, "rug": rug, "severity_near": sev}
