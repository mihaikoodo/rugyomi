"""Formatting for the pages: UTC times, NEAR amounts, shares, explorer links. Pure functions, no I/O."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

NEAR = Decimal(10) ** 24
EXPLORER = "https://nearblocks.io"
ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")        # NEAR account ids: 2-64 chars (implicit = 64 hex)


def valid_account(s: str | None) -> bool:
    return bool(s) and bool(ACCOUNT_RE.match(s))


def utc(ts_ns: int | None, seconds: bool = True) -> str:
    if ts_ns is None:
        return "—"
    t = dt.datetime.fromtimestamp(ts_ns / 1e9, dt.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S UTC" if seconds else "%Y-%m-%d %H:%M UTC")


def near(yocto: str | int | None, digits: int = 2) -> str:
    """Yocto-NEAR (string or int) -> '40.00 NEAR'; small non-zero amounts keep 4 decimals; None -> '—'."""
    if yocto in (None, ""):
        return "—"
    try:
        v = Decimal(str(yocto)) / NEAR
    except InvalidOperation:
        return "—"
    if v != 0 and abs(v) < Decimal("0.01"):
        digits = 4
    return f"{v:,.{digits}f} NEAR"


def signed_near(yocto: str | int | None) -> str:
    s = near(yocto)
    return s if s.startswith(("—", "-")) or s.startswith("0.00 ") else "+" + s


def nf(v: float | None, digits: int = 2) -> str:
    """A float amount in NEAR (the derived dossier tables hold floats) -> '40.00 NEAR'; None -> '—'."""
    if v is None:
        return "—"
    if v != 0 and abs(v) < 0.01:
        digits = 4
    return f"{v:,.{digits}f} NEAR"


def snf(v: float | None) -> str:
    s = nf(v)
    return s if s.startswith(("—", "-")) or s.startswith("0.00 ") else "+" + s


def ratio(v: float | None) -> str:
    """0.37 -> '37%'; above 1000% -> '>1000%'; None -> '—'."""
    if v is None:
        return "—"
    return ">1000%" if v > 10 else f"{v * 100:.0f}%"


def duration(sec: float | int | None) -> str:
    if sec is None:
        return "—"
    sec = float(sec)
    return f"{sec:.0f} s" if sec < 120 else f"{sec / 60:.0f} min" if sec < 7200 else f"{sec / 3600:.1f} h" if sec < 172800 else f"{sec / 86400:.1f} d"


def pct_bps(bps: int | None) -> str:
    return "—" if bps is None else f"{bps / 100:.0f}%" if bps % 100 == 0 else f"{bps / 100:.1f}%"


def seconds(v: float | int | None) -> str:
    if v is None:
        return "—"
    v = float(v)
    return f"{v:.0f} s" if v < 120 else f"{v / 60:.1f} min"


def minute_label(minute: int) -> str:
    return "T+24 h" if minute == 1440 else f"T+{minute} min"


def mult(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}x"


def short(account: str | None, n: int = 18) -> str:
    if not account:
        return "—"
    return account if len(account) <= n else account[: n - 7] + "…" + account[-5:]


def tx_url(tx: str | None) -> str | None:
    return f"{EXPLORER}/txns/{tx}" if tx else None


def address_url(account: str) -> str:
    return f"{EXPLORER}/address/{account}"


COVERAGE_TEXT = {"live": "live", "launch_only": "history: launch only", "index_only": "history: index only",
                 "legacy_window": "earlier collection"}
PAD_TEXT = {"nearly": "nearly.trade", "justhoot_v1": "justhoot v1", "justhoot_v2": "justhoot v2"}


def launchpad(name: str | None) -> str:
    return PAD_TEXT.get(name or "", name or "—")


def evidence_href(ev: dict) -> str | None:
    """Where an evidence item of a risk card points: a transaction -> the explorer, an account / a launch -> our pages."""
    kind, id_ = ev.get("kind"), ev.get("id")
    if not id_:
        return None
    if kind == "tx":
        return tx_url(id_)
    if kind == "account" and valid_account(id_):
        return f"/creator/{id_}"
    if kind == "launch" and valid_account(id_):
        return f"/launch/{id_}"
    return None


def evidence_label(ev: dict) -> str:
    kind, id_ = ev.get("kind"), str(ev.get("id") or "")
    return {"tx": "transaction ", "account": "account ", "launch": "launch "}.get(kind, "") + short(id_, 24)
