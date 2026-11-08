"""Signal registry v0 (RGY-015) — the single source of the signals: ids, versions, descriptions (the methodology page
is generated from here), thresholds (read from the config, never hard-coded) and the computation.

A signal is `compute(ctx) -> Result` where ctx is a rules.context.LaunchContext: the ONLY way a signal reads the
database, and it only returns what was knowable at the launch time (no lookahead — enforced in the context, and a test
checks that this module has no database access of its own). Initial signals use pre-launch data plus the launch itself;
what the dev does after the launch is shown as facts on the page, never as part of the initial score.

Result: {id, version, fired, value, contribution, reason, evidence: [{kind: tx | account | launch, id}]}.
Reasons describe behaviour with numbers and evidence; they never name a person's intent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .context import NS_DAY, NS_H, NS_MIN, LaunchContext, name_keys, near


@dataclass(frozen=True)
class Signal:
    id: str
    version: int
    describe: Callable[[dict[str, Any]], str]          # EN text for the methodology page, from the signal's config
    compute: Callable[[LaunchContext, dict[str, Any]], dict[str, Any]]


def _res(sig_id: str, version: int, fired: bool = False, value: Any = None, points: int = 0, reason: str = "",
         evidence: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"id": sig_id, "version": version, "fired": fired, "value": value, "contribution": points if fired else 0,
            "reason": reason if fired else "", "evidence": evidence or []}


def _ago(ns: int) -> str:
    m = ns / NS_MIN
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def _n(x: float) -> str:
    return f"{x:,.2f} NEAR"


def _pct(bps: int) -> str:
    return f"{bps / 100:.0f}%"


# ---------------------------------------------------------------- fingerprint

def _fingerprint(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "fingerprint", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    win = int(c["window_min"] * NS_MIN)
    created = ctx.created(ctx.dev)
    fresh = created is not None and ctx.asof_ns - created["ts_ns"] < win
    funds = ctx.inflows(ctx.dev, win, c["min_funding_near"])
    funded = bool(funds)
    total = sum(f["near"] for f in funds)
    spent = near(ctx.dev_buy)
    spends = funded and spent >= c["dev_buy_share"] * total
    n = int(fresh) + int(funded) + int(spends)
    value = {"fresh_account": fresh, "funded": funded, "dev_buy_vs_funding": spends, "conditions_met": n,
             "funding_near": round(total, 4), "dev_buy_near": round(spent, 4)}
    if n < 2:
        return _res(sid, v, value=value)
    pts = c["points_3_of_3"] if n == 3 else c["points_2_of_3"]
    parts, ev = [], []
    if fresh:
        parts.append(f"the creator account was created {_ago(ctx.asof_ns - created['ts_ns'])} before the launch")
        ev.append({"kind": "tx", "id": created["tx"]} if created.get("tx") else {"kind": "account", "id": ctx.dev})
    if funded:
        big = max(funds, key=lambda f: f["near"])
        parts.append(f"it received {_n(total)} {_ago(ctx.asof_ns - big['ts_ns'])} before the launch "
                     f"(largest transfer from {big['sender']})")
        ev.append({"kind": "tx", "id": big["tx"]} if big["tx"] else {"kind": "account", "id": big["sender"]})
    if spends:
        parts.append(f"the dev buy of {_n(spent)} is {spent / total * 100:.0f}% of that funding")
    return _res(sid, v, True, value, pts, f"Fresh creator setup ({n} of 3): " + "; ".join(parts) + ".", ev)


def _fingerprint_doc(c: dict[str, Any]) -> str:
    return (f"A fresh, single-purpose creator setup. Three conditions, each looked at as of the launch: (1) the creator "
            f"account was created less than {c['window_min']} min before the launch; (2) it received at least "
            f"{c['min_funding_near']} NEAR from a non-service account within {c['window_min']} min before the launch; "
            f"(3) the dev buy is at least {c['dev_buy_share'] * 100:.0f}% of that funding. Two of three adds "
            f"{c['points_2_of_3']} points, three of three {c['points_3_of_3']}.")


# ---------------------------------------------------------------- funded_by_dumper

def _dumped(ctx: LaunchContext, token: str, c: dict[str, Any]) -> dict[str, Any] | None:
    for m in c["minutes"]:
        s = ctx.snapshot(token, m)
        if s and s["dev_sold_share_bps"] is not None and s["dev_sold_share_bps"] >= c["dumper_sold_bps"]:
            return s
    return None


def _funded_by_dumper(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "funded_by_dumper", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    funds = ctx.inflows(ctx.dev, int(c["lookback_h"] * NS_H), c["min_funding_near"])
    checked: dict[str, dict[str, Any] | None] = {}
    for f in funds:                                                     # newest transfer first
        sender = f["sender"]
        if sender not in checked:
            checked[sender] = None
            for prior in ctx.prior_launches(sender):                   # newest launch first
                s = _dumped(ctx, prior["token"], c)
                if s:
                    checked[sender] = {"token": prior["token"], "snap": s}
                    break
        hit = checked[sender]
        if hit:
            s = hit["snap"]
            value = {"funder": sender, "funding_near": round(f["near"], 4), "earlier_launch": hit["token"],
                     "dev_sold_bps": s["dev_sold_share_bps"], "at_minute": s["minute"]}
            reason = (f"Funded with {_n(f['near'])} {_ago(ctx.asof_ns - f['ts_ns'])} before the launch by {sender}, who "
                      f"launched {hit['token']} earlier: its dev had sold {_pct(s['dev_sold_share_bps'])} of the tokens "
                      f"within {s['minute']} min.")
            ev = [{"kind": "tx", "id": f["tx"]} if f["tx"] else {"kind": "account", "id": sender},
                  {"kind": "launch", "id": hit["token"]}]
            return _res(sid, v, True, value, c["points"], reason, ev)
    return _res(sid, v, value={"funders_checked": sorted(checked)})


def _funded_by_dumper_doc(c: dict[str, Any]) -> str:
    mins = " or ".join(f"T+{m}" for m in c["minutes"])
    return (f"The creator (or the real owner behind a launch bot) received at least {c['min_funding_near']} NEAR or wNEAR "
            f"in the {c['lookback_h']} h before the launch from an account that launched an earlier token whose dev "
            f"had sold at least {c['dumper_sold_bps'] / 100:.0f}% of the tokens by {mins} minutes — a snapshot that "
            f"already existed at the launch time. Adds {c['points']} points.")


# ---------------------------------------------------------------- serial_launcher

def _serial_launcher(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    group = c.get("scope") == "group"
    sid, v = "serial_launcher", 3 if group else 2
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    if not group:
        prior = ctx.prior_launches(ctx.dev, int(c["window_h"] * NS_H))
    else:                                                              # RGY-021: the creator AND the accounts linked to him
        prior = ctx.group_launches(ctx.dev, int(c["window_h"] * NS_H))
    n = len(prior)
    pts = 0
    if n >= c["min_prior_low"]:
        pts = (c["points_high"] if n >= c["min_prior_high"] else c["points"] if n >= c["min_prior"] else c["points_low"])
    value: dict[str, Any] = {"prior_launches": n}
    ev = [{"kind": "launch", "id": r["token"]} for r in prior[:5]]
    who = " by the creator and the accounts linked to him" if group else ""
    reason = f"{n} other launches{who} in the {c['window_h']} h before this one (all launchpads, all sources)."
    if group and c.get("window_long_d"):                               # the longer window: a slower but steady serial launcher
        long_prior = ctx.group_launches(ctx.dev, int(c["window_long_d"] * NS_DAY))
        value["prior_launches_long"] = len(long_prior)
        if len(long_prior) >= c["min_prior_long"] and c["points_long"] > pts:
            pts = c["points_long"]
            ev = [{"kind": "launch", "id": r["token"]} for r in long_prior[:5]]
            reason = (f"{len(long_prior)} other launches{who} in the {c['window_long_d']:g} days before this one "
                      f"(all launchpads, all sources).")
    if not pts:
        return _res(sid, v, value=value)
    return _res(sid, v, True, value, pts, f"The creator made {reason}" if not group else f"The creator's group made {reason}", ev)


def _serial_launcher_doc(c: dict[str, Any]) -> str:
    base = (f"How many other launches the creator (or real owner) made in the {c['window_h']} h before this one, across "
            f"all launchpads and sources. {c['min_prior_low']} adds {c['points_low']} points, {c['min_prior']} adds "
            f"{c['points']}, {c['min_prior_high']} or more adds {c['points_high']}.")
    if c.get("scope") == "group":
        base = base.replace("the creator (or real owner)", "the creator (or real owner) and the accounts linked to him "
                            "by recorded links as of the launch time")
        if c.get("window_long_d"):
            base += (f" Also counted over {c['window_long_d']:g} days: {c['min_prior_long']} or more adds {c['points_long']} "
                     f"(the higher of the two applies).")
    return base


# ---------------------------------------------------------------- prior_fast_dump

def _prior_fast_dump(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "prior_fast_dump", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    for r in ctx.prior_launches(ctx.dev):
        if r["coverage"] != "live" and not ctx.read_backfill:
            continue                                                    # profile v0: history launches have no snapshots
        s = ctx.snapshot(r["token"], c["minute"])
        if s and s["dev_sold_share_bps"] is not None and s["dev_sold_share_bps"] >= c["sold_bps"]:
            return _res(sid, v, True, {"earlier_launch": r["token"], "dev_sold_bps": s["dev_sold_share_bps"]},
                        c["points"], f"An earlier launch of this creator, {r['token']}, had its dev sell "
                        f"{_pct(s['dev_sold_share_bps'])} of the tokens within {c['minute']} min.",
                        [{"kind": "launch", "id": r["token"]}])
    return _res(sid, v, value={"earlier_fast_dump": False})


def _prior_fast_dump_doc(c: dict[str, Any]) -> str:
    return (f"The creator (or real owner) has an earlier launch seen live whose dev had sold at least "
            f"{c['sold_bps'] / 100:.0f}% of the tokens by T+{c['minute']} min, from a snapshot that already existed at "
            f"the launch time. Adds {c['points']} points.")


# ---------------------------------------------------------------- name_copy

def _name_copy(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "name_copy", 1
    mine = ctx.my_name_keys()
    if not mine:
        return _res(sid, v, value={"skipped": "no name"})
    me = ctx.dev
    for r in ctx.launches_window(int(c["other_creator_days"] * NS_DAY)):          # newest first
        theirs = r.get("owner") or r.get("creator")
        if not theirs or (me and theirs == me):
            continue
        common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
        if common:
            k = sorted(common)[0]
            return _res(sid, v, True, {"kind": "other_creator", "name": k, "other_launch": r["token"]}, c["points"],
                        f"The name '{k}' was already used {_ago(ctx.asof_ns - r['ts_ns'])} earlier by another creator "
                        f"({r['token']}).", [{"kind": "launch", "id": r["token"]}])
    if me:
        for r in ctx.launches_window(int(c["same_creator_h"] * NS_H)):
            if (r.get("owner") or r.get("creator")) != me or r["launchpad"] == ctx.launch["launchpad"]:
                continue
            common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
            if common:
                k = sorted(common)[0]
                return _res(sid, v, True, {"kind": "same_creator_other_launchpad", "name": k, "other_launch": r["token"]},
                            c["points"], f"The same creator launched the name '{k}' on another launchpad "
                            f"{_ago(ctx.asof_ns - r['ts_ns'])} earlier ({r['token']}).",
                            [{"kind": "launch", "id": r["token"]}])
    return _res(sid, v, value={"copy": False})


def _name_copy_doc(c: dict[str, Any]) -> str:
    return (f"The launch reuses a name already launched: by a different creator in the previous {c['other_creator_days']} "
            f"days, or by the same creator on another launchpad in the previous {c['same_creator_h']} h. Names are "
            f"compared by the token id (without a -2 style suffix), symbol and name, case-insensitively. "
            f"Adds {c['points']} points.")


# ---------------------------------------------------------------- creator_tax

def _creator_tax(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "creator_tax", 1
    t = ctx.tax_options()
    bps = (t or {}).get("creator_bps")
    if not isinstance(bps, int) or bps < c["min_bps"]:
        return _res(sid, v, value={"creator_bps": bps})
    return _res(sid, v, True, {"creator_bps": bps}, c["points"],
                f"The launch is set up so that the creator takes {bps / 100:.2f}% of trades as a tax (nearly creator_bps = {bps}).",
                [{"kind": "launch", "id": ctx.token}])


def _creator_tax_doc(c: dict[str, Any]) -> str:
    return (f"A nearly launch whose tax options give the creator a share of every trade (creator_bps of at least "
            f"{c['min_bps']}). Adds {c['points']} points.")


# ---------------------------------------------------------------- creator_track_record (RGY-021)

def _smoothed(rugs: int, n: int, c: dict[str, Any]) -> float:
    """Rug share of the earlier launches pulled toward the prior (the base rate) by `prior_strength` pseudo-launches, so one rug
    out of one launch is a signal but not a certainty."""
    return (rugs + c["prior_strength"] * c["prior_rate"]) / (n + c["prior_strength"])


def _creator_track_record(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "creator_track_record", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    own_n = own_rugs = grp_n = grp_rugs = 0
    rugged: list[dict[str, Any]] = []
    for r in ctx.group_launches(ctx.dev):                              # newest first
        o = ctx.outcome(r["token"])
        if o["rug"] is None:
            continue                                                    # not decided at the launch time
        mine = r["via"] == ctx.dev
        own_n += mine
        grp_n += not mine
        if o["rug"]:
            own_rugs += mine
            grp_rugs += not mine
            rugged.append(r)
    n, rugs = own_n + grp_n, own_rugs + grp_rugs
    share = _smoothed(rugs, n, c)
    value = {"known_launches": n, "rugs": rugs, "share": round(share, 3), "own_known": own_n, "own_rugs": own_rugs,
             "group_known": grp_n, "group_rugs": grp_rugs, "group_size": len(ctx.group(ctx.dev))}
    if rugs < c["min_rugs"] or share < c["share_mid"]:
        return _res(sid, v, value=value)
    high = share >= c["share_high"]
    ev = [{"kind": "launch", "id": r["token"]} for r in rugged[:5]]
    where = f"{own_rugs} by the creator and {grp_rugs} by accounts linked to him" if grp_rugs else f"{own_rugs} by the creator"
    return _res(sid, v, True, value, c["points_high"] if high else c["points_mid"],
                f"{rugs} of the {n} earlier launches that were already decided at launch time ended as a rug ({where}); "
                f"smoothed rug share {share * 100:.0f}%.", ev)


def _creator_track_record_doc(c: dict[str, Any]) -> str:
    return (f"The earlier launches of the creator (or real owner) and of the accounts linked to him by recorded links as of the "
            f"launch time, counting only those whose outcome was already decided at the launch time (rug_fast from 60 min "
            f"after that launch, rug_late from 24 h). The rug share is smoothed with {c['prior_strength']:g} imaginary "
            f"launches at the base rate {c['prior_rate'] * 100:.0f}%. At least {c['min_rugs']} rug and a share of "
            f"{c['share_mid'] * 100:.0f}% adds {c['points_mid']} points, {c['share_high'] * 100:.0f}% or more adds "
            f"{c['points_high']}.")


# ---------------------------------------------------------------- funded_by_rugger (RGY-021)

def _funded_by_rugger(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "funded_by_rugger", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    funds = ctx.funders(ctx.dev, int(c["lookback_days"] * NS_DAY) if c.get("lookback_days") else None, c["min_funding_near"],
                        int(c["max_funders"]))
    hits: list[dict[str, Any]] = []
    checked: set[str] = set()
    for f in funds:                                                     # newest transfer first
        sender = f["sender"]
        if sender in checked or sender == ctx.dev:
            continue
        checked.add(sender)
        for r in ctx.group_launches(sender):                            # the funder's own launches and his group's
            o = ctx.outcome(r["token"])
            if o["rug"]:
                hits.append({"funder": sender, "transfer": f, "launch": r})
                break
    value = {"funders_checked": len(checked), "rugging_funders": len(hits)}
    if not hits:
        return _res(sid, v, value=value)
    h = hits[0]
    f, r = h["transfer"], h["launch"]
    pts = c["points_multi"] if len(hits) >= 2 else c["points"]
    value.update({"funder": h["funder"], "funding_near": round(f["near"], 4), "earlier_launch": r["token"]})
    via = "" if r["via"] == h["funder"] else f" (made by {r['via']}, linked to him)"
    reason = (f"Received {_n(f['near'])} {_ago(ctx.asof_ns - f['ts_ns'])} before the launch from {h['funder']}, whose earlier "
              f"launch {r['token']}{via} ended as a rug" + (f"; {len(hits)} such funders in all." if len(hits) >= 2 else "."))
    ev = [{"kind": "tx", "id": f["tx"]} if f["tx"] else {"kind": "account", "id": h["funder"]}, {"kind": "launch", "id": r["token"]}]
    return _res(sid, v, True, value, pts, reason, ev)


def _funded_by_rugger_doc(c: dict[str, Any]) -> str:
    back = f"in the {c['lookback_days']:g} days before" if c.get("lookback_days") else "at any time before"
    return (f"The creator (or the real owner behind a launch bot) received at least {c['min_funding_near']} NEAR or wNEAR "
            f"{back} the launch, in a direct transfer, from an account that launched an earlier token that ended as a rug — "
            f"or whose linked accounts did — judged only on what was decided at the launch time. Adds {c['points']} points, "
            f"{c['points_multi']} when two or more such funders are found.")


# ---------------------------------------------------------------- cluster_rugs (RGY-021)

def _cluster_rugs(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "cluster_rugs", 1
    if not ctx.dev:
        return _res(sid, v, value={"skipped": "dev account unknown"})
    peers = ctx.cluster_peers(ctx.dev)
    value: dict[str, Any] = {"cluster_peers": len({p["account"] for p in peers})}
    if not peers:
        return _res(sid, v, value=value)
    rugged, known = [], 0
    for p in sorted(peers, key=lambda p: (p["funded_ns"], p["account"])):
        for r in ctx.prior_launches(p["account"]):
            o = ctx.outcome(r["token"])
            if o["rug"] is None:
                continue
            known += 1
            if o["rug"]:
                rugged.append({**r, "source": p["source"], "peer": p["account"]})
    value["peer_known_launches"] = known
    value["peer_rugs"] = len(rugged)
    if len(rugged) < c["min_rugs"]:
        return _res(sid, v, value=value)
    r = rugged[0]
    return _res(sid, v, True, value, c["points"],
                f"The creator was funded from the same narrow source ({r['source']}) as {len({x['peer'] for x in rugged})} other "
                f"creator(s) whose earlier launches ended as a rug ({len(rugged)} in all).",
                [{"kind": "account", "id": r["source"]}, {"kind": "launch", "id": r["token"]}])


def _cluster_rugs_doc(c: dict[str, Any]) -> str:
    return (f"The creator belongs to a funding cluster (one narrow source that funded two or more launch creators shortly "
            f"before they launched) and at least {c['min_rugs']} earlier launch(es) of the other members, made before this one "
            f"and already decided at the launch time, ended as a rug. Adds {c['points']} points.")


SIGNALS: tuple[Signal, ...] = (
    Signal("fingerprint", 1, _fingerprint_doc, _fingerprint),
    Signal("funded_by_dumper", 1, _funded_by_dumper_doc, _funded_by_dumper),
    Signal("serial_launcher", 2, _serial_launcher_doc, _serial_launcher),
    Signal("prior_fast_dump", 1, _prior_fast_dump_doc, _prior_fast_dump),
    Signal("name_copy", 1, _name_copy_doc, _name_copy),
    Signal("creator_tax", 1, _creator_tax_doc, _creator_tax),
    Signal("creator_track_record", 1, _creator_track_record_doc, _creator_track_record),      # RGY-021: profile v1 only
    Signal("funded_by_rugger", 1, _funded_by_rugger_doc, _funded_by_rugger),
    Signal("cluster_rugs", 1, _cluster_rugs_doc, _cluster_rugs),
)
BY_ID = {s.id: s for s in SIGNALS}
