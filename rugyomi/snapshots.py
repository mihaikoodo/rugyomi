"""Launch snapshots at T+5/15/30/60 min: continuous metrics, no labels (single source; labels and thresholds
live in the rules module). Derived data: computed only from `facts` + `launches`, recomputable at any time.

Time: a snapshot for minute N uses facts with ts_ns <= launch ts + N min (block time, UTC). The collector computes
it while processing the first block whose time is >= that cutoff, so live mode, catch-up and `rebuild-snapshots`
give the same numbers.

Trades are DCL swaps on the launch pool (`dcl_swap`, amounts in base units). The swapper (`dcl_swap.a`) is NOT the
beneficiary — a nearly dev buy is swapped by lock_N.nearlytrade.near — so trades are attributed by the token legs
(NEP-141 `ft_transfer` of the token) in the same transaction. Matching rule, per transaction, in fact order:
  buy  (token_out = T): the leg dclv2 -> X (X not the token contract) whose amount, plus a tax leg dclv2 -> token
       contract, equals the swap's amount_out (or equals it alone, no tax). If no exact pair: the transaction's
       only unused recipient leg, when the transaction has exactly one buy. X gets the NET tokens (the leg).
  sell (token_in = T):  the leg X -> dclv2 whose amount equals amount_in (else the only unused one, when the
       transaction has one sell); the seller's tax leg X -> token contract in the same tx is added: gross sold.
  A swap with a zero amount (the 0 -> 0 swap while a nearly launch adds liquidity) is not a trade. A swap whose
  legs are not found (e.g. the leg lands in a later block after the cutoff) is counted in unattributed_swaps.
Dev = the launch owner (real owner behind launchbot) and the creator, launchbot itself excluded.
  dev_acquired        net tokens of buys attributed to the dev + mints to the dev in the launch transaction
                      (justhoot v2 mints the dev buy). Tax / fee inflows are never purchases: transfers from the
                      token contract, LP-fee withdrawals from dclv2 (no swap in the tx) are not counted.
  dev_sold            gross tokens the dev sold (pool leg + tax leg); dev_sold_share_bps = dev_sold / dev_acquired.
  dev_transferred_out other outgoing transfers of the token by the dev (not the legs of his sells).
Buyers = accounts with at least one attributed buy, other than the dev and service accounts (`SERVICE`: dclv2,
the token contract, lock_N, launchbot, the launchpad contracts, wrap.near, nearly's fee seller). spent = NEAR of their buys, received = NEAR of
their sells, unsold = bought - sold (per buyer, >= 0) valued at price_last; pnl = received + unsold_value - spent.
Prices are NEAR base units per token base unit (decimal strings; decimals are not needed for multiples or PnL):
  price of a buy = amount_in / amount_out, of a sell = amount_out / amount_in. price_last = the last trade;
  price_first_buyer = the first buy by a non-dev buyer; max_x = max trade price from that buy on / price_first_buyer.
Holders: balances rebuilt from ft_mint / ft_transfer / ft_burn of the token, service accounts excluded;
  top10_share_bps = the 10 largest balances / all included balances.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal, localcontext
from typing import Any

from .parse import DCL, JH_V1, JH_V2, JH_V2_POOL, LAUNCHBOT, NEARLY, WRAP
from .store.db import Store, hash_str, utcnow_iso

MINUTES = (5, 15, 30, 60)
METRIC_VERSION = 1
NS_MIN = 60 * 10**9
_LOCKER = re.compile(r"^lock_\d+\.nearlytrade\.near$")
# Explicit list (never a name pattern, rule 8). feeswap.near: nearly's fee seller — receives the collected tax tokens
# from nearlytrade.near and sells them into the pool (seen 29.10, ISLAND tx 2VtzMKvq / 52TZkvYA): not a holder.
SERVICE = frozenset({DCL, LAUNCHBOT, NEARLY, JH_V1, JH_V2, JH_V2_POOL, WRAP, "feeswap.near", ""})
COLUMNS = ("n_swaps", "unattributed_swaps", "price_last", "price_first_buyer", "max_x", "dev_acquired", "dev_sold",
           "dev_sold_share_bps", "dev_transferred_out", "dev_first_sell_after_s", "buyers_count", "buyers_spent",
           "buyers_received", "buyers_unsold_value", "buyers_pnl", "holders_count", "top10_share_bps")


def is_service(acc: str | None, token: str) -> bool:
    return acc is None or acc == token or acc in SERVICE or bool(_LOCKER.match(acc))


def _dec(x: Decimal | None) -> str | None:
    if x is None:
        return None
    with localcontext() as c:
        c.prec = 30
        return format(+x, "f")


def _price(num: int, den: int) -> Decimal:
    with localcontext() as c:
        c.prec = 50
        return Decimal(num) / Decimal(den)


# ---------------------------------------------------------------- reading

def _launch(store: Store, token: str) -> dict | None:
    r = store.con.execute("SELECT token, creator, owner, pool, ts_ns, tx FROM launches WHERE token = ?", (token,)).fetchone()
    return dict(zip(("token", "creator", "owner", "pool", "ts_ns", "tx"), r)) if r else None


def _rows(store: Store, launch: dict, cutoff: int) -> tuple[list[dict], list[dict]]:
    """(token legs, swaps) up to the cutoff, in fact order."""
    tok_id = store.lookup_account(launch["token"])
    legs: list[dict] = []
    if tok_id is not None:
        for id_, kind, ts, tx, a, b, amount in store.con.execute(
                "SELECT f.id, k.name, f.ts_ns, f.tx, aa.name, bb.name, f.amount FROM facts f "
                "JOIN kinds k ON k.id = f.kind LEFT JOIN accounts aa ON aa.id = f.a LEFT JOIN accounts bb ON bb.id = f.b "
                "WHERE f.token = ? AND f.ts_ns <= ? ORDER BY f.id", (tok_id, cutoff)):
            if kind in ("ft_transfer", "ft_mint", "ft_burn"):
                legs.append({"id": id_, "kind": kind, "ts": ts, "tx": hash_str(tx), "a": a, "b": b,
                             "amount": int(amount or 0)})
    swaps: list[dict] = []
    pool_id = store.lookup_account(launch["pool"]) if launch.get("pool") else None
    kind_id = store.lookup_kind("dcl_swap")
    if pool_id is not None and kind_id is not None:
        for id_, ts, tx, a, extra in store.con.execute(
                "SELECT f.id, f.ts_ns, f.tx, aa.name, f.extra FROM facts f LEFT JOIN accounts aa ON aa.id = f.a "
                "WHERE f.kind = ? AND f.b = ? AND f.ts_ns <= ? ORDER BY f.id", (kind_id, pool_id, cutoff)):
            e = json.loads(extra) if extra else {}
            swaps.append({"id": id_, "ts": ts, "tx": hash_str(tx), "swapper": a, "token_in": e.get("token_in"),
                          "token_out": e.get("token_out"), "amount_in": int(e.get("amount_in") or 0),
                          "amount_out": int(e.get("amount_out") or 0)})
    return legs, swaps


# ---------------------------------------------------------------- attribution

def attribute(token: str, legs: list[dict], swaps: list[dict]) -> list[dict]:
    """Trades: [{id, ts, tx, side buy|sell, who, tokens (net for buys, gross for sells), near, price, legs}];
    swaps without legs get who=None. Uses each leg at most once. See the module docstring for the rule."""
    by_tx: dict[str | None, list[dict]] = defaultdict(list)
    for leg in legs:
        if leg["kind"] == "ft_transfer":
            by_tx[leg["tx"]].append(leg)
    used: set[int] = set()
    trades: list[dict] = []
    swaps_by_tx: dict[str | None, list[dict]] = defaultdict(list)
    for s in swaps:
        swaps_by_tx[s["tx"]].append(s)
    for s in swaps:
        if not s["amount_in"] or not s["amount_out"]:
            continue                                         # 0 -> 0 (nearly liquidity add): not a trade
        if s["token_out"] == token:
            side = "buy"
        elif s["token_in"] == token:
            side = "sell"
        else:
            continue
        tx_legs = [x for x in by_tx.get(s["tx"], []) if x["id"] not in used]
        same_side = [x for x in swaps_by_tx[s["tx"]] if x["amount_in"] and x["amount_out"]
                     and (x["token_out"] == token) == (side == "buy")]
        t = {"id": s["id"], "ts": s["ts"], "tx": s["tx"], "side": side, "who": None, "tokens": 0, "near": 0,
             "price": None, "legs": []}
        if side == "buy":
            t["near"], gross = s["amount_in"], s["amount_out"]
            t["price"] = _price(s["amount_in"], s["amount_out"])
            recv = [x for x in tx_legs if x["a"] == DCL and x["b"] not in (token, DCL)]
            tax = [x for x in tx_legs if x["a"] == DCL and x["b"] == token]
            pick = next(([r, tx_] for r in recv for tx_ in tax if r["amount"] + tx_["amount"] == gross), None) \
                or next(([r] for r in recv if r["amount"] == gross), None)
            if pick is None and len(recv) == 1 and len(same_side) == 1:
                pick = recv[:1] + tax[:1]
            if pick:
                t["who"], t["tokens"], t["legs"] = pick[0]["b"], pick[0]["amount"], [x["id"] for x in pick]
        else:
            t["near"], net = s["amount_out"], s["amount_in"]
            t["price"] = _price(s["amount_out"], s["amount_in"])
            ins = [x for x in tx_legs if x["b"] == DCL and x["a"] not in (token, DCL)]
            pick_in = next((x for x in ins if x["amount"] == net), None)
            if pick_in is None and len(ins) == 1 and len(same_side) == 1:
                pick_in = ins[0]
            if pick_in:
                tax = next((x for x in tx_legs if x["a"] == pick_in["a"] and x["b"] == token), None)
                t["who"] = pick_in["a"]
                t["tokens"] = pick_in["amount"] + (tax["amount"] if tax else 0)
                t["legs"] = [pick_in["id"]] + ([tax["id"]] if tax else [])
        used.update(t["legs"])
        trades.append(t)
    return trades


# ---------------------------------------------------------------- metrics

def compute(store: Store, token: str, minute: int) -> dict[str, Any]:
    launch = _launch(store, token)
    if launch is None:
        raise KeyError(f"no launch row for {token}")
    cutoff = launch["ts_ns"] + minute * NS_MIN
    legs, swaps = _rows(store, launch, cutoff)
    return metrics(launch, legs, swaps, cutoff)


def metrics(launch: dict, legs: list[dict], swaps: list[dict], cutoff: int) -> dict[str, Any]:
    token = launch["token"]
    dev = {x for x in (launch.get("owner"), launch.get("creator")) if x and x != LAUNCHBOT}
    trades = attribute(token, legs, swaps)
    trade_leg_ids = {i for t in trades for i in t["legs"]}

    dev_acq = dev_sold = dev_out = 0
    first_sell_ts = None
    for t in trades:
        if t["who"] in dev:
            if t["side"] == "buy":
                dev_acq += t["tokens"]
            else:
                dev_sold += t["tokens"]
                first_sell_ts = t["ts"] if first_sell_ts is None else first_sell_ts
    for leg in legs:
        if leg["kind"] == "ft_mint" and leg["a"] in dev and leg["tx"] == launch.get("tx"):
            dev_acq += leg["amount"]
        elif leg["kind"] == "ft_transfer" and leg["a"] in dev and leg["id"] not in trade_leg_ids:
            dev_out += leg["amount"]

    buyers: dict[str, dict[str, int]] = {}
    for t in trades:
        if t["side"] == "buy" and t["who"] and t["who"] not in dev and not is_service(t["who"], token):
            b = buyers.setdefault(t["who"], {"spent": 0, "bought": 0, "received": 0, "sold": 0})
            b["spent"] += t["near"]
            b["bought"] += t["tokens"]
    for t in trades:
        if t["side"] == "sell" and t["who"] in buyers:
            buyers[t["who"]]["received"] += t["near"]
            buyers[t["who"]]["sold"] += t["tokens"]

    priced = [t for t in trades if t["price"] is not None]
    price_last = priced[-1]["price"] if priced else None
    first_buy = next((t for t in trades if t["side"] == "buy" and t["who"] in buyers), None)
    max_x = None
    if first_buy is not None and first_buy["price"]:
        top = max(t["price"] for t in priced if t["id"] >= first_buy["id"])
        max_x = round(float(top / first_buy["price"]), 6)

    spent = sum(b["spent"] for b in buyers.values())
    received = sum(b["received"] for b in buyers.values())
    unsold = sum(max(0, b["bought"] - b["sold"]) for b in buyers.values())
    unsold_value = int(unsold * price_last) if price_last is not None else 0

    bal: dict[str, int] = defaultdict(int)
    for leg in legs:
        if leg["kind"] == "ft_mint":
            bal[leg["a"]] += leg["amount"]
        elif leg["kind"] == "ft_burn":
            bal[leg["a"]] -= leg["amount"]
        else:
            bal[leg["a"]] -= leg["amount"]
            bal[leg["b"]] += leg["amount"]
    held = sorted((v for k, v in bal.items() if v > 0 and not is_service(k, token)), reverse=True)
    total = sum(held)

    return {
        "at_ts_ns": cutoff,
        "n_swaps": sum(1 for s in swaps if s["amount_in"] and s["amount_out"]),
        "unattributed_swaps": sum(1 for t in trades if t["who"] is None),
        "price_last": _dec(price_last),
        "price_first_buyer": _dec(first_buy["price"]) if first_buy else None,
        "max_x": max_x,
        "dev_acquired": str(dev_acq),
        "dev_sold": str(dev_sold),
        "dev_sold_share_bps": dev_sold * 10_000 // dev_acq if dev_acq else None,
        "dev_transferred_out": str(dev_out),
        "dev_first_sell_after_s": round((first_sell_ts - launch["ts_ns"]) / 1e9, 3) if first_sell_ts else None,
        "buyers_count": len(buyers),
        "buyers_spent": str(spent),
        "buyers_received": str(received),
        "buyers_unsold_value": str(unsold_value),
        "buyers_pnl": str(received + unsold_value - spent),
        "holders_count": len(held),
        "top10_share_bps": sum(held[:10]) * 10_000 // total if total else None,
    }


# ---------------------------------------------------------------- writing

def _write(store: Store, token: str, minute: int, launch_ts: int, replace: bool) -> str:
    """Compute and store one snapshot inside the caller's transaction. A failure is stored as the row's error
    (visible, retried by rebuild) and never breaks the caller's transaction. Returns 'ok' or 'error'."""
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    cols = ("token", "minute", "at_ts_ns") + COLUMNS + ("metric_version", "computed_at", "error")
    sql = f"{verb} INTO launch_snapshots({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
    store.con.execute("SAVEPOINT snap")
    try:
        m = compute(store, token, minute)
        store.con.execute(sql, (token, minute, m["at_ts_ns"], *(m[c] for c in COLUMNS), METRIC_VERSION,
                                utcnow_iso(), None))
        store.con.execute("RELEASE snap")
        return "ok"
    except Exception as e:  # noqa: BLE001 — stored on the row, shown by status
        store.con.execute("ROLLBACK TO snap")
        store.con.execute("RELEASE snap")
        store.con.execute(sql, (token, minute, launch_ts + minute * NS_MIN, *([None] * len(COLUMNS)),
                                METRIC_VERSION, utcnow_iso(), f"{type(e).__name__}: {e}"[:500]))
        return "error"


