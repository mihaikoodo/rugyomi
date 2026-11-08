#!/usr/bin/env python3
"""Build tests/fixtures/refrax_context.jsonl.gz: the PRE-launch context of the REFRAX reference launch (RGY-015).

REFRAX's creator `thecoins.near` was created at 222136304 (relayer `meteor-relayer.near`) and funded with 40.97 NEAR by
`coinisland.near` — the creator of ISLAND and COIN, whose dev dumped 100% — at 222136374, about 5.5 minutes before the
launch at 222136928. Those two blocks are not in snapshots.jsonl.gz (it keeps only the launch transactions).

Same trimming as make_snapshot_fixture.py: per listed height, only the receipt outcomes of the transactions that touch
the account; receipts are copied unchanged. One JSON line per height.

    python public/tests/make_context_fixture.py data/blocks thecoins.near 222136304 222136374
"""
import gzip
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from rugyomi.parse import parse_block  # noqa: E402

OUT = HERE / "fixtures" / "refrax_context.jsonl.gz"


def main(src: str, account: str, heights: list[int]) -> int:
    src_dir = Path(src)
    blocks = {h: json.loads(gzip.decompress((src_dir / f"{h}.json.gz").read_bytes())) for h in heights}
    txs = {f.tx_hash for b in blocks.values() for f in parse_block(b)
           if f.tx_hash and account in (f.a, f.b, f.token)}
    kept = 0
    with gzip.open(OUT, "wt", compresslevel=9) as out:
        for h, b in blocks.items():
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
    print(f"{len(heights)} heights, {len(txs)} transactions, {kept} receipts -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3:]]))
