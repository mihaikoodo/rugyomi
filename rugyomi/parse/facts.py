"""Parse raw facts out of a neardata block (format of GET /v0/block/{h}). Standard library only, no module state.

Success rule: a receipt counts only if its execution outcome status is `SuccessValue` or `SuccessReceiptId`.
A `Failure` receipt changed no state (NEAR receipts are atomic), so neither its actions nor its logs become
facts; it is counted in `ParseStats.skipped_failed`. Delegate (meta-transaction) actions are not facts: their
inner actions run as a separate receipt `sender -> receiver`, parsed like any other.

Stable fact key: (receipt_id, origin). It depends only on where the fact sits in the receipt, never on parse
order or on which kinds are kept:
  a{i}       the i-th action of the receipt (0-based)
  l{j}       the j-th log line of the outcome, a plain-text line (wrap.near movements)
  l{j}.{k}   the k-th data item of the EVENT_JSON log line j
The transaction hash is not part of the key (it may be missing).

Error isolation: an exception while reading one action or one log line becomes a `ParseError` with that
origin; the receipt's other facts are kept. Nothing is dropped silently.

Fact kinds (`Fact.kind`):
  near_transfer     Transfer action, a=predecessor -> b=receiver. Protocol refunds (predecessor `system`) are
                    NOT facts (option A excludes them); they are only counted in stats.near_refunds.
  near_fc_deposit   NEAR attached to a FunctionCall (> 1 yocto), a=predecessor -> b=receiver; extra.method
  wnear_transfer / wnear_mint / wnear_burn   wrap.near movements. Rule (one fact per movement): wrap.near logs
                    them as plain text (`Transfer N from A to B`, `Refund N from A to B` -> extra.refund=true,
                    `Deposit N NEAR to A` = mint, `Withdraw N NEAR from A` = burn). If a wrap.near receipt has
                    at least one such text line, its NEP-141 EVENT_JSON lines (if wrap ever emits them too) are
                    ignored and counted in stats.wrap_events_ignored; if it has none, the events are used.
  create_account    a=new account, b=creator (predecessor)
  add_key           a=account, b=predecessor; extra: public_key, permission ("full" or {receiver, methods})
  intents:<event>   every EVENT_JSON item emitted by intents.near
  launch:<event>    events of nearlytrade.near / launchpad.justhoot.near (v1) / v2-launchpad.justhoot.near /
                    v2-pool-creator.justhoot.near;
                    extra: contract, standard, data (the raw item)
  dcl_swap          `swap` events of dclv2.ref-labs.near; a=swapper, b=pool id; amounts in extra as decimal
                    strings or null (never "None"); extra.launch_pool when a pool token is a launch token
  ft_transfer / ft_mint / ft_burn   NEP-141 events of launch tokens
Launch tokens: sub-accounts of nearlytrade.near, launchpad.justhoot.near (v1) or v2-launchpad.justhoot.near — only the parent can create a
sub-account, so the namespace cannot be faked — except the launchpad's own service sub-accounts:
launchbot.nearlytrade.near and the liquidity lockers lock_<n>.nearlytrade.near.
Amounts are integers in base units, kept as decimal strings (u128 does not fit SQLite INTEGER).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

WRAP = "wrap.near"
INTENTS = "intents.near"
DCL = "dclv2.ref-labs.near"
NEARLY = "nearlytrade.near"
LAUNCHBOT = "launchbot.nearlytrade.near"
JH_V1 = "launchpad.justhoot.near"                 # justhoot v1 (standard `hoot_launcher`; tokens <name>-<hash>.launchpad.justhoot.near)
JH_V2 = "v2-launchpad.justhoot.near"
JH_V2_POOL = "v2-pool-creator.justhoot.near"
LAUNCH_CONTRACTS = (NEARLY, JH_V1, JH_V2, JH_V2_POOL)
LAUNCH_NAMESPACES = (NEARLY, JH_V1, JH_V2)
_NEARLY_LOCKER = re.compile(r"^lock_\d+\.nearlytrade\.near$")     # inside the nearly namespace: only nearly creates it
EVENT_PREFIX = "EVENT_JSON:"
_ACC = r"([a-z0-9_\-\.]+)"
WRAP_LOG = (
    (re.compile(rf"^Transfer (\d+) from {_ACC} to {_ACC}$"), "wnear_transfer", False),
    (re.compile(rf"^Refund (\d+) from {_ACC} to {_ACC}$"), "wnear_transfer", True),
    (re.compile(rf"^Deposit (\d+) NEAR to {_ACC}$"), "wnear_mint", False),
    (re.compile(rf"^Withdraw (\d+) NEAR from {_ACC}$"), "wnear_burn", False),
)
OPTION_A_KINDS = frozenset({"near_transfer", "near_fc_deposit", "wnear_transfer", "wnear_mint", "wnear_burn",
                            "create_account", "add_key", "ft_transfer", "ft_mint", "ft_burn"})


@dataclass(slots=True)
class Fact:
    kind: str
    height: int
    ts_ns: int
    tx_hash: str | None
    receipt_id: str
    origin: str
    a: str | None
    b: str | None = None
    token: str | None = None
    amount: str | None = None
    extra: dict[str, Any] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return self.receipt_id, self.origin


@dataclass(slots=True)
class ParseError:
    height: int
    receipt_id: str | None
    origin: str
    error: str


@dataclass
class ParseStats:
    blocks: int = 0
    receipts: int = 0
    receipts_ok: int = 0
    receipts_failed: int = 0
    receipts_unknown_status: int = 0
    near_refunds: int = 0
    delegate_actions: int = 0
    wrap_events_ignored: int = 0
    skipped_failed: Counter = field(default_factory=Counter)


def keep_option_a(f: Fact) -> bool:
    """Storage policy, option A: chain-wide movements, accounts, keys, Intents; launch-specific events."""
    k = f.kind
    if k in OPTION_A_KINDS or k.startswith("intents:") or k.startswith("launch:"):
        return True
    return k == "dcl_swap" and bool((f.extra or {}).get("launch_pool"))


def block_height(block: dict[str, Any]) -> int:
    return int(block["block"]["header"]["height"])


def block_ts_ns(block: dict[str, Any]) -> int:
    h = block["block"]["header"]
    return int(h.get("timestamp_nanosec") or h["timestamp"])


def status_kind(outcome: dict[str, Any]) -> str:
    st = outcome.get("status")
    if isinstance(st, dict):
        if "SuccessValue" in st or "SuccessReceiptId" in st:
            return "ok"
        if "Failure" in st:
            return "failed"
    return "unknown"


def is_launch_token(account: str | None) -> bool:
    if not account or account == LAUNCHBOT or _NEARLY_LOCKER.match(account):
        return False
    return any(account.endswith("." + ns) for ns in LAUNCH_NAMESPACES)


def _items(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


def _first(d: dict[str, Any], *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return str(v)
    return None


def _amount(v: Any) -> str | None:
    """Base-unit integer as a decimal string; None when absent. Raises on garbage (-> ParseError)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool) or isinstance(v, (list, dict, float)):
        raise ValueError(f"amount is not an integer: {v!r}")
    n = int(str(v))
    if n < 0:
        raise ValueError(f"negative amount: {v!r}")
    return str(n)


