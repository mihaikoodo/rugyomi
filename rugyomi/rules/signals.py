"""Signal registry v0 (RGY-015) — the single source of the signals: ids, versions, descriptions (the methodology page
is generated from here), thresholds (read from the config, never hard-coded) and the computation.

A signal is `compute(ctx) -> Result` where ctx is a rules.context.LaunchContext: the ONLY way a signal reads the
database, and it only returns what was knowable at the launch time (no lookahead — enforced in the context, and a test
checks that this module has no database access of its own). Initial signals use pre-launch data plus the launch itself;
what the dev does after the launch is shown as facts on the page, never as part of the initial score.

Result: {id, version, fired, value, contribution, reason, short, evidence: [{kind: tx | account | launch, id}]}.
`short` (RGY-041) is the one-line form of the reason for the channel post: facts and numbers only, about 90 characters at most;
it is written here, next to the long text, so the wording lives in one place. Cards stored before it have no `short`.
Reasons describe behaviour with numbers and evidence; they never name a person's intent.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from . import notable as NT
from .context import NS_DAY, NS_H, NS_MIN, LaunchContext, name_keys, near


class UndeclaredConfigKey(KeyError):
    """A measure read a config key that its signal did not declare in `keys`: the stored measure would then not be tied to that setting (RGY-050)."""


class _Declared:
    """The signal config as a measure sees it: only the keys the signal declares as measurement settings (RGY-050). Anything else raises, so a measure
    cannot depend on a setting that is not part of its stored key - in the live path and in the stored path alike (they are the same call)."""
    def __init__(self, c: dict[str, Any], keys: tuple[str, ...], who: str) -> None:
        self._c, self._keys, self._who = c, frozenset(keys), who

    def _check(self, k: str) -> None:
        if k not in self._keys:
            raise UndeclaredConfigKey(f"the measure of {self._who} read the config key {k!r}, which is not declared in its `keys`")

    def __getitem__(self, k: str) -> Any:
        self._check(k)
        return self._c[k]

    def get(self, k: str, default: Any = None) -> Any:
        self._check(k)
        return self._c.get(k, default)


def _measured(measure: Callable[..., dict[str, Any]], keys: tuple[str, ...], ctx: Any, c: dict[str, Any], who: str = "?") -> dict[str, Any]:
    """The raw as-of value of a signal: what the measure read, plus the launch's own facts (`_t` launch time, `_tok` token, `_dev` creator). Passed through JSON so that
    what a score sees is exactly what the feature store holds (one code path for the live card and the stored values)."""
    m = measure(ctx, _Declared(c, keys, who))
    m["_t"], m["_tok"], m["_dev"] = getattr(ctx, "asof_ns", None), getattr(ctx, "token", None), getattr(ctx, "dev", None)
    return json.loads(json.dumps(m))


@dataclass(frozen=True)
class Signal:
    """`measure(ctx, c)` reads the database as of the launch and returns a plain, JSON-able dict (the raw value); `score(m, c)` turns it into the result (points,
    reason, short, evidence) and never touches the database. `keys`: the config keys the measure reads (the rest of `c`, the points and tiers, is for the score).
    `deps()`: a fingerprint of the published lists the measure reads (their version). `measure_version`: bump when the measure's logic changes."""
    id: str
    version: int
    describe: Callable[[dict[str, Any]], str]          # EN text for the methodology page, from the signal's config
    measure: Callable[[Any, Any], dict[str, Any]]
    score: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    keys: tuple[str, ...] = ()
    deps: Callable[[], str] | None = None
    measure_version: int = 1

    def measured(self, ctx: Any, c: dict[str, Any]) -> dict[str, Any]:
        return _measured(self.measure, self.keys, ctx, c, self.id)

    def compute(self, ctx: Any, c: dict[str, Any]) -> dict[str, Any]:
        return self.score(self.measured(ctx, c), c)

    def measure_key(self, c: dict[str, Any]) -> str:
        """What the stored measure depends on besides the launch: the declared settings, the lists' versions, the measure's version, the registry version."""
        raw = json.dumps({"v": self.version, "mv": self.measure_version, "k": {k: c.get(k) for k in sorted(self.keys)}, "d": self.deps() if self.deps else None},
                         sort_keys=True, separators=(",", ":"))
        return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _res(sig_id: str, version: int, fired: bool = False, value: Any = None, points: int = 0, reason: str = "",
         evidence: list[dict[str, str]] | None = None, short: str = "") -> dict[str, Any]:
    return {"id": sig_id, "version": version, "fired": fired, "value": value, "contribution": points if fired else 0,
            "reason": reason if fired and points else "", "evidence": evidence or [] if fired and points else [],       # F-043: 0 points -> no text, no evidence links
            **({"short": short} if fired and points and short else {})}


def _ago(ns: int) -> str:
    m = ns / NS_MIN
    return f"{m:.0f} min" if m < 120 else f"{m / 60:.1f} h"


def _n(x: float) -> str:
    return f"{x:,.2f} NEAR"


def _pct(bps: int) -> str:
    return f"{bps / 100:.0f}%"


def _n1(x: float) -> str:
    return f"{x:,.1f} NEAR"


def _acct(a: str, n: int = 22) -> str:
    return a if len(a) <= n else a[:9] + "…" + a[-8:]


def _tok(t: str) -> str:
    """A launch token for a short text: the part before the first dot (`diwali.nearlytrade.near` -> `diwali`)."""
    return t.split(".")[0]


# ---------------------------------------------------------------- fingerprint

def _fingerprint_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    win = int(c["window_min"] * NS_MIN)
    return {"created": ctx.created(ctx.dev), "funds": ctx.inflows(ctx.dev, win, c["min_funding_near"]), "spent": near(ctx.dev_buy)}


