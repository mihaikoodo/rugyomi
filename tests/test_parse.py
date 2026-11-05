"""rugyomi.parse on real mainnet blocks (ported RGY-001 parser tests) + stable keys, error isolation, traps."""
import copy
import json

import pytest

from rugyomi_testkit import block, fixture_blocks, hid, reo
from rugyomi.parse import ParseStats, is_launch_token, keep_option_a, parse_block, status_kind
from rugyomi.store.db import b58decode


@pytest.fixture(scope="module")
def parsed():
    out = {}
    for h, b in fixture_blocks("cache"):
        if b is None:
            out[h] = None
            continue
        st, errs = ParseStats(), []
        out[h] = (b, parse_block(b, st, errs), st, errs)
    return out


# ---------------------------------------------------------------- parity with the RGY-001 parser

def test_null_marker_is_a_skipped_height(parsed):
    assert parsed[220657477] is None


def test_no_parse_errors(parsed):
    for h, v in parsed.items():
        if v:
            assert v[3] == [], h


def test_failed_receipts_produce_no_facts(parsed):
    seen_failed = 0
    for h, v in parsed.items():
        if not v:
            continue
        b, fs, _, _ = v
        failed = {r["receipt"]["receipt_id"] for s in b["shards"] for r in s["receipt_execution_outcomes"]
                  if "Failure" in r["execution_outcome"]["outcome"]["status"]}
        seen_failed += len(failed)
        assert not failed & {f.receipt_id for f in fs}, h
    assert seen_failed > 0
    assert parsed[217919751][2].skipped_failed["near_transfer"] == 1


def test_native_transfers_match_an_independent_count(parsed):
    for h, v in parsed.items():
        if not v:
            continue
        b, fs, st, _ = v
        want_user, want_refund = 0, 0
        for s in b["shards"]:
            for r in s["receipt_execution_outcomes"]:
                if status_kind(r["execution_outcome"]["outcome"]) != "ok":
                    continue
                for a in r["receipt"]["receipt"].get("Action", {}).get("actions", []):
                    if isinstance(a, dict) and "Transfer" in a:
                        if r["receipt"]["predecessor_id"] == "system":
                            want_refund += 1
                        else:
                            want_user += 1
        assert sum(f.kind == "near_transfer" for f in fs) == want_user, h
        # protocol refunds are not facts any more (option A excludes them); they are counted
        assert not any(f.kind == "near_refund" for f in fs)
        assert st.near_refunds == want_refund, h


def test_nearly_launch_started_fields(parsed):
    _b, fs, _, _ = parsed[217919619]
    (f,) = [x for x in fs if x.kind == "launch:launch_started"]
    d = f.extra["data"]
    assert (f.a, f.b) == ("dancecat.near", "dancecat.nearlytrade.near")
    assert d["pool_id"] == "dancecat.nearlytrade.near|wrap.near|10000"
    assert d["dev_buy"] == "34000000000000000000000000"
    assert f.tx_hash == "8eEyHxg8QXvkCpFLWd5fbdSmgZ8j4bw773jwSu6HCPGA"
    assert f.ts_ns == 1790772754264308046


def test_justhoot_v2_launch_events(parsed):
    _b, fs, _, _ = parsed[220657479]
    kinds = {f.kind for f in fs}
    assert {"launch:launch_planned", "launch:launch_priced", "launch:dev_buy_near_wrapped"} <= kinds
    (p,) = [f for f in fs if f.kind == "launch:launch_planned"]
    assert p.b and p.b.endswith(".v2-launchpad.justhoot.near")


def test_create_account_and_add_key(parsed):
    _b, fs, _, _ = parsed[217919622]
    created = [f for f in fs if f.kind == "create_account"]
    keys = [f for f in fs if f.kind == "add_key"]
    assert created and keys
    for f in keys:
        assert f.extra["public_key"].startswith(("ed25519:", "secp256k1:"))
        assert f.extra["permission"] == "full" or "receiver" in f.extra["permission"]


