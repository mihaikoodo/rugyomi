"""RGY-070 (B of Q-046): the NEAR price of a quote token, from swaps that really happened, and the conversion of a launch's swaps into NEAR.

A launch pool `T|Q|fee` quotes the launch token T in another token Q (not wNEAR). Its swap amounts on the quote side are raw units of Q. To get NEAR amounts the
price of Q in NEAR is needed AT THE TIME OF EACH SWAP. The only price accepted here is a PROVABLE one: a swap of the pool of Q with wNEAR (`Q|wrap.near|fee`, any
fee tier, same DEX contract) - its two amounts give yocto NEAR per raw unit of Q, with the transaction hash. No off-chain feed, no interpolation, no look-ahead:

  price(Q, t) = the price of the LAST such swap at or before t, when it is at most `max_age` older than t; otherwise there is no price.
  A swap whose wNEAR side is below MIN_WNEAR is not a price (a dust swap is mostly rounding).
  Several pools of the pair (fee tiers): the last swap over all of them.

Where the swaps come from (all already stored, no credits): `facts` (kind dcl_swap, b = the pool; the live collector stores a swap when a pool token is a launch
token, so the wNEAR pool of a memetoken that is our own launch is there) and `api_facts` (any scope: the launch window of a history launch and the quote windows the
collector `backfill_quote` loads for the tokens that have no stored pool). The same swap in both is read once (receipt + origin).

`convert_swaps` turns the swaps of the launch pool into the same dicts with the quote side in yocto NEAR and the quote token replaced by wNEAR, so `snapshots.attribute`
and everything after it run unchanged (one conversion function, no second copy of the attribution). It is all or nothing: ONE swap without a price and the launch
stays "undetermined" (`ok` False) - never a partly converted sum. Amounts are integers: raw Q * price (Decimal, 60 digits) truncated. Decimals of Q are not needed
(price is NEAR yocto per RAW unit), so 6- and 24-decimal quotes work the same.

Pure except `load` / `series_for` (read only). Leaf module: imports the wNEAR constant and the store helpers only."""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from typing import Any

from .parse import WRAP
from .store.db import Store, hash_str

MAX_AGE_MIN = 15                 # a price older than this at the swap time is no price (the diag prints the coverage at 5 / 15 / 60 / 360 min: the user chooses)
DISPLAY_MAX_AGE_MIN = 24 * 60    # RGY-075: the page rule - the last known price up to 24 h old, shown as approximate («≈») with its age; outcomes, labels and cards keep MAX_AGE_MIN
MIN_WNEAR = 10**21               # 0.001 NEAR: a swap with less wNEAR is not a price
WINDOW_MIN = 1440                # the launch window the snapshots read (T+24 h)
NS_MIN = 60 * 10**9
PREC = 60


@dataclass
class Point:
    ts: int
    price: Decimal               # yocto NEAR per RAW unit of the quote token
    pool: str
    tx: str | None


@dataclass
class Series:
    token: str                   # the quote token Q
    points: list[Point] = field(default_factory=list)
    max_age_ns: int = MAX_AGE_MIN * NS_MIN

    def __post_init__(self) -> None:
        self.points.sort(key=lambda p: p.ts)         # stable: equal times keep the reading order
        self._ts = [p.ts for p in self.points]

    def at(self, ts: int, max_age_ns: int | None = None) -> Point | None:
        """The last price at or before `ts`, at most `max_age_ns` older (default: the series' own age limit). ONE rule for the strict 15 min of the outcomes and the 24 h of the pages."""
        i = bisect_right(self._ts, ts) - 1
        if i < 0:
            return None
        p = self.points[i]
        return p if ts - p.ts <= (self.max_age_ns if max_age_ns is None else max_age_ns) else None


def is_approx(age_s: float | int | None) -> bool:
    """RGY-075: an amount converted with a price OLDER than the strict age of the outcomes (`MAX_AGE_MIN`) is approximate and is shown with «≈» and the age."""
    return age_s is not None and age_s > MAX_AGE_MIN * 60


