"""DSN-002b: the markup of the account page "As a trader" block and of the wallet page labels (the context is RGY-065's, tested in test_account_trader.py).
Facts only in the page: the words of the core note, no judgement; labels show their text, not their key."""
import re
import shutil

import pytest

import rugyomi_testkit  # noqa: F401

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi import buyers as BU  # noqa: E402
from rugyomi.rules import config as C  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi_testkit import refrax_world_db  # noqa: E402

A = "b4adf54a4ccc38cba50dc818140c8568baddd5b8e072d7a44992520ffdc70486"
ISLAND = "island.nearlytrade.near"
BOT = "tyty.sendler-b.near"
BANNED = ("smart", "profitable", "win rate", "winrate", "skill claim ", "best trader", "whale")


@pytest.fixture
def client(tmp_path):
    p = tmp_path / "w.db"
    refrax_world_db(p)
    s = open_store(p)
    BU.build_launch_buyers(s, C.load_config(profile="v1"))
    s.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, pnl) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (ISLAND, BOT, 9, 7, "t", 1, "1", "1", "0", "0", "0"))
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(p)) as c:
        yield c


def test_the_account_page_has_the_trader_block_with_a_table_and_a_jump_link(client):
    t = client.get(f"/creator/{A}").text
    assert 'href="#as-trader"' in t and '<section id="as-trader">' in t and "<h2>As a trader" in t
    block = t[t.index('<section id="as-trader">'):]
    block = block[:block.index("</section>")]
    assert re.search(r"Total P&amp;L at T\+60 min: <strong>[^<]+</strong>", block)
    assert '<table class="rows stack" role="table">' in block and "<th>Launch</th>" in block and "<th>Outcome</th>" in block
    assert 'href="/launch/' in block and "not a profit statement and not a skill claim" in block
    assert f'href="/traders/{A}"' in block
    low = block.lower()
    assert not any(w in low for w in BANNED)


def test_an_account_that_is_no_buyer_has_no_trader_block(client):
    t = client.get("/creator/coinisland.near").text
    assert 'id="as-trader"' not in t and 'href="#as-trader"' not in t


def test_the_wallet_page_shows_the_label_text_and_a_link_to_the_account_page(client):
    t = client.get(f"/traders/{BOT}").text
    assert '<span class="tag"' in t and "trading bot wallet" in t and ">bot_wallet<" not in t
    assert f'<a href="/creator/{BOT}">launches and trading</a>' in t
    assert "trading bot wallet" in client.get(f"/creator/{BOT}").text


def test_the_methodology_and_accuracy_texts_of_rgy061(client):
    m = client.get("/methodology").text
    assert "<h2>Earlier profile: rules v1 " in m and '<a href="#rules-v1">Earlier profile (v1)</a>' in m and 'id="rules-v1"' in m
    a = client.get("/accuracy").text
    assert "Part of the weights were fitted on history and part are set by hand (see the methodology page);" in a and "The weights are provisional," not in a


def test_the_note_of_a_non_near_pool_is_on_the_launch_page_and_the_account_page(tmp_path):
    """DSN-004: where the core sends no NEAR amounts it sends `amount_note`; the pages print that text (escaped), not a bare dash."""
    p = tmp_path / "w.db"
    refrax_world_db(p)
    s = open_store(p)
    BU.build_launch_buyers(s, C.load_config(profile="v1"))
    s.con.execute("UPDATE launches SET pool = ? WHERE token = ?", ("coin-2.nearlytrade.near|prl.omft.near|10000", "coin-2.nearlytrade.near"))
    s.con.commit()
    s.close()
    note = "n/a (quote: prl.omft.near)"
    with TestClient(webapp.create_app(p)) as c:
        t = c.get("/launch/coin-2.nearlytrade.near").text
        sect = t[t.index('id="early-buyers"'):]
        sect = sect[:sect.index("</section>")]
        assert f'data-label="Spent"><span class="muted">{note}</span>' in sect
        a = c.get("/creator/976b2ad5b28f7665d2890ee05b331a8996e37b548f7072ed9d4bbf0069493fe8").text
        blk = a[a.index('<section id="as-trader">'):]
        assert f'data-label="Spent"><span class="muted">{note}</span>' in blk[:blk.index("</section>")]
        n = c.get("/launch/island.nearlytrade.near").text
        assert "n/a (quote:" not in n[n.index('id="early-buyers"'):]