def _fingerprint_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "fingerprint", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    T, dev, created, funds = m["_t"], m["_dev"], m["created"], m["funds"]
    win = int(c["window_min"] * NS_MIN)
    fresh = created is not None and T - created["ts_ns"] < win
    funded = bool(funds)
    total = sum(f["near"] for f in funds)
    spent = m["spent"]
    spends = funded and spent >= c["dev_buy_share"] * total
    n = int(fresh) + int(funded) + int(spends)
    value = {"fresh_account": fresh, "funded": funded, "dev_buy_vs_funding": spends, "conditions_met": n,
             "funding_near": round(total, 4), "dev_buy_near": round(spent, 4)}
    if n < 2:
        return _res(sid, v, value=value)
    pts = c["points_3_of_3"] if n == 3 else c["points_2_of_3"]
    parts, ev, sp = [], [], []
    if fresh:
        sp.append(f"creator account {_ago(T - created['ts_ns'])} old")
        parts.append(f"the creator account was created {_ago(T - created['ts_ns'])} before the launch")
        ev.append({"kind": "tx", "id": created["tx"]} if created.get("tx") else {"kind": "account", "id": dev})
    if funded:
        big = max(funds, key=lambda f: f["near"])
        parts.append(f"it received {_n(total)} {_ago(T - big['ts_ns'])} before the launch "
                     f"(largest transfer from {big['sender']})")
        ev.append({"kind": "tx", "id": big["tx"]} if big["tx"] else {"kind": "account", "id": big["sender"]})
        sp.append(f"got {_n1(total)} {_ago(T - big['ts_ns'])} before launch")
    if spends:
        parts.append(f"the dev buy of {_n(spent)} is {spent / total * 100:.0f}% of that funding")
        sp.append(f"dev buy {spent / total * 100:.0f}% of it")
    return _res(sid, v, True, value, pts, f"Fresh creator setup ({n} of 3): " + "; ".join(parts) + ".", ev, ", ".join(sp))


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


def _funded_by_dumper_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
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
            return {"hit": {"f": f, "token": hit["token"], "snap": hit["snap"]}, "checked": sorted(checked)}
    return {"hit": None, "checked": sorted(checked)}


def _funded_by_dumper_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "funded_by_dumper", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    h = m["hit"]
    if not h:
        return _res(sid, v, value={"funders_checked": m["checked"]})
    f, s, sender = h["f"], h["snap"], h["f"]["sender"]
    value = {"funder": sender, "funding_near": round(f["near"], 4), "earlier_launch": h["token"],
             "dev_sold_bps": s["dev_sold_share_bps"], "at_minute": s["minute"]}
    reason = (f"Funded with {_n(f['near'])} {_ago(m['_t'] - f['ts_ns'])} before the launch by {sender}, who "
              f"launched {h['token']} earlier: its dev had sold {_pct(s['dev_sold_share_bps'])} of the tokens "
              f"within {s['minute']} min.")
    ev = [{"kind": "tx", "id": f["tx"]} if f["tx"] else {"kind": "account", "id": sender},
          {"kind": "launch", "id": h["token"]}]
    return _res(sid, v, True, value, c["points"], reason, ev,
                f"funded {_n1(f['near'])} by a past dumper ({_tok(h['token'])}: dev sold {_pct(s['dev_sold_share_bps'])} in {s['minute']} min)")


def _funded_by_dumper_doc(c: dict[str, Any]) -> str:
    mins = " or ".join(f"T+{m}" for m in c["minutes"])
    return (f"The creator (or the real owner behind a launch bot) received at least {c['min_funding_near']} NEAR or wNEAR "
            f"in the {c['lookback_h']} h before the launch from an account that launched an earlier token whose dev "
            f"had sold at least {c['dumper_sold_bps'] / 100:.0f}% of the tokens by {mins} minutes — a snapshot that "
            f"already existed at the launch time. Adds {c['points']} points.")


# ---------------------------------------------------------------- serial_launcher

def _serial_launcher_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    group = c.get("scope") == "group"
    if not ctx.dev:
        return {"nodev": True}
    if not group:
        prior = ctx.prior_launches(ctx.dev, int(c["window_h"] * NS_H))
    else:                                                              # RGY-021: the creator AND the accounts linked to him
        prior = ctx.group_launches(ctx.dev, int(c["window_h"] * NS_H))
    m: dict[str, Any] = {"n": len(prior), "ev": [r["token"] for r in prior[:5]]}
    if group and c.get("window_long_d"):                               # the longer window: a slower but steady serial launcher
        long_prior = ctx.group_launches(ctx.dev, int(c["window_long_d"] * NS_DAY))
        m["nl"], m["evl"] = len(long_prior), [r["token"] for r in long_prior[:5]]
    return m


def _serial_launcher_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    group = c.get("scope") == "group"
    sid, v = "serial_launcher", 3 if group else 2
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    n = m["n"]
    pts = 0
    if n >= c["min_prior_low"]:
        pts = (c["points_high"] if n >= c["min_prior_high"] else c["points"] if n >= c["min_prior"] else c["points_low"])
    value: dict[str, Any] = {"prior_launches": n}
    ev = [{"kind": "launch", "id": t} for t in m["ev"]]
    who = " by the creator and the accounts linked to him" if group else ""
    reason = f"{n} other launches{who} in the {c['window_h']} h before this one (all launchpads, all sources)."
    sshort = f"{n} other launches by the creator{chr(39) + 's group' if group else ''} in {c['window_h']} h"
    if group and c.get("window_long_d"):                               # the longer window: a slower but steady serial launcher
        nl = m["nl"]
        value["prior_launches_long"] = nl
        if nl >= c["min_prior_long"] and c["points_long"] > pts:
            pts = c["points_long"]
            ev = [{"kind": "launch", "id": t} for t in m["evl"]]
            reason = (f"{nl} other launches{who} in the {c['window_long_d']:g} days before this one "
                      f"(all launchpads, all sources).")
            sshort = f"{nl} other launches by the creator's group in {c['window_long_d']:g} days"
    if not pts:
        return _res(sid, v, value=value)
    return _res(sid, v, True, value, pts, f"The creator made {reason}" if not group else f"The creator's group made {reason}", ev, sshort)


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

