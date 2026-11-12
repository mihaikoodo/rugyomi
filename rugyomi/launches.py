"""Launch detection from stored facts — the same code live (per block) and in rebuild_launches().

A launch is a fact, not a judgement: token, launchpad, creator, real owner, pool, dev buy, when, which tx.

nearly (`launch:launch_started` from nearlytrade.near):
  creator = the event's `creator`. Launches made through launchbot.nearlytrade.near carry launchbot as the
  creator; the REAL OWNER is whoever paid launchbot the launch deposit: the sum `dep` of `near_fc_deposit`
  to nearlytrade.near with method `launch` in the launch tx; the owner is the sender of a `near_transfer` to
  launchbot of dep + 0.02 NEAR (±0.001 NEAR), the newest one not after the launch, within 150 blocks, not
  already used as evidence for another launch. "Not after" inside the launch block = stored before the launch
  event: facts of one block are inserted in parse order (shard, outcome, action / log), so a lower facts.id in
  the same block means an earlier receipt; no extra column is needed (RGY-005). owner_evidence = that transfer's receipt id. No match ->
  owner stays NULL and extra.owner_unresolved = true (launchbot itself is a service account, never an owner).
  Without launchbot, owner = creator. dev_buy = the event's `dev_buy` (yocto NEAR).
justhoot v1 (`launch:meme_launched` from launchpad.justhoot.near, standard `hoot_launcher`; RGY-011): token =
  data.token_id; creator = data.creator_id (else `creator_id` of the token's meta events: launch_requested /
  launch_priced / dev_buy_near_wrapped, the same names as in v2); dev_buy = data.dev_buy_quote (wNEAR base units);
  pool = the DCL pool derived from the data: the two token ids sorted, joined with `|`, fee tier 10000
  (extra.pool_derived = true; seen in the v1 launch of HOOT). v1 launches are found in the block that holds the
  event, 4–6 blocks after the transaction (the event `launch_requested` is a log only in some versions).
justhoot v2 (`launch:launch_planned` from v2-launchpad.justhoot.near):
  token = data.token_id; pool = data.plan.pool_id; dev_buy = data.plan.dev_quote; creator = `creator_id` of
  the token's v2 meta events (launch_requested / launch_priced / dev_buy_near_wrapped). A meta event stored
  after the launch row fills a missing creator (and owner) later.
All lookups read the DB (facts are inserted before detection, in the same transaction), so a restart changes
nothing. Re-detecting the same launch is a no-op (token is the primary key).
Coverage (RGY-011): `launches.coverage` NULL = the launch was seen by the live collector, or in a range the collector
covers completely; 'launch_only' = found in the few blocks around a historical launch (`ingest-blocks`): no snapshots,
no window facts — the snapshot guard (snapshots.window_covered) enforces it independently of the tag.
"""
from __future__ import annotations

import json
from typing import Any

from .parse import JH_V1, JH_V2, LAUNCHBOT, NEARLY
from .store.db import Store, hash_bytes, hash_str, utcnow_iso

LAUNCHBOT_WINDOW = 150            # blocks: the owner's payment to launchbot is at most this far before the launch
LAUNCHBOT_FEE = 2 * 10**22        # 0.02 NEAR
LAUNCHBOT_TOL = 10**21            # ±0.001 NEAR
DEPOSIT_LOOKBACK_NS = 600 * 10**9  # the launch deposit is in the launch tx: look 10 min back at most
V2_META = ("launch:launch_requested", "launch:launch_priced", "launch:dev_buy_near_wrapped")
LAUNCH_EVENTS = ("launch:launch_started", "launch:launch_planned", "launch:meme_launched")
COVERAGE_LAUNCH_ONLY = "launch_only"
JH_V1_FEE_TIER = 10000


def _extra(s: str | None) -> dict[str, Any]:
    return json.loads(s) if s else {}


def _kind_ids(store: Store, names: tuple[str, ...]) -> list[int]:
    return [k for k in (store.lookup_kind(n) for n in names) if k is not None]


def detect_in_block(store: Store, height: int, coverage: str | None = None) -> int:
    """Detect launches among the facts of `height` (already inserted). Returns the number of new launch rows.
    coverage: the tag of NEW rows (None = live / fully covered; COVERAGE_LAUNCH_ONLY for ingested history)."""
    kinds = _kind_ids(store, LAUNCH_EVENTS + V2_META)
    if not kinds:
        return 0
    rows = store.con.execute(
        f"SELECT f.id, k.name, f.height, f.ts_ns, f.tx, f.b, f.extra FROM facts f JOIN kinds k ON k.id = f.kind "
        f"WHERE f.height = ? AND f.kind IN ({','.join('?' * len(kinds))}) ORDER BY f.id", (height, *kinds)).fetchall()
    return _apply(store, rows, coverage)