def test_wrap_text_logs(parsed):
    kinds = {f.kind for v in parsed.values() if v for f in v[1]}
    assert {"wnear_transfer", "wnear_mint", "wnear_burn"} <= kinds
    for h, v in parsed.items():
        if not v:
            continue
        b, fs, _, _ = v
        lines = 0
        for s in b["shards"]:
            for r in s["receipt_execution_outcomes"]:
                o = r["execution_outcome"]["outcome"]
                if o["executor_id"] != "wrap.near" or status_kind(o) != "ok":
                    continue
                lines += sum(1 for l in o["logs"] if l.split(" ", 1)[0] in ("Transfer", "Refund", "Deposit", "Withdraw"))
        assert sum(f.kind.startswith("wnear_") for f in fs) == lines, h


def test_launch_token_ft_and_launch_pool_swaps(parsed):
    _b, fs, _, _ = parsed[217919672]
    ft = [f for f in fs if f.kind == "ft_transfer"]
    assert ft and all(is_launch_token(f.token) for f in ft)
    assert any(f.kind == "dcl_swap" and f.extra["launch_pool"] for f in fs)


def test_intents_withdrawals(parsed):
    _b, fs, _, _ = parsed[217919672]
    w = [f for f in fs if f.kind == "intents:native_withdraw"][0]
    assert w.a and w.b and int(w.amount) > 0


def test_fc_deposit_excludes_one_yocto(parsed):
    for v in parsed.values():
        if v:
            assert all(int(f.amount) > 1 for f in v[1] if f.kind == "near_fc_deposit")


def test_b58_length():
    assert len(b58decode("8eEyHxg8QXvkCpFLWd5fbdSmgZ8j4bw773jwSu6HCPGA")) == 32
    assert b58decode("1111") == b"\x00" * 4


# ---------------------------------------------------------------- stable keys

def test_keys_are_unique_and_stable_across_reparse_and_filters(parsed):
    for h, v in parsed.items():
        if not v:
            continue
        b = v[0]
        first = parse_block(copy.deepcopy(b))
        second = parse_block(copy.deepcopy(b))
        keys1 = [f.key for f in first]
        assert keys1 == [f.key for f in second], h
        assert len(set(keys1)) == len(keys1), h                  # unique within the block
        kept = [f.key for f in first if keep_option_a(f)]
        assert set(kept) <= set(keys1)                          # the filter does not renumber anything
        assert all(o.startswith(("a", "l")) for _r, o in keys1)


def test_origin_scheme():
    rid = hid("r", 1)
    b = block(1, [reo(rid, hid("t", 1), "alice.near", "bob.near",
                      [{"Transfer": {"deposit": "5"}}, "CreateAccount", {"Transfer": {"deposit": "7"}}])])
    fs = parse_block(b)
    assert [(f.kind, f.origin) for f in fs] == [("near_transfer", "a0"), ("create_account", "a1"), ("near_transfer", "a2")]


# ---------------------------------------------------------------- error isolation and traps

def test_a_malformed_log_keeps_the_other_facts():
    rid = hid("r", 2)
    good_event = 'EVENT_JSON:' + json.dumps({"standard": "nep245", "event": "mt_mint",
                                             "data": [{"owner_id": "x.near", "token_ids": ["nep141:wrap.near"],
                                                       "amounts": ["5"]}, "not-an-object"]})
    b = block(2, [reo(rid, hid("t", 2), "alice.near", "intents.near", [{"Transfer": {"deposit": "9"}}],
                      logs=["EVENT_JSON:{bad json", good_event], executor="intents.near")])
    errs = []
    fs = parse_block(b, errors=errs)
    assert [f.kind for f in fs] == ["near_transfer", "intents:mt_mint"]
    assert {(e.receipt_id, e.origin) for e in errs} == {(rid, "l0"), (rid, "l1.1")}
    assert fs[1].origin == "l1.0"


