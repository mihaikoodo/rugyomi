"""python -m rugyomi.web [--db PATH | --demo] [--host 127.0.0.1] [--port 8000] — serves the site (local address only by default)."""
from __future__ import annotations

import argparse
import logging


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rugyomi.web", description=__doc__)
    ap.add_argument("--db", default=None, help="SQLite file (default $RUGYOMI_DB or ~/.rugyomi/rugyomi.db), opened read-only")
    ap.add_argument("--demo", action="store_true", help="serve the demo database shipped in demo/ (unpacked once into a temp directory); no network, no keys")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    if args.demo:
        from .demo import unpack
        args.db = str(unpack())
    elif args.db and str(args.db).endswith(".gz"):
        from .demo import unpack
        args.db = str(unpack(args.db))
    import uvicorn

    from .app import create_app
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="warning", access_log=False,
                timeout_keep_alive=5, server_header=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
