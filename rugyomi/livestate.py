"""Live state of a launch (RGY-043): what the trades of a launch show RIGHT NOW, and one number built from four of them by a fixed formula.

It is NOT a forecast, NOT a grade of any kind and NOT part of the risk score: no card, no signal and no rug label reads it, the
Telegram channel does not show it, and the site shows it on the launch page only, always with "as of HH:MM UTC" and the version of the formula.

THE NUMBER (version `live_state.version`, e.g. `ls-1`; parameters in the config section `live_state`, which is not part of a card's config hash).
Four parts, each an integer 0..100 (rounded half up), and the number is their mean (rounded half up). Nothing is fitted; the weights are equal.
  profit    the share of the outside buyers who are in profit now: received + (bought - sold, never below 0) valued at the last price - spent > 0.
            Outside buyers = the accounts of `snapshots.outside_buyers` (not the dev, not the wallets that got tokens from the dev, not service accounts).
  volume    the buys' share of the NEAR traded in the last `volume_window_min` minutes (buys / (buys + sells), every swap on the pool).
  peak      the last price as a percentage of the highest price since the launch.
  buyers    the unique outside buyers who bought in the last `buyers_window_min` minutes, on a log scale: 100 * ln(1 + n) / ln(1 + buyers_full_at), 100 from
            `buyers_full_at` buyers up, 0 for none.
A part with nothing to divide (no outside buyer, no trade in the window, no price) is 0: the formula never skips a part. No trade for `no_trading_hours` hours
(or no trade at all) -> the state is `no_trading` and there is no number. The dev's and the insiders' holdings are FACTS beside the number, not in it.

FACTS "now" (all as of the same moment, `asof_ns` = the last processed block): the price against the first outside buyer's entry and against the peak, the
outside buyers in profit, who still holds what (`holdings.compute_minute` with an explicit cutoff - the SAME code as the T+5 / T+15 / T+60 table), the buys and sells
of the last `recent_min` minutes (NEAR and count), the unique buyers of the last hour, the time of the last trade.

HISTORY GAP. A launch the live collector did not see (`launches.coverage` set) has its trades only for its first 24 hours (`api_facts`) and from the start of the live data
(`facts`); what lies between is missing. Its state is `history_gap`: no number, and only the facts that do not need the missing trades (the last trade, the last price, the
activity of the last hour in our live data). A number is never computed over missing data. RGY-055: once the gap of a launch is filled and CLOSED (`backfill_gap.closed`: the fill reached
the live data without a cap or a missing transaction), its trades are the window + the gap + the live data (`backfill_gap.full_rows`) and the state is computed exactly like a
live launch's (`compute`, with the number). An open gap keeps `history_gap`.

STORAGE (schema v20, DERIVED, rebuildable): `launch_live_state` (one row per launch, the collector rewrites it: every `active_every_s` seconds for a launch with a trade in the last
`active_window_min` minutes, every `idle_every_s` seconds for one that traded within `no_trading_hours` hours, every `dormant_every_s` seconds otherwise - a new trade makes a dormant
launch active at once) and `launch_live_state_at` (the same at the fixed minutes `fixed_minutes`, per version, for later study; never shown as a track record).
"""
from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from decimal import ROUND_FLOOR, Decimal
from typing import Any, Callable

from . import backfill as BF
from . import backfill_gap as BG
from . import holdings as HO
from . import quote_price as QP
from . import snapshots as S
from .quote import not_near_quote
from .rules.config import live_state_config_hash, load_config
from .store import Store
from .store.db import utcnow_iso

NS_MIN = S.NS_MIN
NS_H = 60 * NS_MIN
NS_DAY = 24 * NS_H
PARTS = ("profit", "volume", "peak", "buyers")
HOLD_KEYS = ("dev", "insiders", "dev_side", "bundle", "group", "fresh")      # the rows of the "who still holds" facts (RGY-036 rule order)
MAX_PER_TICK = 150
BUDGET_S = 8.0
CHECK_EVERY_S = 15.0
FIXED_EVERY_S = 300.0       # the fixed minutes are looked for at most this often (a launch waits for its window, nothing is lost)
SKIP_S = 3600.0
NOT_COMPUTED = ""
# RGY-055 / F-059: what a version does with the swaps of service accounts (`snapshots.is_service`, the same list everywhere; e.g. `feeswap.near` selling the collected
# nearly tax into the pool). ls-1 counts them like any trade. ls-2 keeps them for the PRICE (a tax sale really moves the pool price - deliberate; a sale cannot raise the
# peak) and leaves them out of the last trade / `no_trading`, the activity counts and the buy / sell pressure (volume), so a dead token does not look traded.
VERSIONS = {"ls-1": {"service_swaps": "count"}, "ls-2": {"service_swaps": "exclude"}}


# ============================================================================================ config

def live_from_of(day: str) -> int:
    """`live_state.history_until` (UTC, `YYYY-MM-DD` or `YYYY-MM-DD HH:MM`) in UTC ns: the first moment from which `facts` holds EVERY swap of every followed pool. The first stored fact is
    older (sparse windows of earlier collection), so it is not that moment; the diagnostics print the measured candidates next to this configured date."""
    import datetime as dt
    fmt = "%Y-%m-%d %H:%M" if " " in day.strip() else "%Y-%m-%d"
    return int(dt.datetime.strptime(day.strip(), fmt).replace(tzinfo=dt.timezone.utc).timestamp()) * 10**9


