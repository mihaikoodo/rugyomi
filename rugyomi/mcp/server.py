"""The four tools. Each one is a single GET of the API (`web/api.py`) through a backend; the answer is the API's JSON, unchanged."""
from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from .backend import ApiError, check_id

NAME = "rugyomi"
VERSION = "0.1.0"
INSTRUCTIONS = (
    "rugyomi scores token launches on NEAR launchpads (nearly.trade, justhoot.fun) for the risk that the launch is a rug within 24 hours. "
    "It is analysis, not advice: a score is an estimate from public on-chain facts, every reason carries evidence links, and a launch can be scored "
    "'live' (a card issued at launch time) or computed afterwards (never presented as a prediction). Start with get_meta for the bands and the recent rug rate."
)
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True)


def build(backend: Any) -> MCPServer:
    mcp = MCPServer(NAME, title="rugyomi - NEAR launch risk", instructions=INSTRUCTIONS, version=VERSION)

    def ident(value: Any, what: str) -> str:
        try:
            return check_id(value, what)
        except ApiError as e:
            raise ToolError(str(e)) from None

    async def call(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await backend.get(path, params)
        except ApiError as e:
            raise ToolError(str(e)) from None

    @mcp.tool(title="Risk card of one token launch", annotations=READ_ONLY)
    async def get_launch_risk(token: str) -> dict[str, Any]:
        """Risk analysis of ONE token launch on NEAR (nearly.trade, justhoot.fun), by its token account id (e.g. 'name.nearlytrade.near').

        Returns the launch facts (launchpad, creator, owner, launch time, quote token), `risk` (score 0-100 = risk that the launch turns into a rug within 24 h,
        the band Low / Elevated / High, the reasons that gave points, each with evidence links to transactions or accounts; `kind` is 'live' for a card issued at
        launch time; any other value is a card computed afterwards, not a forecast published at the time) and `outcome` (rug / no_rug / undefined / waiting once known; a pool paired with a non-NEAR quote token can be 'undefined').
        Analysis, not advice. Errors: 'not found' when the site has not seen the token."""
        return await call(f"/api/v1/launch/{ident(token, 'token')}")

    @mcp.tool(title="Recent launches with their risk", annotations=READ_ONLY)
    async def recent_launches(since: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Newest launches first, each with its score / band (live or computed afterwards) and its outcome state.

        `since`: only launches at or after this time - unix seconds or an ISO 8601 UTC date / date-time ('2026-11-10', '2026-11-10T12:00:00Z'). `limit`: 1..200 (default 50).
        Use get_launch_risk for the reasons of one launch. Analysis, not advice."""
        return await call("/api/v1/launches", {"since": since, "limit": limit})

    @mcp.tool(title="What the site knows about one account", annotations=READ_ONLY)
    async def get_account(account: str) -> dict[str, Any]:
        """One NEAR account: its labels (with the reason and source of each), its launches as a creator (count and outcomes: rug / no_rug / undefined / waiting / not_tracked)
        and a summary of its trading (no rows - the page lists them). Errors: 'not found' for an account the site has never seen. Analysis, not advice."""
        return await call(f"/api/v1/account/{ident(account, 'account')}")

    @mcp.tool(title="Live profile, bands and recent rug rate", annotations=READ_ONLY)
    async def get_meta() -> dict[str, Any]:
        """What the scores mean: the live rules profile and configuration, the score bands (name, from, to), the recent rug rate with its interval, the disclaimer,
        and links to the methodology, the accuracy table and the append-only ledger of issued cards. Call it first to read a score."""
        return await call("/api/v1/meta")

    return mcp
