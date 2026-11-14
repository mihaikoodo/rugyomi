"""Account labels (master §13: manual edits are a separate layer; raw facts are never edited). Two kinds, one read path.

MANUAL labels: the table `manual_labels` is append-only: a label is added as a row; revoking it sets `revoked_at` on that row. Written by private tooling; nothing here writes.
DERIVED labels (RGY-060): computed from the account id by an explicit list (`rules/bot_wallets.json`), never stored. A listed parent account has sub-accounts that are wallets of one
trading bot; a NEAR sub-account can be created only by its direct parent, so the id itself is the evidence. `<anything>.<parent>` at any depth matches; the parent itself, a look-alike
(`xparent`, `parent.evil.near`) and every other name pattern do not.

`active_labels` is the single read path for every reader (rules module, site, API): the manual labels that are not revoked, then the derived ones. Every label dict has the keys of
`FIELDS` plus `text` (the words a page may show). A derived label has `id` None, `source` "derived", `set_by` "rule" and a reason that names the parent and the bot.

Known manual labels:
  independent_sniper    an account that snipes launches on its own: not linked to launch creators
  bot_developer  the developer of a trading bot whose wallets are labelled by the derived rule (the row is added by the user with a reason)
Known derived labels: see `rules/bot_wallets.json`.
"""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

from .store.db import Store

LABELS = ("independent_sniper", "bot_developer")
TEXT = {"independent_sniper": "independent sniper", "bot_developer": "trading bot developer"}      # the words of the manual labels (the derived ones carry theirs in the list)
FIELDS = ("id", "account", "label", "reason", "source", "set_by", "set_at", "revoked_at", "revoke_reason")
BOT_WALLETS_PATH = Path(__file__).resolve().parent / "rules" / "bot_wallets.json"


def _con(db: Store | sqlite3.Connection) -> sqlite3.Connection:
    return db.con if isinstance(db, Store) else db


@lru_cache(maxsize=1)
def bot_wallets() -> dict[str, Any]:
    """{"version", "bots": [{"parent", "label", "text", "bot"}]} of the shipped list (one place for the wording)."""
    d = json.loads(BOT_WALLETS_PATH.read_text(encoding="utf-8"))
    for b in d["bots"]:
        if not (b["parent"] and b["label"] and b["text"] and b["bot"]):
            raise ValueError(f"incomplete entry in {BOT_WALLETS_PATH.name}: {b}")
    return {"version": d["version"], "bots": d["bots"]}


def derived_labels(account: str | None) -> list[dict[str, Any]]:
    """The labels computed from the account id (none for None / an unlisted account). Pure: reads no database."""
    if not account:
        return []
    out = []
    for b in bot_wallets()["bots"]:
        if account.endswith("." + b["parent"]) and len(account) > len(b["parent"]) + 1:
            out.append({"id": None, "account": account, "label": b["label"], "text": b["text"],
                        "reason": f"sub-account of {b['parent']} ({b['bot']})", "source": "derived", "set_by": "rule", "set_at": None,
                        "revoked_at": None, "revoke_reason": None})
    return out


def _row(r: tuple) -> dict[str, Any]:
    d = dict(zip(FIELDS, r))
    d["text"] = TEXT.get(d["label"], d["label"])
    return d


def active_labels(db: Store | sqlite3.Connection, account: str) -> list[dict[str, Any]]:
    """Labels of `account` that are not revoked: the manual ones oldest first, then the derived ones."""
    manual = [_row(r) for r in _con(db).execute(
        f"SELECT {','.join(FIELDS)} FROM manual_labels WHERE account = ? AND revoked_at IS NULL ORDER BY id",
        (account,))]
    return manual + derived_labels(account)


def has_label(db: Store | sqlite3.Connection, account: str, label: str) -> bool:
    """A manual label of that name, or a derived one."""
    if any(d["label"] == label for d in derived_labels(account)):
        return True
    return _con(db).execute("SELECT 1 FROM manual_labels WHERE account = ? AND label = ? AND revoked_at IS NULL",
                            (account, label)).fetchone() is not None


def all_labels(db: Store | sqlite3.Connection, include_revoked: bool = True) -> list[dict[str, Any]]:
    """The MANUAL rows (the audit trail); derived labels are not rows."""
    q = f"SELECT {','.join(FIELDS)} FROM manual_labels"
    if not include_revoked:
        q += " WHERE revoked_at IS NULL"
    return [_row(r) for r in _con(db).execute(q + " ORDER BY id")]