def _prior_fast_dump_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    for r in ctx.prior_launches(ctx.dev):
        if r["coverage"] != "live" and not ctx.read_backfill:
            continue                                                    # profile v0: history launches have no snapshots
        s = ctx.snapshot(r["token"], c["minute"])
        if s and s["dev_sold_share_bps"] is not None and s["dev_sold_share_bps"] >= c["sold_bps"]:
            return {"hit": {"token": r["token"], "bps": s["dev_sold_share_bps"]}}
    return {"hit": None}


def _prior_fast_dump_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "prior_fast_dump", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    h = m["hit"]
    if not h:
        return _res(sid, v, value={"earlier_fast_dump": False})
    return _res(sid, v, True, {"earlier_launch": h["token"], "dev_sold_bps": h["bps"]},
                c["points"], f"An earlier launch of this creator, {h['token']}, had its dev sell "
                f"{_pct(h['bps'])} of the tokens within {c['minute']} min.",
                [{"kind": "launch", "id": h["token"]}],
                f"creator's earlier launch {_tok(h['token'])}: dev sold {_pct(h['bps'])} in {c['minute']} min")


def _prior_fast_dump_doc(c: dict[str, Any]) -> str:
    return (f"The creator (or real owner) has an earlier launch seen live whose dev had sold at least "
            f"{c['sold_bps'] / 100:.0f}% of the tokens by T+{c['minute']} min, from a snapshot that already existed at "
            f"the launch time. Adds {c['points']} points.")


# ---------------------------------------------------------------- name_copy