def window_covered(store: Store, launch_height: int, cutoff_ns: int) -> bool:
    """Coverage guard (RGY-012; the same rule RGY-011 needs for history): a snapshot is computed only when every
    height of its window — from the launch height up to the first ok block at or after the cutoff — is stored as
    ok / null. A quarantined, failing or missing height inside the window -> not covered: no row until it is
    processed; a later block's compute_due then fills it (once)."""
    end = store.con.execute("SELECT height FROM blocks WHERE height >= ? AND status = 'ok' AND block_ts_ns >= ? "
                            "ORDER BY height LIMIT 1", (launch_height, cutoff_ns)).fetchone()
    return end is not None and store.covered(launch_height, int(end[0]))


def _full_coverage(store: Store, alias: str = "") -> str:
    """SQL condition: the launch is not tagged `launch_only` (history launches never get snapshots; RGY-011).
    Before schema v6 there is no tag."""
    return f" AND {alias}coverage IS NULL" if store.user_version() >= 6 else ""


def compute_due(store: Store, block_ts_ns: int) -> int:
    """Inside the collector's block transaction: every (launch, minute) whose cutoff <= this block's time, that
    has no row yet, is not a `launch_only` launch and whose window is covered (window_covered). Returns the number
    of rows written."""
    n = 0
    for m in MINUTES:
        for token, ts, height in store.con.execute(
                "SELECT l.token, l.ts_ns, l.height FROM launches l WHERE l.ts_ns <= ? AND NOT EXISTS "
                "(SELECT 1 FROM launch_snapshots s WHERE s.token = l.token AND s.minute = ?)"
                + _full_coverage(store, "l.") + " ORDER BY l.ts_ns",
                (block_ts_ns - m * NS_MIN, m)).fetchall():
            if not window_covered(store, height, ts + m * NS_MIN):
                continue
            _write(store, token, m, ts, replace=False)
            n += 1
    return n


