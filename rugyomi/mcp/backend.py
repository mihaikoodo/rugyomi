"""Where a tool's GET goes: the live site over HTTPS (standard library only) or, in demo mode, the same FastAPI app on the demo database called in this process.

Both give `await backend.get("/api/v1/launches", {"limit": 5})` -> the decoded JSON object, or raise `ApiError` with a message the agent can read. Nothing but GET exists here."""
from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_BODY = 4 * 1024 * 1024
TIMEOUT_S = 20.0
USER_AGENT = "rugyomi-mcp/0.1 (read-only; +https://mihaikoodo.dev)"
ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")        # the same shape the site accepts for a token / account (NEAR ids, 2-64 chars)


class ApiError(Exception):
    """A failure the agent should read: `status` is the HTTP status (None for a network failure)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def check_id(value: Any, what: str) -> str:
    """A token / account id is checked before it goes into a URL path: no slash, no dots-only, no encoding tricks."""
    if not isinstance(value, str) or not ACCOUNT_RE.match(value.strip().lower()):
        raise ApiError(f"bad {what}: expected a NEAR account id like 'name.nearlytrade.near' (lowercase letters, digits, '.', '-', '_'; 2-64 characters)")
    return value.strip().lower()


def clean_params(params: dict[str, Any] | None) -> dict[str, str]:
    return {k: str(v) for k, v in (params or {}).items() if v is not None}


def decode(status: int, body: bytes, where: str) -> dict[str, Any]:
    """The JSON object of a response, or an ApiError that names the status and the site's own error text."""
    try:
        data = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError):
        data = None
    if status == 200 and isinstance(data, dict):
        return data
    msg = data.get("error") if isinstance(data, dict) else None
    if status == 404:
        raise ApiError("not found: the site has no such record" + (f" ({msg})" if msg and msg != "not found" else "") + f" [HTTP 404, {where}]", 404)
    if status == 200:
        raise ApiError(f"unexpected answer from {where}: not a JSON object", 200)
    raise ApiError(f"{where} answered HTTP {status}" + (f": {msg}" if msg else ""), status)


class HttpBackend:
    def __init__(self, base_url: str, timeout: float = TIMEOUT_S):
        u = urllib.parse.urlsplit(base_url.strip())
        if u.scheme not in ("http", "https") or not u.netloc or u.query or u.fragment or u.username or u.password:
            raise ValueError(f"bad base URL {base_url!r}: expected http(s)://host[:port]")
        self.base = f"{u.scheme}://{u.netloc}" + u.path.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return self.base

    def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = self.base + path + (("?" + urllib.parse.urlencode(params)) if params else "")
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:                       # noqa: S310 - http(s) only, checked in __init__
                body = r.read(MAX_BODY + 1)
                status = r.status
        except urllib.error.HTTPError as e:
            return decode(e.code, e.read(MAX_BODY), self.base)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(f"could not reach {self.base}: {getattr(e, 'reason', e)} (use --demo for the offline demo database)") from None
        if len(body) > MAX_BODY:
            raise ApiError(f"answer from {self.base} is too large", status)
        return decode(status, body, self.base)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._get, path, clean_params(params))


class DemoBackend:
    """The site's own FastAPI app on the demo database, called through ASGI in this process (no socket). The same routes, queries and JSON as the live site."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path
        self._app: Any = None

    def describe(self) -> str:
        return "the demo database (offline)"

    def app(self) -> Any:
        if self._app is None:
            try:
                from ..web.app import create_app
                from ..web.demo import unpack
            except ImportError as e:
                raise ApiError(f"demo mode needs the web extra: pip install -e '.[web]' ({e})") from None
            self._app = create_app(self.db_path or str(unpack()))
        return self._app

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        app = await asyncio.to_thread(self.app)
        qs = urllib.parse.urlencode(clean_params(params)).encode()
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": path, "raw_path": path.encode(),
                 "root_path": "", "query_string": qs, "headers": [(b"host", b"127.0.0.1"), (b"accept", b"application/json")],
                 "client": ("127.0.0.1", 0), "server": ("127.0.0.1", 80)}
        status, chunks, sent = 500, [], False

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            nonlocal status, sent
            if msg["type"] == "http.response.start":
                status = msg["status"]
            elif msg["type"] == "http.response.body":
                chunks.append(msg.get("body", b""))
                sent = True

        await app(scope, receive, send)
        return decode(status, b"".join(chunks), "the demo database")
