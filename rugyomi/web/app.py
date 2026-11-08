"""The read-only site (RGY-014): FastAPI + Jinja2, server-rendered, no JavaScript beyond self-hosted htmx.

  /                       feed of the latest live launches (refreshes itself every ~10 s)
  /launch/{token}         launch facts, the four snapshots, the creator's other launches
  /creator/{account}      every launch of the account (all sources), account creation, active labels
  /cluster/{source}       a common narrow funding source and the creators it funded (RGY-019)
  /rankings               groups and clusters over the whole history by the outside buyers' losses / number of rugs; clean creators (RGY-023)
  /accuracy               the frozen risk cards against the stored 24 h outcomes (RGY-018), with the base rate
  /healthz                tiny JSON: database readable, last block time
Rules: the database is opened read-only (`mode=ro`, a pool of a few connections, each query has a time limit); no write
endpoints; every query is parameterized; ids are validated; a strict CSP and no external assets; errors never show
a stack trace. Product text is English and describes behaviour, never labels.
"""
from __future__ import annotations

import logging
import os
import queue
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .. import rules
from . import fmt, queries
from ..store import Store, open_store

HERE = Path(__file__).resolve().parent
log = logging.getLogger("rugyomi.web")

CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SECURITY_HEADERS = {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY", "Cross-Origin-Resource-Policy": "same-origin"}
QUERY_TIMEOUT_S = 5.0
FEED_TTL_S = 3.0
FEED_LIMIT = 100
ACCURACY_TTL_S = 30.0
POOL_SIZE = 3


def default_db() -> Path:
    return Path(os.environ.get("RUGYOMI_DB") or Path.home() / ".rugyomi" / "rugyomi.db")


class ReadPool:
    """A few read-only connections. A query that runs longer than `timeout` is interrupted (progress handler)."""

    def __init__(self, path: Path, size: int = POOL_SIZE, timeout: float = QUERY_TIMEOUT_S) -> None:
        self.path, self.timeout = Path(path), timeout
        self._q: queue.Queue[Store] = queue.Queue()
        self._stores: list[Store] = []
        self._size = size
        self._lock = threading.Lock()

    def _new(self) -> Store:
        s = open_store(self.path, readonly=True)
        s.con.execute("PRAGMA query_only=ON")
        self._stores.append(s)
        return s

    @contextmanager
    def store(self) -> Iterator[Store]:
        try:
            s = self._q.get_nowait()
        except queue.Empty:
            with self._lock:
                s = self._new() if len(self._stores) < self._size else None
            if s is None:
                s = self._q.get(timeout=self.timeout)
        deadline = time.monotonic() + self.timeout
        s.con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100_000)
        try:
            yield s
        finally:
            s.con.set_progress_handler(None, 0)
            self._q.put(s)

    def close(self) -> None:
        for s in self._stores:
            s.close()


class TTL:
    """A tiny in-process cache: one value per key for a few seconds."""

    def __init__(self, ttl: float) -> None:
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key: Any, make) -> Any:
        now = time.monotonic()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        v = make()
        with self._lock:
            self._d[key] = (now, v)
        return v


