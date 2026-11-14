"""python -m rugyomi.mcp [--demo | --base-url URL] - the MCP server on stdio (see the package docstring)."""
from __future__ import annotations

import argparse
import os
import sys

from . import DEFAULT_BASE_URL, ENV_BASE_URL


def make_backend(args: argparse.Namespace):
    from .backend import DemoBackend, HttpBackend
    if args.demo:
        return DemoBackend(args.db)
    return HttpBackend(args.base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rugyomi.mcp", description=__doc__)
    ap.add_argument("--demo", action="store_true", help="answer from the demo database shipped in demo/ (this process, no network, no keys); needs the [web] extra")
    ap.add_argument("--db", default=None, help="with --demo: another SQLite file (or a .gz) instead of the shipped demo")
    ap.add_argument("--base-url", default=None, help=f"the site to ask (default ${ENV_BASE_URL} or {DEFAULT_BASE_URL})")
    args = ap.parse_args(argv)
    if args.db and not args.demo:
        ap.error("--db needs --demo")
    try:
        from .server import build
    except ImportError as e:
        print(f"the MCP SDK is not installed ({e}); install it with: pip install -e '.[mcp]'", file=sys.stderr)
        return 2
    try:
        backend = make_backend(args)
    except ValueError as e:
        ap.error(str(e))
    print(f"rugyomi MCP server (read-only, stdio): {backend.describe()}", file=sys.stderr)       # stderr: stdout is the protocol
    build(backend).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
