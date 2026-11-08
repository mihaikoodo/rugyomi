"""Telegram publisher (RGY-022): every live risk card goes to the channel, the outcome follows as a reply, once a day a summary.

A SEPARATE process (`rugyomi-publisher` unit), not part of the collector: it reads the collector's database READ-ONLY and writes
only its own small SQLite file (`publisher.db`, `channel_posts`). A Telegram failure can therefore never slow or break block
processing, and there is no second writer on the collector's database. The price: a post appears up to `poll_seconds` after the card.

    python -m rugyomi.publisher run            the loop (the systemd unit)
    python -m rugyomi.publisher once           one pass (tests, a manual check)
    python -m rugyomi.publisher preview TOKEN  print the card post (and the outcome reply, if final) — sends nothing
    python -m rugyomi.publisher preview-daily YYYY-MM-DD
    python -m rugyomi.publisher status

Modes (config `publisher.*`, see publisher.json; the server override is a JSON file named by $RUGYOMI_PUBLISHER_CONFIG):
  enabled false (default)         the loop idles, posts nothing
  enabled true,  dry_run true     posts go to the ADMIN chat(s) ($TG_ALERT_CHAT_ID / $TELEGRAM_ALLOWED_IDS) with a "[dry run]" line
  enabled true,  dry_run false    posts go to the CHANNEL ($TG_CHANNEL_ID) — real, public, never edited
The bot token is $TG_ALERT_BOT_TOKEN or $TELEGRAM_BOT_TOKEN (the existing admin bot, added to the channel as an admin).
Only cards issued after `since` are posted (the moment the mode was first used, unless the config names one): switching on never
floods the channel with the past. History launches have no card and are never posted. Posts are never edited or deleted.

Idempotency: `channel_posts` has one row per (token, kind, chat). A row is written BEFORE the send ("sending") and finished after;
a crash in between leaves "unknown" and that post is NOT repeated (at most once — a duplicate in a public channel is worse than a gap).
A failed send is retried with backoff (and Telegram's `retry_after`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .rules import card_row, outcome_row
from .rules.accuracy import card_outcome_rows, summarize
from .rules.backtest import chance_for_score
from .rules.config import ConfigError, _merge
from .store import open_store

API = "https://api.telegram.org"
HERE = Path(__file__).resolve().parent
CONFIG_ENV = "RUGYOMI_PUBLISHER_CONFIG"
NS = 10**9
NEAR = 10**24
PADS = {"nearly": ("nearly.trade", "https://nearly.trade/{t}"), "justhoot_v2": ("justhoot.fun", "https://justhoot.fun/p/{t}"),
        "justhoot_v1": ("justhoot.fun (v1)", "https://justhoot.fun/p/{t}")}
CARD, OUTCOME, DAILY = "card", "outcome", "daily"
SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_posts(
    token TEXT NOT NULL,                 -- the launch token; for kind 'daily' the UTC day (YYYY-MM-DD)
    kind TEXT NOT NULL CHECK (kind IN ('card','outcome','daily')),
    chat TEXT NOT NULL,
    mode TEXT NOT NULL,                  -- 'channel' | 'dry'
    status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','unknown','skipped')),
    message_id INTEGER,
    reply_to INTEGER,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY (token, kind, chat)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


# ------------------------------------------------------------------------------------------------ config

def load_config(path: str | Path | None = None) -> dict[str, Any]:
    base = json.loads((HERE / "publisher.json").read_text(encoding="utf-8"))
    p = path or os.environ.get(CONFIG_ENV)
    if not p:
        return base
    try:
        over = json.loads(Path(p).read_text(encoding="utf-8"))
        return _merge(base, over)
    except FileNotFoundError:
        return base                                            # no override file yet = the defaults (publisher off)
    except (OSError, ValueError, ConfigError) as e:
        raise ConfigError(f"{p}: {e}") from None


def env_targets(environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ

    def get(*names: str) -> str:
        for n in names:
            v = (env.get(n) or "").strip().strip('"').strip("'")
            if v:
                return v
        return ""
    chats = get("TG_ALERT_CHAT_ID", "TELEGRAM_ALLOWED_IDS")
    return {"token": get("TG_ALERT_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"), "channel": get("TG_CHANNEL_ID"),
            "admins": [c.strip() for c in chats.replace(";", ",").split(",") if c.strip()]}


# ------------------------------------------------------------------------------------------------ Telegram

class TelegramError(RuntimeError):
    def __init__(self, msg: str, retry_after: float = 0.0, permanent: bool = False) -> None:
        super().__init__(msg)
        self.retry_after, self.permanent = retry_after, permanent


class Telegram:
    """sendMessage only. `transport(url, payload, timeout) -> dict` is injectable (tests); the token never appears in an error."""

    def __init__(self, token: str, api: str = API, timeout: float = 15.0,
                 transport: Callable[[str, dict, float], dict] | None = None) -> None:
        self.token, self.api, self.timeout = token, api.rstrip("/"), timeout
        self.transport = transport or self._http

    def scrub(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    @staticmethod
    def _http(url: str, payload: dict, timeout: float) -> dict:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except ValueError:
                return {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}"}

    def send(self, chat: str, text: str, reply_to: int | None = None) -> int:
        payload: dict[str, Any] = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if reply_to:
            payload["reply_to_message_id"] = reply_to
            payload["allow_sending_without_reply"] = True
        try:
            ans = self.transport(f"{self.api}/bot{self.token}/sendMessage", payload, self.timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise TelegramError(self.scrub(f"{type(e).__name__}: {e}")) from None
        if ans.get("ok"):
            return int(ans["result"]["message_id"])
        code = int(ans.get("error_code") or 0)
        ra = float((ans.get("parameters") or {}).get("retry_after") or 0)
        desc = self.scrub(str(ans.get("description"))[:200])
        raise TelegramError(f"Telegram {code}: {desc}", retry_after=ra, permanent=code in (400, 401, 403, 404) and not ra)


# ------------------------------------------------------------------------------------------------ reading the collector's database

def short(account: str | None, n: int = 18) -> str:
    if not account:
        return "—"
    return account if len(account) <= n else account[: n - 7] + "…" + account[-5:]


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def read_launch(con: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    r = con.execute("SELECT token, launchpad, creator, extra FROM launches WHERE token = ?", (token,)).fetchone()
    if r is None:
        return None
    extra = json.loads(r[3]) if r[3] else {}
    return {"token": r[0], "launchpad": r[1], "creator": r[2], "symbol": extra.get("symbol")}


def read_chance(con: sqlite3.Connection, card: dict[str, Any]) -> dict[str, Any] | None:
    """"Rug chance ≈ X %": only for a card of rules v1 (the same gate as the site: v0 scores are not probabilities)."""
    if not str(card.get("rules_version", "")).startswith("1") or not _has(con, "collector_status"):
        return None
    row = con.execute("SELECT value FROM collector_status WHERE key = 'calibration_history'").fetchone()
    try:
        hist = json.loads(row[0]) if row and row[0] else None
    except ValueError:
        return None
    return chance_for_score(hist if isinstance(hist, dict) and "profiles" in hist else None, int(card["score"]), "v1")


# ------------------------------------------------------------------------------------------------ formatting (pure)

def _utc(ns: int | None = None, iso: str | None = None) -> str:
    if iso:
        return iso[:16].replace("T", " ") + " UTC"
    return dt.datetime.fromtimestamp(ns / NS, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _near(v: float | None, signed: bool = False) -> str:
    return "n/a" if v is None else (f"{v:+,.2f}" if signed else f"{v:,.2f}") + " NEAR"


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def launch_url(launch: dict[str, Any]) -> str | None:
    pad = PADS.get(launch["launchpad"])
    return pad[1].format(t=launch["token"]) if pad else None


def format_card(launch: dict[str, Any], card: dict[str, Any], cfg: dict[str, Any], chance: dict[str, Any] | None = None) -> str:
    """The card post (<= card_max_chars). Facts only: a score, the reasons with their numbers, the config that produced it."""
    pub, base = cfg["publisher"], cfg["site"]["base_url"].rstrip("/")
    pad_name = PADS.get(launch["launchpad"], (launch["launchpad"],))[0]
    sym = launch.get("symbol")
    head = [f"New launch on {pad_name}", f"{sym + ' · ' if sym else ''}{launch['token']}",
            f"Creator: {short(launch.get('creator') or card.get('dev_account'), 30)}" if (launch.get("creator") or card.get("dev_account"))
            else "Creator: not known yet", "",
            f"Risk score {card['score']}/100 — {card['band']}"]
    if chance and chance.get("predicted") is not None:
        head.append(f"Rug chance ≈ {round(chance['predicted'] * 100)} % (calibrated on history)")
    reasons = [r for r in card["reasons"] if r.get("fired")][: pub["reasons_max"]]
    body: list[str] = []
    for r in reasons:
        ev = (r.get("evidence") or [None])[0]
        tag = f" [{ev['kind']} {short(ev['id'], 30)}]" if ev and ev.get("id") else ""
        body.append("• " + _clip(r["reason"], 170) + tag)
    if not body:
        body.append("No risk signal fired at launch time.")
    foot = [f"Rules {card['rules_version']} · config {card['config_version']} · issued {_utc(iso=card['issued_at'])}"]
    if pub.get("disclaimer"):
        foot.append(pub["disclaimer"])
    url = launch_url(launch)
    if url:
        foot.append(url)
    if base:
        foot.append(f"{base}/launch/{launch['token']}")
    text = lambda b: "\n".join(head + ["Why:"] + b + [""] + foot)  # noqa: E731
    out = text(body)
    while len(out) > pub["card_max_chars"] and len(body) > 1:      # drop the weakest reason first, then trim the last one
        body.pop()
        out = text(body)
    if len(out) > pub["card_max_chars"]:
        body[0] = _clip(body[0], max(40, len(body[0]) - (len(out) - pub["card_max_chars"]) - 1))
        out = text(body)
    return out


def format_outcome(launch: dict[str, Any], card: dict[str, Any], out: dict[str, Any], max_x: float | None = None) -> str:
    """The reply to the card post when the stored outcome is final (T+24 h)."""
    rug = out.get("rug") or {}
    ev = rug.get("evidence") or {}
    initial = f"The initial score was {card['score']} ({card['band']})."
    if rug.get("value") is True:
        sev = out.get("severity_near")
        kind = "fast (by T+60 min)" if (out.get("rug_fast") or {}).get("value") else "late (by T+24 h)"
        lines = [f"Outcome after 24 h: rug, {kind}.", f"Outside buyers lost {_near(sev)}.",
                 f"Insiders' net result: {_near(ev.get('insiders_pnl_near'), True)}."]
        if ev.get("insiders_sold_bps") is not None:
            lines.append(f"Insiders had sold {ev['insiders_sold_bps'] / 100:.0f} % of what they acquired.")
    elif rug.get("value") is False:
        lines = ["Outcome after 24 h: no rug.", f"Outside buyers' net result: {_near(ev.get('buyers_pnl_near'), True)}."]
    else:
        lines = ["Outcome after 24 h: could not be decided (data missing)."]
    if max_x is not None:
        lines.append(f"Highest price vs the first buyer: {max_x:.2f}x.")
    lines += [initial, f"Rules {card['rules_version']} · outcome config {out.get('config_version', '—')}"]
    return "\n".join(lines)


def daily_stats(con: sqlite3.Connection, day: str) -> dict[str, Any]:
    """Numbers of one UTC day of live launches (those with a card), read from the collector's database."""
    lo = int(dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()) * NS
    hi = lo + 86400 * NS
    rows = [r for r in card_outcome_rows(con) if lo <= r["launch_ts_ns"] < hi]
    final = [r for r in rows if r["final"] and r["rug"] in (0, 1)]
    rugs = [r for r in final if r["rug"] == 1]
    worst = max(rugs, key=lambda r: r["severity_near"] or 0, default=None)
    # the link group with the biggest buyers' loss (a creator without a group counts as itself)
    by_group: dict[str, list[float]] = {}
    for r in rugs:
        dev = con.execute("SELECT COALESCE(c.dev_account, l.creator) FROM cards c JOIN launches l ON l.token = c.token WHERE c.token = ?",
                          (r["token"],)).fetchone()
        key = dev[0] if dev and dev[0] else "unknown"
        if key != "unknown" and _has(con, "link_members"):
            m = con.execute("SELECT group_id FROM link_members WHERE account = ?", (key,)).fetchone()
            key = m[0] if m else key
        g = by_group.setdefault(key, [0.0, 0])
        g[0] += r["severity_near"] or 0.0
        g[1] += 1
    top = max(by_group.items(), key=lambda kv: kv[1][0], default=None)
    allrows = card_outcome_rows(con)
    return {"day": day, "launches": len(rows), "final": len(final), "waiting": len(rows) - len(final), "rugs": len(rugs),
            "loss_near": sum(r["severity_near"] or 0.0 for r in rugs), "worst": (worst["token"], worst["severity_near"]) if worst else None,
            "group": (top[0], top[1][0], top[1][1]) if top and top[1][0] > 0 else None,
            "track": summarize(allrows)}


