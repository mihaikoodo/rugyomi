"""RGY-068: a read-only MCP server (stdio) over the rugyomi API, for AI agents.

    python -m rugyomi.mcp                    the live site (https://mihaikoodo.dev, or --base-url / $RUGYOMI_API_BASE)
    python -m rugyomi.mcp --demo             the demo database shipped in demo/, answered by the very same API code in this process: no network, no keys

The server runs LOCALLY at the agent (stdio): the site itself speaks GET and HEAD only. Every tool is one GET of `/api/v1/...` (`web/api.py`) and returns the JSON the
API returns, so the numbers are the ones the site shows. No write tool, no key, no other host. Install: `pip install -e ".[mcp]"` (demo mode also needs `".[web]"`)."""
from __future__ import annotations

DEFAULT_BASE_URL = "https://mihaikoodo.dev"
ENV_BASE_URL = "RUGYOMI_API_BASE"
