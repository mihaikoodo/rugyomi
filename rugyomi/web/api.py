"""RGY-067: the read-only JSON API (`/api/v1/...`) and the SVG badge (`/badge/{token}.svg`), on the same host as the pages.

  GET /api/v1/launch/{token}      one launch: its risk card (live or computed afterwards), reasons with evidence, outcome, ledger hash
  GET /api/v1/launches            newest first: ?since=<unix seconds | ISO 8601 UTC>&limit=<1..200, default 50>
  GET /api/v1/account/{account}   labels, the account's launches as a creator (count, outcomes), the trader summary (no rows)
  GET /api/v1/meta                live profile, bands, recent rug rate, disclaimer, links
  GET /badge/{token}.svg          ?style=a|b  (a pill / the lens of the logo); style b also ?theme=light|dark (default dark)

Nothing here is computed again: every number comes from the same query functions as the pages (`web/queries.py`), the card is the stored
card. GET and HEAD only; ids are validated like the page routes (`fmt.valid_account`); a bad id is a 404 that never echoes it. Every JSON
body carries `"api": "v1"`; every error is `{"api": "v1", "error": "..."}`. No write endpoint, no key, no cookie.
Headers (set in `app.py` from `is_public_path`): `Access-Control-Allow-Origin: *` (GET/HEAD only), `Cache-Control: public, max-age=60` for a scored answer and `no-store` for every
error and for a known launch that has no card yet (RGY-076: an unknown or pending answer must not be cached), and
`Cross-Origin-Resource-Policy: cross-origin` so an `<img>` on another site may show the badge; every other security header stays.
"""
from __future__ import annotations

import datetime as dt
import re
import sqlite3
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .. import retro as retro_mod
from .. import rules
from ..rules.accuracy import outcome_state
from ..safe_text import safe_symbol
from . import badge, fmt, queries

API_VERSION = "v1"
PUBLIC_PREFIXES = ("/api/", "/badge/")
MAX_LIMIT, DEFAULT_LIMIT = 200, 50
DISCLAIMER = "Analysis, not advice."
_SINCE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?Z?$")
OUTCOME_KEYS = ("rug", "no_rug", "undefined", "waiting", "not_tracked")


def is_public_path(path: str) -> bool:
    return path.startswith(PUBLIC_PREFIXES)


def _body(**kw: Any) -> dict[str, Any]:
    return {"api": API_VERSION, **kw}


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse(_body(error=message), status_code=status)


