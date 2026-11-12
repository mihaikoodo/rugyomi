"""Names that look like a notable account, and senders that hide where money came from (RGY-045). Two explicit data files, one pure rule.

`notable_accounts.json`  the notable NEAR accounts: root.near and the like. A source per entry; additions are approved by the user. Membership is never a name pattern.
`funding_sources.json`   senders (NEAR Intents contract / bridges, and exchange hot wallets once confirmed) whose transfer does not show who the money really came from.

`resembles(creator)` is deterministic and pure: it reads no database. The creator's name (without the `.near` / `.tg` suffix) is compared to the name of every notable account
(its own name without the suffix, and its aliases) after a normalisation: lower case, no `.` `-` `_`, look-alike characters mapped (`0` -> `o`, `1` -> `l`, `rn` -> `m`, `vv` -> `w`)
and repeated characters collapsed. A hit is (1) the same normalised name, (2) one character added / dropped / changed / two neighbours swapped (both names at least 7 characters),
or (3) the notable name plus a decoration: `near`, `official`, `real`, `team`, `app`, before or after it (digits are not a decoration: `root111` is not a hit). Never a hit: the notable account itself and its own sub-accounts
(`x.root.near` is created by `root.near`; the registrar `near` has no such sub-accounts), and any account that is itself on the list. The wording that goes with a hit says "resembles", never "impersonates".
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
NOTABLE_PATH = HERE / "notable_accounts.json"
SOURCES_PATH = HERE / "funding_sources.json"
SUFFIXES = (".near", ".tg")
DECORATIONS = ("near", "official", "real", "team", "app")
MIN_EDIT_LEN = 7                      # names shorter than this get no "one character off" hit
MIN_BASE_LEN = 3                      # a notable name shorter than this (after normalising) gets no decoration hit
HOW = {"same": "the same name once look-alike characters and repeated letters are ignored",
       "one_off": "one character different",
       "decorated": "the same name with a decoration added"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def stem(account: str) -> str:
    """The name of an account without its top-level suffix: `rootnearr.near` -> `rootnearr`, `x.root.near` -> `x.root`."""
    a = account.strip().lower()
    for s in SUFFIXES:
        if a.endswith(s) and len(a) > len(s):
            return a[: -len(s)]
    return a


def normalize(name: str, *, homoglyphs: bool = True) -> str:
    s = re.sub(r"[.\-_]", "", name.lower())
    if homoglyphs:
        s = s.replace("rn", "m").replace("vv", "w").replace("0", "o").replace("1", "l")
    return re.sub(r"(.)\1+", r"\1", s)


def within_one(a: str, b: str) -> bool:
    """Edit distance <= 1: one substitution, insertion or deletion, or two neighbouring characters swapped."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        diff = [i for i in range(la) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = 0
    while i < la and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def _loose(word: str) -> str:
    """A decoration word that may have repeated letters (`nearr`): every letter one or more times."""
    return "".join(re.escape(ch) + "+" for ch in word)


_WORDS = "|".join(_loose(w) for w in sorted(DECORATIONS, key=len, reverse=True))
_TAIL = re.compile(r"^(?:%s)$" % _WORDS)
_HEAD = re.compile(r"^(?:%s)$" % _WORDS)


def _decorated(creator_stem: str, notable_stem: str) -> bool:
    """The creator name is the notable name with a decoration before or after it (`near`, `official`, `real`, `team`, `app`; letters may repeat; digits are not a decoration)."""
    raw = re.sub(r"[.\-_]", "", creator_stem.lower())
    for mode in (True, False):
        n = normalize(notable_stem, homoglyphs=mode)
        if len(n) < MIN_BASE_LEN:
            continue
        for i in range(1, len(raw)):
            if _TAIL.match(raw[i:]) and normalize(raw[:i], homoglyphs=mode) == n:
                return True
            if _HEAD.match(raw[:i]) and normalize(raw[i:], homoglyphs=mode) == n:
                return True
    return False


def _hit(creator_stem: str, notable_stem: str) -> str | None:
    cs, ns = normalize(creator_stem), normalize(notable_stem)
    if cs and cs == ns:
        return "same"
    if len(cs) >= MIN_EDIT_LEN and len(ns) >= MIN_EDIT_LEN and within_one(cs, ns):
        return "one_off"
    if _decorated(creator_stem, notable_stem):
        return "decorated"
    return None


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _digest(d: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]


@lru_cache(maxsize=1)
def notable() -> dict[str, Any]:
    """{"version", "hash", "accounts": [...]} of the shipped list."""
    d = _read(NOTABLE_PATH)
    return {"version": d["version"], "hash": _digest(d), "accounts": d["accounts"]}


@lru_cache(maxsize=1)
def funding_sources() -> dict[str, Any]:
    d = _read(SOURCES_PATH)
    return {"version": d["version"], "hash": _digest(d), "intents": d["intents"], "exchanges": d["exchanges"]}


def funding_source_kind(sender: str | None) -> str | None:
    """"intents" (the Intents contract / bridges) or "exchange" (a confirmed exchange wallet) for a listed sender; else None."""
    fs = funding_sources()
    if sender in {e["account"] for e in fs["intents"]}:
        return "intents"
    if sender in {e["account"] for e in fs["exchanges"]}:
        return "exchange"
    return None


def resembles(creator: str | None, entries: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The notable account the creator's name resembles, or None: {"account", "how", "name"} (`name` = the notable name that matched; `how` = same | one_off | decorated)."""
    if not creator or _HEX64.match(creator):
        return None
    c = creator.strip().lower()
    entries = notable()["accounts"] if entries is None else entries
    if c in {e["account"] for e in entries}:
        return None                                                            # a notable account is not an imitation of another one
    cs = stem(c)
    best: dict[str, Any] | None = None
    order = {"same": 0, "one_off": 1, "decorated": 2}
    for e in entries:
        acct = e["account"]
        if c.endswith("." + acct) and not e.get("registrar"):
            continue                                                           # its own sub-account: created by the notable account
        for name in dict.fromkeys([stem(acct), *e.get("aliases", [])]):
            how = _hit(cs, name)
            if how and (best is None or order[how] < order[best["how"]]):
                best = {"account": acct, "how": how, "name": name}
    return best
