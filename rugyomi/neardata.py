"""neardata (FastNear) client: final blocks by height and the final head. Standard library only.

  block(h)       GET /v0/block/{h}            -> dict, or None for a height with no block (skipped / not yet)
  final_head()   GET /v0/last_block/final     -> int (read from the redirect Location, not followed: 1 hop)
Keys come from a KeySource (rugyomi.keys) and are sent as the apiKey query parameter on EVERY hop (headers can
be lost on redirects). 1 HTTP hop = 1 credit (`spend`). HTTP 401/402/403 -> `rejected()` and the request is
repeated at once with the next key (or without one if none is left — the collector reports it). HTTP 429
never changes the key: the client waits (Retry-After, else 2 s) and halves its request rate.
Errors other than these raise NeardataError; the caller decides how to retry.
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .keys import REJECT_STATUSES, KeySource, fingerprint

BASE = "https://mainnet.neardata.xyz"
USER_AGENT = "rugyomi-collector/0"


class NeardataError(Exception):
    def __init__(self, msg: str, status: int | None = None) -> None:
        super().__init__(msg)
        self.status = status


class Stopped(Exception):
    """The caller asked to stop: no new hop is started."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


class Pacer:
    """Minimum interval between hops, shared by all threads; the rate halves on 429 and recovers slowly."""

    def __init__(self, rps: float, stop: threading.Event) -> None:
        self.max_rps = rps
        self.rps = rps
        self.stop = stop
        self._next = 0.0
        self._ok = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(self._next, now) + 1.0 / self.rps
        if wait > 0 and self.stop.wait(wait):
            raise Stopped()

    def on_429(self, pause: float) -> None:
        with self._lock:
            self.rps = max(0.5, self.rps / 2)
            self._ok = 0
            self._next = max(self._next, time.monotonic() + pause)

    def on_ok(self) -> None:
        with self._lock:
            self._ok += 1
            if self._ok >= 200 and self.rps < self.max_rps:
                self.rps = min(self.max_rps, self.rps * 1.25)
                self._ok = 0


class Neardata:
    def __init__(self, keys: KeySource, base: str = BASE, rps: float = 8.0, timeout: float = 30.0,
                 stop: threading.Event | None = None) -> None:
        self.keys = keys
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.stop = stop or threading.Event()
        self.pacer = Pacer(rps, self.stop)
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._lock = threading.Lock()
        self.counters = {"hops": 0, "keyless_hops": 0, "http_429": 0, "key_rejections": 0, "redirects": 0}

    def _count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] += n

    def _url(self, url: str, key: str | None) -> str:
        if not key:
            return url
        parts = urllib.parse.urlsplit(url)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if k != "apiKey"] + [("apiKey", key)]
        return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q)))

    def _once(self, path: str, follow: bool) -> tuple[bytes, dict, str | None]:
        key = self.keys.current()
        url = self.base + path
        for _hop in range(5):
            if self.stop.is_set():
                raise Stopped()
            self.pacer.acquire()
            req = urllib.request.Request(self._url(url, key), headers={"Accept-Encoding": "gzip",
                                                                        "User-Agent": USER_AGENT})
            self._count("hops")
            if not key:
                self._count("keyless_hops")
            self.keys.spend(key, 1)
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    return r.read(), dict(r.headers), None
            except urllib.error.HTTPError as e:
                headers = dict(e.headers or {})
                body = e.read() if e.fp else b""
                if e.code in (301, 302, 303, 307, 308):
                    loc = headers.get("Location") or headers.get("location")
                    if not loc:
                        raise NeardataError(f"redirect without Location on {path}", e.code) from None
                    loc = urllib.parse.urljoin(url, loc)
                    if not follow:
                        return b"", headers, loc
                    self._count("redirects")
                    url = loc
                    continue
                # scrub FIRST, then cut: cutting first can leave a partial key at the cut
                snippet = self.keys.scrub(body[:65536].decode("utf-8", "replace"))[:200]
                if e.code in REJECT_STATUSES and key:
                    self._count("key_rejections")
                    self.keys.rejected(key, e.code, snippet)
                    raise _Rejected(f"key {fingerprint(key)} rejected (HTTP {e.code})") from None
                if e.code == 429:
                    ra = headers.get("Retry-After") or headers.get("retry-after")
                    try:
                        pause = float(ra) if ra is not None else 2.0
                    except ValueError:
                        pause = 2.0
                    self._count("http_429")
                    self.pacer.on_429(pause)
                    raise _RateLimited() from None
                raise NeardataError(f"HTTP {e.code} on {path}: {snippet}", e.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                raise NeardataError(f"{type(e).__name__} on {path}: {self.keys.scrub(str(e))}") from None
        raise NeardataError(f"too many redirects on {path}")

    def get(self, path: str, follow: bool = True, max_429: int = 20) -> tuple[bytes, dict, str | None]:
        switches = n429 = 0
        while True:
            try:
                res = self._once(path, follow)
                self.pacer.on_ok()
                return res
            except _Rejected:
                switches += 1
                if switches > 64:
                    raise NeardataError(f"too many key rejections on {path}") from None
            except _RateLimited:
                n429 += 1
                if n429 >= max_429:
                    raise NeardataError(f"HTTP 429 {n429} times in a row on {path}", 429) from None

    def block(self, height: int) -> dict[str, Any] | None:
        body, headers, _ = self.get(f"/v0/block/{height}")
        data = _decode(body, headers)
        if data is None:
            return None
        got = int(data["block"]["header"]["height"])
        if got != height:
            raise NeardataError(f"block {height}: response has height {got}")
        return data

    def final_head(self) -> int:
        body, headers, loc = self.get("/v0/last_block/final", follow=False)
        if loc:
            tail = urllib.parse.urlsplit(loc).path.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                return int(tail)
        data = _decode(body, headers)
        return int(data["block"]["header"]["height"])


class _Rejected(Exception):
    pass


class _RateLimited(Exception):
    pass


def _decode(body: bytes, headers: dict) -> Any:
    enc = (headers.get("Content-Encoding") or headers.get("content-encoding") or "").lower()
    if enc == "gzip" or body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return json.loads(body) if body.strip() else None