def format_daily(st: dict[str, Any], cfg: dict[str, Any]) -> str:
    base = cfg["site"]["base_url"].rstrip("/")
    lines = [f"rugyomi daily summary · {st['day']} (UTC)", "",
             f"Live launches: {st['launches']} · decided after 24 h: {st['final']} · still waiting: {st['waiting']}",
             f"Rugs among decided: {st['rugs']}" + (f" · outside buyers lost {_near(st['loss_near'])} in total" if st["rugs"] else "")]
    if st["worst"]:
        lines.append(f"Biggest loss: {_near(st['worst'][1])} on {short(st['worst'][0], 30)}")
    if st["group"]:
        who, loss, n = st["group"]
        lines.append(f"Creator group with the biggest buyers' loss: {short(who, 30)} — {n} rug(s), {_near(loss)}")
    t = st["track"]
    s = t["summary"]
    lines += ["", "Track record of the live cards so far:",
              f"{s['cards']} cards · {s['final']} decided · {s['rugs']} rugs" + (f" · base rate {s['base_rate'] * 100:.0f} %" if s.get("base_rate") is not None else "")]
    for b in t["by_band"]:
        if b["n"]:
            lines.append(f"• {b['band']}: {b['rugs']}/{b['n']} rugs" + (f" ({b['rate'] * 100:.0f} %)" if b.get("rate") is not None else "")
                         + (" — too few to judge" if b.get("too_few") else ""))
    if cfg["publisher"].get("disclaimer"):
        lines += ["", cfg["publisher"]["disclaimer"]]
    if base:
        lines += [f"{base}/accuracy"]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ the publisher