def rebuild_launches(store: Store) -> int:
    """Recompute the launches table from facts (derived data). Runs in one transaction. The coverage tag of a token
    that existed before is kept; a token that is new is tagged by the rule: below the collector's start height =
    'launch_only', else NULL."""
    store.begin()
    try:
        old = dict(store.con.execute("SELECT token, coverage FROM launches")) if store.user_version() >= 6 else {}
        st = store.get_status().get("start_height")
        store.con.execute("DELETE FROM launches")
        kinds = _kind_ids(store, LAUNCH_EVENTS + V2_META)
        n = 0
        if kinds:
            rows = store.con.execute(
                f"SELECT f.id, k.name, f.height, f.ts_ns, f.tx, f.b, f.extra FROM facts f JOIN kinds k ON k.id = f.kind "
                f"WHERE f.kind IN ({','.join('?' * len(kinds))}) ORDER BY f.height, f.id", kinds).fetchall()
            n = _apply(store, rows, None, old=old, start_height=st if isinstance(st, int) else None)
        store.commit()
        return n
    except BaseException:
        store.rollback()
        raise


def _apply(store: Store, rows: list[tuple], coverage: str | None = None, old: dict[str, str | None] | None = None,
           start_height: int | None = None) -> int:
    n = 0
    for _id, kind, height, ts_ns, tx, _b, extra_s in rows:
        extra = _extra(extra_s)
        d = extra.get("data") or {}
        contract = extra.get("contract")
        cov = coverage
        if old is not None:                  # rebuild: the tag of a known token, else the start-height rule
            tok = d.get("token") or d.get("token_id")
            cov = old[tok] if tok in old else (COVERAGE_LAUNCH_ONLY if start_height and height < start_height else None)
        if kind == "launch:launch_started" and contract == NEARLY:
            n += _nearly(store, d, height, ts_ns, hash_str(tx), _id, cov)
        elif kind == "launch:launch_planned" and contract == JH_V2:
            n += _justhoot(store, d, height, ts_ns, hash_str(tx), cov)
        elif kind == "launch:meme_launched" and contract == JH_V1:
            n += _justhoot_v1(store, d, height, ts_ns, hash_str(tx), cov)
        elif kind in V2_META:
            _meta_fill(store, d)
    return n


def _insert(store: Store, row: dict[str, Any], coverage: str | None = None) -> int:
    has_cov = store.user_version() >= 6              # a database that is not migrated yet has no column
    cols = "token,launchpad,creator,owner,owner_evidence,pool,dev_buy,height,ts_ns,tx,detected_at,extra"
    vals = ":token,:launchpad,:creator,:owner,:owner_evidence,:pool,:dev_buy,:height,:ts_ns,:tx,:detected_at,:extra"
    if has_cov:
        cols, vals = cols + ",coverage", vals + ",:coverage"
    cur = store.con.execute(
        f"INSERT OR IGNORE INTO launches({cols}) VALUES({vals})",
        {**row, "detected_at": utcnow_iso(), "coverage": coverage,
         "extra": json.dumps(row.get("extra") or None, separators=(",", ":")) if row.get("extra") else None})
    return cur.rowcount


def _int_str(v: Any) -> str | None:
    try:
        return str(int(str(v))) if v not in (None, "") else None
    except ValueError:
        return None


def _nearly(store: Store, d: dict, height: int, ts_ns: int, tx: str | None, event_id: int,
            coverage: str | None = None) -> int:
    token = d.get("token")
    if not token:
        return 0
    creator = d.get("creator")
    row = {"token": token, "launchpad": "nearly", "creator": creator, "owner": creator, "owner_evidence": None,
           "pool": d.get("pool_id"), "dev_buy": _int_str(d.get("dev_buy")), "height": height, "ts_ns": ts_ns,
           "tx": tx, "extra": {k: d.get(k) for k in ("symbol", "name", "id") if d.get(k) is not None}}
    if creator == LAUNCHBOT:
        row["owner"] = None
        dep = _launch_deposit(store, tx, ts_ns)
        row["extra"]["launchbot_deposit"] = str(dep)
        match = _launchbot_payment(store, dep, height, event_id) if dep else None
        if match:
            row["owner"], row["owner_evidence"] = match
        else:
            row["extra"]["owner_unresolved"] = True
    return _insert(store, row, coverage)


