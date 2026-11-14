"""RGY-065: one page for an address. A plain buyer of a launch is no longer a 404; the account page gets an "As a trader" context (the launches the
wallet is among the stored early buyers of, with the facts the stored rows hold), and the wallet page shows the account's labels.

World: the REFRAX / ISLAND / COIN-2 replay of test_buyers.py: the wallets A and B bought ISLAND and REFRAX (A) and ISLAND, COIN-2 and REFRAX (B).
Facts only: no rank or score of a wallet, no words about skill (checked on the whole context)."""
import json
import shutil

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import buyers as BU  # noqa: E402
from rugyomi import labels as LB  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp, queries  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402

CFG = C.load_config(profile="v1")
ISLAND, COIN, REFRAX = "island.nearlytrade.near", "coin-2.nearlytrade.near", "refrax.nearlytrade.near"
A = "b4adf54a4ccc38cba50dc818140c8568baddd5b8e072d7a44992520ffdc70486"
B = "976b2ad5b28f7665d2890ee05b331a8996e37b548f7072ed9d4bbf0069493fe8"
BOT = "tyty.sendler-b.near"
BANNED = ("smart", "profitable", "win rate", "winrate", "skill", "best trader", "whale")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    base = tmp_path_factory.mktemp("acct") / "base.db"
    refrax_world_db(base)
    s = open_store(base)
    BU.build_launch_buyers(s, CFG)
    s.close()
    return base


@pytest.fixture
def db(built, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(built, p)
    s = open_store(p)
    yield s
    s.close()


def test_a_plain_buyer_is_a_known_account_and_unknown_ones_are_not(db):
    assert queries.known_account(db.con, A) and queries.known_account(db.con, B)
    assert not queries.known_account(db.con, "nobody-at-all.near")
    assert not queries.known_account(db.con, "e" * 64)


def test_the_creator_route_gives_200_for_a_buyer_and_404_for_unknown_and_invalid(built, tmp_path):
    p = tmp_path / "w.db"
    shutil.copy(built, p)
    with TestClient(webapp.create_app(p)) as c:
        assert c.get(f"/creator/{A}").status_code == 200
        assert c.get(f"/creator/{B}").status_code == 200
        assert c.get("/creator/nobody-at-all.near").status_code == 404
        assert c.get("/creator/" + "e" * 64).status_code == 404
        assert c.get("/creator/not%20valid!").status_code == 404
        assert c.get(f"/traders/{A}").status_code == 200


def test_the_trader_context_lists_the_launches_newest_first_with_the_stored_facts(db):
    t = BU.account_trades(db.con, A)
    assert t is not None and t["account"] == A and t["trader_url"] == f"/traders/{A}"
    assert [r["token"] for r in t["rows"]] == sorted((r["token"] for r in t["rows"]), key=lambda x: -db.con.execute(
        "SELECT first_buy_ns FROM launch_buyers WHERE token = ? AND buyer = ?", (x, A)).fetchone()[0])
    assert {r["token"] for r in t["rows"]} == {ISLAND, REFRAX} and t["more"] == 0
    stored = {r[0]: r for r in db.con.execute("SELECT token, first_buy_ns, first_buy_tx, spent, bought, sold, pnl, role FROM launch_buyers WHERE buyer = ?", (A,))}
    for r in t["rows"]:
        s = stored[r["token"]]
        assert (r["first_buy_ns"], r["first_buy_tx"], r["spent"], r["pnl"], r["role"]) == (s[1], s[2], s[3], s[6], s[7])
        assert r["launchpad"] == "nearly" and r["launch_ts_ns"] and r["after_s"] is not None and r["after_s"] >= 0
        assert r["outcome"] in ("rug", "no rug", "undecided") and r["role_text"] is None
        assert r["sold_bps"] is None or 0 <= r["sold_bps"] <= 10000
    sm = t["summary"]
    assert sm["launches"] == 2 and sm["pnl_t60"] == str(sum(int(s[6]) for s in stored.values()))
    assert sm["rugs"] + sm["no_rugs"] + sm["undecided"] == 2 and sm["insider_rows"] == 0


def test_the_outcome_is_the_stored_one_and_an_insider_row_is_labelled(db):
    db.con.execute("UPDATE launch_buyers SET role = 'insider' WHERE token = ? AND buyer = ?", (ISLAND, A))
    db.con.execute("UPDATE launch_outcomes SET rug = 1, final = 1 WHERE token = ?", (ISLAND,))
    db.con.execute("UPDATE launch_outcomes SET rug = 0, final = 1 WHERE token = ?", (REFRAX,))
    db.con.commit()
    t = BU.account_trades(db.con, A)
    isl = next(r for r in t["rows"] if r["token"] == ISLAND)
    assert isl["role"] == "insider" and isl["role_text"] == "got tokens from the dev" and t["summary"]["insider_rows"] == 1
    assert isl["outcome"] == "rug"
    other = next(r for r in t["rows"] if r["token"] == REFRAX)
    assert other["role"] == "buyer" and other["role_text"] is None and other["outcome"] == "no rug"
    assert (t["summary"]["rugs"], t["summary"]["no_rugs"], t["summary"]["undecided"]) == (1, 1, 0)


def test_a_launch_with_no_stored_outcome_is_undecided(db):
    db.con.execute("DELETE FROM launch_outcomes WHERE token = ?", (ISLAND,))
    db.con.commit()
    r = next(x for x in BU.account_trades(db.con, A)["rows"] if x["token"] == ISLAND)
    assert r["outcome"] == "undecided"


def test_the_list_is_cut_at_the_limit_and_the_summary_counts_everything(db):
    for i in range(130):
        db.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (f"t{i}.nearlytrade.near", A, 1, 10**18 + i, f"tx{i}", 1, str(10**24), "100", "100", str(2 * 10**24), str(10**24)))
    db.con.commit()
    t = BU.account_trades(db.con, A, 100)
    assert len(t["rows"]) == 100 and t["more"] == 32 and t["summary"]["launches"] == 132
    assert t["rows"][0]["first_buy_ns"] >= t["rows"][1]["first_buy_ns"]
    assert t["rows"][0]["sold_bps"] == 10000 and t["rows"][0]["sold_over_bought"] is False
    assert t["summary"]["pnl_t60"] == str(sum(int(x[0]) for x in db.con.execute("SELECT pnl FROM launch_buyers WHERE buyer = ?", (A,))))
    assert len(BU.account_trades(db.con, A, 100_000)["rows"]) == 132


