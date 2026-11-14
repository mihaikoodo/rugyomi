"""RGY-041: the compact post style (card, outcome reply, daily summary), Telegram HTML, and the switch (off = the old format byte for byte)."""
import pytest
import copy
import re
import sqlite3

import rugyomi_testkit  # noqa: F401
from rugyomi import publisher as P
from rugyomi.store import open_store
from test_publisher import CARD, LAUNCH, NOW, REASONS, TOKEN, FakeTg, fresh_db, make, seed

SHORT = [
    {"id": "fingerprint", "fired": True, "contribution": 35, "reason": "Fresh creator setup (3 of 3): ... long text ...", "short": "creator account 9 min old, got 24.7 NEAR 5 min before launch",
     "evidence": [{"kind": "tx", "id": "3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG"}]},
    {"id": "creator_track_record", "fired": True, "contribution": 20, "reason": "2 of the 3 earlier launches ended as a rug", "short": "2 of 3 earlier decided launches of the creator ended as a rug",
     "evidence": [{"kind": "launch", "id": "old0.nearlytrade.near"}]},
    {"id": "creator_tax", "fired": True, "contribution": 5, "reason": "Trade tax ...", "short": "the creator gets 2% of the trade tax", "evidence": [{"kind": "launch", "id": TOKEN}]},
]
COMPACT = {"post_style": "compact"}
CARD_C = {**CARD, "score": 60, "band": "High", "reasons": SHORT, "signals": {"results": [], "notes": [], "notes_short": []}}


def visible(text):
    return re.sub(r"<[^>]+>", "", text)


def cfgc(**kw):
    c = P.load_config()
    c["publisher"].update(COMPACT, **kw)
    return c


@pytest.mark.core
def test_the_switch_is_off_by_default_and_names_the_style():
    c = P.load_config()
    assert c["publisher"]["post_style"] == "classic" and P.post_style(c) == "classic" and P.post_style(cfgc()) == "compact"
    assert P.render_card(LAUNCH, CARD, c) == P.format_card(LAUNCH, CARD, c)                  # the old format, unchanged
    assert "<" not in P.render_card(LAUNCH, CARD, c) and "Risk score 60/100" in P.render_card(LAUNCH, CARD, c)


@pytest.mark.core
def test_compact_card_is_short_keeps_the_public_facts_and_has_one_emoji():
    text = P.format_card_compact(LAUNCH, CARD_C, cfgc())
    assert len(visible(text)) <= 300, (len(visible(text)), text)
    v = visible(text)
    assert v.startswith("🔴 REFRAX · nearly.trade — risk 60/100 High")
    assert "creator dev.near" in v and "+35 creator account 9 min old, got 24.7 NEAR 5 min before launch (tx)" in v
    assert v.endswith("v0-default-1 · 2026-11-06 12:05 UTC")                                # config version and issue time (UTC) stay in the post
    assert [ch for ch in v if ord(ch) > 0x2000 and ch not in "·—•"] == ["🔴"], v            # the band marker is the only emoji
    assert 'href="https://nearly.trade/refrax.nearlytrade.near">REFRAX</a>' in text         # the token name links to the launchpad page
    assert 'href="https://nearblocks.io/txns/3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG">tx</a>' in text    # evidence as a word, not a raw hash
    assert "3urt5C9x" not in v and "chance" not in v.lower()
    assert 'href="https://nearblocks.io/token/refrax.nearlytrade.near">launch</a>' in text      # a launch evidence without a site link goes to the explorer


@pytest.mark.core
def test_band_markers_and_a_clean_card():
    for band, mark in (("Low", "🟢"), ("Elevated", "🟡"), ("High", "🔴")):
        assert visible(P.format_card_compact(LAUNCH, {**CARD_C, "band": band}, cfgc())).startswith(mark + " ")
    text = P.format_card_compact(LAUNCH, {**CARD_C, "reasons": [], "score": 0, "band": "Low"}, cfgc())
    assert "• no risk signal fired at launch time" in text


@pytest.mark.core
def test_html_is_escaped_in_names_and_texts():
    lau = {**LAUNCH, "symbol": "A<B>&C", "creator": "d&v.near"}
    reasons = [{**SHORT[0], "short": "paid <5 NEAR> & more"}]
    text = P.format_card_compact(lau, {**CARD_C, "reasons": reasons, "band": "Low"}, cfgc())
    assert "paid &lt;5 NEAR&gt; &amp; more" in text and "d&amp;v.near" in text                       # texts and the creator are escaped
    assert "A<B>" not in text and "A&lt;B&gt;&amp;C" not in text and "refrax" in text               # RGY-061: a symbol that is not plain never reaches the post (the token's first label is shown)
    assert "<B" not in text and "<5" not in text and "& more" not in text and "&C" not in text.replace("&amp;C", "")
    assert not re.search(r"<(?!/?a\b)", text), text                                          # the only tags are links


