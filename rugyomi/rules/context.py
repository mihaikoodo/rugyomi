"""The "as of the launch" view of the database that signals read (RGY-015). **No lookahead is enforced here, not in the
signals**: every method returns only what was knowable at the launch time `asof_ns` (the block time of the launch event):
  - facts with ts_ns <= asof_ns, and in the launch block itself only those that execute before the launch event
    (`facts.id` order = execution order within a block) or belong to the launch transaction (RGY-017: a transfer to the
    dev later in the same block is not pre-launch knowledge); facts of later blocks never;
  - other launches strictly before the launch;
  - snapshots whose cutoff (`at_ts_ns`, launch time + N min) is <= asof_ns — a snapshot of an earlier launch that was
    computed in the database after asof_ns but whose cutoff lies before it is knowable and is returned; one whose
    cutoff lies after it is not;
  - account creation only when it happened <= asof_ns;
  - (RGY-021, profile v1) outcomes of earlier launches: a label is computed ONLY from snapshots whose cutoff is <= asof_ns
    (`rug_fast` is therefore known from the earlier launch + 60 min, `rug_late` / `rug` from + 24 h); links: only edges with a
    transaction time <= asof_ns; fundings: only transfers with a time <= asof_ns.
Signals receive a LaunchContext and nothing else (tests check that `signals.py` never touches the database itself).
Everything read here is also available to a later replay, so `score --token` on an old launch gives the card that was
issued live.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from .. import accounts, history
from .. import launches as L
from .. import quote_price as QP
from ..quote import not_near_quote
from ..snapshots import SERVICE, is_service
from ..store import Store
from ..store.db import hash_str
from .rug import rug_labels

if TYPE_CHECKING:
    from .passcache import PassCache

NS_H = 3600 * 10**9
NS_MIN = 60 * 10**9
NS_DAY = 24 * NS_H
FUNDING_KINDS = ("near_transfer", "wnear_transfer")        # NEAR and wNEAR moving between accounts
_SLUG_SUFFIX = re.compile(r"-\d+$")


def near(yocto: int | str | None) -> float:
    return int(yocto or 0) / 10**24


@lru_cache(maxsize=300_000)
def name_keys(token: str, symbol: str | None = None, name: str | None = None) -> frozenset[str]:
    """Comparable names of a launch: the first label of the token id without a `-N` collision suffix (nearly makes
    `panda-2` when `panda` is taken), the symbol and the name, lower-cased. Keys shorter than 3 characters are dropped.
    Read-only (a frozenset) and cached: a pass compares the same launches with thousands of others (RGY-050)."""
    keys = {_SLUG_SUFFIX.sub("", token.split(".")[0].lower())}
    for v in (symbol, name):
        if v and v.strip():
            keys.add(v.strip().lower())
    return frozenset(k for k in keys if len(k) >= 3)


class LaunchContext:
    def __init__(self, store: Store, launch: dict[str, Any], config: dict[str, Any], cache: "PassCache | None" = None, attach: bool = False) -> None:
        self._store = store
        self._attach = attach                   # RGY-054: the live card path (see `group`); a pass and a replay read the link tables as they are
        self._cache = cache                     # RGY-050: a pass-level cache of the big reads (None = read the database every time, the live path)
        self._uv = store.user_version()         # the schema version does not change while a launch is measured: one PRAGMA, not one per helper
        self.launch = launch
        self.config = config
        self.token: str = launch["token"]
        self.asof_ns: int = launch["ts_ns"]
        self.dev: str | None = launch.get("owner") or (launch.get("creator") if launch.get("creator") != L.LAUNCHBOT else None)
        self.dev_buy: int = int(launch.get("dev_buy") or 0)
        self.dev_buy_quote: str | None = not_near_quote(launch["token"], launch.get("pool")) if self.dev_buy else None     # RGY-074: the pool's other token, when it is not wNEAR
        self.extra: dict[str, Any] = launch.get("extra") or {}
        self.notes: list[str] = []
        self._memo: dict[Any, Any] = {}

    @property
    def dev_buy_near(self) -> float | None:
        """RGY-074: the dev buy in NEAR. A pool paired with wNEAR (and a zero dev buy): the stored amount as before. A pool paired with another token: `launches.dev_buy` is raw units
        of THAT token, so it is converted at the launch time by the price rule of the outcomes (`quote_price.near_at`: the last quote / wNEAR swap, at most MAX_AGE_MIN old);
        None = no provable price - the amount in NEAR is unknown and is never read as raw units."""
        if "dev_buy_near" not in self._memo:
            if self.dev_buy_quote is None:
                v: float | None = int(self.dev_buy or 0) / 10**24
            else:
                r = QP.near_at(self._store, self.dev_buy_quote, self.dev_buy, self.asof_ns)
                v = int(r["near"]) / 10**24 if r else None
            self._memo["dev_buy_near"] = v
        return self._memo["dev_buy_near"]

    @property
    def read_backfill(self) -> bool:
        """Profile v1: snapshots computed by the history backfill count like live ones (Q-010 answered, RGY-021)."""
        return bool((self.config.get("context") or {}).get("read_backfill"))

    def _launch_bound(self) -> tuple[int, int, bytes] | None:
        """(launch height, id of the launch event's fact, its transaction) — or None when the launch has no event fact in
        the database (hand-made rows in tests), in which case only the block time bounds the facts."""
        if not hasattr(self, "_bound"):
            self._bound = None
            kinds = [k for k in (self._store.lookup_kind(n) for n in L.LAUNCH_EVENTS) if k is not None]
            if kinds and self.launch.get("height") is not None:
                for fid, tx in self._store.con.execute(
                        f"SELECT id, tx FROM facts WHERE height = ? AND kind IN ({','.join('?' * len(kinds))}) ORDER BY id",
                        (self.launch["height"], *kinds)):
                    if tx is not None and hash_str(tx) == self.launch.get("tx"):
                        self._bound = (self.launch["height"], fid, tx)
                        break
        return self._bound

    # ------------------------------------------------------------ accounts
    def created(self, account: str) -> dict[str, Any] | None:
        """When / how the account was created, only if that happened at or before the launch."""
        c = accounts.account_created(self._store, account)
        return c if c and c["ts_ns"] is not None and c["ts_ns"] <= self.asof_ns else None

    def inflows(self, account: str, window_ns: int, min_near: float = 0.0) -> list[dict[str, Any]]:
        """NEAR / wNEAR transfers INTO `account` in [asof - window, asof] from accounts that are not service accounts
        (DCL, launchpads, wrap.near, launch bot...) and not the account itself; newest first. A row: ts_ns, sender,
        near, tx, kind."""
        con = self._store.con
        b = self._store.lookup_account(account)
        kinds = [k for k in (self._store.lookup_kind(n) for n in FUNDING_KINDS) if k is not None]
        if b is None or not kinds:
            return []
        out = []
        bound, extra = self._launch_bound(), []
        if bound:                                             # the launch block: before the launch event, or its own tx
            h, fid, ltx = bound
            extra = [h, h, fid, ltx]
        for ts, sender, amount, tx, kind in con.execute(
                f"SELECT f.ts_ns, a.name, f.amount, f.tx, k.name FROM facts f JOIN kinds k ON k.id = f.kind "
                f"JOIN accounts a ON a.id = f.a WHERE f.b = ? AND f.kind IN ({','.join('?' * len(kinds))}) "
                f"AND f.ts_ns BETWEEN ? AND ? "
                + ("AND (f.height < ? OR (f.height = ? AND (f.id <= ? OR f.tx = ?))) " if bound else "")
                + "ORDER BY f.ts_ns DESC, f.id DESC",
                (b, *kinds, self.asof_ns - window_ns, self.asof_ns, *extra)):
            if sender == account or is_service(sender, self.token) or sender in SERVICE:
                continue
            n = near(amount)
            if n >= max(min_near, 1e-9):
                out.append({"ts_ns": ts, "sender": sender, "near": n, "tx": hash_str(tx), "kind": kind})
        return out

    # ------------------------------------------------------------ launches
    def prior_launches(self, account: str, window_ns: int | None = None) -> list[dict[str, Any]]:
        """Other launches of `account` (creator or owner, every source) strictly before this one, newest first;
        within `window_ns` of it when given."""
        lo = self.asof_ns - window_ns if window_ns is not None else None
        rows = [r for r in (self._cache.creator_launches(account) if self._cache else history.creator_launches(self._store, account))
                if r["token"] != self.token and r["ts_ns"] is not None and r["ts_ns"] < self.asof_ns
                and (lo is None or r["ts_ns"] >= lo)]
        return sorted(rows, key=lambda r: (r["ts_ns"], r["token"]), reverse=True)

    def launches_window(self, window_ns: int) -> list[dict[str, Any]]:
        """Every other launch (any creator) in [asof - window, asof), newest first."""
        lo, hi = self.asof_ns - window_ns, self.asof_ns
        rows = self._cache.launches_between(lo, hi) if self._cache else history.launches_between(self._store, lo, hi)
        return [r for r in reversed(rows) if r["token"] != self.token]

    def snapshot(self, token: str, minute: int) -> dict[str, Any] | None:
        """The stored snapshot of an EARLIER launch, only if its cutoff was already reached at the launch time and it
        has no error."""
        # RGY-020: snapshots computed by the history backfill (source 'backfill') are NOT read by profile v0 (Q-010): the scores
        # of live cards must not change because history was added. Profile v1 reads them (config context.read_backfill).
        live = " AND source IS NULL" if self._uv >= 11 and not self.read_backfill else ""
        r = self._store.con.execute(
            "SELECT minute, at_ts_ns, dev_sold_share_bps, dev_acquired, buyers_pnl FROM launch_snapshots "
            "WHERE token = ? AND minute = ? AND error IS NULL AND at_ts_ns <= ?" + live,
            (token, minute, self.asof_ns)).fetchone()
        if not r:
            return None
        return dict(zip(("minute", "at_ts_ns", "dev_sold_share_bps", "dev_acquired", "buyers_pnl"), r))

    # ------------------------------------------------------------ outcomes of earlier launches (RGY-021)
    def outcome(self, token: str) -> dict[str, Any]:
        """What was known at the launch time about the rug label of an EARLIER launch: {"rug": True | False | None, "fast",
        "late", "known_ns"}. Computed from the snapshots whose cutoff is <= asof_ns only, with the rug label code of the
        config in force. None = not decided yet at that time (or no usable snapshot)."""
        key = ("outcome", token)
        if key in self._memo:
            return self._memo[key]
        live_only = self._uv >= 11 and not self.read_backfill
        if self._cache is not None:                                  # RGY-050: the same snapshots and the same labels, read once per pass
            out = self._memo[key] = self._cache.outcome(token, self.asof_ns, live_only, self.config)
            return out
        from .stored import SNAP_COLS
        live = " AND source IS NULL" if live_only else ""
        snaps = [dict(zip(SNAP_COLS, r)) for r in self._store.con.execute(
            f"SELECT {','.join(SNAP_COLS)} FROM launch_snapshots WHERE token = ? AND minute IN (?, ?) AND at_ts_ns <= ?{live} "
            f"ORDER BY minute", (token, self.config["rug"]["fast_minute"], self.config["rug"]["late_minute"], self.asof_ns))]
        if not snaps:
            out = {"rug": None, "fast": None, "late": None, "known_ns": None}
        else:
            lab = rug_labels(snaps, self.config)
            out = {"rug": lab["rug"]["value"], "fast": lab["rug_fast"]["value"], "late": lab["rug_late"]["value"],
                   "known_ns": max(s["at_ts_ns"] for s in snaps)}
        self._memo[key] = out
        return out

    # ------------------------------------------------------------ linked accounts as of the launch (RGY-021)
    def has_prior_launch(self, account: str) -> bool:
        """Did `account` launch (as creator or owner) before this launch? `link_edges` only holds edges between accounts that launched
        at SOME time - including after this launch - so a hop through an account without a prior launch would use the future."""
        key = ("has_prior", account)
        if key not in self._memo:
            self._memo[key] = bool(self.prior_launches(account))
        return self._memo[key]

    def group(self, account: str, max_members: int | None = None) -> list[str]:
        """The accounts linked to `account` by recorded links (`link_edges`) whose transaction time is <= asof_ns,
        the account itself first; breadth first, at most `max_members` (links.group_max by default). Empty list of
        extras when the link tables do not exist (an old database)."""
        cap = int(max_members or self.config["links"]["group_max"])
        key = ("group", account, cap)
        if key in self._memo:
            return self._memo[key]
        seen, order = {account}, [account]
        if self._uv >= 10:
            con = self._store.con
            i = 0
            while i < len(order) and len(order) < cap:
                a = order[i]
                i += 1
                others = [o for (o,) in con.execute(
                    "SELECT dst FROM link_edges WHERE src = ? AND ts_ns IS NOT NULL AND ts_ns <= ? "
                    "UNION SELECT src FROM link_edges WHERE dst = ? AND ts_ns IS NOT NULL AND ts_ns <= ?",
                    (a, self.asof_ns, a, self.asof_ns))]
                if self._attach and (a == self.dev or self.has_prior_launch(a)) and (a == account or not self._in_link_members(a)):
                    others += [o for o in self._attached(a) if o not in others]
                for other in others:
                    if other not in seen and not is_service(other, self.token):
                        if not self.has_prior_launch(other):
                            continue                          # not a launcher yet at that time: the link table knows it only from the future
                        seen.add(other)
                        order.append(other)
                        if len(order) >= cap:
                            break
        self._memo[key] = order
        return order

    def _in_link_members(self, account: str) -> bool:
        key = ("in_members", account)
        if key not in self._memo:
            self._memo[key] = self._store.con.execute("SELECT 1 FROM link_members WHERE account = ?", (account,)).fetchone() is not None
        return self._memo[key]

    def _attached(self, account: str) -> list[str]:
        """RGY-054, the live card path only: the neighbours the next link rebuild will give an account the tables do not know yet (a creator who has just launched for the first time, a member who joined
        a minute ago) or whose newest fundings came after the last build - from the data known at the launch time, by the rebuild's own readers (`links.attach_neighbours`). The tables themselves are
        not written; the next `links.refresh` includes the account as always. Only a participant is attached - the launching account itself, or one with a launch before this one: the rebuild gives
        an account that never launched no edges at all ("hops through wallets that are not participants are NOT followed"), so a funder who is only a wallet stays alone."""
        key = ("attached", account)
        if key not in self._memo:
            from ..links import attach_neighbours
            self._memo[key] = attach_neighbours(self._store, account, self.asof_ns, float(self.config["links"]["min_near"]))
        return self._memo[key]

    def group_launches(self, account: str, window_ns: int | None = None) -> list[dict[str, Any]]:
        """Prior launches (strictly before this one) of `account` and of every account linked to it as of now, deduplicated by
        token, newest first; `via` = the account that made it."""
        out, seen = [], set()
        for a in self.group(account):
            for r in self.prior_launches(a, window_ns):
                if r["token"] not in seen:
                    seen.add(r["token"])
                    out.append({**r, "via": a})
        return sorted(out, key=lambda r: (r["ts_ns"], r["token"]), reverse=True)

    # ------------------------------------------------------------ fundings over the whole history (RGY-021)
    def funders(self, account: str, lookback_ns: int | None = None, min_near: float = 1.0, limit: int = 200) -> list[dict[str, Any]]:
        """Direct NEAR / wNEAR transfers INTO `account` with a time <= asof_ns (the whole history, or `lookback_ns` back), from
        accounts that are not service accounts: facts, the imported history and the transfers API; newest first, one row per
        transaction (the facts row wins). A row: ts_ns, sender, near, tx, source."""
        from ..links import SERVICE_ACCOUNTS
        con, T = self._store.con, self.asof_ns
        lo = T - lookback_ns if lookback_ns else -1
        min_y = int(min_near * 10**24)
        rows: dict[str, dict[str, Any]] = {}

        def add(ts, sender, amount, tx, source):
            try:
                y = int(amount or 0)
            except ValueError:
                return
            if not sender or sender == account or sender in SERVICE_ACCOUNTS or is_service(sender, self.token) or y < min_y:
                return
            k = tx or f"{source}:{ts}:{sender}"
            if k not in rows or source == "facts":
                rows[k] = {"ts_ns": ts, "sender": sender, "near": y / 10**24, "tx": tx, "source": source}

        b = self._store.lookup_account(account)
        kinds = [k for k in (self._store.lookup_kind(n) for n in FUNDING_KINDS) if k is not None]
        if b is not None and kinds:
            bound = self._launch_bound()
            extra = [bound[0], bound[0], bound[1], bound[2]] if bound else []
            for ts, sender, amount, tx in con.execute(
                    f"SELECT f.ts_ns, a.name, f.amount, f.tx FROM facts f JOIN accounts a ON a.id = f.a WHERE f.b = ? "
                    f"AND f.kind IN ({','.join('?' * len(kinds))}) AND f.ts_ns BETWEEN ? AND ? "
                    + ("AND (f.height < ? OR (f.height = ? AND (f.id <= ? OR f.tx = ?))) " if bound else "")
                    + "ORDER BY f.ts_ns DESC LIMIT ?", (b, *kinds, lo, T, *extra, limit * 3)):
                add(ts, sender, amount, hash_str(tx), "facts")
        if self._uv >= 4 and con.execute("SELECT 1 FROM sqlite_master WHERE name = 'hist_fundings'").fetchone():
            for ts, funder, token, amount, tx in con.execute(
                    "SELECT ts_ns, funder, token, amount, tx_hash FROM hist_fundings WHERE account = ? AND ts_ns BETWEEN ? AND ? "
                    "ORDER BY ts_ns DESC LIMIT ?", (account, lo, T, limit * 3)):
                if str(token or "near").lower() in ("near", "wnear", "wrap.near"):
                    add(ts, funder, amount, tx, "history")
        if self._uv >= 12:
            direct = "(signer IS NULL OR (signer = other AND pred = other))" if self._uv >= 13 else "1"
            for ts, other, amount, tx in con.execute(
                    f"SELECT ts_ns, other, amount, tx FROM transfers_api WHERE account = ? AND dir = 'in' AND ts_ns BETWEEN ? AND ? "
                    f"AND {direct} ORDER BY ts_ns DESC LIMIT ?", (account, lo, T, limit * 3)):
                add(ts, other, amount, tx, "transfers_api")
        return sorted(rows.values(), key=lambda r: (-r["ts_ns"], r["sender"]))[:limit]

    # ------------------------------------------------------------ funding clusters as of the launch (RGY-021)
    def cluster_peers(self, account: str) -> list[dict[str, Any]]:
        """The funding clusters (link_clusters) `account` belongs to AS OF now: the other members of the cluster whose funding
        was recorded <= asof_ns and who launched strictly before this launch. A row: source, account, funded_ns, launch_ns.
        The narrowness of a source (how many accounts it funded) is the one value taken from the current tables."""
        if self._uv < 10:
            return []
        con = self._store.con
        mine = con.execute("SELECT source, funded_ns FROM link_cluster_members WHERE account = ? AND funded_ns <= ?",
                           (account, self.asof_ns)).fetchall()
        out = []
        for src, _f in mine:
            for acc, funded, launch_ns in con.execute(
                    "SELECT account, funded_ns, launch_ns FROM link_cluster_members WHERE source = ? AND account <> ? "
                    "AND funded_ns <= ? AND launch_ns < ?", (src, account, self.asof_ns, self.asof_ns)):
                out.append({"source": src, "account": acc, "funded_ns": funded, "launch_ns": launch_ns})
        return out

    def tax_options(self, justhoot: bool = False) -> dict[str, Any] | None:
        """The launch's own tax options (part of the launch transaction's events). `justhoot`: where there are none, the creator's fee of a justhoot launch
        from its stored creation call (RGY-045b; only the profiles that ask for it)."""
        t = L.tax_options(self._store, self.launch)
        if t is None and justhoot:
            return L.justhoot_fee(self._store, self.launch)
        return t

    def my_name_keys(self) -> set[str]:
        return name_keys(self.token, self.extra.get("symbol"), self.extra.get("name"))


def load_launch(store: Store, token: str) -> dict[str, Any] | None:
    cov = "coverage" if store.user_version() >= 6 else "NULL"
    r = store.con.execute(f"SELECT token, launchpad, creator, owner, owner_evidence, pool, dev_buy, height, ts_ns, tx, extra, "
                          f"{cov} FROM launches WHERE token = ?", (token,)).fetchone()
    if r is None:
        return None
    d = dict(zip(("token", "launchpad", "creator", "owner", "owner_evidence", "pool", "dev_buy", "height", "ts_ns", "tx",
                  "extra", "coverage"), r))
    d["extra"] = json.loads(d["extra"]) if d["extra"] else {}
    return d
