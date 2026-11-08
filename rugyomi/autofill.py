"""RGY-020 phase E: keep the history filled. A small periodic worker inside the collector.

What it does, between blocks and only when the collector is at the chain tip:
  1. every ENQ_EVERY_S: puts the NEW launch participants (queue kind 'transfers_in', phase C) and the NEW buyers (kind 'buyers_in',
     phase D; the launches whose first 24 hours are over, from a watermark on) into `backfill_queue`;
  2. every TICK_S: works the queue for at most TIME_BUDGET_S seconds / MAX_UNITS wallets with the code of phases C and D, newest first.
Why: the chain-wide facts the collector writes cover the time since it started; a new participant's older money trail and a new
buyer's funding before our facts begin come only from the Transfers API.

Safety (all of it is in the status, `collector_status` -> 'autofill'):
  - OFF by default. `diag/autofill.py on [--daily-cap N] [--total-cap N]` turns it on (a row in `collector_status`, so a restart keeps it).
  - It spends credits of ONE key, FASTNEAR_API_KEY, passed in by the entry point (`backfill_keys`); the rotation of the collector is
    never used. Counted in the key's own counter file. A daily cap (UTC day) and a total cap stop it; a rejected key stops it for the run.
  - A tick never blocks the collector for long: TIME_BUDGET_S, sleeps capped at 1 s, and 3 failing ticks in a row pause it for 30 min.
  - It never runs while a `run_backfill.py` run holds the backfill lock (the CLI and this worker are never at work together).
  - Nothing here touches facts, blocks, cards, the config or the rules; any exception is logged and recorded, never raised.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import time
from pathlib import Path
from typing import Any, Callable

from . import backfill as B
from . import backfill_buyers as BB
from . import backfill_outgoing as BO
from . import backfill_transfers as BT
from .keys import KeySource
from .store import Store
from .store.db import utcnow_iso

CFG_KEY = "autofill_config"          # {"enabled": bool, "daily_cap": int, "total_cap": int}
STATE_KEY = "autofill"               # the counters and the last outcome
WATERMARK_KEY = "autofill_buyers_wm"  # launches up to this launch time (ns) have had their buyers queued
TICK_S = 30.0
ENQ_EVERY_S = 600.0
TIME_BUDGET_S = 5.0
MAX_UNITS = 6
DEFAULT_DAILY_CAP = 2000
DEFAULT_TOTAL_CAP = 50000
PAUSE_AFTER_FAILED_TICKS = 3
PAUSE_S = 1800.0
RETRY_ERRORS_AFTER_S = 6 * 3600       # a failed wallet is tried again after this long (a transient outage heals itself) ...
MAX_ATTEMPTS = 6                      # ... up to this many attempts in all, then it stays `error` for a human
BUYER_SETTLE_NS = (24 * 60 + 10) * 60 * 10**9      # a launch's buyers are final 24 h (+10 min) after the launch
NS_DAY = 24 * 3600 * 10**9


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _read(store: Store, key: str) -> Any:
    import json
    r = store.con.execute("SELECT value FROM collector_status WHERE key = ?", (key,)).fetchone()
    if r is None or r[0] is None:
        return None
    try:
        return json.loads(r[0])
    except ValueError:
        return None


def _write(store: Store, key: str, value: Any) -> None:
    store.begin()
    try:
        store.set_status(key, value)
        store.commit()
    except BaseException:
        store.rollback()
        raise


def config(store: Store) -> dict[str, Any]:
    c = _read(store, CFG_KEY) or {}
    return {"enabled": bool(c.get("enabled")), "daily_cap": int(c.get("daily_cap") or DEFAULT_DAILY_CAP),
            "total_cap": int(c.get("total_cap") or DEFAULT_TOTAL_CAP)}


def set_config(store: Store, enabled: bool, daily_cap: int | None = None, total_cap: int | None = None) -> dict[str, Any]:
    c = config(store)
    c["enabled"] = enabled
    if daily_cap is not None:
        c["daily_cap"] = int(daily_cap)
    if total_cap is not None:
        c["total_cap"] = int(total_cap)
    _write(store, CFG_KEY, c)
    return c


def summary(store: Store) -> dict[str, Any]:
    """For `collector_status` and the diag script: the config, the counters and the queue by kind and status."""
    q: dict[str, dict[str, int]] = {}
    for kind, st, n in store.con.execute("SELECT kind, status, COUNT(*) FROM backfill_queue WHERE kind IN (?, ?, ?) GROUP BY 1, 2",
                                         (BT.KIND, BB.KIND, BO.KIND)):
        q.setdefault(kind, {})[st] = n
    return {"config": config(store), "state": _read(store, STATE_KEY), "buyers_watermark": _read(store, WATERMARK_KEY), "queue": q}


class AutoFill:
    def __init__(self, store: Store, keys: KeySource | None, log: Callable[[str], None], state_dir: Path | None = None,
                 base: str = BT.TR_BASE, gap: float = 0.2, clock: Callable[[], float] = time.monotonic,
                 now_ns: Callable[[], int] = time.time_ns, stop: Callable[[], bool] = lambda: False) -> None:
        self.store, self.keys, self.log = store, keys, log
        self.state_dir, self.base, self.gap = state_dir, base, gap
        self.clock, self.now_ns, self.stop = clock, now_ns, stop
        self._last_tick = -1e9
        self._last_enq = -1e9
        self._failed_ticks = 0
        self._paused_until = 0.0
        self._warned_lock = False

    # ------------------------------------------------------------------ enqueue
    def _watermark(self) -> int:
        wm = _read(self.store, WATERMARK_KEY)
        if isinstance(wm, int):
            return wm
        r = self.store.con.execute("SELECT MIN(queued_at) FROM backfill_queue WHERE kind = ?", (BB.KIND,)).fetchone()[0]
        if r:      # phase D was planned at that time: the launches before it (minus one day) already have their buyers queued
            t = dt.datetime.strptime(r[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
            return int(t.timestamp() * 10**9) - NS_DAY
        return self.now_ns() - 2 * NS_DAY          # phase D never ran: only the last two days, never the whole history

    def requeue_errors(self) -> int:
        """Failed wallets older than RETRY_ERRORS_AFTER_S with fewer than MAX_ATTEMPTS attempts go back to `pending`."""
        before = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.now_ns() / 1e9 - RETRY_ERRORS_AFTER_S))
        self.store.begin()
        try:
            n = self.store.con.execute(
                "UPDATE backfill_queue SET status = 'pending' WHERE kind IN (?, ?, ?) AND status = 'error' AND attempts < ? "
                "AND COALESCE(finished_at, '') <= ?", (BT.KIND, BB.KIND, BO.KIND, MAX_ATTEMPTS, before)).rowcount
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        return n

    def enqueue_new(self) -> dict[str, Any]:
        out: dict[str, Any] = {"participants": BT.enqueue(self.store), "participants_out": BO.enqueue(self.store), "requeued_errors": self.requeue_errors()}
        until = self.now_ns() - BUYER_SETTLE_NS
        wm = self._watermark()
        if until > wm:
            out["buyers"] = BB.enqueue(self.store, wm, until)
            _write(self.store, WATERMARK_KEY, until)
        return out

    # ------------------------------------------------------------------ one tick
    def tick(self, force: bool = False) -> dict[str, Any] | None:
        """Never raises. Returns what it did (None: nothing due / off)."""
        if self.keys is None:
            return None
        now = self.clock()
        if not force and now - self._last_tick < TICK_S:
            return None
        self._last_tick = now
        try:
            cfg = config(self.store)
            if not cfg["enabled"] or now < self._paused_until:
                return None
            return self._work(cfg, now)
        except Exception as e:  # noqa: BLE001 — a failure here must never stop the collector
            self._failed_ticks += 1
            msg = f"{type(e).__name__}: {self.keys.scrub(str(e))}"[:300]
            self.log(f"autofill: {msg}")
            self._record(last_error=msg)
            if self._failed_ticks >= PAUSE_AFTER_FAILED_TICKS:
                self._paused_until = now + PAUSE_S
                self._failed_ticks = 0
                self.log(f"autofill: paused for {int(PAUSE_S / 60)} min after repeated failures")
            return None

    def _record(self, **upd: Any) -> dict[str, Any]:
        st = _read(self.store, STATE_KEY) or {}
        st.update(upd)
        try:
            _write(self.store, STATE_KEY, st)
        except Exception:  # noqa: BLE001 — the status is best effort
            self.store.rollback()
        return st

    def _lock(self):
        if self.state_dir is None:
            return None
        f = open(Path(self.state_dir) / "backfill.lock", "a+")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            if not self._warned_lock:
                self.log("autofill: a run_backfill run holds the lock; waiting")
                self._warned_lock = True
            return False
        self._warned_lock = False
        return f

    def _work(self, cfg: dict[str, Any], now: float) -> dict[str, Any] | None:
        st = _read(self.store, STATE_KEY) or {}
        if st.get("day") != _today():
            st.update(day=_today(), credits_today=0)
        left = min(cfg["daily_cap"] - int(st.get("credits_today") or 0), cfg["total_cap"] - int(st.get("credits_total") or 0))
        if left <= 0:
            reason = "daily cap reached" if cfg["daily_cap"] - int(st.get("credits_today") or 0) <= 0 else "total cap reached"
            if st.get("paused") != reason:
                self._record(**{**st, "paused": reason})
            return None
        if self.keys.current() is None:
            self._record(**{**st, "paused": "no usable key (rejected?)"})
            return None
        lock = self._lock()
        if lock is False:
            return None
        try:
            did: dict[str, Any] = {}
            if now - self._last_enq >= ENQ_EVERY_S:
                self._last_enq = now
                did["enqueued"] = self.enqueue_new()
            deadline = self.clock() + TIME_BUDGET_S
            api = B.Api(self.keys, gap=self.gap, base=self.base, sleep=lambda s: time.sleep(min(s, 1.0)))
            cnt = lambda st: self.store.con.execute(  # noqa: E731
                "SELECT COUNT(*) FROM backfill_queue WHERE kind IN (?, ?, ?) AND status = ?", (BT.KIND, BB.KIND, BO.KIND, st)).fetchone()[0]
            errs0, done0 = cnt("error"), cnt("done")
            units = 0
            reasons: list[str] = []
            for kind, run in ((BT.KIND, BT.run), (BB.KIND, BB.run), (BO.KIND, BO.run)):
                if self.clock() >= deadline or api.hops >= left or units >= MAX_UNITS:
                    break
                res = run(self.store, api, lambda: self.clock() >= deadline or self.stop(), max_credits=left - api.hops,
                          limit=MAX_UNITS - units, log=lambda m: None, retry_errors=False,
                          **({"kind": kind} if run is BT.run else {}))
                units += res["accounts"]
                reasons.append(res["reason"])
                if res["reason"].startswith("key"):
                    break
            self.keys.flush()
            new_errs, ok = max(0, cnt("error") - errs0), cnt("done") - done0
            credits = api.hops
            st.update(credits_today=int(st.get("credits_today") or 0) + credits, credits_total=int(st.get("credits_total") or 0) + credits,
                      units_total=int(st.get("units_total") or 0) + units, last_tick=utcnow_iso(), last_units=units, last_credits=credits,
                      last_reason=", ".join(reasons), paused=None, last_429=api.n429,
                      errors_total=int(st.get("errors_total") or 0) + new_errs)
            if any(r.startswith("key") for r in reasons):
                st["last_error"] = "the key was rejected by the server; autofill idle until the collector restarts"
                self.log("autofill: the key was rejected; idle")
            if new_errs and not ok:
                self._failed_ticks += 1
                st["last_error"] = f"{new_errs} wallet(s) failed in the last tick, none succeeded"
                if self._failed_ticks >= PAUSE_AFTER_FAILED_TICKS:
                    self._paused_until = now + PAUSE_S
                    self._failed_ticks = 0
                    st["paused"] = f"paused {int(PAUSE_S / 60)} min after repeated failures"
                    self.log("autofill: paused after repeated failures")
            elif ok:
                self._failed_ticks = 0
            self._record(**st)
            if credits or did:
                self.log(f"autofill: {units} unit(s), {credits} credit(s) this tick, {st['credits_today']} today"
                         + (f"; enqueued {did['enqueued']}" if did.get("enqueued") else ""))
            return {"units": units, "credits": credits, **did}
        finally:
            if lock:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