def settings(cfg: dict[str, Any]) -> dict[str, Any]:
    c = cfg["live_state"]
    if str(c["version"]) not in VERSIONS:
        raise ValueError(f"live_state.version {c['version']!r}: unknown (known: {', '.join(VERSIONS)})")
    return {"version": str(c["version"]), "service_swaps": VERSIONS[str(c["version"])]["service_swaps"], "recent_min": [int(m) for m in c["recent_min"]], "volume_window_min": int(c["volume_window_min"]),
            "buyers_window_min": int(c["buyers_window_min"]), "buyers_full_at": int(c["buyers_full_at"]),
            "no_trading_hours": float(c["no_trading_hours"]), "fixed_minutes": [int(m) for m in c["fixed_minutes"]],
            "active_every_s": float(c["active_every_s"]), "idle_every_s": float(c["idle_every_s"]),
            "dormant_every_s": float(c.get("dormant_every_s", 86400)), "active_window_min": int(c["active_window_min"]),
            "history_until": str(c["history_until"]), "live_from_ns": live_from_of(str(c["history_until"]))}


def page_settings() -> dict[str, Any]:
    """The settings the page and the methodology print (defaults when the config is broken: a broken override must not break the page)."""
    try:
        return settings(load_config())
    except Exception:  # noqa: BLE001
        return {"version": "ls-2", "service_swaps": "exclude", "recent_min": [15, 60], "volume_window_min": 60, "buyers_window_min": 60, "buyers_full_at": 20,
                "no_trading_hours": 24.0, "fixed_minutes": [5, 15, 60, 1440], "active_every_s": 60.0, "idle_every_s": 3600.0,
                "dormant_every_s": 86400.0, "active_window_min": 60, "history_until": "2026-10-30 12:14", "live_from_ns": live_from_of("2026-10-30 12:14")}


def describe(cfg: dict[str, Any]) -> dict[str, Any]:
    """The methodology text, from the constants the computation uses."""
    s = settings(cfg)
    return {
        "version": s["version"],
        "intro": "The Live state block on a launch page shows what the trades of the launch look like right now, as of the time printed next to it. It is not a forecast, "
                 "it is not a grade, it is not part of the risk score or the rug label, and it is not sent to the channel. Nothing in it is fitted: "
                 f"the number is a fixed formula, version {s['version']}, and the version is printed beside the number.",
        "formula": "The number is the mean of four parts, each from 0 to 100, rounded to a whole number (half up). The parts themselves are rounded to whole numbers first, "
                   "so the mean can be checked from the four numbers shown.",
        "parts": [
            {"id": "profit", "title": "Buyers in profit",
             "description": "The share of the outside buyers who are in profit now: what a buyer received from selling, plus the tokens still held (bought minus sold, never below zero) "
                            "valued at the last trade price, minus what the buyer spent, is above zero. Outside buyers are the wallets that bought from the pool and are not the dev, "
                            "not a wallet that got tokens from the dev and not a service account."},
            {"id": "volume", "title": "Buy pressure",
             "description": f"Of all the NEAR traded on the pool in the last {s['volume_window_min']} minutes, the share that went into buys."},
            {"id": "peak", "title": "Price vs peak",
             "description": "The last trade price as a percentage of the highest trade price since the launch."},
            {"id": "buyers", "title": "Activity",
             "description": f"The number of different outside buyers who bought in the last {s['buyers_window_min']} minutes on a log scale: 0 buyers is 0, "
                            f"{s['buyers_full_at']} or more is 100, in between 100 x ln(1 + n) / ln(1 + {s['buyers_full_at']})."}],
        "empty": "A part that has nothing to divide (no outside buyer, no trade in the window) counts as 0; the formula never leaves a part out.",
        "no_trading": f"If nothing has traded for {s['no_trading_hours']:g} hours, or nothing has traded yet, the block says so and shows no number."
                      + (" A swap by a service account (for example the account that sells the collected trade tax into the pool) is not trading: it is left out of the "
                         "last trade, the buys and sells and the buy pressure. It still moves the pool price, so the price facts include it."
                         if s["service_swaps"] == "exclude" else ""),
        "facts": f"Beside the number the block shows facts as of the same moment: the price against the first outside buyer's entry and against the peak, how many outside buyers "
                 f"are in profit, how much of the supply the dev, the wallets that got tokens from the dev, the early buyers that are linked to the dev side, in a bundle, in a pair or "
                 f"a group{' or on a fresh account' if HO.SHOW_AGE_ROWS else ''} still hold (the same rules and the same code as the table of holdings), the buys and sells of the last "
                 f"{' and '.join(str(m) for m in s['recent_min'])} minutes, the buyers of the last hour and the time of the last trade. The holdings are facts only; they are not in the number.",
        "gap": f"For a launch that happened before our complete live data starts ({s['history_until']}), the trades between its first 24 hours and that day are missing. For such a launch the block says "
               "that the history is incomplete, shows only the facts that do not need the missing trades, and shows no number. For a launch whose missing trades were loaded "
               "afterwards (from the same transaction index as its first 24 hours, up to that day), the block is computed from its whole history like any other launch.",
        "refresh": f"The block is recomputed every {s['active_every_s']:g} seconds while the launch trades, hourly after that and daily once it has been quiet for {s['no_trading_hours']:g} hours "
                   "(a new trade brings it back at once). The numbers at fixed minutes after the launch are kept for study; they are not a track record and are not shown.",
        "not": "The number says how the trading looks at that moment. It does not say what will happen, and it does not say who controls a wallet.",
    }