def test_sold_more_than_bought_is_capped_and_flagged(db):
    db.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   ("x.nearlytrade.near", "over.near", 1, 5, "t", 1, "1", "100", "250", "1", "0"))
    db.con.commit()
    r = BU.account_trades(db.con, "over.near")["rows"][0]
    assert r["sold_bps"] == 10000 and r["sold_over_bought"] is True


def test_a_wallet_with_no_stored_early_buys_has_no_trader_context(db):
    assert BU.account_trades(db.con, "nobody-at-all.near") is None
    assert queries.creator_page(db, A)["trader"]["summary"]["launches"] == 2
    assert queries.creator_page(db, "nobody-at-all.near")["trader"] is None


def test_the_context_has_the_links_to_the_wallet_page_and_no_judgement(db):
    t = queries.creator_page(db, B)["trader"]
    assert t["trader_url"] == f"/traders/{B}" and t["pairs"] >= 0 and "group" in t and "wallet" in t
    text = json.dumps(t).lower()
    assert not any(w in text for w in BANNED)


def test_the_wallet_page_gets_the_manual_and_the_derived_labels(db):
    db.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                   (ISLAND, BOT, 9, 7, "t", 1, "1", "1", "0", "0", "0"))
    db.con.commit()
    db.con.execute("INSERT INTO manual_labels(account, label, reason, source, set_by, set_at) VALUES(?,?,?,?,?,?)",
                   (A, "independent_sniper", "test reason", "test", "test", "2026-11-13T00:00:00Z"))
    db.con.commit()
    assert [x["label"] for x in (queries.trader(db.con, A) or {}).get("labels", [])] == ["independent_sniper"]
    bot = queries.trader(db.con, BOT)
    assert bot is not None and [x["label"] for x in bot["labels"]] == ["bot_wallet"] and bot["labels"][0]["text"]


