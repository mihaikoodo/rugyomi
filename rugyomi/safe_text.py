"""RGY-061 (AUD-005): a name chosen by a token's creator goes into our channel posts and card reasons; it must not be able to carry a link, a
mention or a claim ("Risk 0/100 verified"). Telegram clients turn plain `t.me/…`, `@name` and domains into links, so the only safe name is a plain one.

Choice (sanitize, not wrap in <code>): a name that is not plain is replaced by the first label of the token id. Not relying on Telegram's rendering of
<code> keeps the text safe in every client and in the plain-text forms of the post.
"""
from __future__ import annotations

MAX_LEN = 24
_EXTRA = "$_-"


def _plain(s: str) -> bool:
    return bool(s) and len(s) <= MAX_LEN and all(ch.isalnum() or ch in _EXTRA for ch in s) and any(ch.isalnum() for ch in s)


def safe_symbol(raw: str | None, token: str) -> str:
    """`raw` when it is 1–24 characters of letters, digits and `$_-` only; otherwise the first label of `token` (the part before the first dot),
    cleaned the same way and cut to 24 characters; "token" if nothing is left."""
    s = (raw or "").strip()
    if _plain(s):
        return s
    label = (token or "").split(".")[0]
    out = "".join(ch for ch in label if ch.isalnum() or ch in _EXTRA)[:MAX_LEN]
    return out if _plain(out) else "token"