# ============================================================================================ the four parts (pure)

def _half_up(x: float | Decimal) -> int:
    """Half up on a value rounded to 6 places first (the same rule as the fee points), so no platform decides a .5."""
    return int(math.floor(round(float(x), 6) + 0.5))


def _pct(num: int, den: int) -> int:
    """100 * num / den rounded half up, in integers; 0 when there is nothing to divide."""
    return (200 * num + den) // (2 * den) if den > 0 else 0


def part_profit(buyers: dict[str, dict[str, int]], price_last: Decimal | None) -> dict[str, Any]:
    n = len(buyers)
    k = 0
    for b in buyers.values():
        unsold = max(0, b["bought"] - b["sold"])
        value = int(unsold * price_last) if price_last is not None else 0
        if b["received"] + value - b["spent"] > 0:
            k += 1
    return {"value": _pct(k, n), "in_profit": k, "buyers": n}


def part_volume(trades: list[dict], now_ns: int, window_min: int) -> dict[str, Any]:
    lo = now_ns - window_min * NS_MIN
    buys = sum(t["near"] for t in trades if t["side"] == "buy" and lo < t["ts"] <= now_ns)
    sells = sum(t["near"] for t in trades if t["side"] == "sell" and lo < t["ts"] <= now_ns)
    return {"value": _pct(buys, buys + sells), "buys_near": str(buys), "sells_near": str(sells), "window_min": window_min}


def part_peak(price_last: Decimal | None, peak: Decimal | None) -> dict[str, Any]:
    if price_last is None or peak is None or peak <= 0:
        return {"value": 0, "price_last": None, "price_peak": None}
    v = (Decimal(100) * price_last / peak + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR)
    return {"value": int(min(Decimal(100), v)), "price_last": S._dec(price_last), "price_peak": S._dec(peak)}


def buyers_in_window(trades: list[dict], buyers: dict[str, dict[str, int]], now_ns: int, window_min: int) -> int:
    lo = now_ns - window_min * NS_MIN
    return len({t["who"] for t in trades if t["side"] == "buy" and t["who"] in buyers and lo < t["ts"] <= now_ns})


def part_buyers(n: int, full_at: int, window_min: int) -> dict[str, Any]:
    v = 100 if n >= full_at else _half_up(100 * math.log1p(n) / math.log1p(full_at)) if n > 0 else 0
    return {"value": min(100, v), "unique": n, "window_min": window_min, "full_at": full_at}


def number_of(parts: dict[str, dict[str, Any]]) -> int:
    """The number: the mean of the four integer parts, rounded half up (integer arithmetic)."""
    return (sum(int(parts[k]["value"]) for k in PARTS) + 2) // 4


def activity(trades: list[dict], now_ns: int, minutes: list[int]) -> list[dict[str, Any]]:
    """Buys and sells (NEAR and count, every swap on the pool) of the last N minutes."""
    out = []
    for m in minutes:
        lo = now_ns - m * NS_MIN
        w = [t for t in trades if lo < t["ts"] <= now_ns]
        b = [t for t in w if t["side"] == "buy"]
        s = [t for t in w if t["side"] == "sell"]
        out.append({"minutes": m, "buys": len(b), "sells": len(s), "buys_near": str(sum(t["near"] for t in b)), "sells_near": str(sum(t["near"] for t in s))})
    return out


def counted(trades: list[dict], token: str, s: dict[str, Any]) -> list[dict]:
    """The trades that count as trading (last trade, `no_trading`, activity, volume): all of them (ls-1), or without the swaps of service accounts (ls-2). A trade whose
    account could not be told (a router we cannot see through, `who` None) still counts: it is somebody's trade."""
    if s["service_swaps"] != "exclude":
        return trades
    return [t for t in trades if t.get("who") is None or not S.is_service(t["who"], token)]


def _x(a: Decimal | None, b: Decimal | None) -> float | None:
    return round(float(a / b), 6) if a is not None and b else None


# ============================================================================================ the computation (pure)

def _mask_near(launch: dict[str, Any], parts: dict[str, Any], activity_rows: list[dict[str, Any]], near_ok: bool = False) -> None:
    """RGY-066 (Q-046): a pool that is not paired with wNEAR has no NEAR volumes (raw units of another token): the amounts are None («—»); counts and shares stay.
    RGY-073: not when the swaps handed to `compute` were converted to NEAR at their swap-time price (`near_ok`, `snapshots.to_near`)."""
    if not near_ok and not_near_quote(launch["token"], launch.get("pool")):
        if "volume" in parts:
            parts["volume"]["buys_near"] = parts["volume"]["sells_near"] = None
        for a in activity_rows:
            a["buys_near"] = a["sells_near"] = None


