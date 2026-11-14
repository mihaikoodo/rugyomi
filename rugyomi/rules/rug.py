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
2.53 — a rug). A launch whose pool is paired with another token than wNEAR has no NEAR amounts: all its labels are undefined (label version 5, `undefined_kind` "quote", RGY-066).
A label is True / False, or None while it cannot be decided (the snapshot is not there yet, has an
error, or was computed before schema v8 and lacks the insider metrics: run `rebuild-snapshots`).

UNDEFINED (RGY-024, label version 2; Q-012; narrowed by RGY-025, label version 3; Q-016; reasons and the price by RGY-026, version 4; Q-017). The insiders' sold SHARE does not exist when they
"acquired" nothing through the pool (`insiders_acquired` = net pool buys + the dev's mint in the launch tx: on justhoot v1 the dev does not
buy through the pool, on nearly the creator-fee tokens are not a buy). That alone is NOT "undefined": with nothing sold there was nothing
to dump, so "the insiders had not sold enough" is a true "no". The label is None with `undefined: True` and the reason UNDEFINED_REASON
only when the share is unknown AND the insiders DID sell (`insiders_sold` > 0 tokens, the gross tokens they sold into the pool: a field of
its own, not the share - the share is exactly what is missing) AND the outside buyers lost. Once the T+24 h snapshot exists (`final = 1`
in the stored row) it stays undefined, it is not "pending". When the buyers were not hurt, or nobody bought, or the insiders sold nothing,
it is a plain False. An undefined label is excluded from every rate and count and shown as "undefined". A late rug with a defined share
still wins: rug_fast undefined + rug_late True = rug. "Insiders sold" is read at the snapshot the label looks at: T+60 for rug_fast,
T+24 h (cumulative) for rug_late.
RGY-026 (Q-017): undefined means every OTHER condition of the rug test holds and one input is missing - the sold share (`undefined_kind` "share") or,
at T+24 h, the price ("price": `price_peak` / `price_last` missing, the label cannot say whether the price collapsed). The price condition itself
stays: with a known price that held, the label is a plain "no". See `_judge` for the order of the conditions and the reasons.
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from .config import load_config

NS_MIN = 60 * 10**9
YOCTO = 10**24
LABELS = ("rug_fast", "rug_late", "rug")
LABEL_VERSION = 5          # 1: RGY-016/017; 2: RGY-024 (the undefined case); 3: RGY-025 (undefined only when the insiders sold something);
                           # 4: RGY-026 (a missing T+24 h price is undefined, not a silent "no"; every "no" names the first failed condition)
                           # 5: RGY-066 (Q-046): a pool that is not paired with wNEAR is undefined (`undefined_kind` "quote"): its amounts are not NEAR
UNDEFINED_REASON = "insiders' sold share unknown"
PRICE_MISSING_REASON = "price missing at T+24 h"
QUOTE_REASON = "the pool is not paired with NEAR, the loss in NEAR is unknown"


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
        "insiders_sold": s.get("insiders_sold"),
    }


def _insiders_sold_any(s: dict[str, Any]) -> bool:
    """The insiders sold at least one token into the pool by this snapshot (gross `insiders_sold`; NOT the share, which is None
    exactly when nothing was "acquired")."""
    try:
        return int(s.get("insiders_sold") or 0) > 0
    except (TypeError, ValueError):
        return False


def _label(value: bool | None, reason: str, evidence: dict[str, Any] | None = None, short: str = "", **extra: Any) -> dict[str, Any]:
    """`short` (RGY-041): the clause the channel's outcome reply puts after «Rug» / «No rug» / «Undecided» — the label's own reason in a few
    words, with its number; written here so the wording lives in one place. Rows stored before it have none."""
    return {"value": value, "reason": reason, "evidence": evidence or {}, **({"short": short} if short else {}), **extra}


def _undefined(ev: dict[str, Any]) -> dict[str, Any]:
    return _label(None, UNDEFINED_REASON, ev, UNDEFINED_REASON, undefined=True)


def _after(sec: float) -> str:
    return f"{sec:g} s" if sec < 120 else f"{sec / 60:.0f} min" if sec < 7200 else f"{sec / 3600:.0f} h"


def _sold_clause(ev: dict[str, Any]) -> str:
    """«insiders sold 99 % in 35 s» (the time only when the crossing of the threshold is known)."""
    bps = ev.get("insiders_sold_bps")
    if bps is None:
        return "insiders sold"
    cross = ev.get("crossed_after_s")
    return f"insiders sold {bps / 100:.0f} %" + (f" in {_after(cross)}" if cross is not None else "")