def test_a_bad_amount_is_an_error_not_a_string_none():
    rid = hid("r", 3)
    swap = 'EVENT_JSON:' + json.dumps({"standard": "dcl", "event": "swap", "data": [
        {"swapper": "s.near", "pool_id": "abc.nearlytrade.near|wrap.near|10000", "token_in": "wrap.near",
         "token_out": "abc.nearlytrade.near", "amount_in": "10"}]})
    ft = 'EVENT_JSON:' + json.dumps({"standard": "nep141", "event": "ft_transfer", "data": [
        {"old_owner_id": "a.near", "new_owner_id": "b.near", "amount": "oops"}]})
    b = block(3, [reo(rid, hid("t", 3), "s.near", "dclv2.ref-labs.near", [], logs=[swap], executor="dclv2.ref-labs.near"),
                  reo(hid("r", 33), hid("t", 3), "a.near", "abc.nearlytrade.near", [], logs=[ft],
                      executor="abc.nearlytrade.near")])
    errs = []
    fs = parse_block(b, errors=errs)
    (s,) = [f for f in fs if f.kind == "dcl_swap"]
    assert s.extra["amount_in"] == "10" and s.extra["amount_out"] is None and s.extra["launch_pool"] is True
    assert "None" not in json.dumps(s.extra)
    assert not [f for f in fs if f.kind == "ft_transfer"]
    assert [(e.origin, "ValueError" in e.error) for e in errs] == [("l0.0", True)]


def test_wnear_one_fact_per_movement():
    text = "Transfer 5 from a.near to b.near"
    ev = 'EVENT_JSON:' + json.dumps({"standard": "nep141", "event": "ft_transfer", "data": [
        {"old_owner_id": "a.near", "new_owner_id": "b.near", "amount": "5"}]})
    both = block(4, [reo(hid("r", 4), None, "a.near", "wrap.near", [], logs=[text, ev], executor="wrap.near")])
    st = ParseStats()
    fs = parse_block(both, st)
    assert [(f.kind, f.origin, f.amount) for f in fs] == [("wnear_transfer", "l0", "5")]
    assert st.wrap_events_ignored == 1
    only_event = block(5, [reo(hid("r", 5), None, "a.near", "wrap.near", [], logs=[ev], executor="wrap.near")])
    assert [(f.kind, f.origin) for f in parse_block(only_event)] == [("wnear_transfer", "l0.0")]


def test_lockers_and_launchbot_are_not_launch_tokens():
    assert is_launch_token("refrax.nearlytrade.near") and is_launch_token("abc.v2-launchpad.justhoot.near")
    assert not is_launch_token("lock_8.nearlytrade.near")
    assert not is_launch_token("launchbot.nearlytrade.near")
    assert not is_launch_token("nearlytrade.near") and not is_launch_token(None)
    assert is_launch_token("lock_8x.nearlytrade.near")     # only the exact locker shape is excluded


def test_option_a_policy():
    from rugyomi.parse import Fact
    mk = lambda kind, extra=None: Fact(kind, 1, 1, None, "r", "a0", "x", extra=extra)  # noqa: E731
    assert keep_option_a(mk("near_transfer")) and keep_option_a(mk("intents:mt_mint"))
    assert keep_option_a(mk("launch:launch_started")) and keep_option_a(mk("ft_transfer"))
    assert keep_option_a(mk("dcl_swap", {"launch_pool": True})) and not keep_option_a(mk("dcl_swap", {"launch_pool": False}))
    assert not keep_option_a(mk("near_refund"))


def test_a_non_string_log_line_in_a_wrap_receipt_affects_only_itself():
    """RGY-005 #5: the wrap regex step must not fail the whole receipt on a non-string log line."""
    rid = hid("r", 9)
    b = block(9, [reo(rid, None, "a.near", "wrap.near", [{"Transfer": {"deposit": "3"}}],
                      logs=[123, "Transfer 5 from a.near to b.near"], executor="wrap.near")])
    errs = []
    fs = parse_block(b, errors=errs)
    assert [(f.kind, f.origin) for f in fs] == [("near_transfer", "a0"), ("wnear_transfer", "l1")]
    assert [(e.receipt_id, e.origin) for e in errs] == [(rid, "l0")]
