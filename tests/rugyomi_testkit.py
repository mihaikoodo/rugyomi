"""Shared helpers for the public tests (not a conftest: test dirs of the dev repo run in one pytest call).

Puts public/ on sys.path (works without `pip install -e public`), loads fixture blocks (real mainnet blocks),
builds synthetic blocks, and runs a fake neardata server.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PUBLIC = Path(__file__).resolve().parents[1]
if str(PUBLIC) not in sys.path:
    sys.path.insert(0, str(PUBLIC))

from rugyomi.store.db import b58encode  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T0 = 1_800_000_000 * 10**9


def load_block(path: Path):
    if path.name.endswith(".null"):
        return None
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw)


def fixture_blocks(sub: str):
    """[(height, block or None)] in height order."""
    out = []
    for p in (FIXTURES / sub).iterdir():
        stem = p.name.split(".", 1)[0]
        if stem.isdigit():
            out.append((int(stem), load_block(p)))
    return sorted(out, key=lambda x: x[0])


def hid(*parts) -> str:
    """A deterministic base58 32-byte id (receipt / tx)."""
    return b58encode(hashlib.sha256(":".join(map(str, parts)).encode()).digest())


def reo(rid: str, tx: str | None, pred: str, recv: str, actions: list, logs: list | None = None,
        executor: str | None = None, ok: bool = True, signer: str | None = None) -> dict:
    return {"tx_hash": tx,
            "receipt": {"receipt_id": rid, "predecessor_id": pred, "receiver_id": recv,
                        "receipt": {"Action": {"signer_id": signer or pred, "actions": actions}}},
            "execution_outcome": {"outcome": {"executor_id": executor or recv, "logs": logs or [],
                                              "status": {"SuccessValue": ""} if ok else {"Failure": {}}}}}


def block(height: int, reos: list | None = None, ts_ns: int | None = None) -> dict:
    return {"block": {"header": {"height": height, "timestamp_nanosec": str(ts_ns or T0 + height * 10**9)}},
            "shards": [{"shard_id": 0, "receipt_execution_outcomes": reos or []}]}


def transfer_block(height: int) -> dict:
    """One NEAR transfer alice -> bob of `height` yocto: exactly one fact."""
    return block(height, [reo(hid("r", height), hid("t", height), "alice.near", "bob.near",
                              [{"Transfer": {"deposit": str(height)}}])])


class FakeNeardata:
    """Synthetic chain [lo, hi]; `head` is the final head (blocks above it answer null, like a not-yet height)."""

    def __init__(self, lo: int, hi: int, nulls=(), make=transfer_block):
        self.lo, self.hi, self.head = lo, hi, hi
        self.nulls = set(nulls)
        self.make = make
        self.fail = {}             # height -> remaining failures (HTTP 500)
        # tip race: a height in `bump_on_null` answers null once (not final yet) and the final head jumps
        # past it right after — the head the client reads AFTER that null already covers the height
        self.bump_on_null: dict[int, int] = {}
        self.latency = 0.0
        self.hits: list[tuple[str, str | None]] = []
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass                     # the client was killed on purpose (kill / restart test)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                key = dict(urllib.parse.parse_qsl(u.query)).get("apiKey")
                with fake.lock:
                    fake.hits.append((u.path, key))
                if u.path == "/v0/last_block/final":
                    return self.send(302, headers={"Location": f"/v0/block/{fake.head}"})
                parts = u.path.strip("/").split("/")
                if parts[:2] != ["v0", "block"] or len(parts) != 3:
                    return self.send(404, b'{"error":"no"}')
                h = int(parts[2])
                with fake.lock:
                    n = fake.fail.get(h, 0)
                    if n:
                        fake.fail[h] = n - 1
                if n:
                    return self.send(500, b'{"error":"boom"}')
                if fake.latency:
                    time.sleep(fake.latency)
                with fake.lock:
                    bump = fake.bump_on_null.pop(h, None) if h > fake.head else None
                if bump is not None:
                    fake.head = bump
                    return self.send(200, b"null")
                if h in fake.nulls or h > fake.head or h < fake.lo or h > fake.hi:
                    return self.send(200, b"null")
                body = gzip.compress(json.dumps(fake.make(h)).encode())
                return self.send(200, body, {"Content-Encoding": "gzip"})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()

    def block_paths(self) -> list[str]:
        with self.lock:
            return [p for p, _k in self.hits if p.startswith("/v0/block/")]


def refrax_replay_blocks():
    """[(height, block or None)] of the 29.10 reference window for the rules tests (RGY-015): the launch transactions of
    ISLAND / COIN / REFRAX (snapshots.jsonl.gz) plus the pre-launch context of REFRAX's creator (refrax_context.jsonl.gz:
    the creation of thecoins.near at 222136304 and the 40.97 NEAR transfer from coinisland.near at 222136374), merged
    by height. Every height of the window is present, as the collector sees it."""
    def read(name):
        out = {}
        for line in gzip.decompress((FIXTURES / name).read_bytes()).splitlines():
            if line.strip():
                d = json.loads(line)
                out[d["height"]] = d["block"]
        return out
    base, ctx = read("snapshots.jsonl.gz"), read("refrax_context.jsonl.gz")
    for h, b in ctx.items():
        if base.get(h) is None:
            base[h] = b
        else:
            base[h] = {**base[h], "shards": list(base[h].get("shards") or []) + list(b.get("shards") or [])}
    return sorted(base.items())