class Publisher:
    def __init__(self, db: Path, state: Path, cfg: dict[str, Any], tg: Telegram | None, targets: dict[str, Any],
                 log: Callable[[str], None] = print, clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.db, self.cfg, self.tg, self.targets, self.log, self.clock, self.sleep = db, cfg, tg, targets, log, clock, sleep
        state.mkdir(parents=True, exist_ok=True)
        self.posts = sqlite3.connect(state / "publisher.db", isolation_level=None, timeout=10)
        self.posts.executescript(SCHEMA)
        self._last_send = 0.0

    # --- mode and targets
    @property
    def pub(self) -> dict[str, Any]:
        return self.cfg["publisher"]

    def mode(self) -> str | None:
        """'channel' | 'dry' | None (disabled or nothing to send to)."""
        if not self.pub["enabled"]:
            return None
        if self.pub["dry_run"]:
            return "dry" if self.targets["admins"] else None
        return "channel" if self.targets["channel"] else None

    def chats(self, mode: str) -> list[str]:
        return [self.targets["channel"]] if mode == "channel" else list(self.targets["admins"])

    def since_ns(self, mode: str) -> int:
        """Cards issued before this are never posted. Set once per mode (now), or by config `since`."""
        if self.pub["since"]:
            return int(dt.datetime.fromisoformat(self.pub["since"].replace("Z", "+00:00")).timestamp() * NS)
        row = self.posts.execute("SELECT value FROM meta WHERE key = ?", (f"since_{mode}",)).fetchone()
        if row:
            return int(row[0])
        now = int(self.clock() * NS)
        self.posts.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (f"since_{mode}", str(now)))
        return now

    # --- one pass
    def tick(self) -> int:
        mode = self.mode()
        if mode is None or self.tg is None:
            return 0
        since = self.since_ns(mode)
        n = self._retry_due()
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
        try:
            for token, kind, text, parent in self._candidates(con, mode, since):
                if n >= self.pub["max_posts_per_tick"]:
                    break
                for chat in self.chats(mode):
                    n += self._post(token, kind, chat, mode, text, parent_of=(token if kind == OUTCOME else None))
        finally:
            con.close()
        return n

    def _candidates(self, con: sqlite3.Connection, mode: str, since: int):
        if not _has(con, "cards"):
            return
        done = {(t, k, c) for t, k, c in self.posts.execute("SELECT token, kind, chat FROM channel_posts")}
        chats = self.chats(mode)
        since_iso = dt.datetime.fromtimestamp(since / NS, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        cards = con.execute("SELECT c.token FROM cards c JOIN launches l ON l.token = c.token WHERE l.coverage IS NULL "
                            "AND c.issued_at >= ? ORDER BY c.launch_ts_ns", (since_iso,)).fetchall()
        for (token,) in cards:
            lau, card = read_launch(con, token), card_row(con, token)
            if not lau or not card:
                continue
            if any((token, CARD, c) not in done for c in chats):
                yield token, CARD, format_card(lau, card, self.cfg, read_chance(con, card)), None
            out = outcome_row(con, token) if _has(con, "launch_outcomes") else None
            if out and out.get("final") and any((token, OUTCOME, c) not in done for c in chats):
                posted = {c for (t, k, c) in done if t == token and k == CARD}
                if posted:                                              # a reply needs its parent; no card post, no reply
                    mx = con.execute("SELECT max_x FROM launch_snapshots WHERE token = ? AND error IS NULL AND max_x IS NOT NULL "
                                     "ORDER BY minute DESC LIMIT 1", (token,)).fetchone() if _has(con, "launch_snapshots") else None
                    yield token, OUTCOME, format_outcome(lau, card, out, float(mx[0]) if mx and mx[0] is not None else None), None
        yield from self._daily(con, mode, since, done)

    def _daily(self, con: sqlite3.Connection, mode: str, since: int, done: set):
        now = dt.datetime.fromtimestamp(self.clock(), dt.timezone.utc)
        if now.hour < self.pub["daily_hour_utc"]:
            return
        day = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        if day < dt.datetime.fromtimestamp(since / NS, dt.timezone.utc).strftime("%Y-%m-%d"):
            return
        if all((day, DAILY, c) in done for c in self.chats(mode)):
            return
        st = daily_stats(con, day)
        yield day, DAILY, ("" if st["launches"] == 0 else format_daily(st, self.cfg)), None

    # --- sending
    def _parent(self, token: str, chat: str) -> int | None:
        r = self.posts.execute("SELECT message_id FROM channel_posts WHERE token = ? AND kind = 'card' AND chat = ? AND status = 'sent'",
                               (token, chat)).fetchone()
        return r[0] if r else None

    def _post(self, token: str, kind: str, chat: str, mode: str, text: str, parent_of: str | None = None) -> int:
        if self.posts.execute("SELECT 1 FROM channel_posts WHERE token = ? AND kind = ? AND chat = ?", (token, kind, chat)).fetchone():
            return 0
        now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not text:                                                     # a day without launches: remembered, never posted
            self.posts.execute("INSERT INTO channel_posts(token, kind, chat, mode, status, text, text_hash, created_at) "
                               "VALUES(?,?,?,?,'skipped','',?,?)", (token, kind, chat, mode, "", now))
            return 0
        reply = self._parent(parent_of, chat) if parent_of else None
        if parent_of and reply is None:
            return 0
        shown = f"[dry run — this would go to the channel]\n{text}" if mode == "dry" else text
        self.posts.execute("INSERT INTO channel_posts(token, kind, chat, mode, status, reply_to, text, text_hash, created_at) "
                           "VALUES(?,?,?,?,'pending',?,?,?,?)", (token, kind, chat, mode, reply, shown,
                                                                 hashlib.sha256(text.encode()).hexdigest()[:16], now))
        return self._send_row(token, kind, chat)

    def _send_row(self, token: str, kind: str, chat: str) -> int:
        text, reply, attempts = self.posts.execute("SELECT text, reply_to, attempts FROM channel_posts WHERE token = ? AND kind = ? AND chat = ?",
                                                   (token, kind, chat)).fetchone()
        wait = self.pub["min_interval_seconds"] - (self.clock() - self._last_send)
        if wait > 0:
            self.sleep(wait)
        # at most once: mark 'sending' first; if the process dies before the answer is recorded, the row stays 'sending' -> 'unknown'
        self.posts.execute("UPDATE channel_posts SET status = 'sending', attempts = attempts + 1 WHERE token = ? AND kind = ? AND chat = ?",
                           (token, kind, chat))
        try:
            mid = self.tg.send(chat, text, reply)
        except TelegramError as e:
            delay = e.retry_after or min(self.pub["retry_max_seconds"] * (6 if e.permanent else 1), self.pub["retry_base_seconds"] * 2 ** attempts)
            if e.permanent:
                delay = self.pub["retry_max_seconds"] * 6            # a wrong chat id / a kicked bot: look again in an hour, do not hammer
            self.posts.execute("UPDATE channel_posts SET status = 'pending', next_try_at = ?, last_error = ? WHERE token = ? AND kind = ? AND chat = ?",
                               (self.clock() + delay, str(e)[:300], token, kind, chat))
            self.log(f"publisher: {kind} {token}: {e} (retry in {delay:.0f} s)")
            self._last_send = self.clock()
            return 0
        self._last_send = self.clock()
        self.posts.execute("UPDATE channel_posts SET status = 'sent', message_id = ?, sent_at = ?, last_error = NULL "
                           "WHERE token = ? AND kind = ? AND chat = ?",
                           (mid, dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), token, kind, chat))
        self.log(f"publisher: posted {kind} {token} to …{chat[-4:]} (message {mid})")
        return 1

    def _retry_due(self) -> int:
        n = 0
        # a row left 'sending' by a crash: we cannot know whether Telegram got it — never repeat it
        self.posts.execute("UPDATE channel_posts SET status = 'unknown' WHERE status = 'sending'")
        for token, kind, chat, mode in self.posts.execute(
                "SELECT token, kind, chat, mode FROM channel_posts WHERE status = 'pending' AND next_try_at <= ? ORDER BY created_at",
                (self.clock(),)).fetchall():
            if mode != (self.mode() or ""):
                continue                                                 # the mode changed since: do not send an old dry-run post to the channel
            n += self._send_row(token, kind, chat)
        return n

    def status(self) -> dict[str, Any]:
        rows = self.posts.execute("SELECT kind, status, COUNT(*) FROM channel_posts GROUP BY 1, 2 ORDER BY 1, 2").fetchall()
        return {"mode": self.mode(), "posts": [{"kind": k, "status": s, "n": n} for k, s, n in rows]}


