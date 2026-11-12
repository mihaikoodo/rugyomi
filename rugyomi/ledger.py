"""The forecast ledger (RGY-037): every live risk card as one line of a hash chain, so that anyone can check that no card was added, removed or changed later.

    python -m rugyomi.ledger verify LEDGER.jsonl [--anchor HASH | COUNT:HASH]... [--anchors-file FILE]
        recomputes the chain; prints OK + the head, or the first broken line; exit 0 ok, 1 broken or an anchor that does not match, 2 usage

What is chained. One line per LIVE card (the `cards` table: written once at launch time, immutable by trigger; the after-the-fact cards of history launches are not
forecasts and are not here), in the order the cards were issued (insertion order of `cards`, which is issue order: the chain only ever grows at its end;
`issued_at` is wall-clock time, so a clock step must not be able to move a card in front of an anchored one). Fields of a line:
  token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, config_version, config_hash, rules_version, computed_ms
  + prev_hash (the hash of the line before; 64 zeros for the first card) + hash.
`reasons` and `signals` are the stored JSON, decoded. A line is the CANONICAL JSON of that object: keys sorted, no spaces, ASCII only (non-ASCII escaped),
integers as integers, floats as Python's shortest repr, no NaN. `hash` = sha256 (hex) of the canonical JSON of the same object WITHOUT the `hash` key
(so it covers `prev_hash`). A reader re-deriving the format in another language must reproduce Python's `json.dumps(obj, sort_keys=True, separators=(",", ":"),
ensure_ascii=True, allow_nan=False)`.
Outcomes are NOT in the chain (they are unknown at launch and change until T+24 h); they are published next to it, one line per card, keyed by token, with
their own label version (`outcome_lines`).

Anchor. Once a day the channel gets the head hash and the number of cards at the end of that UTC day (`anchor_for_day`); a post in the channel is never
edited, so the anchor inherits that. `verify --anchor` checks that the hash is in the chain (and, as `COUNT:HASH`, at that position): anything changed in
a line before it breaks every hash after it, and a card removed or inserted before it moves the position.

Stdlib only: this file is meant to be read and run by anyone. The server derives the ledger from `cards` on every request; nothing here is stored or edited.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from typing import Any, Iterable

GENESIS = "0" * 64
FORMAT = "rugyomi-ledger-1"
FIELDS = ("token", "issued_at", "issued_height", "launch_ts_ns", "score", "band", "reasons", "signals", "dev_account", "config_version", "config_hash",
          "rules_version", "computed_ms")


def canonical(obj: Any) -> str:
    """The one serialization every line and every hash uses."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(obj_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(obj_without_hash).encode("ascii")).hexdigest()


def record(row: tuple) -> dict[str, Any]:
    """A `cards` row (FIELDS order, reasons / signals still JSON text) as a ledger record."""
    r = dict(zip(FIELDS, row))
    r["reasons"] = json.loads(r["reasons"])
    r["signals"] = json.loads(r["signals"])
    return r


def link(prev_hash: str, rec: dict[str, Any]) -> tuple[str, str]:
    """(line, hash) of the record that follows `prev_hash`."""
    body = {**rec, "prev_hash": prev_hash}
    h = digest(body)
    return canonical({**body, "hash": h}), h


def read_cards(con: sqlite3.Connection) -> list[tuple]:
    """The live cards in issue order (insertion order). [] when there is no `cards` table."""
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cards'").fetchone() is None:
        return []
    return con.execute(f"SELECT {','.join(FIELDS)} FROM cards ORDER BY rowid").fetchall()