def compute(launch: dict[str, Any], legs: list[dict], swaps: list[dict], now_ns: int, early: list[dict[str, Any]], tags: dict[str, dict[str, Any]],
            ages: dict[str, int | None], cfg: dict[str, Any], early_final: bool = True, near_ok: bool = False, price_age_s: int | None = None) -> dict[str, Any]:
    """`near_ok` (RGY-073): `swaps` of a pool not paired with wNEAR were converted to NEAR (the amounts are shown, the price is in NEAR per token). `price_age_s` (RGY-075): the oldest
    price the conversion used; older than the strict 15 min = the amounts are approximate (`facts.quote_price.approx`, shown with «≈» and the age).
    The state of a launch at `now_ns`: {state ok | no_trading, number, parts, facts, asof_ns, last_trade_ns, version}. `legs` / `swaps` may be longer: they are cut at `now_ns`.
    The same function computes "now" and the fixed minutes. Pure."""
    s = settings(cfg)
    token = launch["token"]
    lg = [x for x in legs if x["ts"] <= now_ns]
    sw = [x for x in swaps if x["ts"] <= now_ns]
    trades = S.attribute(token, lg, sw)
    leg_ids = {i for t in trades for i in t["legs"]}
    dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != S.LAUNCHBOT}
    ins = S._insiders(token, dev, lg, leg_ids)
    buyers = S.outside_buyers(trades, ins["accounts"], token)
    priced, price_last, peak, first_buy = S.market_points(trades, buyers)        # the price: every swap (a service sale moves the pool price too)
    live = counted(trades, token, s)
    last_priced = [t for t in live if t["price"] is not None]
    last_trade = last_priced[-1]["ts"] if last_priced else None

    parts = {
        "profit": part_profit(buyers, price_last),
        "volume": part_volume(live, now_ns, s["volume_window_min"]),
        "peak": part_peak(price_last, peak["price"] if peak else None),
        "buyers": part_buyers(buyers_in_window(trades, buyers, now_ns, s["buyers_window_min"]), s["buyers_full_at"], s["buyers_window_min"]),
    }
    quiet = last_trade is None or now_ns - last_trade > s["no_trading_hours"] * NS_H
    state = "no_trading" if quiet else "ok"

    hold = HO.compute_minute(launch, lg, sw, max(0, (now_ns - launch["ts_ns"]) // NS_MIN), early, tags, ages, HO.settings(cfg)["fresh_hours"] * 3600, cutoff=now_ns)
    in_profit = parts["profit"]["in_profit"]
    facts = {
        "asof_ns": now_ns, "last_trade_ns": last_trade,
        "price": {"last": S._dec(price_last), "first_outside_buyer": S._dec(first_buy["price"]) if first_buy else None, "peak": S._dec(peak["price"]) if peak else None,
                  "vs_entry_x": _x(price_last, first_buy["price"] if first_buy else None), "vs_peak_x": _x(price_last, peak["price"] if peak else None)},
        "outside_buyers": {"count": len(buyers), "in_profit": in_profit, "share_bps": in_profit * 10_000 // len(buyers) if buyers else None},
        "activity": activity(live, now_ns, s["recent_min"]),
        "buyers_hour": parts["buyers"]["unique"],
        "holdings": {"supply": hold["supply"], "supply_note": hold["supply_note"], "early_final": bool(early_final), "early_wallets": hold["extra"]["early_wallets"],
                     "rows": [{"key": r["key"], "wallets": r["wallets"], "holding": r["holding"], "held": r["held"],
                               "bps": HO.share_bps(hold["supply"], hold["supply_note"], int(r["held"]))} for r in hold["rows"]]},
    }
    _mask_near(launch, parts, facts["activity"], near_ok)
    q = not_near_quote(token, launch.get("pool"))
    if q:                                                         # RGY-075: what the NEAR amounts of this state rest on
        facts["quote_price"] = {"quote": q, "near": bool(near_ok), "approx": bool(near_ok) and QP.is_approx(price_age_s), "price_age_s": price_age_s if near_ok else None}
    return {"state": state, "number": number_of(parts) if state == "ok" else None, "parts": parts, "facts": facts, "asof_ns": now_ns,
            "last_trade_ns": last_trade, "version": s["version"]}


def compute_gap(launch: dict[str, Any], legs: list[dict], swaps: list[dict], now_ns: int, cfg: dict[str, Any], live_from_ns: int | None,
                api_last: dict[str, Any] | None) -> dict[str, Any]:
    """A history launch (RGY-043 history gap): `legs` / `swaps` are the rows of the LIVE data only; `api_last` = the last swap of its 24 h window {ts, price} or None.
    No number: only the facts that do not need the missing trades. Pure."""
    s = settings(cfg)
    token = launch["token"]
    lo = live_from_ns if live_from_ns is not None else 0
    lg = [x for x in legs if lo <= x["ts"] <= now_ns]               # only the complete live data: what lies before it is sparse
    sw = [x for x in swaps if lo <= x["ts"] <= now_ns]
    trades = S.attribute(token, lg, sw)
    leg_ids = {i for t in trades for i in t["legs"]}
    dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != S.LAUNCHBOT}
    ins = S._insiders(token, dev, lg, leg_ids)
    buyers = S.outside_buyers(trades, ins["accounts"], token)
    allp = [t for t in trades if t["price"] is not None]                     # the price: every swap
    priced = [t for t in counted(trades, token, s) if t["price"] is not None]  # the last trade: what counts as trading (ls-2: no service swaps)
    if priced and (not api_last or priced[-1]["ts"] >= api_last["ts"]):
        last_trade, where = priced[-1]["ts"], "live"
    elif api_last:
        last_trade, where = api_last["ts"], "window"
    else:
        last_trade, where = None, None
    if allp and (not api_last or allp[-1]["ts"] >= api_last["ts"]):
        price_last = allp[-1]["price"]
    else:
        price_last = api_last["price"] if api_last else None
    window_end = launch["ts_ns"] + S.MINUTES[-1] * NS_MIN
    facts = {
        "asof_ns": now_ns, "last_trade_ns": last_trade, "last_trade_where": where, "price": {"last": S._dec(price_last)},
        "activity": activity(counted(trades, token, s), now_ns, s["recent_min"]),
        "buyers_hour": buyers_in_window(trades, buyers, now_ns, s["buyers_window_min"]),
        "live_swaps": len(priced), "gap_from_ns": window_end, "gap_to_ns": live_from_ns,
    }
    _mask_near(launch, {}, facts["activity"])
    return {"state": "history_gap", "number": None, "parts": {}, "facts": facts, "asof_ns": now_ns, "last_trade_ns": last_trade, "version": s["version"]}


# ============================================================================================ reading the stored inputs

def _has(con: Any, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def first_fact_ns(con: Any) -> int | None:
    """The first stored fact (NOT where the complete live data starts: see `live_from_of`)."""
    return con.execute("SELECT MIN(ts_ns) FROM facts").fetchone()[0]


def asof_now(con: Any) -> int | None:
    """The moment 'now' is as of: the time of the last processed block (not the wall clock - the numbers cover exactly the blocks we have)."""
    r = con.execute("SELECT block_ts_ns FROM blocks WHERE status = 'ok' ORDER BY height DESC LIMIT 1").fetchone()          # the primary key, newest first: no scan of the table
    return int(r[0]) if r and r[0] is not None else None


def _coverage(store: Store, token: str) -> str | None:
    if store.user_version() < 6:
        return None
    r = store.con.execute("SELECT coverage FROM launches WHERE token = ?", (token,)).fetchone()
    return r[0] if r else None


def api_last_swap(store: Store, launch: dict[str, Any]) -> dict[str, Any] | None:
    """The last swap of the pool inside the launch's 24 h window (`api_facts`): {ts, price}; None when there is none."""
    if not launch.get("pool"):
        return None
    r = store.con.execute("SELECT f.ts_ns, f.extra FROM api_facts f JOIN kinds k ON k.id = f.kind JOIN accounts b ON b.id = f.b "
                          "WHERE f.scope = ? AND k.name = 'dcl_swap' AND b.name = ? ORDER BY f.height DESC, f.ord DESC, f.seq DESC LIMIT 1",
                          (launch["token"], launch["pool"])).fetchone()
    if not r:
        return None
    e = json.loads(r[1]) if r[1] else {}
    ai, ao = int(e.get("amount_in") or 0), int(e.get("amount_out") or 0)
    if not ai or not ao:
        return None
    price = S._price(ai, ao) if e.get("token_out") == launch["token"] else S._price(ao, ai)
    return {"ts": int(r[0]), "price": price}


class Context:
    """Per-process cache of what changes slowly (early buyers, their tags and ages) so a launch recomputed every minute does not re-read them."""

    def __init__(self, ttl_s: float = 300.0) -> None:
        self.ttl = ttl_s
        self._d: dict[str, tuple[float, list, dict, dict, bool]] = {}
        self.ages: dict[str, int | None] = {}
        self.fixed_checked = 0.0                           # wall time of the last look for fixed minutes that are due (at most every FIXED_EVERY_S)
        self.skip: dict[tuple[str, int], float] = {}       # (token, minute) -> until when a window that is not covered is not looked at again

    def get(self, store: Store, token: str, now: float | None = None) -> tuple[list, dict, dict, bool]:
        t = time.time() if now is None else now
        hit = self._d.get(token)
        if hit and t - hit[0] < self.ttl:
            return hit[1:]                                   # type: ignore[return-value]
        con = store.con
        early = [{"buyer": b, "first_buy_ns": ts} for b, ts in
                 con.execute("SELECT buyer, first_buy_ns FROM launch_buyers WHERE token = ? ORDER BY rank", (token,))] if _has(con, "launch_buyers") else []
        done = con.execute("SELECT final FROM launch_buyers_done WHERE token = ?", (token,)).fetchone() if _has(con, "launch_buyers_done") else None
        final = bool(done and done[0])
        got = (early, HO.load_tags(con, token), HO.load_ages(store, early, self.ages), final)
        self._d[token] = (t, *got)
        return got


def compute_token(store: Store, token: str, now_ns: int, cfg: dict[str, Any], ctx: Context | None = None, live_from: int | None = None) -> tuple[dict[str, Any], str]:
    """(result, source live | history) of one launch as of `now_ns`, read from the database. Raises KeyError for an unknown launch."""
    launch = S._launch(store, token)
    if launch is None:
        raise KeyError(token)
    ctx = ctx or Context()
    if _coverage(store, token) is not None:
        lf = live_from if live_from is not None else settings(cfg)["live_from_ns"]
        if BG.closed(store, token):                                     # RGY-055: the gap is filled - the whole history, the same computation as a live launch
            legs, swaps = BG.full_rows(store, launch, now_ns, lf)
            early, tags, ages, final = ctx.get(store, token)
            swaps, ok, age = S.to_near_age(launch, swaps, S.quote_series(store, launch, now_ns))          # RGY-073 / 075
            return compute(launch, legs, swaps, now_ns, early, tags, ages, cfg, final, bool(ok), age), "history"
        legs, swaps = S._rows(store, launch, now_ns)
        return compute_gap(launch, legs, swaps, now_ns, cfg, lf, api_last_swap(store, launch)), "history"
    legs, swaps = S._rows(store, launch, now_ns)
    early, tags, ages, final = ctx.get(store, token)
    swaps, ok, age = S.to_near_age(launch, swaps, S.quote_series(store, launch, now_ns))                 # RGY-073 / 075: a quote pool's trades in NEAR at the swap-time price (all or nothing)
    return compute(launch, legs, swaps, now_ns, early, tags, ages, cfg, final, bool(ok), age), "live"


# ============================================================================================ storage and refresh

def _j(x: Any) -> str:
    return json.dumps(x, separators=(",", ":"), sort_keys=True)


def write_now(store: Store, token: str, res: dict[str, Any], source: str, cfg: dict[str, Any]) -> None:
    store.con.execute(
        "INSERT OR REPLACE INTO launch_live_state(token, state, number, parts, facts, asof_ns, last_trade_ns, version, config_hash, source, computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (token, res["state"], res["number"], _j(res["parts"]), _j(res["facts"]), res["asof_ns"], res["last_trade_ns"], res["version"],
         live_state_config_hash(cfg), source, utcnow_iso()))


def _iso_ago(seconds: float, now: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(now - seconds, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def active_tokens(store: Store, since_ns: int) -> set[str]:
    """Launches whose pool had a swap since `since_ns` (one query on the time index: the kind index would walk every swap ever stored)."""
    kid = store.lookup_kind("dcl_swap")
    if kid is None:
        return set()
    pools = {r[0] for r in store.con.execute("SELECT DISTINCT bb.name FROM facts f INDEXED BY facts_ts JOIN accounts bb ON bb.id = f.b WHERE f.kind = ? AND f.ts_ns > ?", (kid, since_ns))}
    if not pools:
        return set()
    return {t for t, p in store.con.execute("SELECT token, pool FROM launches WHERE pool IS NOT NULL") if p in pools}


def due(store: Store, cfg: dict[str, Any], asof_ns: int, now: float | None = None) -> list[str]:
    """Launches whose Live state must be recomputed, the most urgent first: no row, another version / config, or older than its interval (module docstring)."""
    s = settings(cfg)
    t = time.time() if now is None else now
    h = live_state_config_hash(cfg)
    active_now = active_tokens(store, asof_ns - s["active_window_min"] * NS_MIN)
    rows = {tok: (comp, last, ver, ch) for tok, comp, last, ver, ch in
            store.con.execute("SELECT token, computed_at, last_trade_ns, version, config_hash FROM launch_live_state")}
    out: list[tuple[int, str, str]] = []
    for (tok,) in store.con.execute("SELECT token FROM launches ORDER BY ts_ns DESC"):
        r = rows.get(tok)
        if r is None or r[2] != s["version"] or r[3] != h:
            out.append((0, NOT_COMPUTED, tok))
            continue
        comp, last = r[0], r[1]
        if tok in active_now or (last is not None and last > asof_ns - s["active_window_min"] * NS_MIN):
            every, rank = s["active_every_s"], 1
        elif last is not None and last > asof_ns - s["no_trading_hours"] * NS_H:
            every, rank = s["idle_every_s"], 2
        else:
            every, rank = s["dormant_every_s"], 3
        if comp < _iso_ago(every, t):
            out.append((rank, comp, tok))
    out.sort()
    return [tok for _, _, tok in out]


def fixed_due(store: Store, cfg: dict[str, Any], asof_ns: int, ctx: "Context | None" = None, now: float | None = None) -> list[tuple[str, list[int], str]]:
    """(token, minutes, source) of the launches with a fixed minute that has passed, whose row of this version is missing and whose data is complete: a live launch needs the
    covered window, a history launch a finished backfill (status done); both need the early buyers FINAL (the holdings facts are part of the row)."""
    if not (_has(store.con, "launch_live_state_at") and _has(store.con, "launch_buyers_done")):
        return []
    s = settings(cfg)
    con = store.con
    have = defaultdict(set)
    for tok, m in con.execute("SELECT token, minute FROM launch_live_state_at WHERE version = ?", (s["version"],)):
        have[tok].add(m)
    hist = {r[0] for r in con.execute("SELECT key FROM backfill_queue WHERE kind = 'launch' AND status = 'done'")} if _has(con, "backfill_queue") else set()
    cov = "l.coverage" if store.user_version() >= 6 else "NULL"
    out = []
    for tok, ts, height, c, final in con.execute(f"SELECT l.token, l.ts_ns, l.height, {cov}, d.final FROM launches l JOIN launch_buyers_done d ON d.token = l.token ORDER BY l.ts_ns"):
        if not final:
            continue
        mins = [m for m in s["fixed_minutes"] if m not in have[tok] and ts + m * NS_MIN <= asof_ns]
        if not mins:
            continue
        if c is None:
            t = time.time() if now is None else now
            ok = []
            # Every window starts at the launch height, so a longer window contains the shorter ones: the longest covered one covers all below it (ONE count of its blocks, not one
            # per minute). A window with a hole stays one: do not count its blocks every pass.
            for m in sorted(mins, reverse=True):
                if ctx is not None and ctx.skip.get((tok, m), 0.0) > t:
                    continue
                if S.window_covered(store, height, ts + m * NS_MIN):
                    ok.extend(x for x in mins if x <= m)
                    break
                if ctx is not None:
                    ctx.skip[(tok, m)] = t + SKIP_S
            mins = sorted(set(ok))
            src = "live"
        else:
            if tok not in hist:
                continue
            src = "history"
        if mins:
            out.append((tok, mins, src))
    return out


def write_fixed(store: Store, token: str, minute: int, res: dict[str, Any], cfg: dict[str, Any]) -> None:
    store.con.execute(
        "INSERT OR IGNORE INTO launch_live_state_at(token, minute, version, state, number, parts, facts, config_hash, computed_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (token, minute, res["version"], res["state"], res["number"], _j(res["parts"]), _j(res["facts"]), live_state_config_hash(cfg), utcnow_iso()))


def compute_fixed(store: Store, token: str, minutes: list[int], source: str, cfg: dict[str, Any], ctx: Context) -> dict[int, dict[str, Any]]:
    """The state at launch + each minute, from ONE read of the rows (the same `compute` as 'now')."""
    launch = S._launch(store, token)
    cutoff = launch["ts_ns"] + max(minutes) * NS_MIN
    legs, swaps = S._rows(store, launch, cutoff) if source == "live" else BF.rows_from_api(store, launch, cutoff)
    early, tags, ages, final = ctx.get(store, token)
    series = S.quote_series(store, launch, cutoff)                                                        # RGY-073: each fixed minute converts only the trades up to it
    out = {}
    for m in minutes:
        at = launch["ts_ns"] + m * NS_MIN
        sw, ok, age = S.to_near_age(launch, [x for x in swaps if x["ts"] <= at], series)
        out[m] = compute(launch, legs, sw, at, early, tags, ages, cfg, final, bool(ok), age)
    return out


def run(store: Store, cfg: dict[str, Any] | None = None, log: Callable[[str], None] = lambda m: None, asof_ns: int | None = None, now: float | None = None,
        budget_s: float = BUDGET_S, limit: int = MAX_PER_TICK, only: list[str] | None = None, ctx: Context | None = None,
        fixed_limit: int = 20) -> dict[str, Any]:
    """One pass: the due launches (at most `limit`, within `budget_s`), then the fixed minutes that are due (at most `fixed_limit` launches). Each launch is its own small
    transaction batch; a failing launch is counted and never stops the pass."""
    t0 = time.time()
    cfg = cfg or load_config()
    con = store.con
    if store.user_version() < 20:
        raise RuntimeError("livestate: the database is not migrated to schema v20 yet")
    asof = asof_ns if asof_ns is not None else asof_now(con)
    out: dict[str, Any] = {"asof_ns": asof, "due": 0, "computed": 0, "failed": 0, "failed_tokens": [], "fixed": 0, "fixed_launches": 0, "ms": []}
    if asof is None:
        return out
    ctx = ctx or Context()
    live_from = settings(cfg)["live_from_ns"]
    t_a = time.time()
    todo = due(store, cfg, asof, now)
    out["t_due"] = round(time.time() - t_a, 2)
    if only is not None:
        todo = [t for t in todo if t in set(only)]
    out["due"] = len(todo)
    batch: list[tuple[str, dict, str]] = []

    def flush() -> None:
        if not batch:
            return
        store.begin()
        try:
            for tok, res, src in batch:
                write_now(store, tok, res, src, cfg)
            store.commit()
        except BaseException:
            store.rollback()
            raise
        batch.clear()

    for tok in todo[:limit]:
        if time.time() - t0 > budget_s:
            break
        t1 = time.time()
        try:
            res, src = compute_token(store, tok, asof, cfg, ctx, live_from)
            batch.append((tok, res, src))
            out["computed"] += 1
        except Exception as e:  # noqa: BLE001 - one launch must not stop the pass
            out["failed"] += 1
            out["failed_tokens"].append(f"{tok}: {type(e).__name__}: {str(e)[:120]}")
        out["ms"].append(round((time.time() - t1) * 1000, 1))
        if len(batch) >= 25:
            flush()
    flush()
    out["t_compute"] = round(time.time() - t0 - out["t_due"], 2)
    wall = time.time() if now is None else now
    t_f = time.time()
    look = wall - ctx.fixed_checked >= FIXED_EVERY_S
    if look:
        ctx.fixed_checked = wall
    fixed = fixed_due(store, cfg, asof, ctx, now)[:fixed_limit] if look else []
    out["t_fixed_due"] = round(time.time() - t_f, 2)
    for tok, mins, src in fixed:
        if time.time() - t0 > budget_s:
            break
        try:
            got = compute_fixed(store, tok, mins, src, cfg, ctx)
            store.begin()
            try:
                for m, res in got.items():
                    write_fixed(store, tok, m, res, cfg)
                store.commit()
            except BaseException:
                store.rollback()
                raise
            out["fixed"] += len(got)
            out["fixed_launches"] += 1
        except Exception as e:  # noqa: BLE001
            out["failed"] += 1
            out["failed_tokens"].append(f"{tok} fixed: {type(e).__name__}: {str(e)[:120]}")
    out["seconds"] = round(time.time() - t0, 2)
    return out


def refresh(store: Store, log: Callable[[str], None], ctx: Context | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Collector hook, between blocks: recompute what is due (module docstring). A failure is logged and remembered (`live_state_last_error`); it never stops the collector."""
    if store.user_version() < 20:
        return None
    try:
        res = run(store, log=log, now=now, ctx=ctx)
        if res["computed"] or res["fixed"] or res["failed"]:
            ms = sorted(res["ms"])
            store.set_status("live_state_built", {"at": utcnow_iso(), "computed": res["computed"], "fixed": res["fixed"], "failed": res["failed"], "due": res["due"],
                                                  "seconds": res.get("seconds"), "max_ms": ms[-1] if ms else None, "median_ms": ms[len(ms) // 2] if ms else None,
                                                  "t_due": res.get("t_due"), "t_compute": res.get("t_compute"), "t_fixed_due": res.get("t_fixed_due")})
            if res["failed"]:
                log(f"live state: {res['computed']} launches, {res['fixed']} fixed rows, {res['failed']} failed ({res.get('seconds')} s)")
            store.set_status("live_state_last_error", None)
        return res
    except Exception as e:  # noqa: BLE001 - never stops the collector
        log(f"live state: {type(e).__name__}: {e}")
        try:
            store.set_status("live_state_last_error", {"error": f"{type(e).__name__}: {e}"[:300], "at": utcnow_iso()})
        except Exception:  # noqa: BLE001
            pass
        return None


# ============================================================================================ reader (site, diag)

PART_TITLES = {"profit": "Buyers in profit", "volume": "Buy pressure", "peak": "Price vs peak", "buyers": "Activity"}


def launch_live_state(con: Any, token: str) -> dict[str, Any] | None:
    """What the launch page shows, or None when nothing is stored (the block is then left out): {state, number, version, asof_ns, last_trade_ns, source, parts: [{id, title,
    value, ...inputs}], facts, settings}."""
    con = getattr(con, "con", con)
    if not _has(con, "launch_live_state"):
        return None
    r = con.execute("SELECT state, number, parts, facts, asof_ns, last_trade_ns, version, source FROM launch_live_state WHERE token = ?", (token,)).fetchone()
    if not r:
        return None
    parts = json.loads(r[2]) if r[2] else {}
    facts = json.loads(r[3]) if r[3] else {}
    hold = facts.get("holdings") or {}
    hold_rows = [dict(x, label=HO.LABELS[x["key"]]) for x in hold.get("rows", []) if x["key"] in HOLD_KEYS and x["key"] in HO.page_keys()]      # Q-022: no age rows
    qp = facts.get("quote_price") or {}                          # RGY-075: the NEAR amounts of a quote pool rest on a converted price (approx = older than the strict 15 min)
    age = qp.get("price_age_s")
    return {"state": r[0], "number": r[1], "version": r[6], "asof_ns": r[4], "last_trade_ns": r[5], "source": r[7], "facts": facts,
            "quote_token": qp.get("quote"), "near_known": qp.get("near") if qp else None, "approx": bool(qp.get("approx")), "price_age_s": age if qp.get("approx") else None,
            "price_age_text": QP.age_text(age) if qp.get("approx") else None,
            "hold_rows": hold_rows, "hold_ok": bool(hold) and hold.get("supply_note") == "ok" and any(x["bps"] is not None for x in hold_rows),
            "hold_note": hold.get("supply_note"), "early_final": bool(hold.get("early_final")),
            "parts": [dict(parts[k], id=k, title=PART_TITLES[k]) for k in PARTS if k in parts], "settings": page_settings()}
