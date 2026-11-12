"""A pass-level cache of the big reads behind the as-of context (RGY-050).

A pass (backtest, fit, the back-filled as-of cards, walk-forward, the feature fill) measures thousands of launches, and each one used to re-read the same tables: the window of recent launches
(about 1 300 rows of JSON per launch for `name_copy`), the launch list of every creator, the snapshots of every earlier launch and the rug label computed from them. Here each is read
ONCE and answered from memory. The answers are the very values `history.launches_between` / `history.creator_launches` / the snapshot query + `rug_labels` give - the as-of rule is not
here, it stays in `LaunchContext` (which still filters by `asof_ns` and by the launch block); this class only holds rows that do not depend on the launch.

Valid for as long as the database is not written to by someone else: build one per pass, drop it afterwards. The live collector never uses it (it builds the context without a cache).
"""
from __future__ import annotations

import json
from bisect import bisect_left
from typing import Any

from .. import history
from ..store import Store
from .rug import rug_labels

NONE_OUTCOME = {"rug": None, "fast": None, "late": None, "known_ns": None}


class PassCache:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._uv = store.user_version()
        self._launches: list[dict[str, Any]] | None = None         # from `launches`, sorted by time
        self._launch_ts: list[int] = []
        self._index: list[dict[str, Any]] | None = None            # from `launch_index` (token only), sorted by time
        self._index_ts: list[int] = []
        self._creator: dict[str, list[dict[str, Any]]] = {}
        self._snaps: dict[tuple[int, int], dict[str, list[tuple[Any, dict[str, Any]]]]] = {}
        self._labels: dict[tuple, dict[str, Any]] = {}
        self.stats = {"launch_windows": 0, "creator_hits": 0, "creator_misses": 0, "outcome_hits": 0, "outcome_misses": 0}

    # ------------------------------------------------------------ launches in a time window (history.launches_between)
    def _load_launches(self) -> None:
        cov = "coverage" if self._uv >= 6 else "NULL"
        rows = []
        for token, lp, creator, owner, ts, c, extra in self.store.con.execute(
                f"SELECT token, launchpad, creator, owner, ts_ns, {cov}, extra FROM launches WHERE ts_ns IS NOT NULL"):
            x = json.loads(extra) if extra else {}
            rows.append({"token": token, "launchpad": lp, "creator": creator, "owner": owner, "ts_ns": ts,
                         "coverage": c or "live", "symbol": x.get("symbol"), "name": x.get("name")})
        rows.sort(key=lambda r: r["ts_ns"])
        self._launches, self._launch_ts = rows, [r["ts_ns"] for r in rows]
        idx = []
        if self._uv >= 6:
            for token, lp, creator, ts in self.store.con.execute(
                    "SELECT token, launchpad, creator, create_ts_ns FROM launch_index WHERE create_ts_ns IS NOT NULL"):
                idx.append({"token": token, "launchpad": lp, "creator": creator, "owner": None, "ts_ns": ts,
                            "coverage": "index_only", "symbol": None, "name": None})
        idx.sort(key=lambda r: r["ts_ns"])
        self._index, self._index_ts = idx, [r["ts_ns"] for r in idx]

    def launches_between(self, lo_ns: int, hi_ns: int) -> list[dict[str, Any]]:
        """Same rows, same order as `history.launches_between(store, lo_ns, hi_ns)` (lo <= time < hi; a launch in `launches` wins over its index row)."""
        if self._launches is None:
            self._load_launches()
        self.stats["launch_windows"] += 1
        a, b = bisect_left(self._launch_ts, lo_ns), bisect_left(self._launch_ts, hi_ns)
        found = {r["token"]: r for r in self._launches[a:b]}
        if self._index:
            c, d = bisect_left(self._index_ts, lo_ns), bisect_left(self._index_ts, hi_ns)
            for r in self._index[c:d]:
                if r["token"] not in found:
                    found[r["token"]] = r
        return sorted(found.values(), key=lambda r: (r["ts_ns"] or 0, r["token"]))

    # ------------------------------------------------------------ every launch of one account (history.creator_launches)
    def creator_launches(self, account: str) -> list[dict[str, Any]]:
        r = self._creator.get(account)
        if r is None:
            self.stats["creator_misses"] += 1
            r = self._creator[account] = history.creator_launches(self.store, account)
        else:
            self.stats["creator_hits"] += 1
        return r

    # ------------------------------------------------------------ what was known about the rug label of an earlier launch (LaunchContext.outcome)
    def _snap_rows(self, fast: int, late: int) -> dict[str, list[tuple[Any, dict[str, Any]]]]:
        from .stored import SNAP_COLS
        key = (fast, late)
        if key not in self._snaps:
            src = "source" if self._uv >= 11 else "NULL"
            by: dict[str, list[tuple[Any, dict[str, Any]]]] = {}
            for r in self.store.con.execute(
                    f"SELECT token, {src}, {','.join(SNAP_COLS)} FROM launch_snapshots WHERE minute IN (?, ?) ORDER BY token, minute", key):
                by.setdefault(r[0], []).append((r[1], dict(zip(SNAP_COLS, r[2:]))))
            self._snaps[key] = by
        return self._snaps[key]

    def outcome(self, token: str, asof_ns: int, live_only: bool, config: dict[str, Any]) -> dict[str, Any]:
        """The dict `LaunchContext.outcome` returns: from the snapshots of `token` with a cutoff <= asof_ns (only the live ones when `live_only`), labelled by `rug_labels`."""
        rug = config["rug"]
        rows = self._snap_rows(rug["fast_minute"], rug["late_minute"]).get(token, ())
        snaps = [s for src, s in rows if s["at_ts_ns"] is not None and s["at_ts_ns"] <= asof_ns and not (live_only and src is not None)]
        if not snaps:
            return dict(NONE_OUTCOME)
        key = (token, tuple((s["minute"], s["at_ts_ns"]) for s in snaps), json.dumps(rug, sort_keys=True))
        out = self._labels.get(key)
        if out is None:
            self.stats["outcome_misses"] += 1
            lab = rug_labels([dict(s) for s in snaps], config)
            out = self._labels[key] = {"rug": lab["rug"]["value"], "fast": lab["rug_fast"]["value"], "late": lab["rug_late"]["value"],
                                       "known_ns": max(s["at_ts_ns"] for s in snaps)}
        else:
            self.stats["outcome_hits"] += 1
        return out
