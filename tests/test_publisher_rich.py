"""RGY-059: the rich post style (card and daily summary, Telegram HTML) and the outcome replies switched off. classic / compact are unchanged (their own tests)."""
import html
import json
import re

import pytest

import rugyomi_testkit  # noqa: F401
from rugyomi import publisher as P
from rugyomi.store import open_store
from test_publisher import CARD, LAUNCH, NOW, TOKEN, Clock, FakeTg, fresh_db, make, seed
from test_publisher_compact import SHORT

HEX = "82030f" + "a" * 53 + "5f4f9"
BASE = "https://rugyomi.example"
FOUR = SHORT + [{"id": "name_copy", "fired": True, "contribution": 5, "reason": f"The name was used before by {HEX}.", "short": f"name used before by {HEX}",
                 "evidence": [{"kind": "launch", "id": "old.nearlytrade.near"}]}]
CARD_R = {**CARD, "score": 65, "band": "High", "reasons": FOUR, "dev_account": "dev.near",
          "signals": {"results": [], "notes": [f"The largest funder's ({HEX}) history before 2026-10-30 is not loaded yet, so the history-based signals may be incomplete."],
                      "notes_short": [f"funder's ({HEX}) history not loaded yet"]}}
ST = {"day": "2026-11-05", "launches": 12, "final": 9, "waiting": 3, "rugs": 4, "loss_near": 12.3, "worst": ("a.nearlytrade.near", 4.1), "group": ("grp_1", 5.2, 2),
      "track": {"summary": {"cards": 140, "final": 98, "rugs": 31, "base_rate": 0.32},
                "by_band": [{"band": "Low", "n": 60, "rugs": 5, "rate": 0.08}, {"band": "Elevated", "n": 30, "rugs": 12, "rate": 0.4},
                            {"band": "High", "n": 8, "rugs": 6, "rate": 0.75, "too_few": True}]}}


def cfgr(base=BASE, **kw):
    c = P.load_config()
    c["publisher"].update({"post_style": "rich", **kw})
    c["site"]["base_url"] = base
    return c