def build(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """[{line, hash, token, issued_at}] in chain order, from the stored cards. Deterministic: the same cards give the same bytes."""
    out, prev = [], GENESIS
    for row in read_cards(con):
        rec = record(row)
        line, prev = link(prev, rec)
        out.append({"line": line, "hash": prev, "token": rec["token"], "issued_at": rec["issued_at"]})
    return out


def text(entries: list[dict[str, Any]]) -> str:
    return "".join(e["line"] + "\n" for e in entries)


def head(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"format": FORMAT, "count": len(entries), "head": entries[-1]["hash"] if entries else GENESIS, "genesis_prev_hash": GENESIS,
            "first_issued_at": entries[0]["issued_at"] if entries else None, "last_issued_at": entries[-1]["issued_at"] if entries else None}


def anchor_for_day(entries: list[dict[str, Any]], day: str) -> dict[str, Any] | None:
    """The head at the end of the UTC day `day` (YYYY-MM-DD): the last card in chain order issued before the next midnight. None when no card existed then.
    {day, count, head, new} - `new` = the cards issued that day."""
    nxt = _next_day(day)
    last, new = -1, 0
    for i, e in enumerate(entries):
        if e["issued_at"] < nxt:
            last = i
        if day <= e["issued_at"][:10] < nxt:
            new += 1
    if last < 0:
        return None
    return {"day": day, "count": last + 1, "head": entries[last]["hash"], "new": new}


def _next_day(day: str) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


OUTCOME_SQL = "SELECT token, rug, rug_fast, rug_late, final, evidence, config_version, config_hash, computed_at FROM launch_outcomes"


def _bool(v: Any) -> bool | None:
    return None if v is None else bool(v)


def _label_version(evidence: str | None) -> Any:
    try:
        return json.loads(evidence or "{}").get("label_version")
    except ValueError:
        return None


def outcome_lines(con: sqlite3.Connection, entries: list[dict[str, Any]]) -> str:
    """One line per card of the chain (chain order): the stored outcome label, keyed by token. NOT hashed: an outcome changes until it is final.
    `rug`: true / false, or null (undefined, or not decided yet - see `final`); a card without an outcome row yet has final false and nulls."""
    have: dict[str, tuple] = {}
    if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'launch_outcomes'").fetchone() is not None:
        have = {r[0]: r for r in con.execute(OUTCOME_SQL)}
    out = []
    for e in entries:
        r = have.get(e["token"])
        out.append(canonical({"token": e["token"], "card_hash": e["hash"], "final": bool(r[4]) if r else False, "rug": _bool(r[1]) if r else None,
                              "rug_fast": _bool(r[2]) if r else None, "rug_late": _bool(r[3]) if r else None,
                              "label_version": _label_version(r[5]) if r else None, "config_version": r[6] if r else None,
                              "config_hash": r[7] if r else None, "computed_at": r[8] if r else None}))
    return "".join(x + "\n" for x in out)


# ============================================================================================ verification (what a reader runs)

class Broken(Exception):
    def __init__(self, line: int, why: str) -> None:
        super().__init__(f"line {line}: {why}")
        self.line, self.why = line, why


def verify_lines(lines: Iterable[str]) -> dict[str, Any]:
    """Recompute the chain. Returns {count, head, hashes: [hash...]} or raises Broken(line, why) at the FIRST broken line (1-based)."""
    prev, hashes, tokens = GENESIS, [], set()
    for n, raw in enumerate(lines, 1):
        line = raw.rstrip("\n")
        if line == "" or line != line.strip():
            raise Broken(n, "empty or padded line")
        try:
            obj = json.loads(line)
        except ValueError:
            raise Broken(n, "not valid JSON") from None
        if not isinstance(obj, dict) or "hash" not in obj or "prev_hash" not in obj:
            raise Broken(n, "not a ledger line (no hash / prev_hash)")
        missing = [k for k in FIELDS if k not in obj]
        if missing or set(obj) != set(FIELDS) | {"prev_hash", "hash"}:
            raise Broken(n, "the fields are not the ledger's fields")
        try:
            again = canonical(obj)
        except ValueError:
            raise Broken(n, "not canonical JSON") from None
        if again != line:
            raise Broken(n, "the line is not in canonical form")
        h = obj.pop("hash")
        if obj["prev_hash"] != prev:
            raise Broken(n, f"prev_hash does not match the hash of line {n - 1}" if n > 1 else "the first line must start from the genesis hash")
        if digest(obj) != h:
            raise Broken(n, "hash does not match the content of the line")
        if obj["token"] in tokens:
            raise Broken(n, "the same card twice")
        tokens.add(obj["token"])
        prev = h
        hashes.append(h)
    return {"count": len(hashes), "head": prev, "hashes": hashes}


def parse_anchor(s: str) -> tuple[int | None, str]:
    """'HASH' or 'COUNT:HASH'."""
    s = s.strip()
    if ":" in s:
        c, h = s.split(":", 1)
        return int(c), h.strip().lower()
    return None, s.lower()


def check_anchors(result: dict[str, Any], anchors: list[str]) -> list[str]:
    """Why each anchor does not match the chain (an empty list = all match)."""
    bad = []
    pos = {h: i + 1 for i, h in enumerate(result["hashes"])}
    for a in anchors:
        try:
            count, h = parse_anchor(a)
        except ValueError:
            bad.append(f"anchor {a!r}: not HASH or COUNT:HASH")
            continue
        if h not in pos:
            bad.append(f"anchor {h[:16]}…: not in the chain")
        elif count is not None and pos[h] != count:
            bad.append(f"anchor {h[:16]}…: the chain has it at card {pos[h]}, the anchor says {count}")
    return bad


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rugyomi.ledger", description="Verify the rugyomi forecast ledger.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="recompute the chain and check the anchors")
    v.add_argument("file")
    v.add_argument("--anchor", action="append", default=[], help="HASH or COUNT:HASH (a head published in the channel)")
    v.add_argument("--anchors-file", help="one anchor per line (blank lines and # comments ignored)")
    a = ap.parse_args(argv)
    anchors = list(a.anchor)
    try:
        if a.anchors_file:
            with open(a.anchors_file, encoding="utf-8") as f:
                anchors += [x.split("#")[0].strip() for x in f if x.split("#")[0].strip()]
        with open(a.file, encoding="utf-8", newline="") as f:
            raw = f.read()
    except OSError as e:
        print(f"cannot read: {e}", file=sys.stderr)
        return 2
    if raw and not raw.endswith("\n"):
        print("BROKEN: the file does not end with a newline (a cut-off last line?)")
        return 1
    try:
        res = verify_lines(raw.splitlines(keepends=True))
    except Broken as e:
        print(f"BROKEN at {e}")
        return 1
    bad = check_anchors(res, anchors)
    if bad:
        print(f"chain recomputes ({res['count']} cards, head {res['head']}), but:")
        for b in bad:
            print("  " + b)
        print("BROKEN")
        return 1
    print(f"OK: {res['count']} cards, head {res['head']}" + (f", {len(anchors)} anchor(s) match" if anchors else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
