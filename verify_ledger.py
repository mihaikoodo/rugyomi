#!/usr/bin/env python3
"""Check the rugyomi forecast ledger without installing anything (Python 3.9+, standard library only).

    python verify_ledger.py ledger/ledger.jsonl
    python verify_ledger.py ledger/ledger.jsonl --anchor 113:<hash from the channel post> --anchor <another hash>
    python verify_ledger.py ledger/ledger.jsonl --anchors-file anchors.txt        (one `COUNT:HASH` or `HASH` per line)

It recomputes the hash chain of every line and prints `OK` with the head, or the first broken line. An anchor is the head hash the channel posted at the end of a UTC
day (`anchor COUNT:HASH`): the script checks that it is in the chain, at that position. The same code runs as `python -m rugyomi.ledger verify ...`.
"""
import importlib.util
import pathlib
import sys

_spec = importlib.util.spec_from_file_location("rugyomi_ledger", pathlib.Path(__file__).resolve().parent / "rugyomi" / "ledger.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

if __name__ == "__main__":
    raise SystemExit(_mod.main(["verify", *sys.argv[1:]]))
