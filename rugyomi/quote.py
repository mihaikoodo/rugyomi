"""RGY-066: the quote token of a launch pool.

A launch pool id is `A|B|fee` (the two token ids sorted, joined with `|`). The amounts of its swaps are raw units of the two tokens. The trade attribution
(`snapshots.attribute`) reads the quote side as NEAR (yocto) - true only when the quote is wNEAR. Another quote token (USDC-like with 6 decimals, a meme token with 24)
gives numbers in that token's units: they must never be shown as NEAR, added to NEAR amounts or compared with a NEAR threshold (the rug test's loss line).
Q-046 (13.11): the NEAR-denominated stored values of such launches are removed (`snapshots.NEAR_COLUMNS`) and their rug label is "undefined: the pool is not paired
with NEAR"; a price source for the quote (RGY-070) may bring the numbers back. Leaf module: imports nothing of the package except the wNEAR constant."""
from __future__ import annotations

from .parse import WRAP

UNDEFINED_KIND = "quote"
REASON = "the pool is not paired with NEAR, the loss in NEAR is unknown"


def quote_token(token: str, pool: str | None) -> str | None:
    """The other token of the pool id `A|B|fee`, None when the id does not have that shape."""
    toks = [p for p in (pool or "").split("|") if p and not p.isdigit()]
    others = [p for p in toks if p != token]
    return others[0] if len(toks) == 2 and len(others) == 1 and token in toks else None


def not_near_quote(token: str, pool: str | None) -> str | None:
    """The quote token when the pool is NOT paired with wNEAR, else None (also None when the pool id cannot be read: such a launch stays as it was)."""
    q = quote_token(token, pool)
    return q if q and q != WRAP else None