def create_app(db_path: str | Path | None = None) -> FastAPI:
    pool = ReadPool(Path(db_path) if db_path else default_db())
    cache = TTL(FEED_TTL_S)
    slow_cache = TTL(ACCURACY_TTL_S)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.autoescape = True
    env.globals.update(utc=fmt.utc, near=fmt.near, signed_near=fmt.signed_near, pct=fmt.pct_bps, secs=fmt.seconds,
                       mult=fmt.mult, nf=fmt.nf, snf=fmt.snf, ratio=fmt.ratio, duration=fmt.duration, short=fmt.short, tx_url=fmt.tx_url, address_url=fmt.address_url,
                       launchpad=fmt.launchpad, coverage_text=lambda c: fmt.COVERAGE_TEXT.get(c, c),
                       evidence_href=fmt.evidence_href, evidence_label=fmt.evidence_label, minute_label=fmt.minute_label)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        pool.close()

    app = FastAPI(title="rugyomi", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.pool = pool
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        try:
            resp = await call_next(request)
        except Exception:                                                     # noqa: BLE001 — never a trace to the client
            log.exception("unhandled error on %s", request.url.path)
            resp = HTMLResponse(templates.get_template("error.html").render(request=request, code=500,
                                                                            message="Something went wrong. Try again in a moment."),
                                status_code=500)
        for k, v in SECURITY_HEADERS.items():
            resp.headers[k] = v
        resp.headers.setdefault("Cache-Control", "public, max-age=3600" if request.url.path.startswith("/static/") else "no-store")
        return resp

    def page(request: Request, name: str, status: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    def not_found(request: Request, what: str) -> HTMLResponse:
        return page(request, "error.html", 404, code=404, message=what)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        rows = cache.get("feed", lambda: _with(pool, lambda s: queries.feed(s.con, FEED_LIMIT)))
        return page(request, "feed.html", rows=rows, now_ns=time.time_ns())

    @app.get("/partials/feed", response_class=HTMLResponse)
    def feed_partial(request: Request):
        rows = cache.get("feed", lambda: _with(pool, lambda s: queries.feed(s.con, FEED_LIMIT)))
        return page(request, "_feed_table.html", rows=rows)

    @app.get("/launch/{token}", response_class=HTMLResponse)
    def launch_page(request: Request, token: str):
        if not fmt.valid_account(token):
            return not_found(request, "No such launch.")

        def work(s: Store):
            d = queries.launch(s.con, token)
            if d is None:
                return None
            owner = d["owner"] or d["creator"]
            others = [r for r in queries.creator_page(s, owner)["launches"] if r["token"] != token] if owner else []
            snaps = queries.snapshots(s.con, token)
            card = queries.card(s.con, token)
            return d, snaps, others, card, queries.tax_options(s.con, d), queries.outcome(s.con, token), queries.chance(s.con, card)

        res = _with(pool, work)
        if res is None:
            return not_found(request, "No such launch.")
        d, snaps, others, card, tax, rug, chance = res
        return page(request, "launch.html", l=d, snaps=snaps, others=others, card=card, tax=tax, rug=rug, chance=chance, note=rules.NOTE)

    @app.get("/creator/{account}", response_class=HTMLResponse)
    def creator(request: Request, account: str, all: int = 0):
        if not fmt.valid_account(account):
            return not_found(request, "No such account.")

        def work(s: Store):
            return queries.creator_page(s, account) if queries.known_account(s.con, account) else None

        c = _with(pool, work)
        if c is None:
            return not_found(request, "No such account.")
        ln = _with(pool, lambda s: queries.creator_links(s, account))
        d = _with(pool, lambda s: queries.dossier(s.con, account, 100_000 if all else 100))
        return page(request, "creator.html", c=c, ln=ln, d=d, show_all=bool(all))

    @app.get("/cluster/{source}", response_class=HTMLResponse)
    def cluster_page(request: Request, source: str):
        if not fmt.valid_account(source):
            return not_found(request, "No such cluster.")
        c = _with(pool, lambda s: queries.cluster(s.con, source))
        if c is None:
            return not_found(request, "No such cluster.")
        return page(request, "cluster.html", c=c)

    @app.get("/rankings", response_class=HTMLResponse)
    def rankings_page(request: Request, window: str = "all", launchpad: str = "", view: str = "loss"):
        window = window if window in ("all", "30d", "7d") else "all"
        view = view if view in ("loss", "rugs") else "loss"
        pad = launchpad if launchpad in ("nearly", "justhoot_v1", "justhoot_v2") else None
        r = slow_cache.get(("rankings", window, pad), lambda: _with(pool, lambda s: queries.rankings(s.con, window, pad)))
        return page(request, "rankings.html", r=r, window=window, pad=pad or "", view=view)

    @app.get("/methodology", response_class=HTMLResponse)
    def methodology_page(request: Request):
        return page(request, "methodology.html", m=rules.methodology())

    @app.get("/accuracy", response_class=HTMLResponse)
    def accuracy_page(request: Request):
        a = slow_cache.get("accuracy", lambda: _with(pool, lambda s: queries.accuracy(s.con)))
        return page(request, "accuracy.html", a=a)

    @app.get("/healthz")
    def healthz():
        try:
            h = _with(pool, lambda s: queries.health(s.con))
        except Exception:                                                     # noqa: BLE001
            return JSONResponse({"ok": False, "db": "unreadable"}, status_code=503)
        ts = h["last_block_ts_ns"]
        return {"ok": True, "db": "ok", "schema": h["schema"], "last_height": h["last_height"],
                "last_block_age_s": round(time.time() - ts / 1e9, 1) if isinstance(ts, int) else None}

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        msg = {404: "Page not found.", 405: "Not allowed."}.get(exc.status_code, "Request failed.")
        return HTMLResponse(templates.get_template("error.html").render(request=request, code=exc.status_code, message=msg),
                            status_code=exc.status_code)

    return app


def _with(pool: ReadPool, fn):
    with pool.store() as s:
        try:
            return fn(s)
        except sqlite3.OperationalError as e:
            raise RuntimeError(f"database error: {e}") from None