def test_the_account_query_uses_the_index_not_a_scan(db):
    plan = " ".join(str(r) for r in db.con.execute(
        "EXPLAIN QUERY PLAN SELECT b.token FROM launch_buyers b LEFT JOIN launches k ON k.token = b.token WHERE b.buyer = ? ORDER BY b.first_buy_ns DESC, b.token LIMIT 100", (A,)))
    assert "launch_buyers_buyer" in plan and "SCAN b" not in plan and "SCAN launch_buyers" not in plan


# ---------------------------------------------------------------- RGY-066: a pool that is not paired with wNEAR has no NEAR amounts

def _unpair(db, token, quote="prl.omft.near"):
    db.con.execute("UPDATE launches SET pool = ? WHERE token = ?", (f"{token}|{quote}|10000", token))


def test_a_launch_not_paired_with_wnear_shows_no_near_amounts_and_stays_out_of_the_sum(db):
    before = BU.account_trades(db.con, B)
    assert before["summary"]["other_quote_rows"] == 0 and before["summary"]["pnl_launches"] == before["summary"]["launches"] == 3
    _unpair(db, COIN)
    t = BU.account_trades(db.con, B)
    rows = {r["token"]: r for r in t["rows"]}
    assert rows[COIN]["spent"] is None and rows[COIN]["pnl"] is None
    assert rows[COIN]["quote_token"] == "prl.omft.near" and rows[COIN]["amount_note"] == "n/a (quote: prl.omft.near)"
    for tok in (ISLAND, REFRAX):                                              # the NEAR-paired rows are untouched
        assert rows[tok]["quote_token"] is None and rows[tok]["spent"] and rows[tok]["pnl"] is not None
    assert t["summary"]["other_quote_rows"] == 1 and t["summary"]["pnl_launches"] == 2 and t["summary"]["launches"] == 3
    exact = sum(int(r[0]) for r in db.con.execute("SELECT pnl FROM launch_buyers WHERE buyer = ? AND token != ?", (B, COIN)))
    assert t["summary"]["pnl_t60"] == str(exact)                              # never adds raw units of another token
    assert rows[COIN]["sold_bps"] == before["rows"][[r["token"] for r in before["rows"]].index(COIN)]["sold_bps"]   # token shares do not depend on the quote


def test_the_launch_page_early_buyers_of_a_non_near_pool_have_no_amounts(db):
    _unpair(db, COIN)
    rows = BU.early_buyers(db.con, COIN)
    assert rows and all(r["spent"] is None and r["received"] is None and r["pnl"] is None and r["quote_token"] == "prl.omft.near" for r in rows)
    assert all(r["amount_note"] == "n/a (quote: prl.omft.near)" for r in rows)
    normal = BU.early_buyers(db.con, ISLAND)
    assert normal and all(r["quote_token"] is None and r["spent"] and "amount_note" not in r for r in normal)


def test_the_pages_render_for_a_non_near_pool(built, tmp_path):
    from rugyomi.web import fmt
    p = tmp_path / "w.db"
    shutil.copy(built, p)
    s = open_store(p)
    raw = s.con.execute("SELECT spent FROM launch_buyers WHERE token = ? AND buyer = ?", (COIN, B)).fetchone()[0]
    _unpair(s, COIN)
    s.close()
    shown = fmt.near(raw)
    with TestClient(webapp.create_app(p)) as c:
        assert c.get(f"/creator/{B}").status_code == 200
        r = c.get(f"/launch/{COIN}")
        assert r.status_code == 200
        sect = r.text[r.text.index('id="early-buyers"'):]
        sect = sect[:sect.index("</section>")]
        assert shown not in sect and "—" in sect                                  # the early-buyers table prints «—», not the raw units as NEAR (the snapshot tables are the stored side: Q-046)
        assert shown not in c.get(f"/creator/{B}").text