def _soft_amount(v: Any) -> tuple[str | None, Any]:
    """For amounts inside extra: (decimal string or None, raw value if it was not an integer)."""
    try:
        return _amount(v), None
    except (ValueError, TypeError):
        return None, v


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _would_be_kinds(receipt: dict[str, Any], outcome: dict[str, Any]) -> list[str]:
    kinds = []
    action = (receipt.get("receipt") or {}).get("Action") or {}
    for a in action.get("actions") or []:
        k = a if isinstance(a, str) else next(iter(a), "") if isinstance(a, dict) else ""
        kinds.append({"Transfer": "near_transfer", "CreateAccount": "create_account", "AddKey": "add_key",
                      "FunctionCall": "function_call"}.get(k, k))
    if outcome.get("logs"):
        kinds.append("logs")
    return kinds


def parse_block(block: dict[str, Any], stats: ParseStats | None = None,
                errors: list[ParseError] | None = None) -> list[Fact]:
    """All facts of one block, in block order. Parse errors go to `errors` (a list you pass in; created if None)."""
    stats = stats if stats is not None else ParseStats()
    errors = errors if errors is not None else []
    stats.blocks += 1
    height = block_height(block)
    ts = block_ts_ns(block)
    out: list[Fact] = []
    for shard in block.get("shards") or []:
        for reo in (shard or {}).get("receipt_execution_outcomes") or []:
            rid = ((reo.get("receipt") or {}).get("receipt_id")) if isinstance(reo, dict) else None
            try:
                _parse_reo(reo, height, ts, stats, out, errors)
            except Exception as e:  # noqa: BLE001 — receipt-level structure error: recorded, never silent
                errors.append(ParseError(height, rid, "r", f"{type(e).__name__}: {e}"))
    return out