@pytest.mark.core
def test_creator_line_only_when_the_creator_is_not_the_token_account():
    has = lambda t: any(ln.startswith("creator ") for ln in visible(t).splitlines())  # noqa: E731
    assert not has(P.format_card_compact({**LAUNCH, "creator": TOKEN}, {**CARD_C, "dev_account": TOKEN}, cfgc()))
    assert has(P.format_card_compact(LAUNCH, CARD_C, cfgc()))


@pytest.mark.core
def test_an_old_card_without_short_falls_back_to_the_clipped_long_reason():
    long = [{**r, "reason": "y" * 300} for r in REASONS]
    text = P.format_card_compact(LAUNCH, {**CARD, "reasons": long}, cfgc())
    bullets = [ln for ln in visible(text).splitlines() if ln.startswith("• ")]
    assert bullets[-1] == "• +1 more (+5)"                                                    # Q-021 (2): REASONS has a 4th reason
    bullets = bullets[:-1]
    assert len(bullets) == 3 and all(len(b) <= 110 for b in bullets), bullets
    assert "• +50 " in visible(text) and "…" in bullets[0]


@pytest.mark.core
def test_notes_are_one_short_line_and_the_site_link_is_added_when_known():
    c = cfgc()
    c["site"]["base_url"] = "https://rugyomi.example"
    text = P.format_card_compact(LAUNCH, {**CARD_C, "signals": {"notes": ["long ..."], "notes_short": ["creator's history not loaded yet", "x"]}}, c)
    assert "note: creator's history not loaded yet; x" in text
    assert text.splitlines()[-1].endswith('<a href="https://rugyomi.example/launch/refrax.nearlytrade.near">page</a>')
    old = P.format_card_compact(LAUNCH, {**CARD_C, "signals": {"notes": ["The creator's history before 2026-10-20 is not loaded yet, so the history-based signals may be incomplete."]}}, cfgc())
    assert "note: The creator's history before 2026-10-20 is not loaded yet" in old and "…" in old


@pytest.mark.core
def test_outcome_reply_rug_no_rug_undecided_and_old_rows():
    ev = {"insiders_sold_bps": 9900, "buyers_pnl_near": -2.53, "crossed_after_s": 35.0}
    rug = P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": True, "short": "insiders sold 99 % in 35 s", "evidence": ev}})
    assert rug == "🔴 Rug · insiders sold 99 % in 35 s · outside buyers −2.53 NEAR"
    ok = P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": False, "short": "price held at 64 % of peak", "evidence": {"buyers_pnl_near": 0.4}}})
    assert ok == "⚪ No rug · price held at 64 % of peak · outside buyers +0.40 NEAR"
    und = P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": None, "undefined": True, "short": "price missing at T+24 h", "reason": "price missing at T+24 h"}})
    assert und == "⏳ Undecided · price missing at T+24 h"
    assert P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": None}}) == "⏳ Undecided · data missing"
    # rows stored before `short`: the facts of the evidence, or the clipped reason of the deciding label
    old_rug = P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": True, "evidence": ev}})
    assert old_rug == "🔴 Rug · insiders sold 99 % · outside buyers −2.53 NEAR"
    old_no = P.format_outcome_compact(LAUNCH, CARD, {"rug": {"value": False, "reason": "neither rug_fast nor rug_late", "evidence": {"buyers_pnl_near": 0.4}},
                                                    "rug_late": {"reason": "the price held at 64 % of its peak (a rug needs 10 % or less)"}})
    assert old_no.startswith("⚪ No rug · the price held at 64 %") and old_no.endswith("outside buyers +0.40 NEAR")
    assert "\n" not in rug and len(rug) < 90


@pytest.mark.core
def test_daily_compact_is_at_most_nine_short_lines_with_the_disclaimer_and_few_data_in_words():
    st = {"day": "2026-11-05", "launches": 12, "final": 9, "waiting": 3, "rugs": 4, "loss_near": 12.3, "worst": ("a.nearlytrade.near", 4.1),
          "group": ("grp_1", 5.2, 2),
          "track": {"summary": {"cards": 140, "final": 98, "rugs": 31, "base_rate": 0.32},
                    "by_band": [{"band": "Low", "n": 60, "rugs": 5, "rate": 0.08}, {"band": "Elevated", "n": 30, "rugs": 12, "rate": 0.4},
                                {"band": "High", "n": 8, "rugs": 6, "rate": 0.75, "too_few": True}]}}
    c = cfgc()
    c["site"]["base_url"] = "https://rugyomi.example"
    text = P.format_daily_compact(st, c)
    assert len(text.splitlines()) <= 9 and len(visible(text)) < 650, text
    assert "12 launches · 9 decided · 3 waiting" in text and "Rugs 4 of 9 · buyers lost 12.30 NEAR" in text and "Low 5/60 (8 %) · Elevated 12/30 (40 %) · High 6/8 (75 %) (few data)" in text
    assert "*" not in text                                                                    # Q-021 (3): no unexplained star
    assert text.splitlines()[-2] == c["publisher"]["disclaimer"] == "Alpha: scores are provisional, calibration in progress."      # Q-021 (1): one line, from the config
    assert "accuracy</a>" in text and P.render_daily(st, c) == text and P.render_daily(st, P.load_config()) == P.format_daily(st, P.load_config())
    c["publisher"]["disclaimer"] = ""
    assert "Alpha" not in P.format_daily_compact(st, c) and len(P.format_daily_compact(st, c).splitlines()) <= 8


