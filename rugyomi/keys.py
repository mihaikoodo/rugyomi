"""Where the FastNear API key comes from.

Public code reads ONE key, `FASTNEAR_API_KEY` (environment; `key` or `key:budget`, the budget is ignored here).
A deployment can pass any other object with the same methods (`KeySource`) to the collector — e.g. a pool
that rotates several keys. Keys are never printed, logged or stored: only fingerprints (sha256, 12 hex).
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Protocol

REJECT_STATUSES = (401, 402, 403)


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


class KeySource(Protocol):
    def current(self) -> str | None: ...                                # the key to use now; None = no key left
    def spend(self, key: str | None, n: int = 1) -> None: ...           # n HTTP hops sent with this key
    def rejected(self, key: str, status: int | None, body_snippet: str = "") -> None: ...   # 401/402/403
    def flush(self) -> bool: ...                                        # persist counters, if any
    def status(self) -> list[dict]: ...                                 # per fingerprint, never the key
    def scrub(self, text: str) -> str: ...                              # remove any key from a text


class EnvKeySource:
    """One key from FASTNEAR_API_KEY. Rejected by the server -> no key for the rest of the run."""

    def __init__(self, environ: dict | None = None) -> None:
        env = os.environ if environ is None else environ
        raw = (env.get("FASTNEAR_API_KEY") or "").strip().strip('"').strip("'").strip()
        key, sep, rest = raw.rpartition(":")
        self._key = key.strip() if sep and key.strip() and rest.strip().isdigit() else (raw or None)
        self._lock = threading.Lock()
        self._spent = 0
        self._rejected: dict | None = None

    def current(self) -> str | None:
        with self._lock:
            return None if self._rejected else self._key

    def spend(self, key: str | None, n: int = 1) -> None:
        if key:
            with self._lock:
                self._spent += n

    def rejected(self, key: str, status: int | None, body_snippet: str = "") -> None:
        with self._lock:
            self._rejected = {"status": status}

    def flush(self) -> bool:
        return True

    def status(self) -> list[dict]:
        if not self._key:
            return []
        with self._lock:
            return [{"fingerprint": fingerprint(self._key), "spent_this_run": self._spent,
                     "status": "rejected" if self._rejected else ("active" if self._spent else "unused"),
                     "rejected_status": (self._rejected or {}).get("status")}]

    def scrub(self, text: str) -> str:
        return text.replace(self._key, "<key>") if self._key and self._key in text else text