def tax_options(con: Any, launch: dict[str, Any]) -> dict[str, Any] | None:
    """The nearly tax options of a launch (`launch:tax_opts`: buy_bps, sell_bps, creator_bps, holders_bps, burn_bps,
    platform_bps) or None. The event is emitted in the block of `launch_started` and carries the launch `id`, which
    launches.extra keeps. `con` is a sqlite3 connection or a Store. Single reader: the rules module (`creator_tax`)
    and the site both call it."""
    con = getattr(con, "con", con)
    lid = (launch.get("extra") or {}).get("id")
    if lid is None or launch.get("height") is None:
        return None
    for (extra_s,) in con.execute(
            "SELECT f.extra FROM facts f JOIN kinds k ON k.id = f.kind WHERE k.name = 'launch:tax_opts' AND f.height = ?",
            (launch["height"],)):
        d = _extra(extra_s).get("data") or {}
        if d.get("id") == lid:
            return {k: v for k, v in d.items() if k != "id"}
    return None


JUSTHOOT_PADS = ("justhoot_v1", "justhoot_v2")
PIPS = 1_000_000                       # transfer_fee_pips: 1 000 000 = 100 %


def justhoot_fee(con: Any, launch: dict[str, Any]) -> dict[str, Any] | None:
    """The creator's fee of a justhoot launch from its stored token-creation call (`launch_call`, RGY-045b), in the shape of the nearly tax options:
    {"buy_bps", "sell_bps", "creator_bps", "holders_bps", "burn_bps", "transfer_fee_pips", "source": "justhoot"}, or None when the call was not stored
    (every launch before the collector kept it) or carries no fee (0 pips). `transfer_fee_pips` is a fee on every transfer of the token (a trade moves it): it is
    the same rate on a buy and on a sell; `shares` split it between the token owner (= the creator), the holders and a burn. The platform's own cut is
    NOT in the call (`get_config` does not exist), so `platform_bps` is absent: nobody may state a net rate from it. `buy_bps` / `sell_bps` are given only
    when the rate is a whole number of bps. `con` is a sqlite3 connection or a Store."""
    con = getattr(con, "con", con)
    tx = launch.get("tx")
    if launch.get("launchpad") not in JUSTHOOT_PADS or not tx:
        return None
    for (extra_s,) in con.execute(
            "SELECT f.extra FROM facts f JOIN kinds k ON k.id = f.kind WHERE k.name = 'launch_call' AND f.tx = ? ORDER BY f.id", (hash_bytes(tx),)):
        args = _extra(extra_s).get("args")
        if not isinstance(args, dict):
            continue
        a = args.get("args") if isinstance(args.get("args"), dict) else args
        pips, shares = a.get("transfer_fee_pips"), a.get("shares")
        if not (isinstance(pips, int) and not isinstance(pips, bool) and 0 < pips <= PIPS and isinstance(shares, dict)):
            continue                                   # 0 pips = no fee at all: nothing to read, whoever the shares go to
        sh = {k: shares.get(k) for k in ("token_owner_bps", "holders_bps", "burn_bps")}
        if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10000 for v in sh.values()):
            continue
        out: dict[str, Any] = {"transfer_fee_pips": pips, "creator_bps": sh["token_owner_bps"], "holders_bps": sh["holders_bps"], "burn_bps": sh["burn_bps"],
                               "source": "justhoot"}
        if pips % 100 == 0:
            out["buy_bps"] = out["sell_bps"] = pips // 100
        return out
    return None


def _launch_deposit(store: Store, tx: str | None, ts_ns: int) -> int:
    nearly, kind = store.lookup_account(NEARLY), store.lookup_kind("near_fc_deposit")
    if tx is None or nearly is None or kind is None:
        return 0
    total = 0
    for amount, extra in store.con.execute(
            "SELECT amount, extra FROM facts WHERE b = ? AND ts_ns BETWEEN ? AND ? AND kind = ? AND tx = ?",
            (nearly, ts_ns - DEPOSIT_LOOKBACK_NS, ts_ns, kind, hash_bytes(tx))):
        if _extra(extra).get("method") == "launch":
            total += int(amount or 0)
    return total


def _launchbot_payment(store: Store, dep: int, height: int, event_id: int) -> tuple[str, str] | None:
    lb, kind = store.lookup_account(LAUNCHBOT), store.lookup_kind("near_transfer")
    if lb is None or kind is None:
        return None
    want = dep + LAUNCHBOT_FEE
    used = {r[0] for r in store.con.execute("SELECT owner_evidence FROM launches WHERE owner_evidence IS NOT NULL")}
    for receipt, a, amount in store.con.execute(
            "SELECT f.receipt, f.a, f.amount FROM facts f WHERE f.b = ? AND f.kind = ? AND f.height BETWEEN ? AND ? "
            "AND (f.height < ? OR f.id < ?) ORDER BY f.height DESC, f.id DESC",
            (lb, kind, height - LAUNCHBOT_WINDOW, height, height, event_id)):
        rid = hash_str(receipt)
        if rid in used:
            continue
        if abs(int(amount or 0) - want) <= LAUNCHBOT_TOL:
            return store.account_name(a), rid
    return None