@pytest.mark.core
def test_hidden_reasons_are_counted_so_the_visible_points_add_up_to_the_score():
    four = SHORT + [{"id": "name_copy", "fired": True, "contribution": 5, "reason": "Name copy ...", "short": "name used before", "evidence": []},
                    {"id": "zero", "fired": True, "contribution": 0, "reason": "", "short": "", "evidence": []}]          # F-043: a 0-point signal is no reason and not counted
    card = {**CARD_C, "score": 65, "reasons": four}
    v = visible(P.format_card_compact(LAUNCH, card, cfgc()))
    bullets = [ln for ln in v.splitlines() if ln.startswith("• ")]
    assert bullets[-1] == "• +1 more (+5)" and len(bullets) == 4
    pts = sum(int(re.match(r"• \+(\d+) ", b).group(1)) for b in bullets[:-1]) + 5
    assert pts == card["score"]
    assert "more (" not in visible(P.format_card_compact(LAUNCH, CARD_C, cfgc()))           # three reasons: nothing hidden, no line
    assert "Alpha" not in P.format_card_compact(LAUNCH, card, cfgc())                        # the disclaimer is not in the compact card post


# ------------------------------------------------------------------------------------------------ sending
@pytest.mark.core
def test_classic_sends_plain_text_exactly_as_before_and_compact_sends_html(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=False)
    assert p.tick() == 0
    seed(tmp_path / "c.db", ts=NOW + 300)
    assert p.tick() == 1
    assert set(tg.sent[0]) == {"chat_id", "text", "disable_web_page_preview"}                    # no parse_mode: the old payload
    assert tg.sent[0]["text"].startswith("New launch on nearly.trade\nREFRAX · refrax.nearlytrade.near\nCreator: dev.near\n\nRisk score 60/100 — High")


@pytest.mark.core
def test_compact_posts_go_out_with_parse_mode_html_and_a_retry_keeps_it(tmp_path):
    fresh_db(tmp_path)
    tg = FakeTg()
    tg.script = [{"ok": False, "error_code": 500, "description": "boom"}]
    p, tg = make(tmp_path, tg=tg, dry_run=False, post_style="compact")
    assert p.tick() == 0
    seed(tmp_path / "c.db", ts=NOW + 300)
    assert p.tick() == 0 and tg.sent == []                                                       # the first send failed, the row is pending
    row = sqlite3.connect(tmp_path / "state" / "publisher.db").execute("SELECT fmt, status FROM channel_posts").fetchone()
    assert row == ("html", "pending")
    p.clock = lambda: NOW + 100000
    assert p.tick() >= 1
    assert tg.sent[0]["parse_mode"] == "HTML" and tg.sent[0]["disable_web_page_preview"] is True
    assert tg.sent[0]["text"].startswith("🔴 REFRAX") or tg.sent[0]["text"].startswith("🟡 ") or "risk 60/100 High" in tg.sent[0]["text"]


@pytest.mark.core
def test_a_publisher_db_made_before_the_style_gets_the_column(tmp_path):
    (tmp_path / "state").mkdir()
    con = sqlite3.connect(tmp_path / "state" / "publisher.db")
    con.executescript("CREATE TABLE channel_posts(token TEXT NOT NULL, kind TEXT NOT NULL, chat TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, "
                      "message_id INTEGER, reply_to INTEGER, text TEXT NOT NULL, text_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
                      "next_try_at REAL NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT, PRIMARY KEY (token, kind, chat));"
                      "INSERT INTO channel_posts VALUES('t','card','c','dry','sent',1,NULL,'old','h',1,0,NULL,'x','x');")
    con.commit()
    con.close()
    fresh_db(tmp_path)
    make(tmp_path)
    cols = {r[1] for r in sqlite3.connect(tmp_path / "state" / "publisher.db").execute("PRAGMA table_info(channel_posts)")}
    assert "fmt" in cols
    assert sqlite3.connect(tmp_path / "state" / "publisher.db").execute("SELECT fmt FROM channel_posts").fetchone() == (None,)         # the old post stays plain
