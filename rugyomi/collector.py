"""Live collector: final blocks -> raw facts (option A) + launches + risk cards, one SQLite transaction per block.

    python -m rugyomi.collector run     [--db PATH] [--rps N] [--prefetch 16] [--workers 4]
    python -m rugyomi.collector status  [--db PATH]          (offline: reads the DB only, prints JSON)
    python -m rugyomi.collector rebuild-launches [--db PATH] (recompute the derived launches table)
    python -m rugyomi.collector recheck-nulls [--db PATH] [--since H] (re-request every height stored as null)
    python -m rugyomi.collector rebuild-snapshots [--db PATH] [--token T | --since H] (recompute launch snapshots
                                                   and the stored outcome labels of the launches it touched)
    python -m rugyomi.collector rebuild-outcomes [--db PATH] [--token T]   (recompute all stored outcome labels with the
                                                   config in force — after a config change; prints counts as JSON)
    python -m rugyomi.collector rebuild-links [--db PATH]    (recompute the link tables: linked accounts, funding clusters;
                                                   prints counts as JSON)
    python -m rugyomi.collector score --token T [--db PATH]  (the risk card as of the launch; writes nothing)
    python -m rugyomi.collector extend-start --to H [--db PATH]  (lower the start height: the collector fills the gap)
    python -m rugyomi.collector reprocess-quarantine [--db PATH] [--height H]  (after a fix: re-run quarantined heights)
    python -m rugyomi.collector import-launch-index CSV [ITEMS.jsonl] [--db PATH]  (launch index: idempotent)
    python -m rugyomi.collector ingest-blocks DIR [--db PATH] [--keep] [--limit N]  (history blocks -> facts, launch_only)

Rules (master §13):
  * Start from the cursor — the lowest height not yet done (ok / null) above the recorded start; on an empty DB
    from the final head, recorded as start_height (earlier history is a separate task). Never from head after
    an error or a restart.
  * One transaction per block: its facts, its parse errors, its `blocks` row and the launches detected in it.
    A crash never leaves a block half-written.
  * A height with no block is a skipped height (`null` row) only if the final head was read BEFORE that request
    and was >= h; a null at the tip is re-requested once the head passes h. `recheck-nulls` re-requests every
    stored null (repairs nulls written by an older version).
  * An error on a height: an `error` row + last_error in the status, then the SAME height again with backoff
    (2, 4 … 60 s). Two kinds (RGY-012, rule `is_processing_error`):
      - network / source / environment: fetching failed (HTTP 5xx, timeouts, 429, a bad answer) or the DB / OS did
        (sqlite3.OperationalError: locked, disk full, I/O; OSError; MemoryError) -> retried forever, never skipped;
      - processing: the block WAS fetched and parse / store / launch detection / snapshots raised anything else
        (a bug or a data case the code cannot handle) -> after QUARANTINE_AFTER (5) such failures on the height it
        goes to QUARANTINE: kept as an `error` row with quarantined_at / quarantine_error, shown in the status and
        by the watchdog, and the collector moves on. Never silent. `reprocess-quarantine` re-runs it after the fix.
        Burst guard: if the QUARANTINE_BURST (3) heights right before it are all in quarantine, the error is
        systemic (e.g. a bad deploy), not one odd block -> no more quarantine, the height is retried forever.
  * The status is written every STATUS_EVERY_S (10 s) also while a height fails or a fetch hangs, so a stall shows:
    status_written_at / last_ok_commit_at / final_head_seen_at stop being fresh.
  * Catching up: at most `prefetch` (<= 16) blocks fetched ahead by <= 4 workers, committed strictly in order.
  * SIGTERM / Ctrl-C: the current block is finished and committed, then exit 0.
DB: --db, else $RUGYOMI_DB, else ~/.rugyomi/rugyomi.db. Key: a KeySource passed to main() by a deployment,
else one FASTNEAR_API_KEY (rugyomi.keys).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any, Callable

from . import launches as L
from . import dossier as DS
from . import links as K
from . import autofill as AF
from . import snapshots as S
from .rules import card as R
from .rules import stored as O
from .rules.config import ConfigError
from .keys import EnvKeySource, KeySource
from .neardata import BASE, Neardata, Stopped
from .parse import ParseStats, block_ts_ns, keep_option_a, parse_block
from .store import Store, open_store
from .store.db import utcnow_iso

MAX_PREFETCH, MAX_WORKERS = 16, 4
STATUS_EVERY_S = 10.0
HEAD_EVERY_S = 5.0          # at the tip, re-read the final head this often to notice falling behind
CURSOR_EVERY_S = 10.0       # re-read the cursor this often: `extend-start` may have lowered the start (RGY-008)
LINKS_CHECK_S = 30.0        # RGY-019: is a rebuild of the link tables due? (the rebuild itself is throttled in rugyomi.links)
HEAD_CATCHUP_EVERY_S = 30.0  # while catching up, re-read the final head this often (1 hop): the lag stays honest
HEAD_STALE_S = 120.0        # status: a final head read longer ago than this is stale -> lag / remaining unknown
QUARANTINE_AFTER = 5        # processing failures on one height before it goes to quarantine (user, 31.10)
QUARANTINE_BURST = 3        # this many quarantined heights right before -> systemic error: retry, never quarantine
# Errors of the environment, not of the block: retried forever like network errors, never quarantined.
ENVIRONMENT_ERRORS: tuple[type[BaseException], ...] = (sqlite3.OperationalError, OSError, MemoryError)


def is_processing_error(e: BaseException) -> bool:
    """An exception raised while processing a block that WAS fetched: anything but an environment error."""
    return not isinstance(e, ENVIRONMENT_ERRORS)


def default_db() -> Path:
    return Path(os.environ.get("RUGYOMI_DB") or Path.home() / ".rugyomi" / "rugyomi.db")


def iso(ts_ns: int | None) -> str | None:
    if ts_ns is None:
        return None
    return dt.datetime.fromtimestamp(ts_ns / 1e9, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Collector:
    def __init__(self, store: Store, client: Neardata, *, prefetch: int = MAX_PREFETCH, workers: int = MAX_WORKERS,
                 log: Callable[[str], None] | None = None, retry_delay: float = 2.0,
                 backfill_keys: KeySource | None = None, state_dir: Path | None = None) -> None:
        self.store = store
        self.client = client
        self.stop = client.stop
        self.retry_delay = retry_delay          # first backoff after an error; doubles up to 60 s
        self.prefetch = max(1, min(MAX_PREFETCH, prefetch))
        self.workers = max(1, min(MAX_WORKERS, workers))
        self.log = log or (lambda m: print(f"{utcnow_iso()} {m}", file=sys.stderr, flush=True))
        self.stats = ParseStats()
        self.known_head: int | None = None
        self.blocks_done = 0
        self.quarantined_this_run = 0
        self._last_status = 0.0
        self._head_at = 0.0
        self._head_read_at: str | None = None    # wall clock of the last real head observation
        self._cursor_at = time.monotonic()
        self._rate_mark: tuple[float, int] | None = None
        self.rate_bps: float | None = None
        self._links_checked = 0.0                # monotonic time of the last "is a links rebuild due?" check
        # RGY-020 phase E: the history top-up worker. Only with its own key source (the private entry point passes
        # FASTNEAR_API_KEY alone) and only when switched on in the DB (`diag/autofill.py on`); otherwise it does nothing.
        self._autofill = AF.AutoFill(store, backfill_keys, self.log, state_dir=state_dir,
                                     stop=lambda: self.stop.is_set()) if backfill_keys is not None else None

    # ---------------------------------------------------------------- one block

    def process(self, height: int, block: dict[str, Any] | None, live: bool = True) -> int:
        """Commit one height: a block's facts / errors / launches, or a `null` row. Returns new fact rows.
        live=False (recheck of an old height): the last-height status is not moved."""
        st = self.store
        if block is None:
            st.begin()
            try:
                st.mark_block(height, "null", None, 0)
                if live:
                    st.set_status("last_height", height)
                    st.set_status("last_ok_commit_at", utcnow_iso())
                st.commit()
            except BaseException:
                st.rollback()
                raise
            return 0
        errors: list = []
        facts = [f for f in parse_block(block, self.stats, errors) if keep_option_a(f)]
        ts = block_ts_ns(block)
        st.begin()
        try:
            n = st.insert_facts(facts)
            st.insert_parse_errors(errors)
            st.mark_block(height, "ok", ts, len(facts))
            L.detect_in_block(st, height)
            S.compute_due(st, ts)                    # snapshots whose cutoff this block reached (own savepoints)
            O.refresh_touched(st, self.log)           # stored outcome labels of the launches whose T+60 / T+24 h row was written (RGY-017)
            R.issue_for_height(st, height, self.log)  # risk cards of the live launches of this block (RGY-015): after the
                                                      # snapshots due at this time, so the card sees what is knowable then
            if live:
                st.set_status("last_height", height)
                st.set_status("last_block_ts_ns", ts)
                st.set_status("last_ok_commit_at", utcnow_iso())
            st.commit()
        except BaseException:
            st.rollback()
            raise
        for e in errors:
            self.log(f"parse error at {e.height} {e.receipt_id} {e.origin}: {e.error}")
        return n

    # ---------------------------------------------------------------- loop

    def _start_height(self) -> int:
        c = self.store.cursor()
        if c is not None:
            return c
        head = self._retry("final head", self.client.final_head)
        self.store.begin()
        try:
            self.store.set_status("start_height", head)
            self.store.set_status("started_from", "final head (empty database)")
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        self.known_head = head
        self._head_at, self._head_read_at = time.monotonic(), utcnow_iso()
        self.log(f"empty database: starting at the final head {head}")
        return head

    def _retry(self, what: str, fn: Callable[[], Any], height: int | None = None) -> Any:
        delay = self.retry_delay
        while True:
            try:
                return fn()
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001 — recorded, shown in status, retried
                msg = f"{type(e).__name__}: {self.client.keys.scrub(str(e))}"
                self.log(f"{what} failed: {msg}; retry in {delay:.0f}s")
                try:
                    if height is not None:
                        self.store.record_error(height, msg)
                    else:
                        self.store.begin()
                        self.store.set_status("last_error", {"what": what, "error": msg, "at": utcnow_iso()})
                        self.store.commit()
                except Exception as e2:  # noqa: BLE001
                    self.store.rollback()
                    self.log(f"!!! could not record the error in the DB: {e2}")
                if self._wait(delay):
                    raise Stopped() from None
                delay = min(60.0, delay * 2)

    def _wait(self, delay: float) -> bool:
        """Sleep `delay` s, writing the status at least every STATUS_EVERY_S. True if asked to stop."""
        end = time.monotonic() + delay
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return self.stop.is_set()
            if self.stop.wait(min(left, STATUS_EVERY_S)):
                return True
            self.write_status()

    def _await(self, fut: cf.Future) -> Any:
        """The result of a fetch, writing the status at least every STATUS_EVERY_S while it runs."""
        while True:
            try:
                return fut.result(timeout=STATUS_EVERY_S)
            except cf.TimeoutError:
                self.write_status()
                if self.stop.is_set():
                    raise Stopped() from None

    def _refresh_head(self) -> int:
        self.known_head = self._retry("final head", self.client.final_head)
        self._head_at = time.monotonic()
        self._head_read_at = utcnow_iso()
        return self.known_head

    def write_status(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_status < STATUS_EVERY_S:
            return
        self._last_status = now
        try:
            self.client.keys.flush()
            self.store.begin()
            self.store.set_status("final_head", self.known_head)
            self.store.set_status("final_head_read_at", self._head_read_at)
            self.store.set_status("keys", self.client.keys.status())
            self.store.set_status("http", dict(self.client.counters, rps=round(self.client.pacer.rps, 2)))
            # not dataclasses.asdict: it rebuilds a Counter from (key, value) pairs -> tuple keys -> no JSON
            self.store.set_status("parse_stats", {f.name: (dict(getattr(self.stats, f.name))
                                                           if isinstance(getattr(self.stats, f.name), dict)
                                                           else getattr(self.stats, f.name))
                                                  for f in fields(self.stats)})
            if self._rate_mark is not None and now > self._rate_mark[0]:
                self.rate_bps = round((self.blocks_done - self._rate_mark[1]) / (now - self._rate_mark[0]), 2)
            self._rate_mark = (now, self.blocks_done)
            self.store.set_status("collector", {"pid": os.getpid(), "heartbeat": utcnow_iso(),
                                                "blocks_this_run": self.blocks_done, "blocks_per_s": self.rate_bps,
                                                "quarantined_this_run": self.quarantined_this_run,
                                                "prefetch": self.prefetch, "workers": self.workers})
            self.store.set_status("status_write_error", None)
            self.store.set_status("status_written_at", utcnow_iso())
            self.store.commit()
        except Exception as e:  # noqa: BLE001 — never silent: logged AND shown by `status`
            self.store.rollback()
            msg = f"{type(e).__name__}: {e}"
            self.log(f"!!! status write failed: {msg}")
            try:
                self.store.begin()
                self.store.set_status("status_write_error", {"error": msg[:500], "at": utcnow_iso()})
                self.store.commit()
            except Exception as e2:  # noqa: BLE001
                self.store.rollback()
                self.log(f"!!! could not record the status write error either: {e2}")

    def run(self, max_blocks: int | None = None) -> int:
        """Follow the chain until stopped (or `max_blocks` committed — tests). Returns the next height."""
        try:
            c = self._start_height()
        except Stopped:
            self.log("collector stopped before the first block")
            return -1
        self.log(f"collector: next height {c}")
        self.write_status(force=True)                        # a fresh status right away (the watchdog reads it)
        pool = cf.ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="fetch")
        inflight: dict[int, cf.Future] = {}
        fail_delay = self.retry_delay
        try:
            while not self.stop.is_set():
                if max_blocks is not None and self.blocks_done >= max_blocks:
                    break
                if time.monotonic() - self._cursor_at > CURSOR_EVERY_S:
                    self._cursor_at = time.monotonic()
                    cur = self.store.cursor()
                    if cur is not None and cur < c:
                        # the start was lowered (extend-start): fill the gap first, in order, then come back
                        self.log(f"cursor moved back to {cur} (start lowered): filling heights {cur}.. before {c}")
                        for f in inflight.values():
                            f.cancel()
                        inflight.clear()
                        c = cur
                since_head = time.monotonic() - self._head_at
                if self.known_head is None or (c > self.known_head and since_head > HEAD_EVERY_S) \
                        or (c <= self.known_head and since_head > HEAD_CATCHUP_EVERY_S):
                    self._refresh_head()
                if c <= self.known_head:
                    # catching up: a bounded window of final heights, committed in order; never past a height that is
                    # already done (the end of a gap being filled)
                    hi = min(c + self.prefetch - 1, self.known_head)
                    nd = self.store.next_done(c)
                    if nd is not None:
                        hi = min(hi, nd - 1)
                    for h in range(c, hi + 1):
                        if h not in inflight:
                            inflight[h] = pool.submit(self.client.block, h)
                    try:
                        block = self._await(inflight[c])
                    except Stopped:
                        break
                    except Exception as e:  # noqa: BLE001
                        inflight.pop(c, None)
                        self._fail(c, e, fail_delay)
                        fail_delay = min(60.0, fail_delay * 2)
                        continue
                    inflight.pop(c)
                    # c <= the final head: no block means a skipped height
                else:
                    # at the tip: ask for the next height. A null here is NOT final: neardata may answer null for a
                    # height that is not final yet. It counts as a skipped height only when the final head was read
                    # BEFORE the request and was >= h (RGY-005: reading the head after the null lost real blocks).
                    try:
                        block = self._await(pool.submit(self.client.block, c))
                        if block is None:
                            if self._refresh_head() < c:
                                self.write_status()
                                if self.stop.wait(0.3):
                                    break
                                continue
                            # the head (read just now) has passed c: ask again — this answer is final
                            block = self._await(pool.submit(self.client.block, c))
                    except Stopped:
                        break
                    except Exception as e:  # noqa: BLE001
                        self._fail(c, e, fail_delay)
                        fail_delay = min(60.0, fail_delay * 2)
                        continue
                    if block is not None and (self.known_head is None or c > self.known_head):
                        self._head_read_at = utcnow_iso()            # a final block at c: the head is >= c now
                    self.known_head = max(self.known_head or c, c)
                try:
                    self.process(c, block)
                except Exception as e:  # noqa: BLE001 — processing failed: retried, quarantined after N (RGY-012)
                    inflight.pop(c, None)
                    if not self._fail(c, e, fail_delay, processing=is_processing_error(e)):
                        fail_delay = min(60.0, fail_delay * 2)
                        continue
                    # quarantined: visible in the status and alerts; move on as if done
                    fail_delay = self.retry_delay
                    self.quarantined_this_run += 1
                else:
                    fail_delay = self.retry_delay
                    self.blocks_done += 1
                c += 1
                if self.store.is_done(c):
                    # the end of a filled gap: jump to the next height that is not done (the live tip)
                    nxt = self.store.cursor(start=c)
                    self.log(f"gap filled up to {c - 1}; continuing at {nxt}")
                    for h in [h for h in inflight if h < nxt]:
                        inflight.pop(h).cancel()
                    c = nxt
                    self._snapshots_catch_up()
                self.write_status()
                if self.known_head is not None and c > self.known_head:
                    self._links_tick()                            # at the tip only: a rebuild takes a moment
                    if self._autofill is not None:
                        self._autofill.tick()                     # at the tip only; never raises (RGY-020 phase E)
        except Stopped:
            pass
        finally:
            self.stop.set()                                  # fetchers stop at their next hop
            for f in inflight.values():
                f.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            self.write_status(force=True)
        self.log(f"collector stopped; next height {c}")
        return c

    def _links_tick(self, force: bool = False) -> None:
        """RGY-019: between blocks, outside the block transaction, rebuild the link tables when due (a new launch, a changed
        config, or the last build is old). Checked at most every LINKS_CHECK_S; a failure never stops the collector."""
        now = time.monotonic()
        if not force and now - self._links_checked < LINKS_CHECK_S:
            return
        self._links_checked = now
        K.refresh(self.store, self.log, force=force)
        DS.refresh(self.store, self.log, force=force)         # RGY-023: the dossier tables follow the links, the outcomes and the loaded transfers

    def _snapshots_catch_up(self) -> None:
        """After a filled gap: snapshots whose cutoff lies in heights processed earlier are due now (RGY-008)."""
        ts = self.store.con.execute("SELECT MAX(block_ts_ns) FROM blocks WHERE status = 'ok'").fetchone()[0]
        if ts is None:
            return
        self.store.begin()
        try:
            n = S.compute_due(self.store, ts)
            O.refresh_touched(self.store, self.log)
            self.store.commit()
            if n:
                self.log(f"snapshots computed after the gap: {n}")
        except Exception as e:  # noqa: BLE001 — the next block's hook retries the same rows
            self.store.rollback()
            self.log(f"!!! snapshot catch-up failed: {e}")

    def extend_start(self, to: int) -> dict[str, Any]:
        return extend_start(self.store, to)

    def recheck_nulls(self, since: int | None = None) -> dict[str, Any]:
        """Re-request every height stored as `null` (>= since). A block that exists now is processed in one
        transaction (facts + `blocks` null -> ok + launches). Repairs nulls written before the RGY-005 tip fix.
        The result goes to the status (last run + running totals)."""
        heights = [h for (h,) in self.store.con.execute(
            "SELECT height FROM blocks WHERE status = 'null' AND height >= ? ORDER BY height", (since or 0,))]
        checked, recovered = 0, []
        self.log(f"recheck-nulls: {len(heights)} null height(s) to re-request")
        try:
            for h in heights:
                if self.stop.is_set():
                    break
                block = self._retry(f"recheck {h}", lambda h=h: self.client.block(h))
                checked += 1
                if block is not None:
                    self.process(h, block, live=False)
                    recovered.append(h)
                    for tok in S.launches_covering(self.store, block_ts_ns(block)):
                        S.rebuild_snapshots(self.store, token=tok)     # their windows now hold more facts
                    O.refresh_touched_committed(self.store, self.log)
                    self.log(f"recheck-nulls: height {h} has a block — recovered")
        except Stopped:
            pass
        result = {"at": utcnow_iso(), "since": since, "null_heights": len(heights), "checked": checked,
                  "recovered": len(recovered), "recovered_heights": recovered[:200],
                  "complete": checked == len(heights)}
        prev = self.store.get_status().get("null_recheck_totals") or {}
        self.store.begin()
        try:
            self.store.set_status("null_recheck_last", result)
            self.store.set_status("null_recheck_totals", {"runs": int(prev.get("runs", 0)) + 1,
                                                          "checked": int(prev.get("checked", 0)) + checked,
                                                          "recovered": int(prev.get("recovered", 0)) + len(recovered)})
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        return result

    def _fail(self, height: int, e: BaseException, delay: float, processing: bool = False) -> bool:
        """Record a failed attempt. A processing failure that reached QUARANTINE_AFTER puts the height in quarantine
        and returns True (the caller moves on). Otherwise waits `delay` (writing the status) and returns False."""
        msg = f"{type(e).__name__}: {self.client.keys.scrub(str(e))}"
        kind = "processing" if processing else "network/environment"
        try:
            n = self.store.record_error(height, msg, processing=processing)
        except Exception as e2:  # noqa: BLE001
            self.store.rollback()
            self.log(f"!!! could not record the error in the DB: {e2}")
            n = 0                                            # never quarantine what could not be recorded
        if processing and n >= QUARANTINE_AFTER:
            if self._burst(height):
                self.log(f"height {height}: processing failed {n} times, but the {QUARANTINE_BURST} heights before "
                         f"it are quarantined too — systemic error, NOT quarantining; retrying")
            else:
                try:
                    self.store.quarantine(height, msg)
                    self.log(f"!!! height {height} QUARANTINED after {n} processing failures: {msg}")
                    self.write_status(force=True)
                    return True
                except Exception as e2:  # noqa: BLE001 — not recorded -> not skipped
                    self.store.rollback()
                    self.log(f"!!! could not quarantine height {height}: {e2}; retrying it")
        self.log(f"height {height} failed ({kind}, processing failures {n}): {msg}; retrying the same height in "
                 f"{delay:.0f}s")
        self.write_status()
        self._wait(delay)
        return False

    def _burst(self, height: int) -> bool:
        return all(self.store.is_quarantined(h) for h in range(height - QUARANTINE_BURST, height))

    def reprocess_quarantine(self, height: int | None = None) -> dict[str, Any]:
        """Re-run quarantined heights (after the bug is fixed): fetch again, process in one transaction each
        (INSERT OR IGNORE: no duplicates). Success -> the row becomes 'ok', quarantined_at / quarantine_error stay
        for history. A height that still fails stays in quarantine (its error is updated). Then the snapshots that
        were waiting for these heights, and launchbot owners that could not be found without them."""
        open_ = self.store.quarantined_open()
        heights = [h for h, _e, _a in open_ if height is None or h == height]
        if height is not None and not heights:
            raise ValueError(f"height {height} is not in quarantine")
        ok, failed = [], []
        self.log(f"reprocess-quarantine: {len(heights)} height(s)")
        try:
            for h in heights:
                if self.stop.is_set():
                    break
                block = self._retry(f"reprocess {h}", lambda h=h: self.client.block(h))
                try:
                    self.process(h, block, live=False)
                except Exception as e:  # noqa: BLE001 — still failing: stays quarantined, shown
                    msg = f"{type(e).__name__}: {self.client.keys.scrub(str(e))}"
                    self.store.record_error(h, msg, processing=is_processing_error(e))
                    self.store.begin()
                    self.store.con.execute("UPDATE blocks SET quarantine_error = ? WHERE height = ?", (msg[:2000], h))
                    self.store.commit()
                    failed.append({"height": h, "error": msg[:300]})
                    self.log(f"reprocess-quarantine: height {h} still fails: {msg}")
                    continue
                ok.append(h)
                self.log(f"reprocess-quarantine: height {h} ok")
        except Stopped:
            pass
        owners = L.resolve_late_owners(self.store, ok) if ok else []
        for tok in owners:
            S.rebuild_snapshots(self.store, token=tok)          # their dev is known now
        O.refresh_touched_committed(self.store, self.log)
        self._snapshots_catch_up()                               # windows that waited for these heights
        result = {"at": utcnow_iso(), "requested": len(heights), "ok": ok, "failed": failed,
                  "owners_resolved": owners, "still_quarantined": len(self.store.quarantined_open())}
        self.store.begin()
        try:
            self.store.set_status("quarantine_reprocess_last", result)
            self.store.commit()
        except BaseException:
            self.store.rollback()
            raise
        return result


def extend_start(store: Store, to: int) -> dict[str, Any]:
    """Lower the recorded start height (never raise it). The running collector notices within CURSOR_EVERY_S and
    fills [to, old start) in order before continuing at the tip. The change is recorded in collector_status."""
    st = store.get_status()
    old = st.get("start_height")
    if not isinstance(old, int):
        raise ValueError("no start height yet: the collector has not started on this database")
    if to > old:
        raise ValueError(f"extend-start only lowers the start: {to} > current start {old}")
    if to == old:
        return {"start_height": old, "changed": False}
    if to < 1:
        raise ValueError("bad height")
    changes = st.get("start_height_changes") or []
    changes.append({"from": old, "to": to, "at": utcnow_iso()})
    store.begin()
    try:
        store.set_status("start_height", to)
        store.set_status("start_height_changes", changes)
        store.commit()
    except BaseException:
        store.rollback()
        raise
    return {"start_height": to, "previous": old, "changed": True, "gap_heights": old - to}


# ---------------------------------------------------------------- status

def _parse_iso(v: Any) -> dt.datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return dt.datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def _age_s(v: Any, now_ns: int) -> float | None:
    t = _parse_iso(v)
    return round(now_ns / 1e9 - t.timestamp(), 1) if t else None


def _last_error(store: Store, err: Any, head_seen_at: str | None) -> Any:
    """last_error + resolved_at: the time its height became ok / null (or, for an error without a height, the
    first real head read after it). Never deleted: the history stays readable."""
    if not isinstance(err, dict):
        return err
    out = dict(err)
    h = err.get("height")
    if isinstance(h, int):
        row = store.con.execute("SELECT status, processed_at FROM blocks WHERE height = ?", (h,)).fetchone()
        out["resolved_at"] = row[1] if row and row[0] in ("ok", "null") else None
    else:
        at, seen = _parse_iso(err.get("at")), _parse_iso(head_seen_at)
        out["resolved_at"] = head_seen_at if at and seen and seen > at else None
    return out


def status(store: Store, now_ns: int | None = None) -> dict[str, Any]:
    s = store.get_status()
    con = store.con
    now_ns = now_ns or time.time_ns()
    upd = s.get("_updated_at") or {}
    last = s.get("last_height")
    last_ts = s.get("last_block_ts_ns")
    head = s.get("final_head")
    start = s.get("start_height")
    # when the head was really read (not merely re-written by a status write); databases before RGY-012: the time
    # of the last write of final_head
    head_seen_at = s.get("final_head_read_at") or upd.get("final_head")
    head_age = _age_s(head_seen_at, now_ns)
    head_stale = head is not None and (head_age is None or head_age > HEAD_STALE_S)
    cursor = store.cursor() if start is not None or last is not None else None
    remaining = store.undone_count(start, head) if isinstance(start, int) and isinstance(head, int) else None
    rate = (s.get("collector") or {}).get("blocks_per_s")
    eta = round(remaining / rate) if remaining and rate else (0 if remaining == 0 else None)
    lag_blocks = max(0, head - last) if isinstance(head, int) and isinstance(last, int) else None
    if head_stale:
        # a stale head cannot tell how far behind we are: unknown, never a reassuring 0
        lag_blocks = remaining = eta = None
    status_written_at = s.get("status_written_at") or (s.get("collector") or {}).get("heartbeat")
    v5 = store.user_version() >= 5
    fpm = None
    if last_ts:
        n = con.execute("SELECT COUNT(*) FROM facts WHERE ts_ns > ?", (last_ts - 600 * 10**9,)).fetchone()[0]
        fpm = round(n / 10, 1)
    launches = []
    if last_ts:
        for token, pad, creator, owner, ts, tx in con.execute(
                "SELECT token, launchpad, creator, owner, ts_ns, tx FROM launches WHERE ts_ns >= ? ORDER BY ts_ns",
                (last_ts - 3600 * 10**9,)):
            launches.append({"token": token, "launchpad": pad, "creator": creator, "owner": owner,
                             "time": iso(ts), "tx": tx})
    if v5:
        retrying = [{"height": h, "attempts": a, "process_errors": p or 0, "since": since, "error": e}
                    for h, a, p, since, e in con.execute(
                        "SELECT height, attempts, process_errors, error_since, error FROM blocks WHERE status = 'error' "
                        "AND quarantined_at IS NULL ORDER BY height LIMIT 20")]
        quarantined = [{"height": h, "error": e, "at": at, "attempts": a} for h, e, at, a in con.execute(
            "SELECT height, quarantine_error, quarantined_at, attempts FROM blocks WHERE status = 'error' AND "
            "quarantined_at IS NOT NULL ORDER BY height LIMIT 50")]
        q_open, q_total = con.execute(
            "SELECT COUNT(CASE WHEN status = 'error' THEN 1 END), COUNT(*) FROM blocks "
            "WHERE quarantined_at IS NOT NULL").fetchone()
    else:
        retrying = [{"height": h, "attempts": a, "error": e} for h, a, e in con.execute(
            "SELECT height, attempts, error FROM blocks WHERE status = 'error' ORDER BY height LIMIT 20")]
        quarantined, q_open, q_total = [], 0, 0
    return {
        "db": str(store.path),
        "schema_version": store.user_version(),
        "start_height": start,
        "last_height": last,
        "last_block_time": iso(last_ts),
        "final_head": head,
        "final_head_seen_at": head_seen_at,
        "final_head_age_s": head_age,
        # null when the head is stale (> HEAD_STALE_S): see final_head_age_s
        "head_stale": head_stale,
        # final_head is the last head the collector read; the last height is written per block and can pass it —
        # never a negative lag
        "lag_blocks": lag_blocks,
        "lag_seconds": round((now_ns - last_ts) / 1e9, 1) if last_ts else None,
        "last_ok_commit_at": s.get("last_ok_commit_at"),
        "status_written_at": status_written_at,
        "status_age_s": _age_s(status_written_at, now_ns),
        "facts_per_minute": fpm,
        "facts_total": con.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        "parse_errors": con.execute("SELECT COUNT(*) FROM parse_errors").fetchone()[0],
        "missing_heights": store.missing_heights(start, cursor) if start is not None and cursor is not None else [],
        "cursor": cursor,
        "catch_up_remaining": remaining,
        "catch_up_eta_s": eta,
        "start_height_changes": s.get("start_height_changes"),
        "retrying_heights": retrying,
        "quarantined_heights": quarantined,
        "quarantined_open": q_open,
        "quarantined_total": q_total,
        "quarantine_reprocess_last": s.get("quarantine_reprocess_last"),
        "snapshots": dict(zip(("rows", "errors"), con.execute(
            "SELECT COUNT(*), COUNT(error) FROM launch_snapshots").fetchone())) if store.user_version() >= 2 else None,
        "cards": ({"total": con.execute("SELECT COUNT(*) FROM cards").fetchone()[0],
                   "last_issued_at": con.execute("SELECT MAX(issued_at) FROM cards").fetchone()[0],
                   "last_error": s.get("cards_last_error")} if store.user_version() >= 7 else None),
        "outcomes": ({"total": con.execute("SELECT COUNT(*) FROM launch_outcomes").fetchone()[0],
                      "final": con.execute("SELECT COUNT(*) FROM launch_outcomes WHERE final = 1").fetchone()[0],
                      "last_error": s.get("outcomes_last_error")} if store.user_version() >= 9 else None),
        "links": ({"built": s.get("links_built"), "last_error": s.get("links_last_error")}
                  if store.user_version() >= 10 else None),
        "autofill": AF.summary(store) if store.user_version() >= 12 else None,
        "null_heights": con.execute("SELECT COUNT(*) FROM blocks WHERE status = 'null'").fetchone()[0],
        "null_recheck_last": s.get("null_recheck_last"),
        "null_recheck_totals": s.get("null_recheck_totals"),
        "last_error": _last_error(store, s.get("last_error"), head_seen_at),
        "status_write_error": s.get("status_write_error"),
        "launches_last_hour": launches,
        "keys": s.get("keys"),
        "http": s.get("http"),
        "collector": s.get("collector"),
    }


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None, key_source: KeySource | None = None, backfill_keys: KeySource | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rugyomi.collector", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="follow the chain (until SIGTERM / Ctrl-C)")
    r.add_argument("--db", type=Path, default=None, help="SQLite file (default $RUGYOMI_DB or ~/.rugyomi/rugyomi.db)")
    r.add_argument("--rps", type=float, default=None, help="max HTTP hops per second (default 8 with a key, 2 without)")
    r.add_argument("--prefetch", type=int, default=MAX_PREFETCH, help=f"blocks fetched ahead when behind (<= {MAX_PREFETCH})")
    r.add_argument("--workers", type=int, default=MAX_WORKERS, help=f"parallel fetchers when behind (<= {MAX_WORKERS})")
    r.add_argument("--base", default=BASE, help=argparse.SUPPRESS)
    s = sub.add_parser("status", help="print the collector status as JSON (reads the DB only)")
    s.add_argument("--db", type=Path, default=None)
    b = sub.add_parser("rebuild-launches", help="recompute the launches table from facts")
    b.add_argument("--db", type=Path, default=None)
    es = sub.add_parser("extend-start", help="lower the start height; the running collector fills the gap in order")
    es.add_argument("--db", type=Path, default=None)
    es.add_argument("--to", type=int, required=True, help="the new (lower) start height")
    rs = sub.add_parser("rebuild-snapshots", help="recompute launch snapshots (T+5/15/30/60 min and T+24 h, with the insider columns) from facts")
    rs.add_argument("--db", type=Path, default=None)
    g = rs.add_mutually_exclusive_group()
    g.add_argument("--token", default=None, help="only this launch token")
    g.add_argument("--since", type=int, default=None, help="only launches at heights >= this")
    ro = sub.add_parser("rebuild-outcomes", help="recompute the stored outcome labels (launch_outcomes) with the config in force")
    ro.add_argument("--db", type=Path, default=None)
    ro.add_argument("--token", default=None, help="only this launch token")
    rl = sub.add_parser("rebuild-links", help="recompute the link tables (linked accounts, funding clusters) from facts and history")
    rl.add_argument("--db", type=Path, default=None)
    q = sub.add_parser("reprocess-quarantine", help="re-run quarantined heights (after the bug is fixed)")
    q.add_argument("--db", type=Path, default=None)
    q.add_argument("--height", type=int, default=None, help="only this height")
    q.add_argument("--rps", type=float, default=4.0, help="max HTTP hops per second (the collector runs alongside)")
    q.add_argument("--base", default=BASE, help=argparse.SUPPRESS)
    n = sub.add_parser("recheck-nulls", help="re-request every height stored as null; recover blocks that exist")
    n.add_argument("--db", type=Path, default=None)
    n.add_argument("--since", type=int, default=None, help="only heights >= this")
    n.add_argument("--rps", type=float, default=4.0, help="max HTTP hops per second (the collector runs alongside)")
    n.add_argument("--base", default=BASE, help=argparse.SUPPRESS)
    sc = sub.add_parser("score", help="compute the risk card of a launch as of its launch time; writes nothing")
    sc.add_argument("--db", type=Path, default=None)
    sc.add_argument("--token", required=True)
    ii = sub.add_parser("import-launch-index", help="import the chain index of all launches (idempotent)")
    ii.add_argument("--db", type=Path, default=None)
    ii.add_argument("csv", type=Path, help="launch_index.csv")
    ii.add_argument("items", type=Path, nargs="?", default=None, help="nearly_items.jsonl (optional)")
    ib = sub.add_parser("ingest-blocks", help="run the block files of a directory through the parser (history; "
                                              "launch_only coverage), then delete them")
    ib.add_argument("--db", type=Path, default=None)
    ib.add_argument("dir", type=Path)
    ib.add_argument("--keep", action="store_true", help="do not delete the files")
    ib.add_argument("--limit", type=int, default=None, help="at most this many blocks")
    args = ap.parse_args(argv)
    db = args.db or default_db()

    if args.cmd in ("import-launch-index", "ingest-blocks"):
        from . import history as H
        store = open_store(db)
        try:
            if args.cmd == "import-launch-index":
                res = H.import_launch_index(store, args.csv, args.items)
            else:
                res = H.ingest_blocks(store, args.dir, delete=not args.keep, limit=args.limit,
                                      log=lambda m: print(m, file=sys.stderr))
            print(json.dumps(res, indent=1))
        finally:
            store.close()
        return 0

    if args.cmd == "score":
        from .rules import compute_card
        if not db.exists():
            print(json.dumps({"db": str(db), "error": "no database yet"}))
            return 1
        store = open_store(db, readonly=True)
        try:
            print(json.dumps(compute_card(store, args.token), indent=1, ensure_ascii=False))
        except (LookupError, ConfigError) as e:
            print(json.dumps({"error": str(e)}))
            return 1
        finally:
            store.close()
        return 0

    if args.cmd == "status":
        if not db.exists():
            print(json.dumps({"db": str(db), "error": "no database yet"}))
            return 1
        store = open_store(db, readonly=True)
        try:
            print(json.dumps(status(store), indent=1))
        finally:
            store.close()
        return 0

    if args.cmd == "extend-start":
        store = open_store(db)
        try:
            print(json.dumps(extend_start(store, args.to)))
        except ValueError as e:
            print(json.dumps({"error": str(e)}))
            return 2
        finally:
            store.close()
        return 0

    if args.cmd == "rebuild-snapshots":
        store = open_store(db)
        try:
            print(json.dumps(S.rebuild_snapshots(store, token=args.token, since=args.since)))
            O.refresh_touched_committed(store, print)           # the stored labels follow the snapshots they come from
        finally:
            store.close()
        return 0

    if args.cmd == "rebuild-outcomes":
        store = open_store(db)
        try:
            print(json.dumps(O.rebuild_outcomes(store, token=args.token)))
        except ConfigError as e:
            print(json.dumps({"error": f"ConfigError: {e}"}))
            return 2
        finally:
            store.close()
        return 0

    if args.cmd == "rebuild-links":
        store = open_store(db)
        try:
            print(json.dumps(K.rebuild_links(store)))
        except ConfigError as e:
            print(json.dumps({"error": f"ConfigError: {e}"}))
            return 2
        finally:
            store.close()
        return 0

    if args.cmd == "rebuild-launches":
        store = open_store(db)
        try:
            n = L.rebuild_launches(store)
            print(json.dumps({"launches": n}))
        finally:
            store.close()
        return 0

    keys = key_source or EnvKeySource()
    stop = threading.Event()
    rps = args.rps or (8.0 if keys.current() else 2.0)
    if args.cmd in ("recheck-nulls", "reprocess-quarantine"):
        client = Neardata(keys, base=args.base, rps=rps, stop=stop)
        store = open_store(db)
        old = {sig: signal.signal(sig, lambda *_a: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            col = Collector(store, client)
            if args.cmd == "recheck-nulls":
                res = col.recheck_nulls(args.since)
            else:
                try:
                    res = col.reprocess_quarantine(args.height)
                except ValueError as e:
                    print(json.dumps({"error": str(e)}))
                    return 2
            print(json.dumps(res, indent=1))
        finally:
            for sig, h in old.items():
                signal.signal(sig, h)
            keys.flush()
            store.close()
        return 0
    if not keys.current():
        print("!!! no FastNear key: the free per-IP limit is not enough to follow the chain for long", file=sys.stderr)
    client = Neardata(keys, base=args.base, rps=rps, stop=stop)
    store = open_store(db)

    def on_signal(signum, _frame):  # noqa: ANN001
        print(f"{utcnow_iso()} signal {signum}: finishing the current block", file=sys.stderr, flush=True)
        stop.set()

    old = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        Collector(store, client, prefetch=args.prefetch, workers=args.workers, backfill_keys=backfill_keys,
                  state_dir=Path(os.environ.get("RUGYOMI_STATE_DIR") or Path.home() / ".rugyomi")).run()
    finally:
        for sig, h in old.items():
            signal.signal(sig, h)
        keys.flush()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