# ------------------------------------------------------------------------------------------------ CLI

def default_db() -> Path:
    return Path(os.environ.get("RUGYOMI_DB") or Path.home() / ".rugyomi" / "rugyomi.db")


def state_dir() -> Path:
    return Path(os.environ.get("RUGYOMI_STATE_DIR") or Path.home() / ".rugyomi")


def build(db: Path | None = None) -> Publisher:
    cfg = load_config()
    targets = env_targets()
    tg = Telegram(targets["token"]) if targets["token"] else None
    return Publisher(db or default_db(), state_dir(), cfg, tg, targets, log=lambda m: print(m, flush=True))


def preview(db: Path, token: str, cfg: dict[str, Any]) -> str:
    con = open_store(db, readonly=True).con
    lau, card = read_launch(con, token), card_row(con, token)
    if not lau or not card:
        return f"no card for {token}"
    parts = [format_card(lau, card, cfg, read_chance(con, card))]
    out = outcome_row(con, token) if _has(con, "launch_outcomes") else None
    if out and out.get("final"):
        parts += ["— reply 24 h later —", format_outcome(lau, card, out)]
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rugyomi.publisher", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "once", "status"):
        sub.add_parser(name).add_argument("--db", type=Path, default=None)
    p = sub.add_parser("preview")
    p.add_argument("token")
    p.add_argument("--db", type=Path, default=None)
    d = sub.add_parser("preview-daily")
    d.add_argument("day")
    d.add_argument("--db", type=Path, default=None)
    a = ap.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"publisher: bad config: {e}", file=sys.stderr)
        return 2
    if a.cmd == "preview":
        print(preview(a.db or default_db(), a.token, cfg))
        return 0
    if a.cmd == "preview-daily":
        con = open_store(a.db or default_db(), readonly=True).con
        print(format_daily(daily_stats(con, a.day), cfg))
        return 0
    pub = build(a.db)
    if a.cmd == "status":
        print(json.dumps(pub.status(), indent=1))
        return 0
    if a.cmd == "once":
        print(f"posted {pub.tick()}")
        return 0
    pub.log(f"publisher: start, mode {pub.mode() or 'disabled'} (enabled={pub.pub['enabled']}, dry_run={pub.pub['dry_run']})")
    while True:
        try:
            pub.cfg = load_config()                      # a changed override takes effect without a restart
            pub.tick()
        except Exception as e:  # noqa: BLE001 — the loop never dies on one bad pass
            pub.log(f"publisher: pass failed: {type(e).__name__}: {pub.tg.scrub(str(e)) if pub.tg else e}")
        time.sleep(pub.pub["poll_seconds"])


if __name__ == "__main__":
    sys.exit(main())
