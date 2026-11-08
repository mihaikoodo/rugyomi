"""Facts about accounts that several readers need (single source).

account_created(): when and by whom an account was created — from our own `create_account` facts first, then from
the imported history (`hist_accounts`, v4). No network. The start of exact account age (OQ-07): implicit accounts
and accounts older than both sources return None.

`creator` differs by source: in our facts it is the receipt predecessor — for top-level names the registrar
(`near`, `tg`) — and the transaction signer, who paid, is `signer`; in the history it is the account recorded then,
usually that signer (e.g. thecoins.near: facts creator `near`, signer and history creator `meteor-relayer.near`).
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .store.db import Store, hash_str


def _con(db: Store | sqlite3.Connection) -> sqlite3.Connection:
    return db.con if isinstance(db, Store) else db


def account_created(db: Store | sqlite3.Connection, account: str) -> dict[str, Any] | None:
    """{height, ts_ns, creator, tx, source: 'facts' | 'history'} or None if unknown."""
    con = _con(db)
    r = con.execute(
        # INDEXED BY: without it the planner walks every `create_account` fact (facts_kind_height) and filters by
        # account; by account (facts_a_ts) it touches the few facts of this one account (RGY-014: a page query).
        "SELECT f.height, f.ts_ns, bb.name, f.tx, f.extra FROM accounts aa "
        "JOIN facts f INDEXED BY facts_a_ts ON f.a = aa.id JOIN kinds k ON k.id = f.kind "
        "LEFT JOIN accounts bb ON bb.id = f.b "
        "WHERE k.name = 'create_account' AND aa.name = ? ORDER BY f.height, f.id LIMIT 1", (account,)).fetchone()
    if r:
        signer = (json.loads(r[4]) if r[4] else {}).get("signer") or r[2]
        return {"height": r[0], "ts_ns": r[1], "creator": r[2], "signer": signer, "tx": hash_str(r[3]),
                "source": "facts"}
    try:
        h = con.execute("SELECT height, ts_ns, creator FROM hist_accounts WHERE account = ?", (account,)).fetchone()
    except sqlite3.OperationalError:          # a database before v4
        return None
    if h:
        return {"height": h[0], "ts_ns": h[1], "creator": h[2], "signer": None, "tx": None, "source": "history"}
    return None
