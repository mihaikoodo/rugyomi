"""Manual account labels (master §13: manual edits are a separate layer; raw facts are never edited).

The table `manual_labels` is append-only: a label is added as a row; revoking it sets `revoked_at` on that row.
This module is the single read path for every future reader (rules module, site, API). Labels are written by
private tooling; nothing here writes.

Known labels:
  independent_sniper   an account that snipes launches on its own: not linked to launch creators
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .store.db import Store

LABELS = ("independent_sniper",)
FIELDS = ("id", "account", "label", "reason", "source", "set_by", "set_at", "revoked_at", "revoke_reason")


def _con(db: Store | sqlite3.Connection) -> sqlite3.Connection:
    return db.con if isinstance(db, Store) else db


def active_labels(db: Store | sqlite3.Connection, account: str) -> list[dict[str, Any]]:
    """Labels of `account` that are not revoked, oldest first."""
    return [dict(zip(FIELDS, r)) for r in _con(db).execute(
        f"SELECT {','.join(FIELDS)} FROM manual_labels WHERE account = ? AND revoked_at IS NULL ORDER BY id",
        (account,))]


def has_label(db: Store | sqlite3.Connection, account: str, label: str) -> bool:
    return _con(db).execute("SELECT 1 FROM manual_labels WHERE account = ? AND label = ? AND revoked_at IS NULL",
                            (account, label)).fetchone() is not None


def all_labels(db: Store | sqlite3.Connection, include_revoked: bool = True) -> list[dict[str, Any]]:
    q = f"SELECT {','.join(FIELDS)} FROM manual_labels"
    if not include_revoked:
        q += " WHERE revoked_at IS NULL"
    return [dict(zip(FIELDS, r)) for r in _con(db).execute(q + " ORDER BY id")]
