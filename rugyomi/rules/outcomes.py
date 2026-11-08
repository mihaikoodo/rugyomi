"""Outcome labels (RGY-015) — facts about what happened after a launch, computed from `launch_snapshots` and the
config thresholds. They are NOT the score's target (that is `rug`, see `rug.py`) and there is no accuracy table yet;
the thresholds live in the config (`outcomes` section).

  dev_exit     the dev had sold at least `sold_bps` of the tokens he acquired by T+30 min
  buyers_loss  the other buyers' net result at T+60 min is below `pnl_below_near` NEAR (default 0: a loss)

A label is True / False, or None when the needed snapshot does not exist yet, has an error, or lacks the number.
"""
from __future__ import annotations

from typing import Any

from .config import load_config

LABELS = ("dev_exit", "buyers_loss")


def outcome_labels(snapshots: dict[int, dict[str, Any]] | list[dict[str, Any]],
                   config: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """`snapshots`: rows of launch_snapshots of one launch (a list of dicts, or {minute: dict}) with at least
    minute, dev_sold_share_bps, buyers_pnl, error."""
    cfg = (config or load_config())["outcomes"]
    by_min = snapshots if isinstance(snapshots, dict) else {s["minute"]: s for s in snapshots}
    out: dict[str, dict[str, Any]] = {}

    s = by_min.get(cfg["dev_exit"]["minute"])
    ok = s is not None and not s.get("error") and s.get("dev_sold_share_bps") is not None
    out["dev_exit"] = {"value": (s["dev_sold_share_bps"] >= cfg["dev_exit"]["sold_bps"]) if ok else None,
                       "minute": cfg["dev_exit"]["minute"], "dev_sold_bps": s["dev_sold_share_bps"] if ok else None}

    s = by_min.get(cfg["buyers_loss"]["minute"])
    ok = s is not None and not s.get("error") and s.get("buyers_pnl") is not None
    pnl = int(s["buyers_pnl"]) / 10**24 if ok else None
    out["buyers_loss"] = {"value": (pnl < cfg["buyers_loss"]["pnl_below_near"]) if ok else None,
                          "minute": cfg["buyers_loss"]["minute"], "buyers_pnl_near": pnl}
    return out