def _judge(s: dict[str, Any], cfg: dict[str, Any], line: int, ev: dict[str, Any], when: str, ratio: float | None,
          price: bool = False) -> dict[str, Any]:
    """One branch of the rug test on ONE snapshot. The conditions, in this order: outside buyers exist; the insiders sold (their sold share
    reached the threshold, or the share does not exist and they sold something); [late only] the price fell to <= `price_max_share` of its
    peak; the outside buyers lost >= `buyers_loss_near`. All hold -> True. The first one that FAILS gives the "no", and its reason names the
    condition with its value (RGY-026). A condition whose input is missing (the sold share, the price) is not failed: if every other condition
    holds, the label is undefined and `undefined_kind` says which input is missing ("share", "price" or "share+price"); if another condition
    fails, that one is the reason - the missing input could not have changed the answer."""
    pnl = int(s["buyers_pnl"])
    if not s["buyers_count"]:
        return _label(False, "no outside buyers", ev, "no outside buyers")
    bps = s.get("insiders_sold_share_bps")
    gaps = []
    if bps is None:
        if not _insiders_sold_any(s):
            return _label(False, f"insiders' sold share unknown and the insiders sold nothing{when}", ev, "insiders sold nothing")
        gaps.append("share")
    elif bps < cfg["insiders_sold_bps"]:
        return _label(False, f"insiders had sold {bps / 100:.1f} % of what they acquired{when}, below the {cfg['insiders_sold_bps'] / 100:g} % threshold", ev,
                      f"insiders sold {bps / 100:.0f} %, under the {cfg['insiders_sold_bps'] / 100:g} % line")
    if price:
        if ratio is None:
            gaps.append("price")
        elif ratio > cfg["price_max_share"]:
            return _label(False, f"the price held at {ratio * 100:.0f} % of its peak (a rug needs {cfg['price_max_share'] * 100:g} % or less)", ev,
                          f"price held at {ratio * 100:.0f} % of peak")
    if pnl > line:
        return _label(False, f"outside buyers' net result {pnl / YOCTO:+.2f} NEAR{when}, short of the {cfg['buyers_loss_near']} NEAR loss a rug needs", ev,
                      f"buyers' loss under {cfg['buyers_loss_near']:g} NEAR")
    if gaps:
        kind = "+".join(gaps)
        why = UNDEFINED_REASON if "share" in gaps else PRICE_MISSING_REASON
        return _label(None, why, ev, why, undefined=True, undefined_kind=kind)
    return _label(True, "insiders sold and the outside buyers lost" if not price
                  else "by T+24 h the insiders had sold, the price collapsed and the outside buyers lost", ev,
                  _sold_clause(ev) + (f", price fell to {ratio * 100:.0f} % of peak" if price and ratio is not None else ""))


def _quote_labels(quote: str) -> dict[str, Any]:
    """RGY-066 (Q-046): every label of a launch whose pool is paired with `quote` (not wNEAR) is undefined: the loss in NEAR is unknown."""
    why = f"{QUOTE_REASON} (quote: {quote})"

    def one() -> dict[str, Any]:
        return _label(None, why, {"quote_token": quote}, QUOTE_REASON, undefined=True, undefined_kind="quote")
    return {"rug_fast": one(), "rug_late": one(), "rug": one(), "severity_near": None}


def rug_labels(snapshots: dict[int, dict[str, Any]] | list[dict[str, Any]],
               config: dict[str, Any] | None = None, quote: str | None = None) -> dict[str, Any]:
    """`snapshots`: the launch_snapshots rows of ONE launch (list or {minute: row}) with the v8 columns. Returns
    {"rug_fast": label, "rug_late": label, "rug": label, "severity_near": float | None}; a label is
    {"value": True | False | None, "reason": str, "evidence": {...}}."""
    cfg = (config or load_config())["rug"]
    by_min = snapshots if isinstance(snapshots, dict) else {s["minute"]: s for s in snapshots}
    if quote:                                       # RGY-066 (Q-046): `quote` = the quote token when the pool is not paired with wNEAR (`quote.not_near_quote`)
        return _quote_labels(quote)
    line = _loss_line(cfg)

    # ---- rug_fast: the state at T+60
    s = by_min.get(cfg["fast_minute"])
    why = _usable(s)
    if why:
        fast = _label(None, why)
    else:
        fast = _judge(s, cfg, line, _evidence(s, cfg), "", None)

    # ---- rug_late: the state at T+24 h, given the state at T+60
    s = by_min.get(cfg["late_minute"])
    if fast["value"] is True:
        late = _label(False, "already rug_fast")
    elif fast["value"] is None and not fast.get("undefined"):
        late = _label(None, "waiting for the T+60 snapshot" if fast["reason"] == "pending" else fast["reason"])
    elif (why := _usable(s)):
        late = _label(None, why)
    else:
        ev = _evidence(s, cfg)
        peak, last = s.get("price_peak"), s.get("price_last")
        ratio = float(Decimal(last) / Decimal(peak)) if peak and last and Decimal(peak) > 0 else None
        ev["price_vs_peak"] = round(ratio, 4) if ratio is not None else None
        late = _judge(s, cfg, line, ev, " by T+24 h", ratio, price=True)

    # ---- rug and severity
    if fast["value"] is True or late["value"] is True:
        rug = _label(True, "rug_fast" if fast["value"] else "rug_late", None, (fast if fast["value"] else late).get("short", ""))
    elif fast["value"] is False and late["value"] is False:
        rug = _label(False, "neither rug_fast nor rug_late", None, late.get("short") or fast.get("short", ""))
    elif (fast.get("undefined") or late.get("undefined")) and all(x["value"] is False or x.get("undefined") for x in (fast, late)):
        kinds = {k for x in (fast, late) if x.get("undefined") for k in x["undefined_kind"].split("+")}
        why = UNDEFINED_REASON if "share" in kinds else PRICE_MISSING_REASON
        rug = _label(None, why, None, why, undefined=True, undefined_kind="+".join(k for k in ("share", "price") if k in kinds))
    else:
        rug = _label(None, "pending")
    # the evidence shown with `rug`: the deciding snapshot, else the latest one that exists
    src = fast if fast["value"] else late if (late["value"] or late["evidence"]) else fast
    rug["evidence"] = src["evidence"]
    sev = -src["evidence"]["buyers_pnl_near"] if rug["value"] else None
    return {"rug_fast": fast, "rug_late": late, "rug": rug, "severity_near": sev}