def age_text(age_s: float | int | None) -> str | None:
    """«45 min», «3 h»: the price age as a page writes it (minutes under 90 min, whole hours after)."""
    if age_s is None:
        return None
    m = int(round(age_s / 60))
    return f"{max(1, m)} min" if m < 90 else f"{int(round(age_s / 3600))} h"


def with_age(series: Series, max_age_min: float) -> Series:
    """The same points under another maximum price age (the diag's sensitivity table)."""
    return Series(series.token, list(series.points), int(max_age_min * NS_MIN))


def pool_prefix(quote: str) -> str:
    """The pool id prefix of the pair Q / wNEAR: the two tokens sorted and joined with `|` (the fee follows)."""
    return "|".join(sorted([quote, WRAP])) + "|"


def price_of(swap: dict[str, Any], quote: str) -> Decimal | None:
    """Yocto NEAR per raw unit of `quote` from one swap of the Q / wNEAR pool, None when it is not a usable price."""
    ti, to = swap.get("token_in"), swap.get("token_out")
    ai, ao = int(swap.get("amount_in") or 0), int(swap.get("amount_out") or 0)
    if not ai or not ao:
        return None
    if ti == WRAP and to == quote:
        w, q = ai, ao
    elif ti == quote and to == WRAP:
        w, q = ao, ai
    else:
        return None
    if w < MIN_WNEAR:
        return None
    with localcontext() as c:
        c.prec = PREC
        return Decimal(w) / Decimal(q)


def scope_prefix(quote: str) -> str:
    """The `api_facts` scope prefix of the quote windows of Q (`backfill_quote`): "quote:<Q>:<window>"."""
    return f"quote:{quote}:"


def pair_pools(store: Store, quote: str) -> dict[int, str]:
    """{account id: pool name} of every stored pool of the pair Q / wNEAR (any fee). A range scan on the unique account name index."""
    pre = pool_prefix(quote)
    return {int(i): n for i, n in store.con.execute("SELECT id, name FROM accounts WHERE name >= ? AND name < ?", (pre, pre + "\U0010ffff"))}


def load(store: Store, quote: str, lo_ts: int, hi_ts: int, max_age_min: float = MAX_AGE_MIN) -> Series:
    """The price series of `quote` from the stored swaps in [lo_ts, hi_ts] (`facts` + `api_facts`, see the module docstring)."""
    s = Series(quote, [], int(max_age_min * NS_MIN))
    kind = store.lookup_kind("dcl_swap")
    pools = pair_pools(store, quote)
    if kind is None or not pools:
        return s
    ids = ",".join(str(i) for i in pools)
    seen: set[tuple[bytes, str]] = set()
    pts: list[Point] = []
    sources = [("facts", "", ())]
    if store.user_version() >= 11:               # api_facts has only the scope index: read the scopes that can hold the pool of Q (its own launch window, its gap, the quote windows)
        sources.append(("api_facts", " AND (scope = ? OR scope = ? OR (scope >= ? AND scope < ?))", (quote, "gap:" + quote, scope_prefix(quote), scope_prefix(quote) + "\U0010ffff")))
    for table, cond, args in sources:
        for ts, tx, b, receipt, origin, extra in store.con.execute(
                f"SELECT ts_ns, tx, b, receipt, origin, extra FROM {table} WHERE kind = ? AND b IN ({ids}) AND ts_ns >= ? AND ts_ns <= ?{cond} ORDER BY ts_ns, id",
                (kind, lo_ts, hi_ts, *args)):
            key = (receipt, origin)
            if key in seen:
                continue
            seen.add(key)
            p = price_of(json.loads(extra) if extra else {}, quote)
            if p is not None:
                pts.append(Point(int(ts), p, pools[int(b)], hash_str(tx) if tx else None))
    s.points = pts
    s.__post_init__()
    return s


def series_for(store: Store, launch: dict[str, Any], quote: str, max_age_min: float = MAX_AGE_MIN) -> Series:
    """The series a launch's 24 h snapshots need: from `max_age` before the launch to its T+24 h."""
    return load(store, quote, launch["ts_ns"] - int(max_age_min * NS_MIN), launch["ts_ns"] + WINDOW_MIN * NS_MIN, max_age_min)