def _name_uses(ctx: LaunchContext, c: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """RGY-033: every EARLIER launch (strictly before this one, `ctx.launches_window`) that shares a name key with this launch and counts as
    a copy: by another creator in the previous `other_creator_days`, or by the same creator on another launchpad in the previous
    `same_creator_h`. {token: {ts_ns, kind, name}}; a launch is counted once however many keys it shares."""
    mine, me, hits = ctx.my_name_keys(), ctx.dev, {}
    for r in ctx.launches_window(int(c["other_creator_days"] * NS_DAY)):
        theirs = r.get("owner") or r.get("creator")
        if not theirs or (me and theirs == me):
            continue
        common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
        if common:
            hits[r["token"]] = {"ts_ns": r["ts_ns"], "kind": "other_creator", "name": sorted(common)[0]}
    if me:
        for r in ctx.launches_window(int(c["same_creator_h"] * NS_H)):
            if (r.get("owner") or r.get("creator")) != me or r["launchpad"] == ctx.launch["launchpad"] or r["token"] in hits:
                continue
            common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
            if common:
                hits[r["token"]] = {"ts_ns": r["ts_ns"], "kind": "same_creator_other_launchpad", "name": sorted(common)[0]}
    return hits


def _name_copy_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    """RGY-033 (shape `counted`): every earlier use of the name (`_name_uses`); the first shape: only the newest use."""
    mine = ctx.my_name_keys()
    if not mine:
        return {"noname": True}
    if c.get("shape") == "counted":
        return {"hits": _name_uses(ctx, c)}
    me = ctx.dev
    for r in ctx.launches_window(int(c["other_creator_days"] * NS_DAY)):          # newest first
        theirs = r.get("owner") or r.get("creator")
        if not theirs or (me and theirs == me):
            continue
        common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
        if common:
            return {"hit": {"kind": "other_creator", "name": sorted(common)[0], "other_launch": r["token"], "ts_ns": r["ts_ns"]}}
    if me:
        for r in ctx.launches_window(int(c["same_creator_h"] * NS_H)):
            if (r.get("owner") or r.get("creator")) != me or r["launchpad"] == ctx.launch["launchpad"]:
                continue
            common = mine & name_keys(r["token"], r.get("symbol"), r.get("name"))
            if common:
                return {"hit": {"kind": "same_creator_other_launchpad", "name": sorted(common)[0], "other_launch": r["token"], "ts_ns": r["ts_ns"]}}
    return {"hit": None}


def _name_copy_counted_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """RGY-033, signal version 2: the points grow with the number of earlier uses of the name and are higher when the latest use
    is recent (the gap). `points` for one earlier use; `+ points_many` from `many_uses` uses; `+ points_recent` when the latest use
    was at most `recent_h` hours before. Only earlier launches count (see `_name_uses`)."""
    sid, v = "name_copy", 2
    if m.get("noname"):
        return _res(sid, v, value={"skipped": "no name"})
    hits = m["hits"]
    if not hits:
        return _res(sid, v, value={"copy": False})
    tok, last = max(hits.items(), key=lambda kv: (kv[1]["ts_ns"], kv[0]))
    uses, gap = len(hits), m["_t"] - last["ts_ns"]
    pts = c["points"] + (c["points_many"] if uses >= c["many_uses"] else 0) + (c["points_recent"] if gap <= c["recent_h"] * NS_H else 0)
    value = {"kind": last["kind"], "name": last["name"], "other_launch": tok, "uses": uses, "gap_h": round(gap / NS_H, 3)}
    return _res(sid, v, True, value, pts, f"The name '{last['name']}' was already used {uses} time{'s' if uses != 1 else ''} before; the latest "
                f"use was {_ago(gap)} earlier ({tok}).", [{"kind": "launch", "id": t} for t in sorted(hits, key=lambda t: -hits[t]["ts_ns"])[:3]],
                f"name '{last['name']}' used {uses}x before, the last {_ago(gap)} earlier")


def _name_copy_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    if c.get("shape") == "counted":
        return _name_copy_counted_score(m, c)
    sid, v = "name_copy", 1
    if m.get("noname"):
        return _res(sid, v, value={"skipped": "no name"})
    h = m["hit"]
    if not h:
        return _res(sid, v, value={"copy": False})
    k, T = h["name"], m["_t"]
    if h["kind"] == "other_creator":
        return _res(sid, v, True, {"kind": "other_creator", "name": k, "other_launch": h["other_launch"]}, c["points"],
                    f"The name '{k}' was already used {_ago(T - h['ts_ns'])} earlier by another creator "
                    f"({h['other_launch']}).", [{"kind": "launch", "id": h["other_launch"]}],
                    f"name '{k}' used {_ago(T - h['ts_ns'])} earlier by another creator")
    return _res(sid, v, True, {"kind": "same_creator_other_launchpad", "name": k, "other_launch": h["other_launch"]},
                c["points"], f"The same creator launched the name '{k}' on another launchpad "
                f"{_ago(T - h['ts_ns'])} earlier ({h['other_launch']}).",
                [{"kind": "launch", "id": h["other_launch"]}],
                f"same creator used name '{k}' on another launchpad {_ago(T - h['ts_ns'])} earlier")


def _name_copy_doc(c: dict[str, Any]) -> str:
    if c.get("shape") == "counted":
        tiers = [t for t in (f"a copy of an earlier name adds {c['points']} points" if c["points"] else "",
                             f"{c['many_uses']} or more earlier uses add {c['points_many']}" if c["points_many"] else "",
                             f"a latest use at most {c['recent_h']:g} h earlier adds {c['points_recent']}" if c["points_recent"] else "") if t]
        return (f"The launch reuses a name already launched: by a different creator in the previous {c['other_creator_days']} days, "
                f"or by the same creator on another launchpad in the previous {c['same_creator_h']} h. Names are compared by the token "
                f"id (without a -2 style suffix), symbol and name, case-insensitively. Earlier launches only. "
                + ("Points: " + "; ".join(tiers) + "." if tiers else "No points are given."))
    return (f"The launch reuses a name already launched: by a different creator in the previous {c['other_creator_days']} "
            f"days, or by the same creator on another launchpad in the previous {c['same_creator_h']} h. Names are "
            f"compared by the token id (without a -2 style suffix), symbol and name, case-insensitively. "
            f"Adds {c['points']} points.")


# ---------------------------------------------------------------- creator_tax

def _is_bps(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def creator_take(t: dict[str, Any], burn_mode: str = "unknown") -> dict[str, Any]:
    """RGY-033: the creator's EFFECTIVE take per trade, in bps of the trade value: the mean of the buy and the sell tax
    (buy_bps, sell_bps) x the creator's share of the tax = creator_bps x (1 - platform_bps) (F-033, RGY-030: the platform takes
    platform_bps off the top, creator_bps / holders_bps are shares of what is left). Returns {"take_bps": float | None, "basis": ...}
    where basis is "known" or why the take is not known: "no_creator_bps", "unknown_rate", "unknown_platform", "unknown_burn".
    `burn_mode` "ignore": a burn share does not change the creator's share (it is its own share of what is left); "unknown":
    burn_bps > 0 (or not given) means the take is not known. Never a silent guess: an unknown take gets the lowest tier only."""
    buy, sell, plat, burn, cb = (t.get(k) for k in ("buy_bps", "sell_bps", "platform_bps", "burn_bps", "creator_bps"))
    if not _is_bps(cb):
        return {"take_bps": None, "basis": "no_creator_bps"}
    if not (_is_bps(buy) and _is_bps(sell)):
        return {"take_bps": None, "basis": "unknown_rate"}
    if not (_is_bps(plat) and plat <= 10000):
        return {"take_bps": None, "basis": "unknown_platform"}
    if burn_mode != "ignore" and not (burn == 0 and _is_bps(burn)):
        return {"take_bps": None, "basis": "unknown_burn"}
    return {"take_bps": (buy + sell) / 2 * cb * (10000 - plat) / 10**8, "basis": "known"}


def _tax_options_text(t: dict[str, Any], creator_bps: int) -> str:
    """The reason text of `creator_tax`: facts of the nearly tax options and, only where the chain data prove it, the
    creator's effective rate. What the options mean (F-033, RGY-030): buy_bps / sell_bps are the tax on a trade; the
    platform takes platform_bps of the tax off the top; creator_bps / holders_bps are shares of what is LEFT (so
    creator_bps is the creator's share of the tax, never a share of the trades). The per-trade rate is stated only when
    burn_bps is 0 — the observed launches all had burn 0, so the effect of a burn share is not established. The channel
    post clips a reason at 170 characters (`publisher.format_card`): keep the text inside that."""
    if t.get("source") == "justhoot":
        return _justhoot_fee_text(t, creator_bps)
    buy, sell, plat, burn = (t.get(k) for k in ("buy_bps", "sell_bps", "platform_bps", "burn_bps"))
    head = f"Trade tax {buy / 100:.2f}% buy / {sell / 100:.2f}% sell" if _is_bps(buy) and _is_bps(sell) else "Trade tax"
    if _is_bps(buy) and _is_bps(sell) and _is_bps(plat) and plat <= 10000 and burn == 0:
        eff = creator_bps * (10000 - plat)                         # the creator's share of the tax, in bps * 10000
        return (f"{head}; after the platform's {plat / 100:.2f}% cut the creator gets {eff / 1e6:.2f}% of it "
                f"({buy * eff / 1e10:.3f}% per buy, {sell * eff / 1e10:.3f}% per sell; creator_bps {creator_bps}, "
                f"platform_bps {plat}).")
    opts = ", ".join([f"{k} {t[k]}" for k in ("buy_bps", "sell_bps") if _is_bps(t.get(k))] + [f"creator_bps {creator_bps}"])
    return f"{head}; the creator's share of that tax is {creator_bps / 100:.2f}% (nearly tax options: {opts})."


def _justhoot_fee_text(t: dict[str, Any], creator_bps: int) -> str:
    """RGY-045b: the justhoot creator fee, as the creation call sets it: a fee on every transfer of the token and the token owner's share of it. Without `platform_bps` (profile v1r)
    no net rate is stated. With it (profile v2r, RGY-049: the justhoot docs put the transfer tax on top of the pool fee, so the platform takes nothing of it: 0) the reason gives what
    the owner gets of every transfer. At most 170 characters (the post clips there)."""
    pips = t.get("transfer_fee_pips")
    rate = f"{pips / 10000:g}%" if isinstance(pips, int) else "a fee"
    plat = t.get("platform_bps")
    if isinstance(pips, int) and _is_bps(plat) and plat <= 10000:
        eff = pips / 10000 * creator_bps / 10000 * (10000 - plat) / 10000
        return (f"Token transfer fee {rate}; the token owner gets {creator_bps / 100:g}% of it, i.e. {eff:.3g}% of every transfer "
                f"(justhoot launch call: transfer_fee_pips {pips}, token_owner_bps {creator_bps}).")
    return f"Token transfer fee {rate}; the token owner's share of it is {creator_bps / 100:g}% (justhoot launch call: transfer_fee_pips {pips}, token_owner_bps {creator_bps})."


def _tax_short(t: dict[str, Any], creator_bps: int) -> str:
    """The one-line form of `_tax_options_text`: the same facts (RGY-041)."""
    if t.get("source") == "justhoot":
        pips = t.get("transfer_fee_pips")
        if isinstance(pips, int) and _is_bps(t.get("platform_bps")) and t["platform_bps"] <= 10000:
            return f"token fee {pips / 10000:g}%, the owner gets {creator_bps / 100:g}% of it ({pips / 10000 * creator_bps / 10000 * (10000 - t['platform_bps']) / 10000:.3g}% per transfer)"
        return f"token fee {pips / 10000:g}%, the owner gets {creator_bps / 100:g}% of it" if isinstance(pips, int) else f"the owner gets {creator_bps / 100:g}% of the token fee"
    buy, sell, plat, burn = (t.get(k) for k in ("buy_bps", "sell_bps", "platform_bps", "burn_bps"))
    if _is_bps(buy) and _is_bps(sell) and _is_bps(plat) and plat <= 10000 and burn == 0:
        return f"tax {buy / 100:g}% buy / {sell / 100:g}% sell, the creator gets {creator_bps * (10000 - plat) / 1e6:.0f}% of it"
    return f"the creator gets {creator_bps / 100:g}% of the trade tax"


def _creator_tax_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    """The launch's own tax options, or (a profile that asks for it) a justhoot launch's creator fee from its stored creation call."""
    return {"t": ctx.tax_options(justhoot=True) if c.get("justhoot") else ctx.tax_options()}


def _creator_tax_take_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """RGY-033, signal version 2: the points follow the creator's effective take per trade (`creator_take`), not the raw creator_bps.
    `points` from `min_take_bps`; `+ points_2` from `take2_bps`; `+ points_3` from `take3_bps` (never fewer points for a larger take).
    A take that cannot be computed (unknown platform / rate / burn rule) gets `points` only, and the value says why."""
    sid, v = "creator_tax", 2
    t = m["t"]                                                                              # RGY-049: a justhoot launch counts in profile v2r
    bps = (t or {}).get("creator_bps")
    if not isinstance(bps, int) or bps < c["min_bps"]:
        return _res(sid, v, value={"creator_bps": bps})
    jh = (t or {}).get("source") == "justhoot"
    if jh and "platform_bps" not in (t or {}) and isinstance(c.get("justhoot_platform_bps"), int):
        t = {**(t or {}), "platform_bps": c["justhoot_platform_bps"]}      # the platform's share of a justhoot fee: a config constant, null = not established (first tier only)
    tk = creator_take(t or {}, c["burn_mode"])
    take = tk["take_bps"]
    value = {"creator_bps": bps, "take_bps": None if take is None else round(take, 3), "basis": tk["basis"]}
    if jh:
        value.update(source="justhoot", transfer_fee_pips=(t or {}).get("transfer_fee_pips"))
    if take is None:
        pts = c["points"]
    elif take < c["min_take_bps"]:
        return _res(sid, v, value=value)
    else:
        pts = c["points"] + (c["points_2"] if take >= c["take2_bps"] else 0) + (c["points_3"] if take >= c["take3_bps"] else 0)
    return _res(sid, v, True, value, pts, _tax_options_text(t or {}, bps),
                [{"kind": "launch", "id": m["_tok"]}], _tax_short(t or {}, bps))


def linear_points(take_bps: float, per_pct: float, cap: int) -> int:
    """RGY-053: points proportional to the creator's effective take: `per_pct` points per 1 % of the trade (100 bps), at most `cap`, to the nearest whole point (half up)."""
    return int(min(cap, math.floor(round(take_bps / 100 * per_pct, 6) + 0.5)))


def _creator_tax_linear_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    """RGY-053, signal version 3 (shape `linear`, an expert rule, not fitted): the points are `points_per_pct` per 1 % of the trade that the creator gets (`creator_take`, the same
    effective take as version 2), at most `max_points`, rounded to the nearest point. No flat base for 'any tax'. A take below `min_take_bps`, or one that rounds to 0 points, does not fire;
    a take that cannot be computed (unknown platform / rate / burn rule) gets `points_unknown_take` (0: never a silent guess) and the value says why."""
    sid, v = "creator_tax", 3
    t = m["t"]
    bps = (t or {}).get("creator_bps")
    if not isinstance(bps, int) or bps < c["min_bps"]:
        return _res(sid, v, value={"creator_bps": bps})
    jh = (t or {}).get("source") == "justhoot"
    if jh and "platform_bps" not in (t or {}) and isinstance(c.get("justhoot_platform_bps"), int):
        t = {**(t or {}), "platform_bps": c["justhoot_platform_bps"]}
    tk = creator_take(t or {}, c["burn_mode"])
    take = tk["take_bps"]
    value = {"creator_bps": bps, "take_bps": None if take is None else round(take, 3), "basis": tk["basis"]}
    if jh:
        value.update(source="justhoot", transfer_fee_pips=(t or {}).get("transfer_fee_pips"))
    if take is None:
        pts = c["points_unknown_take"]
    elif take < c["min_take_bps"]:
        return _res(sid, v, value=value)
    else:
        pts = linear_points(take, c["points_per_pct"], c["max_points"])
    value["points"] = pts
    return _res(sid, v, True, value, pts, _tax_options_text(t or {}, bps), [{"kind": "launch", "id": m["_tok"]}], _tax_short(t or {}, bps))


def _creator_tax_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    if c.get("shape") == "linear":
        return _creator_tax_linear_score(m, c)
    if c.get("shape") == "take":
        return _creator_tax_take_score(m, c)
    sid, v = "creator_tax", 1
    t = m["t"]
    bps = (t or {}).get("creator_bps")
    if not isinstance(bps, int) or bps < c["min_bps"]:
        return _res(sid, v, value={"creator_bps": bps})
    value = {"creator_bps": bps}
    if (t or {}).get("source") == "justhoot":
        value.update(source="justhoot", transfer_fee_pips=t.get("transfer_fee_pips"))
    return _res(sid, v, True, value, c["points"], _tax_options_text(t or {}, bps),
                [{"kind": "launch", "id": m["_tok"]}], _tax_short(t or {}, bps))


def _creator_tax(ctx: Any, c: dict[str, Any]) -> dict[str, Any]:
    return _creator_tax_score(_measured(_creator_tax_measure, ("justhoot",), ctx, c, "creator_tax"), c)


def _creator_tax_doc(c: dict[str, Any]) -> str:
    if c.get("shape") == "linear":
        return (f"A launch whose tax options give the creator a share of the trades (creator_bps of at least {c['min_bps']}), or a justhoot launch whose token-creation call sets a transfer fee with a "
                f"share for the token owner. The points follow the creator's effective take per trade: the mean of the buy and sell tax x the creator's share of the tax after the platform's cut "
                f"(creator_bps x (1 - platform_bps)); {c['points_per_pct']:g} points per 1 % of the trade that goes to the creator, at most {c['max_points']}, rounded to the nearest whole point. "
                f"There is no flat base for any tax: a take that rounds to 0 points adds nothing. Expert rule, set by hand, not fitted. When the take cannot be computed from the options it adds "
                f"{c['points_unknown_take']} points (the reason does not guess). A burn share is taken as a part of what is left after the platform's cut, so it does not change the creator's share. "
                f"The platform's cut of a justhoot transfer tax is {c.get('justhoot_platform_bps')} bps (justhoot docs: the pool fee is the platform's; the transfer tax goes on top of it).")
    if c.get("shape") == "take":
        return (f"A nearly launch whose tax options give the creator a share of the buy / sell tax (creator_bps of at least "
                f"{c['min_bps']}). The points follow the creator's effective take per trade: the mean of the buy and sell tax x "
                f"the creator's share of the tax after the platform's cut (creator_bps x (1 - platform_bps)). {c['points']} points; "
                f"a take of {c['take2_bps']:g} bps of the trade or more adds {c['points_2']}; {c['take3_bps']:g} bps or more adds "
                f"{c['points_3']} more. When the take cannot be computed from the options only the first {c['points']} points apply."
                + (" A burn share is taken as a part of what is left after the platform's cut, so it does not change the creator's share "
                   "(the reason states the creator's rate per trade only when the options show no burn)." if c.get("burn_mode") == "ignore" else
                   " A burn share makes the take unknown (first tier only).")
                + (" A justhoot launch counts the same way, read from its token-creation call: the transfer fee is the rate, the token owner's share of the fee is creator_bps. Source: justhoot docs "
                   "(pool fee 1 % = RHEA 0.2 % + platform 0.8 %; transfer tax 0-5 % on top of it, split Creator / Burn / Holders): the platform's pool share is not part of the creator's share, so the "
                   f"platform's cut of the transfer tax is {c.get('justhoot_platform_bps')} bps. A launch whose call was not stored is not read." if c.get("justhoot") else ""))
    return (f"A nearly launch whose tax options give the creator a share of the buy / sell tax (creator_bps of at least "
            f"{c['min_bps']}). The reason states the tax rates and the creator's share of the tax — not a share of "
            f"the trades; when the options show no burn it also gives the creator's rate per buy and per sell after "
            f"the platform's cut. Adds {c['points']} points."
            + (" A justhoot launch counts the same way when its token-creation call (kept by the live collector, and loaded once for the launches before it) sets a transfer fee with a share for the token owner; the platform's own cut is not in the call, so no net rate is stated." if c.get("justhoot") else ""))


# ---------------------------------------------------------------- creator_track_record (RGY-021)

def _smoothed(rugs: int, n: int, c: dict[str, Any]) -> float:
    """Rug share of the earlier launches pulled toward the prior (the base rate) by `prior_strength` pseudo-launches, so one rug
    out of one launch is a signal but not a certainty."""
    return (rugs + c["prior_strength"] * c["prior_rate"]) / (n + c["prior_strength"])


def _creator_track_record_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
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
    return {"own_n": own_n, "own_rugs": own_rugs, "grp_n": grp_n, "grp_rugs": grp_rugs, "group_size": len(ctx.group(ctx.dev)),
            "rugged": [r["token"] for r in rugged[:5]]}


def _creator_track_record_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "creator_track_record", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    own_n, own_rugs, grp_n, grp_rugs = m["own_n"], m["own_rugs"], m["grp_n"], m["grp_rugs"]
    n, rugs = own_n + grp_n, own_rugs + grp_rugs
    share = _smoothed(rugs, n, c)
    value = {"known_launches": n, "rugs": rugs, "share": round(share, 3), "own_known": own_n, "own_rugs": own_rugs,
             "group_known": grp_n, "group_rugs": grp_rugs, "group_size": m["group_size"]}
    if rugs < c["min_rugs"] or share < c["share_mid"]:
        return _res(sid, v, value=value)
    high = share >= c["share_high"]
    ev = [{"kind": "launch", "id": t} for t in m["rugged"]]
    where = f"{own_rugs} by the creator and {grp_rugs} by accounts linked to him" if grp_rugs else f"{own_rugs} by the creator"
    return _res(sid, v, True, value, c["points_high"] if high else c["points_mid"],
                f"{rugs} of the {n} earlier launches that were already decided at launch time ended as a rug ({where}); "
                f"smoothed rug share {share * 100:.0f}%.", ev,
                f"{rugs} of {n} earlier decided launches of the creator{' and linked accounts' if grp_rugs else ''} ended as a rug")


def _creator_track_record_doc(c: dict[str, Any]) -> str:
    return (f"The earlier launches of the creator (or real owner) and of the accounts linked to him by recorded links as of the "
            f"launch time, counting only those whose outcome was already decided at the launch time (rug_fast from 60 min "
            f"after that launch, rug_late from 24 h). The rug share is smoothed with {c['prior_strength']:g} imaginary "
            f"launches at the base rate {c['prior_rate'] * 100:.0f}%. At least {c['min_rugs']} rug and a share of "
            f"{c['share_mid'] * 100:.0f}% adds {c['points_mid']} points, {c['share_high'] * 100:.0f}% or more adds "
            f"{c['points_high']}.")


# ---------------------------------------------------------------- funded_by_rugger (RGY-021)

def _funded_by_rugger_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    funds = ctx.funders(ctx.dev, int(c["lookback_days"] * NS_DAY) if c.get("lookback_days") else None, c["min_funding_near"],
                        int(c["max_funders"]))
    nhits, first = 0, None
    checked: set[str] = set()
    for f in funds:                                                     # newest transfer first
        sender = f["sender"]
        if sender in checked or sender == ctx.dev:
            continue
        checked.add(sender)
        for r in ctx.group_launches(sender):                            # the funder's own launches and his group's
            o = ctx.outcome(r["token"])
            if o["rug"]:
                nhits += 1
                if first is None:
                    first = {"funder": sender, "f": f, "token": r["token"], "via": r["via"]}
                break
    return {"checked": len(checked), "nhits": nhits, "first": first}


def _funded_by_rugger_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "funded_by_rugger", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    value = {"funders_checked": m["checked"], "rugging_funders": m["nhits"]}
    if not m["nhits"]:
        return _res(sid, v, value=value)
    h, nhits, T = m["first"], m["nhits"], m["_t"]
    f = h["f"]
    pts = c["points_multi"] if nhits >= 2 else c["points"]
    value.update({"funder": h["funder"], "funding_near": round(f["near"], 4), "earlier_launch": h["token"]})
    via = "" if h["via"] == h["funder"] else f" (made by {h['via']}, linked to him)"
    reason = (f"Received {_n(f['near'])} {_ago(T - f['ts_ns'])} before the launch from {h['funder']}, whose earlier "
              f"launch {h['token']}{via} ended as a rug" + (f"; {nhits} such funders in all." if nhits >= 2 else "."))
    ev = [{"kind": "tx", "id": f["tx"]} if f["tx"] else {"kind": "account", "id": h["funder"]}, {"kind": "launch", "id": h["token"]}]
    return _res(sid, v, True, value, pts, reason, ev,
                f"got {_n1(f['near'])} {_ago(T - f['ts_ns'])} before launch from {_acct(h['funder'])}, whose launch {_tok(h['token'])} was a rug"
                + (f" ({nhits} such funders)" if nhits >= 2 else ""))


def _funded_by_rugger_doc(c: dict[str, Any]) -> str:
    back = f"in the {c['lookback_days']:g} days before" if c.get("lookback_days") else "at any time before"
    return (f"The creator (or the real owner behind a launch bot) received at least {c['min_funding_near']} NEAR or wNEAR "
            f"{back} the launch, in a direct transfer, from an account that launched an earlier token that ended as a rug — "
            f"or whose linked accounts did — judged only on what was decided at the launch time. Adds {c['points']} points, "
            f"{c['points_multi']} when two or more such funders are found.")


# ---------------------------------------------------------------- cluster_rugs (RGY-021)

def _cluster_rugs_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    peers = ctx.cluster_peers(ctx.dev)
    npeers = len({p["account"] for p in peers})
    if not peers:
        return {"peers": npeers}
    rugged, known = [], 0
    for p in sorted(peers, key=lambda p: (p["funded_ns"], p["account"])):
        for r in ctx.prior_launches(p["account"]):
            o = ctx.outcome(r["token"])
            if o["rug"] is None:
                continue
            known += 1
            if o["rug"]:
                rugged.append({**r, "source": p["source"], "peer": p["account"]})
    return {"peers": npeers, "known": known, "rugs": len(rugged), "rpeers": len({x["peer"] for x in rugged}),
            "first": {"source": rugged[0]["source"], "token": rugged[0]["token"]} if rugged else None}


def _cluster_rugs_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "cluster_rugs", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    value: dict[str, Any] = {"cluster_peers": m["peers"]}
    if not m["peers"]:
        return _res(sid, v, value=value)
    value["peer_known_launches"] = m["known"]
    value["peer_rugs"] = m["rugs"]
    if m["rugs"] < c["min_rugs"]:
        return _res(sid, v, value=value)
    r = m["first"]
    return _res(sid, v, True, value, c["points"],
                f"The creator was funded from the same narrow source ({r['source']}) as {m['rpeers']} other "
                f"creator(s) whose earlier launches ended as a rug ({m['rugs']} in all).",
                [{"kind": "account", "id": r["source"]}, {"kind": "launch", "id": r["token"]}],
                f"funded from the same source as {m['rpeers']} creator(s) whose launches ended as a rug ({m['rugs']} in all)")


def _cluster_rugs_doc(c: dict[str, Any]) -> str:
    return (f"The creator belongs to a funding cluster (one narrow source that funded two or more launch creators shortly "
            f"before they launched) and at least {c['min_rugs']} earlier launch(es) of the other members, made before this one "
            f"and already decided at the launch time, ended as a rug. Adds {c['points']} points.")


# ---------------------------------------------------------------- name_imitation (RGY-045: an expert rule, not fitted)

def _name_imitation_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    lst = NT.notable()
    return {"hit": NT.resembles(ctx.dev, lst["accounts"]), "list": lst["version"]}


def _name_imitation_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "name_imitation", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    hit = m["hit"]
    if not hit:
        return _res(sid, v, value={"list": m["list"]})
    value = {"resembles": hit["account"], "how": hit["how"], "name": hit["name"], "list": m["list"]}
    reason = f"The creator account name resembles {hit['account']} ({NT.HOW[hit['how']]}); it is not that account."
    return _res(sid, v, True, value, c["points"], reason, [{"kind": "account", "id": m["_dev"]}, {"kind": "account", "id": hit["account"]}],
                f"creator name resembles {_acct(hit['account'])}")


def _name_imitation_doc(c: dict[str, Any]) -> str:
    return (f"An expert rule, not fitted on data. The creator account's name resembles an account on a published list of notable NEAR accounts (founders, core and ecosystem "
            f"contracts, the launchpads): the same name once look-alike characters (0 for o, 1 for l, rn for m, vv for w) and repeated letters are ignored, one character "
            f"different (names of 7 or more characters), or the notable name with `near`, `official`, `real`, `team` or `app` added (digits alone do not count). It is a similarity of names, "
            f"nothing more: the notable account itself and its own sub-accounts never match, and the card does not say who is behind the name. Adds {c['points']} points.")


# ---------------------------------------------------------------- fresh_obfuscated_funding (RGY-045: an expert rule, not fitted)

def _fresh_obfuscated_funding_measure(ctx: LaunchContext, c: Any) -> dict[str, Any]:
    if not ctx.dev:
        return {"nodev": True}
    created = ctx.created(ctx.dev)
    win = int(c["fresh_min"] * NS_MIN)
    if created is None or ctx.asof_ns - created["ts_ns"] >= win:
        return {"state": "stale", "fresh_account": False if created else None}
    funds = ctx.inflows(ctx.dev, win, c["min_funding_near"])
    if not funds:
        return {"state": "unfunded"}
    big = max(funds, key=lambda f: f["near"])
    return {"state": "funded", "created": created, "big": big, "kind": NT.funding_source_kind(big["sender"]), "list": NT.funding_sources()["version"]}


def _fresh_obfuscated_funding_score(m: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    sid, v = "fresh_obfuscated_funding", 1
    if m.get("nodev"):
        return _res(sid, v, value={"skipped": "dev account unknown"})
    if m["state"] == "stale":
        return _res(sid, v, value={"fresh_account": m["fresh_account"]})
    if m["state"] == "unfunded":
        return _res(sid, v, value={"fresh_account": True, "funded": False})
    T, dev, created, big, kind = m["_t"], m["_dev"], m["created"], m["big"], m["kind"]
    value = {"fresh_account": True, "largest_funder": big["sender"], "funder_kind": kind, "funding_near": round(big["near"], 4),
             "account_age_min": round((T - created["ts_ns"]) / NS_MIN, 1), "list": m["list"]}
    if kind is None:
        return _res(sid, v, value=value)
    what = ("the NEAR Intents contract or a bridge, which forward funds from other chains and exchanges, so the transfer does not show the original sender"
            if kind == "intents" else "an exchange wallet on our list, so the transfer does not show who owns the account behind it")
    reason = (f"The creator account was created {_ago(T - created['ts_ns'])} before the launch, and its largest transfer before the launch "
              f"({_n(big['near'])}, {_ago(T - big['ts_ns'])} before) came from {big['sender']}: {what}.")
    ev = [{"kind": "tx", "id": created["tx"]} if created.get("tx") else {"kind": "account", "id": dev},
          {"kind": "tx", "id": big["tx"]} if big["tx"] else {"kind": "account", "id": big["sender"]}]
    return _res(sid, v, True, value, c["points"], reason, ev, f"account {_ago(T - created['ts_ns'])} old, funded via {big['sender']}")


def _fresh_obfuscated_funding_doc(c: dict[str, Any]) -> str:
    return (f"An expert rule, not fitted on data. The creator account was created less than {c['fresh_min']:g} min before the launch (stored creation data) and its largest "
            f"transfer of at least {c['min_funding_near']:g} NEAR in that time came from the NEAR Intents contract or a bridge, or from an exchange wallet on a published list. "
            f"Such a transfer does not show where the money came from. Adds {c['points']} points.")


SIGNALS: tuple[Signal, ...] = (
    Signal("fingerprint", 1, _fingerprint_doc, _fingerprint_measure, _fingerprint_score, ("window_min", "min_funding_near")),
    Signal("funded_by_dumper", 1, _funded_by_dumper_doc, _funded_by_dumper_measure, _funded_by_dumper_score,
           ("lookback_h", "min_funding_near", "minutes", "dumper_sold_bps")),
    Signal("serial_launcher", 2, _serial_launcher_doc, _serial_launcher_measure, _serial_launcher_score, ("scope", "window_h", "window_long_d")),
    Signal("prior_fast_dump", 1, _prior_fast_dump_doc, _prior_fast_dump_measure, _prior_fast_dump_score, ("minute", "sold_bps")),
    Signal("name_copy", 1, _name_copy_doc, _name_copy_measure, _name_copy_score, ("shape", "other_creator_days", "same_creator_h")),
    Signal("creator_tax", 1, _creator_tax_doc, _creator_tax_measure, _creator_tax_score, ("justhoot",)),
    Signal("creator_track_record", 1, _creator_track_record_doc, _creator_track_record_measure, _creator_track_record_score),      # RGY-021: profile v1 only
    Signal("funded_by_rugger", 1, _funded_by_rugger_doc, _funded_by_rugger_measure, _funded_by_rugger_score,
           ("lookback_days", "min_funding_near", "max_funders")),
    Signal("cluster_rugs", 1, _cluster_rugs_doc, _cluster_rugs_measure, _cluster_rugs_score),
    Signal("name_imitation", 1, _name_imitation_doc, _name_imitation_measure, _name_imitation_score, (), lambda: NT.notable()["version"]),   # RGY-045: profile v1r only
    Signal("fresh_obfuscated_funding", 1, _fresh_obfuscated_funding_doc, _fresh_obfuscated_funding_measure, _fresh_obfuscated_funding_score,
           ("fresh_min", "min_funding_near"), lambda: NT.funding_sources()["version"]),                      # RGY-045: profile v1r only
)
BY_ID = {s.id: s for s in SIGNALS}