def launches_covering(store: Store, ts_ns: int) -> list[str]:
    """Launches whose snapshot windows contain this time (a block recovered later changes their snapshots)."""
    return [t for (t,) in store.con.execute("SELECT token FROM launches WHERE ts_ns <= ? AND ts_ns + ? >= ?" + _full_coverage(store),
                                            (ts_ns, max(MINUTES) * NS_MIN, ts_ns))]


def rebuild_snapshots(store: Store, token: str | None = None, since: int | None = None) -> dict[str, int]:
    """Recompute snapshots (INSERT OR REPLACE) of the launches selected by token / since-height, for every minute
    whose cutoff is already reached by processed blocks (else not_due) and whose window is covered (else
    not_covered: a quarantined / missing height inside — the row is left as it is). `launch_only` launches are not
    selected at all. One transaction per launch."""
    covered = store.con.execute("SELECT MAX(block_ts_ns) FROM blocks WHERE status = 'ok'").fetchone()[0] or 0
    q, args = "SELECT token, ts_ns, height FROM launches WHERE 1 = 1" + _full_coverage(store), []
    if token:
        q, args = q + " AND token = ?", [token]
    elif since is not None:
        q, args = q + " AND height >= ?", [since]
    out = {"launches": 0, "ok": 0, "error": 0, "not_due": 0, "not_covered": 0}
    for tok, ts, height in store.con.execute(q + " ORDER BY ts_ns", args).fetchall():
        out["launches"] += 1
        store.begin()
        try:
            for m in MINUTES:
                if ts + m * NS_MIN > covered:
                    out["not_due"] += 1
                    continue
                if not window_covered(store, height, ts + m * NS_MIN):
                    out["not_covered"] += 1
                    continue
                out[_write(store, tok, m, ts, replace=True)] += 1
            store.commit()
        except BaseException:
            store.rollback()
            raise
    return out