def _v2_creator(store: Store, token: str) -> str | None:
    tok = store.lookup_account(token)
    kinds = _kind_ids(store, V2_META)
    if tok is None or not kinds:
        return None
    for (extra,) in store.con.execute(
            f"SELECT extra FROM facts WHERE b = ? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id", (tok, *kinds)):
        c = (_extra(extra).get("data") or {}).get("creator_id")
        if c:
            return c
    return None


def _justhoot(store: Store, d: dict, height: int, ts_ns: int, tx: str | None, coverage: str | None = None) -> int:
    token = d.get("token_id")
    if not token:
        return 0
    plan = d.get("plan") if isinstance(d.get("plan"), dict) else {}
    creator = _v2_creator(store, token)
    return _insert(store, {"token": token, "launchpad": "justhoot_v2", "creator": creator, "owner": creator,
                           "owner_evidence": None, "pool": plan.get("pool_id"), "dev_buy": _int_str(plan.get("dev_quote")),
                           "height": height, "ts_ns": ts_ns, "tx": tx, "extra": None}, coverage)


def _justhoot_v1(store: Store, d: dict, height: int, ts_ns: int, tx: str | None, coverage: str | None = None) -> int:
    token = d.get("token_id")
    if not token:
        return 0
    creator = d.get("creator_id") or _v2_creator(store, token)
    quote = d.get("quote_token_id") or "wrap.near"
    pool = "|".join(sorted([token, quote])) + f"|{JH_V1_FEE_TIER}"
    return _insert(store, {"token": token, "launchpad": "justhoot_v1", "creator": creator, "owner": creator,
                           "owner_evidence": None, "pool": pool, "dev_buy": _int_str(d.get("dev_buy_quote")),
                           "height": height, "ts_ns": ts_ns, "tx": tx, "extra": {"pool_derived": True}}, coverage)


def _meta_fill(store: Store, d: dict) -> None:
    """A meta event (launch_requested / launch_priced / dev_buy_near_wrapped) stored after the launch row fills a
    missing creator (and owner) — justhoot v1 and v2."""
    token, creator = d.get("token_id") or d.get("token"), d.get("creator_id")
    if token and creator:
        store.con.execute("UPDATE launches SET creator = ?, owner = COALESCE(owner, ?) "
                          "WHERE token = ? AND launchpad IN ('justhoot_v1', 'justhoot_v2') AND creator IS NULL",
                          (creator, creator, token))


def resolve_late_owners(store: Store, heights: list[int]) -> list[str]:
    """After heights were processed late (`reprocess-quarantine`, RGY-012): launchbot launches up to
    LAUNCHBOT_WINDOW blocks after them whose owner was not found may find it now (the deposit or the payment to
    launchbot was in a late height). Same rule as live detection; one transaction. Returns the resolved tokens."""
    if not heights:
        return []
    started = store.lookup_kind("launch:launch_started")
    if started is None:
        return []
    cands: dict[str, tuple] = {}
    for h in heights:
        for row in store.con.execute(
                "SELECT token, height, ts_ns, tx, extra FROM launches WHERE launchpad = 'nearly' AND creator = ? "
                "AND owner IS NULL AND height BETWEEN ? AND ?", (LAUNCHBOT, h, h + LAUNCHBOT_WINDOW)):
            cands[row[0]] = row
    out = []
    store.begin()
    try:
        for token, height, ts_ns, tx, extra_s in cands.values():
            event_id = None
            for fid, fx in store.con.execute("SELECT id, extra FROM facts WHERE height = ? AND kind = ? ORDER BY id",
                                             (height, started)):
                if ((_extra(fx).get("data") or {}).get("token")) == token:
                    event_id = fid
                    break
            if event_id is None:
                continue
            extra = _extra(extra_s)
            dep = int(extra.get("launchbot_deposit") or 0) or _launch_deposit(store, tx, ts_ns)
            match = _launchbot_payment(store, dep, height, event_id) if dep else None
            if not match:
                continue
            extra["launchbot_deposit"] = str(dep)
            extra.pop("owner_unresolved", None)
            extra["owner_resolved_late"] = utcnow_iso()
            store.con.execute("UPDATE launches SET owner = ?, owner_evidence = ?, extra = ? WHERE token = ?",
                              (match[0], match[1], json.dumps(extra, separators=(",", ":")), token))
            out.append(token)
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return out
