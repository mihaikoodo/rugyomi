"""RGY-069: a file of every risk score we have, for a developer who wants the past without calling the API for it.

    python -m rugyomi.risk_snapshot build --db rugyomi.db [--base-url https://mihaikoodo.dev] [--out DIR]
    python -m rugyomi.risk_snapshot split risk_snapshot.json DIR

`build` reads the database read-only and writes the rows as JSON to stdout, or `risk_snapshot.json`, `risk_snapshot.csv` and `risk_scores.json` into DIR.
`split` writes the same three files from a JSON file `build` made earlier (so the database can stay on another machine). Standard library only,
and the file runs on its own: `python3 risk_snapshot.py split IN DIR`.

One row per launch that has a score: the stored card of the launch, or, for a launch that has none, the card computed afterwards (the site draws
both the same way, and so do we). The row says which launch, on which launchpad, the score and the band, when the launch happened, when the number was
computed, and the version of the rules and of the configuration. There is nothing in it that tells the two kinds apart: the number is what the badge
and `/api/v1/launches` show for the token. `risk_scores.json` is the same data cut down to what a page needs to draw a badge (token -> [score, band]).
Analysis, not advice.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_URL = "https://mihaikoodo.dev"
BANDS = ("Low", "Elevated", "High")
BAND_CODE = {"Low": "L", "Elevated": "E", "High": "H"}
ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
DISCLAIMER = "Analysis, not advice."
FIELDS = ("token", "launchpad", "score", "band", "launched_at", "scored_at", "rules_version", "config_version", "site_url")
JSON_NAME, CSV_NAME, SCORES_NAME = "risk_snapshot.json", "risk_snapshot.csv", "risk_scores.json"


def _utc(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns // 1_000_000_000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_text(text: str) -> str:
    """A stored time stamp (ISO 8601, UTC) cut to whole seconds with a Z."""
    t = str(text or "").strip().replace(" ", "T")
    m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", t)
    return (m.group(1) + "Z") if m else ""


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _good(token: str, score: object, band: object) -> bool:
    return bool(ACCOUNT_RE.match(token or "")) and isinstance(score, int) and 0 <= score <= 100 and band in BANDS


def rows_from_db(db: str | Path, base_url: str = DEFAULT_BASE_URL) -> list[dict]:
    """Every launch with a score, newest first. The database is opened read-only."""
    base = base_url.rstrip("/")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    try:
        if not _has(con, "launches"):
            return []
        found: dict[str, tuple] = {}
        live = set()
        if _has(con, "cards"):
            for t, pad, ts, sc, b, at, rv, cv in con.execute(
                    "SELECT c.token, l.launchpad, l.ts_ns, c.score, c.band, c.issued_at, c.rules_version, c.config_version "
                    "FROM cards c JOIN launches l ON l.token = c.token"):
                found[t] = (t, pad, ts, sc, b, at, rv, cv)
                live.add(t)
        from . import retro as later                                           # a card computed afterwards, only for a launch that has no stored card (the newest one)
        for t, pad, ts in con.execute("SELECT token, launchpad, ts_ns FROM launches ORDER BY ts_ns").fetchall():
            if t in live:
                continue
            r = later.retro_row(con, t)
            if r:
                found[t] = (t, pad, ts, r["score"], r["band"], r["computed_at"], r["rules_version"], r["config_version"])
    finally:
        con.close()
    out = []
    for t, pad, ts, sc, b, at, rv, cv in found.values():
        if not _good(t, sc, b):
            continue
        out.append({"token": t, "launchpad": pad, "score": sc, "band": b, "launched_at": _utc(ts), "scored_at": _utc_text(at),
                    "rules_version": rv, "config_version": cv, "site_url": f"{base}/launch/{t}"})
    out.sort(key=lambda r: (r["launched_at"], r["token"]), reverse=True)
    return out


def document(rows: list[dict], base_url: str = DEFAULT_BASE_URL, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    return {"generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": base_url.rstrip("/"), "count": len(rows), "disclaimer": DISCLAIMER,
            "fields": list(FIELDS), "rows": rows}


def scores(doc: dict) -> dict:
    """The part a page needs to draw a badge: token -> [score, band letter]."""
    return {"generated_at": doc["generated_at"], "source": doc["source"], "count": doc["count"], "disclaimer": DISCLAIMER,
            "bands": {v: k for k, v in BAND_CODE.items()}, "scores": {r["token"]: [r["score"], BAND_CODE[r["band"]]] for r in doc["rows"]}}


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    w.writerows({k: r[k] for k in FIELDS} for r in rows)
    return buf.getvalue()


def check(doc: object) -> dict:
    """A document read from a file: the shape and every row are checked before anything is written from it."""
    if not isinstance(doc, dict) or not isinstance(doc.get("rows"), list):
        raise ValueError("not a risk snapshot: no rows")
    for i, r in enumerate(doc["rows"]):
        if not isinstance(r, dict) or any(k not in r for k in FIELDS) or not _good(r["token"], r["score"], r["band"]):
            raise ValueError(f"row {i}: not a valid snapshot row")
    for k in ("generated_at", "source"):
        if not isinstance(doc.get(k), str):
            raise ValueError(f"no {k}")
    doc["count"] = len(doc["rows"])
    return doc


def write_files(doc: dict, out: str | Path) -> dict[str, Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        JSON_NAME: json.dumps(doc, ensure_ascii=False, indent=1) + "\n",
        CSV_NAME: to_csv(doc["rows"]),
        SCORES_NAME: json.dumps(scores(doc), ensure_ascii=False, separators=(",", ":")) + "\n",
    }
    paths = {}
    for name, text in files.items():
        paths[name] = out / name
        paths[name].write_text(text, encoding="utf-8", newline="\n")
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rugyomi.risk_snapshot", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="read the database (read-only) and write the snapshot")
    b.add_argument("--db", required=True)
    b.add_argument("--base-url", default=DEFAULT_BASE_URL)
    b.add_argument("--out", help="a directory for the three files; without it the JSON goes to stdout")
    s = sub.add_parser("split", help="write the three files from a snapshot JSON that `build` made")
    s.add_argument("src")
    s.add_argument("out")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "build":
            doc = document(rows_from_db(a.db, a.base_url), a.base_url)
            if a.out:
                for name, p in write_files(doc, a.out).items():
                    print(f"{p}  {p.stat().st_size} bytes", file=sys.stderr)
                print(f"{doc['count']} launches with a score", file=sys.stderr)
            else:
                sys.stdout.write(json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
        else:
            doc = check(json.loads(Path(a.src).read_text(encoding="utf-8")))
            for name, p in write_files(doc, a.out).items():
                print(f"{p}  {p.stat().st_size} bytes")
            print(f"{doc['count']} launches with a score")
    except (OSError, ValueError, sqlite3.Error) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