class _View:
    """The few `Store` methods `load` needs, over a bare sqlite connection (the site's read-only reader has no Store)."""

    def __init__(self, con: Any) -> None:
        self.con = con

    def lookup_kind(self, name: str) -> int | None:
        r = self.con.execute("SELECT id FROM kinds WHERE name = ?", (name,)).fetchone()
        return int(r[0]) if r else None

    def user_version(self) -> int:
        return int(self.con.execute("PRAGMA user_version").fetchone()[0])


def near_at(db: Any, quote: str, raw: Any, ts: int, max_age_min: float = MAX_AGE_MIN) -> dict[str, Any] | None:
    """RGY-073: `raw` units of the quote token in yocto NEAR at the time `ts`: {near (str), price_ts, pool, tx}, or None when there is no provable price within the age
    (the same series and rule as the trades: the last Q / wNEAR swap at or before `ts`, at most `max_age_min` older). `db`: a Store or a sqlite connection."""
    try:
        r = int(raw or 0)
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    store = db if hasattr(db, "lookup_kind") else _View(db)
    p = load(store, quote, ts - int(max_age_min * NS_MIN), ts, max_age_min).at(ts)
    if p is None:
        return None
    with localcontext() as c:
        c.prec = PREC
        near = int(Decimal(r) * p.price)
    return {"near": str(near), "price_ts": p.ts, "pool": p.pool, "tx": p.tx} if near > 0 else None


def convert_swaps(token: str, quote: str, swaps: list[dict], series: Series | None) -> dict[str, Any]:
    """{ok, swaps, total, missing, missing_ts, pools, max_age_s, price_txs}: `swaps` of the launch pool with the quote side in yocto NEAR (the quote token replaced by wNEAR).
    `ok` False when ONE trade has no price (or the swap does not touch `quote` at all): then `swaps` is the input untouched and the caller keeps the launch undetermined.
    A swap with a zero amount (the 0 -> 0 liquidity swap) is not a trade and is passed through."""
    out: list[dict] = []
    total = missing = 0
    missing_ts: list[int] = []
    pools: set[str] = set()
    ages: list[int] = []
    txs: list[str] = []
    for s in swaps:
        if not s["amount_in"] or not s["amount_out"]:
            out.append(s)
            continue
        if s["token_out"] == token:
            side, other = "in", s["token_in"]                # buy: the quote token was paid
        elif s["token_in"] == token:
            side, other = "out", s["token_out"]              # sell: the quote token was received
        else:
            out.append(s)                                    # not a trade of this pool (the attribution skips it too)
            continue
        total += 1
        p = series.at(s["ts"]) if series is not None and other == quote else None          # the other side is not the quote token the pool id names: nothing to convert with
        raw = s["amount_in"] if side == "in" else s["amount_out"]
        near = 0
        if p is not None:
            with localcontext() as c:
                c.prec = PREC
                near = int(Decimal(raw) * p.price)
        if p is None or near <= 0:
            missing += 1
            missing_ts.append(s["ts"])
            continue
        pools.add(p.pool)
        ages.append(s["ts"] - p.ts)
        if len(txs) < 3 and p.tx:
            txs.append(p.tx)
        y = dict(s, quote_raw=raw, quote_price_ts=p.ts)
        if side == "in":
            y["amount_in"], y["token_in"] = near, WRAP
        else:
            y["amount_out"], y["token_out"] = near, WRAP
        out.append(y)
    if missing:
        return {"ok": False, "swaps": swaps, "total": total, "missing": missing, "pools": sorted(pools), "max_age_s": None, "price_txs": [], "missing_ts": missing_ts}
    return {"ok": True, "swaps": out, "total": total, "missing": 0, "pools": sorted(pools),
            "max_age_s": round(max(ages) / 1e9, 1) if ages else None, "price_txs": txs, "missing_ts": []}