def seen(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def balanced(text):
    for tag in ("b", "i", "a", "blockquote"):
        opened = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", text))
        assert opened == text.count(f"</{tag}>"), (tag, text)
    assert not re.search(r"<(?!/?(?:b|i|a|blockquote)\b)", text), text          # only the tags Telegram's HTML mode takes, and nothing unescaped


@pytest.mark.core
def test_the_rich_card_has_the_approved_layout():
    text = P.format_card_rich(LAUNCH, CARD_R, cfgr())
    lines = text.splitlines()
    assert lines[0] == '🔴 <b><a href="https://nearly.trade/refrax.nearlytrade.near">REFRAX</a> · nearly.trade</b>'
    assert lines[1] == "<b>Risk 65/100 · High</b>"
    assert lines[2] == f'creator <a href="{BASE}/creator/dev.near">dev.near</a>'                         # the site's creator page when the site is known
    quote = re.search(r"<blockquote>(.*?)</blockquote>", text, re.S).group(1)
    assert [ln.split(" ")[0] for ln in quote.splitlines()] == ["<b>+35</b>", "<b>+20</b>", "<b>+5</b>"]   # reasons_max (3) visible, strongest first, points in bold
    assert "<b>+35</b> — creator account 9 min old, got 24.7 NEAR 5 min before launch (" in quote           # the compact text (user 12.11: only the look changes)
    assert "long text" not in text
    exp = re.search(r"<blockquote expandable>(.*?)</blockquote>", text, re.S).group(1)
    assert exp.startswith("<b>+5</b> — name used before by 82030f…5f4f9 (")                               # the rest, expandable; hex accounts shortened
    pts = sum(int(m) for m in re.findall(r"^<b>\+(\d+)</b> — ", quote + "\n" + exp, re.M))
    assert pts == CARD_R["score"]                                                                           # Q-021 p. 2: the points add up to the score
    assert "<i>note: funder's (82030f…5f4f9) history not loaded yet</i>" in text                       # the compact note, in italics
    assert lines[-2] == f'<a href="{BASE}/launch/{TOKEN}">Full breakdown →</a>'
    assert lines[-1] == "<i>v0-default-1 · 2026-11-06 12:05 UTC</i>"                                       # the public fixation
    assert HEX not in text and "Alpha" not in text and "chance" not in seen(text).lower()
    balanced(text)


@pytest.mark.core
def test_a_text_that_starts_with_a_number_does_not_run_into_the_points_and_the_texts_are_the_compact_ones():
    r = {"id": "creator_track_record", "fired": True, "contribution": 30, "reason": "117 of the 320 earlier launches that were already decided ...",
         "short": "117 of 320 earlier decided launches of the creator and linked accounts ended as a rug", "evidence": []}
    card = {**CARD_R, "score": 30, "reasons": [r], "signals": {}}
    v = seen(P.format_card_rich(LAUNCH, card, cfgr()))
    assert "+30 — 117 of 320 earlier decided launches" in v and "+30 117" not in v
    for c in (CARD_R, card, {**CARD, "reasons": [{**x, "reason": "z" * 300} for x in SHORT[:1]] + [{**SHORT[1], "short": ""}]}):     # also an old card without `short`
        compact = seen(P.format_card_compact(LAUNCH, c, cfgr(post_style="compact")))
        rich = seen(P.format_card_rich(LAUNCH, c, cfgr()))
        for ln in compact.splitlines():
            if ln.startswith("• +") and "more (" not in ln:
                assert P.short_hex(ln.split(" ", 2)[2]) in rich, ln                     # the same reason text, the same evidence word


@pytest.mark.core
def test_without_the_site_no_breakdown_link_and_the_creator_goes_to_the_explorer():
    text = P.format_card_rich({**LAUNCH, "creator": HEX}, {**CARD_R, "reasons": SHORT, "score": 60}, cfgr(base=""))
    assert "Full breakdown" not in text and "/launch/" not in text and "<blockquote expandable>" not in text
    assert f'creator <a href="https://nearblocks.io/address/{HEX}">82030f…5f4f9</a>' in text                # the link keeps the account, the label is shortened
    assert text.splitlines()[-1] == "<i>v0-default-1 · 2026-11-06 12:05 UTC</i>"
    clean = P.format_card_rich(LAUNCH, {**CARD_R, "reasons": [], "score": 0, "band": "Low", "signals": {}}, cfgr())
    assert clean.startswith("🟢 ") and "<blockquote>no risk signal fired at launch time</blockquote>" in clean


@pytest.mark.core
def test_names_and_texts_are_escaped_and_the_tags_stay_balanced():
    lau = {**LAUNCH, "symbol": "A<B>&C", "creator": "d&v.near"}
    bad = [{**SHORT[0], "short": "paid <5 NEAR> & more </blockquote><b>x"}] + FOUR[1:]
    text = P.format_card_rich(lau, {**CARD_R, "reasons": bad, "signals": {"notes_short": ["a <note> & </i>"]}}, cfgr())
    assert "paid &lt;5 NEAR&gt; &amp; more &lt;/blockquote&gt;&lt;b&gt;x" in text and "d&amp;v.near" in text and "a &lt;note&gt; &amp; &lt;/i&gt;" in text
    assert "A<B>" not in text and "A&lt;B&gt;&amp;C" not in text                                    # RGY-061: a symbol that is not plain never reaches the post
    balanced(text)


@pytest.mark.core
def test_over_4096_the_expandable_block_goes_first_then_the_notes_then_the_texts_never_the_head_or_footer():
    many = [{"id": f"s{i}", "fired": True, "contribution": 1, "reason": "x", "short": "x" * 600, "evidence": []} for i in range(12)]
    big = {**CARD_R, "score": 100, "reasons": SHORT + many}
    text = P.format_card_rich(LAUNCH, big, cfgr())
    assert P.tg_len(text) <= P.TG_MAX and "<b>Risk 100/100 · High</b>" in text and text.endswith("<i>v0-default-1 · 2026-11-06 12:05 UTC</i>")
    exp = re.search(r"<blockquote expandable>(.*?)</blockquote>", text, re.S).group(1)
    assert re.search(r"\+\d+ more \(\+\d+\)$", exp)                                                       # what did not fit is still counted
    pts = sum(int(m) for m in re.findall(r"^\+(\d+) — ", seen(text), re.M)) + int(re.search(r"more \(\+(\d+)\)", exp).group(1))
    assert pts == 60 + 12
    balanced(text)
    huge = {**CARD_R, "reasons": [{**r, "short": "y" * 3000} for r in SHORT]}
    t2 = P.format_card_rich(LAUNCH, huge, cfgr())
    assert P.tg_len(t2) <= P.TG_MAX and "<i>v0-default-1" in t2 and "<b>Risk 65/100 · High</b>" in t2 and "…" in t2
    balanced(t2)


@pytest.mark.core
def test_the_rich_daily_has_the_compact_numbers_grouped_with_few_data_and_the_disclaimer_in_italics():
    c = cfgr()
    rich, compact = P.format_daily_rich(ST, c), P.format_daily_compact(ST, {**c, "publisher": {**c["publisher"], "post_style": "compact"}})
    assert rich.startswith("<b>rugyomi · 2026-11-05 (UTC)</b>\n\n") and "\n\n" in rich.split("\n\n", 1)[1]
    assert [ln for ln in seen(rich).splitlines() if ln] == seen(compact).splitlines()                    # exactly the compact facts and numbers, in the same order
    assert "High 6/8 (75 %) (few data)" in rich and "*" not in rich
    assert "<i>Alpha: scores are provisional, calibration in progress.</i>" in rich and rich.endswith(f'<a href="{BASE}/accuracy">accuracy</a>')
    balanced(rich)
    assert P.render_daily(ST, c) == rich and P.render_card(LAUNCH, CARD_R, c) == P.format_card_rich(LAUNCH, CARD_R, c)
    assert P.post_style(c) == "rich" and P.post_style(cfgr(post_style="bogus")) == "classic"


# ------------------------------------------------------------------------------------------------ outcome replies off
def decided(path):
    s = open_store(path)
    s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, "
                  "config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                  (TOKEN, 1, 0, 1, 7.0, None, None, None, json.dumps({"rug": {"value": True, "evidence": {}}, "rug_fast": {"value": True}}), "v0-default-1", "h", "x"))
    s.con.commit()
    s.close()


@pytest.mark.core
def test_outcome_replies_are_off_by_default_in_every_style_and_a_pending_one_is_not_sent(tmp_path):
    assert P.load_config()["publisher"]["outcome_replies"] is False
    for style in ("classic", "compact", "rich"):
        d = tmp_path / style
        d.mkdir()
        fresh_db(d)
        p, tg = make(d, dry_run=False, post_style=style)
        assert p.tick() == 0
        seed(d / "c.db", ts=NOW + 300)
        decided(d / "c.db")
        assert p.tick() == 1 and p.tick() == 0                                            # the card, and no reply although the outcome is final
        assert [k for (k,) in p.posts.execute("SELECT kind FROM channel_posts")] == ["card"]
        assert tg.sent[0]["parse_mode" if style != "classic" else "chat_id"]                # rich and compact go as HTML
        if style == "rich":
            assert tg.sent[0]["parse_mode"] == "HTML" and tg.sent[0]["text"].startswith("🔴 <b>")
    # a reply that was pending when the switch went off stays as it is and is not sent
    d = tmp_path / "pending"
    d.mkdir()
    fresh_db(d)
    p, tg = make(d, dry_run=False, outcome_replies=True)
    p.tick()
    seed(d / "c.db", ts=NOW + 300)
    p.tick()
    decided(d / "c.db")
    tg.script = [{"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}]
    assert p.tick() == 0
    row = p.posts.execute("SELECT status, text FROM channel_posts WHERE kind = 'outcome'").fetchone()
    assert row[0] == "pending"
    p.cfg["publisher"]["outcome_replies"] = False
    p.clock.t += 100
    assert p.tick() == 0 and p.posts.execute("SELECT status, text FROM channel_posts WHERE kind = 'outcome'").fetchone() == row


@pytest.mark.core
def test_switched_on_again_only_outcomes_final_after_the_switch_are_replied(tmp_path):
    fresh_db(tmp_path)
    clock = Clock(NOW)
    p, tg = make(tmp_path, dry_run=False, clock=clock)                                    # off (the default)
    p.tick()
    seed(tmp_path / "c.db", ts=NOW + 300)
    decided(tmp_path / "c.db")
    assert p.tick() == 1 and p.tick() == 0                                                # the card; the reply is off
    clock.t = NOW + 3 * 86400                                                             # three days later the replies are switched on again
    p.cfg["publisher"]["outcome_replies"] = True
    p.tick()                                                                              # (the daily summary of the day before may go out)
    assert not p.posts.execute("SELECT 1 FROM channel_posts WHERE kind = 'outcome'").fetchone()        # this launch was final long before: no old reply floods the channel
    seed(tmp_path / "c.db", token="new.nearlytrade.near", ts=clock.t - 3600)             # a launch whose 24 h end after the switch
    s = open_store(tmp_path / "c.db")
    s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, config_version, config_hash, computed_at, final) "
                  "SELECT 'new.nearlytrade.near', rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, config_version, config_hash, computed_at, 1 "
                  "FROM launch_outcomes WHERE token = ?", (TOKEN,))
    s.con.commit()
    s.close()
    p.tick()                                                                              # its card,
    p.tick()                                                                              # then its reply (a reply needs its posted card)
    assert sorted(k for (k,) in p.posts.execute("SELECT kind FROM channel_posts WHERE token = 'new.nearlytrade.near'")) == ["card", "outcome"]
    assert not p.posts.execute(f"SELECT 1 FROM channel_posts WHERE token = '{TOKEN}' AND kind = 'outcome'").fetchone()


@pytest.mark.core
def test_the_token_is_still_scrubbed_from_errors_in_the_rich_style(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=False, post_style="rich")
    p.tick()
    seed(tmp_path / "c.db", ts=NOW + 300)
    tg.script = [{"ok": False, "error_code": 400, "description": "bad SECRET:TOKEN entity"}]
    assert p.tick() == 0
    err = p.posts.execute("SELECT last_error FROM channel_posts").fetchone()[0]
    assert "SECRET:TOKEN" not in err and "***" in err
