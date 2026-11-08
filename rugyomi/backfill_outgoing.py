"""RGY-023 step 1: the OUTGOING NEAR / wNEAR transfers of every launch participant, full history, from the FastNear Transfers API.

Why: the creator dossier answers «where does the money go» — what a creator (and the insiders around him) sent on after a launch.
The incoming side was loaded in RGY-020 phase C; this is the same work with the API's `direction: sender`, for the same participants
(`links.participants`: the explicit service accounts are excluded) and with the same limits (0.1 NEAR floor, 20 pages per asset and
account, oldest rows dropped and the account marked `truncated`).

Storage choice: the SAME table, `transfers_api`, with `dir = 'out'` (the column and its CHECK existed since schema v12, so there is
NO migration); `account` is the sender, `other` the receiver. Every reader of the incoming rows already filters `dir = 'in'`.
The queue kind is `transfers_out` (a unit = one participant), run by the same runner (the backfill runner, kind
`transfers_out`) and kept up to date by the autofill worker. `signer` / `pred` are stored as for the incoming rows.
"""
from __future__ import annotations

from typing import Any, Callable

from . import backfill_transfers as T
from .backfill import Api
from .store import Store

KIND = T.OUT_KIND
TR_BASE = T.TR_BASE


def enqueue(store: Store) -> dict[str, int]:
    return T.enqueue(store, KIND)


def process_account(store: Store, api: Api, account: str, log: Callable[[str], None]) -> dict[str, Any]:
    return T.process_account(store, api, account, log, kind=KIND, direction="sender")


def run(store: Store, api: Api, stop: Callable[[], bool], max_credits: int | None = None, limit: int | None = None,
        only: str | None = None, log: Callable[[str], None] = print, retry_errors: bool = True) -> dict[str, Any]:
    return T.run(store, api, stop, max_credits, limit, only, log, kind=KIND, process=process_account, retry_errors=retry_errors)


def status(store: Store) -> dict[str, Any]:
    return T.status(store, KIND)
