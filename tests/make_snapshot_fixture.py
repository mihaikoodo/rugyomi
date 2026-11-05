#!/usr/bin/env python3
"""Build tests/fixtures/snapshots.jsonl.gz: trimmed REAL mainnet blocks for the RGY-006 reference launches.

From a directory of full blocks ({h}.json.gz, {h}.null — spikes/rgy001/fetch.py output), keeps, per block, only the
receipt outcomes of transactions that touch the reference tokens (their NEP-141 events, DCL swaps on their pools,
their launch events). Receipts are copied unchanged; everything else in the block is dropped. One JSON line per
height in order: the trimmed block, or null for a skipped height — the collector sees every height.

    python public/tests/make_snapshot_fixture.py data/blocks 222133788 222143000
"""
import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rugyomi.parse import parse_block  # noqa: E402

TOKENS = ("island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near")
OUT = HERE / "fixtures" / "snapshots.jsonl.gz"


def touches(f) -> bool:
    if f.token in TOKENS or f.b in TOKENS or f.a in TOKENS:
        return True
    if f.kind == "dcl_swap" and (f.b or "").split("|")[0] in TOKENS:
        return True
    return f.kind.startswith("launch:") and any(t in json.dumps(f.extra or {}) for t in TOKENS)


def load(src_dir: Path, h: int):
    p = src_dir / f"{h}.json.gz"
    if p.exists():
        return json.loads(gzip.decompress(p.read_bytes()))
    if (src_dir / f"{h}.null").exists():
        return None
    raise FileNotFoundError(f"missing height {h}")


def main(src: str, lo: int, hi: int) -> int:
    """Two passes over the blocks (one block in memory at a time): find the transactions, then copy their receipts."""
    src_dir = Path(src)
    txs: set[str] = set()
    for h in range(lo, hi + 1):
        b = load(src_dir, h)
        if b is not None:
            txs.update(f.tx_hash for f in parse_block(b) if f.tx_hash and touches(f))
    kept = 0
    with gzip.open(OUT, "wt", compresslevel=9) as out:
        for h in range(lo, hi + 1):
            b = load(src_dir, h)
            if b is None:
                out.write(json.dumps({"height": h, "block": None}) + "\n")
                continue
            shards = []
            for s in b.get("shards") or []:
                reos = [r for r in s.get("receipt_execution_outcomes") or [] if r.get("tx_hash") in txs]
                if reos:
                    shards.append({"shard_id": s.get("shard_id"), "receipt_execution_outcomes": reos})
                    kept += len(reos)
            hdr = b["block"]["header"]
            slim = {"block": {"header": {"height": hdr["height"], "timestamp": hdr.get("timestamp"),
                                         "timestamp_nanosec": hdr.get("timestamp_nanosec")}}, "shards": shards}
            out.write(json.dumps({"height": h, "block": slim}, separators=(",", ":")) + "\n")
    print(f"{hi - lo + 1} heights, {len(txs)} transactions, {kept} receipts -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3])))