def _parse_reo(reo: dict[str, Any], height: int, ts: int, stats: ParseStats, out: list[Fact],
               errors: list[ParseError]) -> None:
    receipt = reo.get("receipt") or {}
    outcome = (reo.get("execution_outcome") or {}).get("outcome") or {}
    stats.receipts += 1
    sk = status_kind(outcome)
    if sk != "ok":
        if sk == "failed":
            stats.receipts_failed += 1
        else:
            stats.receipts_unknown_status += 1
        for k in _would_be_kinds(receipt, outcome):
            stats.skipped_failed[k] += 1
        return
    stats.receipts_ok += 1

    tx = reo.get("tx_hash") or None
    rid = str(receipt.get("receipt_id") or "")
    if not rid:
        raise ValueError("receipt without receipt_id")
    pred = receipt.get("predecessor_id") or ""
    recv = receipt.get("receiver_id") or ""
    executor = outcome.get("executor_id") or recv
    action = (receipt.get("receipt") or {}).get("Action")

    def fact(kind: str, origin: str, a: str | None, b: str | None = None, token: str | None = None,
             amount: Any = None, extra: dict[str, Any] | None = None) -> None:
        out.append(Fact(kind, height, ts, tx, rid, origin, a, b, token, _amount(amount), extra or None))

    def guarded(origin: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — one action / log item; the rest of the receipt is kept
            errors.append(ParseError(height, rid, origin, f"{type(e).__name__}: {e}"))

    if action:
        signer = action.get("signer_id")
        for i, a in enumerate(action.get("actions") or []):
            guarded(f"a{i}", lambda a=a, i=i: _action(a, f"a{i}", pred, recv, signer, stats, fact))

    logs = outcome.get("logs") or []
    if not logs:
        return
    # per line: a non-string log line is reported below for itself, it must not fail the whole receipt here
    wrap_text = executor == WRAP and any(isinstance(line, str) and rx.match(line)
                                         for line in logs for rx, _k, _r in WRAP_LOG)
    for j, line in enumerate(logs):
        if not isinstance(line, str):
            errors.append(ParseError(height, rid, f"l{j}", f"log line is not a string: {type(line).__name__}"))
            continue
        if line.startswith(EVENT_PREFIX):
            try:
                payload = json.loads(line[len(EVENT_PREFIX):])
            except json.JSONDecodeError as e:
                errors.append(ParseError(height, rid, f"l{j}", f"bad EVENT_JSON: {e}"))
                continue
            if not isinstance(payload, dict):
                errors.append(ParseError(height, rid, f"l{j}", "EVENT_JSON payload is not an object"))
                continue
            _event(payload, j, executor, wrap_text, stats, fact, guarded)
        elif executor == WRAP:
            guarded(f"l{j}", lambda line=line, j=j: _wrap_line(line, f"l{j}", fact))


def _action(a: Any, origin: str, pred: str, recv: str, signer: str | None, stats: ParseStats, fact) -> None:
    if a == "CreateAccount" or (isinstance(a, dict) and "CreateAccount" in a):
        fact("create_account", origin, recv, pred, extra={"signer": signer} if signer and signer != pred else None)
        return
    if not isinstance(a, dict):
        return
    if "Transfer" in a:
        dep = int((a["Transfer"] or {}).get("deposit") or 0)
        if pred == "system":
            stats.near_refunds += 1
        else:
            fact("near_transfer", origin, pred, recv, amount=dep,
                 extra={"signer": signer} if signer and signer != pred else None)
    elif "FunctionCall" in a:
        fc = a["FunctionCall"] or {}
        dep = int(fc.get("deposit") or 0)
        if dep > 1 and pred != "system":
            fact("near_fc_deposit", origin, pred, recv, amount=dep, extra={"method": fc.get("method_name")})
    elif "AddKey" in a:
        ak = a["AddKey"] or {}
        perm = (ak.get("access_key") or {}).get("permission")
        if perm == "FullAccess" or perm is None:
            p: Any = "full"
        else:
            fcp = (perm or {}).get("FunctionCall") or {}
            p = {"receiver": fcp.get("receiver_id"), "methods": fcp.get("method_names") or []}
        fact("add_key", origin, recv, pred, extra={"public_key": ak.get("public_key"), "permission": p})
    elif "Delegate" in a:
        stats.delegate_actions += 1


def _wrap_line(line: str, origin: str, fact) -> None:
    for rx, kind, refund in WRAP_LOG:
        m = rx.match(line)
        if not m:
            continue
        g = m.groups()
        if kind == "wnear_transfer":
            fact(kind, origin, g[1], g[2], WRAP, g[0], {"refund": True} if refund else None)
        else:
            fact(kind, origin, g[1], None, WRAP, g[0])
        return


def _event(ev: dict[str, Any], j: int, executor: str, wrap_text: bool, stats: ParseStats, fact, guarded) -> None:
    name = str(ev.get("event") or "")
    std = ev.get("standard")
    items = _items(ev.get("data"))

    def each(fn: Callable[[dict[str, Any], str], None]) -> None:
        for k, it in enumerate(items):
            origin = f"l{j}.{k}"
            if not isinstance(it, dict):
                guarded(origin, lambda it=it: _fail(f"event item is not an object: {it!r}"[:200]))
                continue
            guarded(origin, lambda it=it, origin=origin: fn(it, origin))

    if executor == INTENTS:
        def intents(it: dict[str, Any], origin: str) -> None:
            a_ = _first(it, "account_id", "owner_id", "old_owner_id", "sender_id")
            b_ = _first(it, "receiver_id", "new_owner_id")
            tok = _first(it, "token", "token_id")
            rest = {k: v for k, v in it.items() if k not in ("account_id", "owner_id", "old_owner_id", "sender_id",
                                                              "receiver_id", "new_owner_id", "token", "token_id",
                                                              "amount")}
            amt, raw = _soft_amount(it.get("amount"))
            if raw is not None:
                rest["amount"] = raw          # lists / objects (multi-token events) stay in extra, as they are
            fact(f"intents:{name}", origin, a_, b_, tok, amt, rest or None)
        each(intents)
    elif executor in LAUNCH_CONTRACTS:
        each(lambda it, origin: fact(f"launch:{name}", origin,
                                     _first(it, "creator", "creator_id", "owner_id", "account_id"),
                                     _first(it, "token", "token_id"), None, None,
                                     {"contract": executor, "standard": std, "data": it}))
    elif executor == DCL and name == "swap":
        def swap(it: dict[str, Any], origin: str) -> None:
            pool = str(it.get("pool_id") or "")
            amount_in, raw_in = _soft_amount(it.get("amount_in"))
            amount_out, raw_out = _soft_amount(it.get("amount_out"))
            extra = {"token_in": it.get("token_in"), "token_out": it.get("token_out"),
                     "amount_in": amount_in, "amount_out": amount_out,
                     "launch_pool": any(is_launch_token(t) for t in pool.split("|")[:2])}
            if raw_in is not None or raw_out is not None:
                extra["raw_amounts"] = [raw_in, raw_out]
            fact("dcl_swap", origin, _first(it, "swapper"), pool, None, None, extra)
        each(swap)
    elif std == "nep141" and name in ("ft_transfer", "ft_mint", "ft_burn"):
        if executor == WRAP:
            if wrap_text:
                stats.wrap_events_ignored += len(items)
                return
            kind = "wnear_" + name[3:]
        elif is_launch_token(executor):
            kind = name
        else:
            return

        def ft(it: dict[str, Any], origin: str) -> None:
            if name == "ft_transfer":
                fact(kind, origin, _first(it, "old_owner_id"), _first(it, "new_owner_id"), executor, it.get("amount"))
            else:
                fact(kind, origin, _first(it, "owner_id"), None, executor, it.get("amount"))
        each(ft)