def _iso(ts_ns: int | None) -> str | None:
    return dt.datetime.fromtimestamp(ts_ns / 1e9, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts_ns else None


def parse_since(raw: str) -> int | None:
    """Unix seconds (digits) or an ISO 8601 UTC date / date-time -> nanoseconds; None for anything else."""
    raw = raw.strip()
    if raw.isascii() and raw.isdigit() and len(raw) <= 11:
        return int(raw) * 1_000_000_000
    m = _SINCE_RE.match(raw)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    try:
        return int(dt.datetime(y, mo, d, h, mi, s, tzinfo=dt.timezone.utc).timestamp()) * 1_000_000_000
    except (ValueError, OverflowError):
        return None


def outcome_of(o: dict[str, Any] | None) -> dict[str, Any] | None:
    """The stored outcome of a launch in plain fields (the same state text as the pages)."""
    if not o:
        return None
    rug = o.get("rug") or {}
    val = rug.get("value")
    state = outcome_state({"rug": None if val is None else int(bool(val)), "final": o.get("final")})
    short = ((o.get("rug_fast") or {}).get("short") or (o.get("rug_late") or {}).get("short")) if val else None
    return {"state": state, "final": bool(o.get("final")), "rug": None if val is None else bool(val), "reason": rug.get("reason"),
            "short": short, "label_version": o.get("label_version"),
            "computed_at": o.get("computed_at")}


def register(app: FastAPI, pool: Any, route: Callable[..., Any], with_: Callable[[Any, Callable[[Any], Any]], Any],
             ledger_state: Callable[[], dict[str, Any]], slow_cache: Any, site_base: Callable[[], str]) -> None:
    """Adds the routes to `app`. `route` = the site's GET+HEAD decorator; `with_` = run a function on a read-only store."""

    def url(path: str) -> str:
        return site_base().rstrip("/") + path

    def evidence(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        out = []
        for ev in items or []:
            href = fmt.evidence_href(ev)
            out.append({"kind": ev.get("kind"), "id": ev.get("id"), "url": (href if href and href.startswith("http") else url(href)) if href else None})
        return out

    def reasons(card: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"id": r.get("id"), "points": r.get("contribution"), "text": r.get("reason"), "short": r.get("short"),
                 "evidence": evidence(r.get("evidence"))}
                for r in card.get("reasons", []) if r.get("contribution")]               # the page lists the reasons that gave points

    def hashes() -> dict[str, str]:
        return slow_cache.get("api_hashes", lambda: {e["token"]: e["hash"] for e in ledger_state()["entries"]})

    def risk(card: dict[str, Any] | None, retro: dict[str, Any] | None, token: str) -> dict[str, Any] | None:
        if card:
            return {"kind": "live", "score": card["score"], "band": card["band"], "issued_at": card["issued_at"], "rules_version": card["rules_version"],
                    "config_version": card["config_version"], "config_hash": card["config_hash"], "ledger_hash": hashes().get(token),
                    "reasons": reasons(card)}
        if retro:
            return {"kind": "retro", "score": retro["score"], "band": retro["band"], "as_of": _iso(retro["launch_ts_ns"]), "computed_at": retro["computed_at"],
                    "rules_version": retro["rules_version"], "config_version": retro["config_version"], "config_hash": retro["config_hash"],
                    "in_sample": retro["in_sample"], "note": retro_mod.BANNER, "reasons": reasons(retro)}
        return None

    def j(body: dict[str, Any]) -> JSONResponse:
        return JSONResponse(body)

    @route("/api/v1/launch/{token}")
    def api_launch(token: str):
        if not fmt.valid_account(token):
            return error_response(404, "not found")

        def work(s):
            d = queries.launch(s.con, token)
            if d is None:
                return None
            card = queries.card(s.con, token)
            retro = queries.retro_card(s.con, token) if card is None else None
            return d, card, retro, queries.outcome(s.con, token)

        res = with_(pool, work)
        if res is None:
            return error_response(404, "not found")
        d, card, retro, o = res
        rk = risk(card, retro, token)
        resp = j(_body(token=token, launchpad=d["launchpad"], symbol=safe_symbol(d.get("symbol"), token), creator=d["creator"], owner=d["owner"],
                       launched_at=_iso(d["ts_ns"]), coverage=d["coverage"], quote_token=d["quote_token"] or None,
                       risk=rk, outcome=outcome_of(o), site_url=url(f"/launch/{token}")))
        if rk is None:                                                              # RGY-076: pending (no card yet): the card appears within minutes, do not cache «no risk yet»
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @route("/api/v1/launches")
    def api_launches(request: Request):
        qp = request.query_params
        limit = DEFAULT_LIMIT
        if "limit" in qp:
            raw = qp["limit"].strip()
            if not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= MAX_LIMIT):
                return error_response(400, f"bad limit (1..{MAX_LIMIT})")
            limit = int(raw)
        since_ns = 0
        if "since" in qp:
            since_ns = parse_since(qp["since"])
            if since_ns is None:
                return error_response(400, "bad since (unix seconds or ISO 8601 UTC)")

        def work(s):
            con = s.con
            has_cards, has_out = queries._has(con, "cards"), queries._has(con, "launch_outcomes")
            cov = "l.coverage" if queries._has_col(con, "launches", "coverage") else "NULL"
            rows = con.execute(
                "SELECT l.token, l.launchpad, l.creator, l.owner, l.ts_ns, " + cov + ", " + ("c.score, c.band" if has_cards else "NULL, NULL")
                + ", " + ("o.rug, o.final" if has_out else "NULL, NULL") + " FROM launches l "
                + ("LEFT JOIN cards c ON c.token = l.token " if has_cards else "") + ("LEFT JOIN launch_outcomes o ON o.token = l.token " if has_out else "")
                + "WHERE l.ts_ns >= ? ORDER BY l.ts_ns DESC LIMIT ?", (since_ns, limit)).fetchall()
            return rows, retro_mod.retro_scores(con, [r[0] for r in rows if r[6] is None])

        rows, retros = with_(pool, work)
        out = []
        for token, pad, creator, owner, ts, cov, score, band, rug, final in rows:
            if score is not None:
                rk = {"kind": "live", "score": score, "band": band}
            elif token in retros:
                rk = {"kind": "retro", "score": retros[token][0], "band": retros[token][1]}
            else:
                rk = None
            out.append({"token": token, "launchpad": pad, "creator": creator, "launched_at": _iso(ts), "risk": rk,
                        "outcome": outcome_state({"rug": rug, "final": final}) if (final is not None or rug is not None) else ("waiting" if cov in (None, "live") else None),
                        "site_url": url(f"/launch/{token}")})
        return j(_body(count=len(out), limit=limit, launches=out))

    @route("/api/v1/account/{account}")
    def api_account(account: str):
        if not fmt.valid_account(account):
            return error_response(404, "not found")

        def work(s):
            return queries.creator_page(s, account, 0) if queries.known_account(s.con, account) else None      # 0 rows: the summary counts all of them

        c = with_(pool, work)
        if c is None:
            return error_response(404, "not found")
        counts = dict.fromkeys(OUTCOME_KEYS, 0)
        for r in c["launches"]:
            counts[{"no rug": "no_rug", None: "not_tracked"}.get(r.get("state"), r.get("state"))] += 1
        t = c["trader"]
        trader = None if t is None else {"summary": t["summary"], "group": t["group"], "pairs": t["pairs"], "wallet": t["wallet"],
                                         "window_min": t["window_min"], "top_n": t["top_n"], "rows_url": url(f"/creator/{account}")}
        return j(_body(account=account, created=c["created"], labels=[{k: lab.get(k) for k in ("label", "text", "reason", "source", "set_at")} for lab in c["labels"]],
                       launches_as_creator={"count": len(c["launches"]), "outcomes": counts}, trader=trader, site_url=url(f"/creator/{account}")))

    @route("/api/v1/meta")
    def api_meta():
        def make():
            def work(s):
                con = s.con
                r = con.execute("SELECT rules_version, config_version FROM cards ORDER BY issued_height DESC, issued_at DESC LIMIT 1").fetchone() \
                    if queries._has(con, "cards") else None
                return r, queries.market(con)
            return with_(pool, work)

        r, m = slow_cache.get("api_meta", make)
        bands = []
        for b in rules.methodology(r[0] if r else None)["bands"]:                  # the bands of the profile that scores new launches, as /methodology shows them
            lo, hi = (int(x) for x in re.findall(r"\d+", b["range"]))
            bands.append({"name": b["name"], "from": lo, "to": hi, "color": badge.COLORS.get(b["name"])})
        return j(_body(live_profile={"rules_version": r[0] if r else None, "config_version": r[1] if r else None}, bands=bands,
                       recent_rug_rate={k: m.get(k) for k in ("days", "n", "rugs", "rate", "lo", "hi", "too_few", "text")},
                       disclaimer=DISCLAIMER, methodology_url=url("/methodology"), accuracy_url=url("/accuracy"), ledger_url=url("/ledger.jsonl")))

    @route("/badge/{name}")
    def badge_svg(name: str, request: Request):
        style = "b" if request.query_params.get("style") == "b" else "a"
        theme = "light" if request.query_params.get("theme") == "light" else "dark"          # style b only; any other value: dark
        token = name[:-4] if name.endswith(".svg") else ""
        if not fmt.valid_account(token):
            return svg(badge.render(style, badge.UNKNOWN, theme), 404)

        def work(s):
            d = queries.launch(s.con, token)
            if d is None:
                return None
            card = queries.card(s.con, token)
            retro = queries.retro_card(s.con, token) if card is None else None
            return (card and {"score": card["score"], "band": card["band"], "kind": "live"}) or \
                   (retro and {"score": retro["score"], "band": retro["band"], "kind": "retro"}) or {}

        res = with_(pool, work)
        if res is None:
            return svg(badge.render(style, badge.UNKNOWN, theme), 404)
        return svg(badge.render(style, res or None, theme), 200, pending=not res)           # RGY-076: «pending» (no card yet) is not cached either

    def svg(text: str, status: int, pending: bool = False) -> Response:
        return Response(text, status_code=status, media_type="image/svg+xml", headers={"Cache-Control": "no-store"} if pending else None)


__all__ = ["register", "is_public_path", "error_response", "parse_since", "outcome_of", "API_VERSION"]
