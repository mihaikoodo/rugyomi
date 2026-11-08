"""RGY-022: the Telegram publisher — post formatting (card, outcome reply, daily summary), modes (disabled / dry run / channel),
no double post after a restart, retry after a failed send, the reply finds its parent, history launches are never posted."""
import copy
import datetime as dt
import json
import subprocess
import sys

import pytest

import rugyomi_testkit  # noqa: F401  (puts the package on sys.path)
from rugyomi import publisher as P
from rugyomi.store import open_store

NOW = dt.datetime(2026, 11, 6, 12, 0, tzinfo=dt.timezone.utc).timestamp()
TOKEN = "refrax.nearlytrade.near"
NEAR = 10**24


def cfg(**pub):
    c = P.load_config()
    c["publisher"].update(pub)
    c["publisher"]["min_interval_seconds"] = 0
    return c


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class FakeTg:
    """A transport: records payloads, answers ok with growing message ids, or fails as scripted."""

    def __init__(self):
        self.sent, self.script, self.n = [], [], 100

    def __call__(self, url, payload, timeout):
        if self.script:
            ans = self.script.pop(0)
            if ans is not None:
                return ans
        self.n += 1
        self.sent.append(payload)
        return {"ok": True, "result": {"message_id": self.n}}


REASONS = [
    {"id": "creator_track_record", "fired": True, "contribution": 50, "reason": "2 of the creator's 3 earlier launches were rugs (smoothed share 55 %)",
     "evidence": [{"kind": "launch", "id": "old0.nearlytrade.near"}]},
    {"id": "funded_by_rugger", "fired": True, "contribution": 10, "reason": "funded by an account that funded 2 earlier rugs", "evidence": []},
    {"id": "creator_tax", "fired": True, "contribution": 5, "reason": "the creator set a tax of 1 bps", "evidence": []},
    {"id": "name_copy", "fired": True, "contribution": 5, "reason": "the name copies an earlier token", "evidence": []},
]


def seed(path, token=TOKEN, ts=NOW + 300, reasons=None, score=60, band="High", rules="0.1", cov=None, pad="nearly",
         outcome=None, creator="dev.near"):
    """A real-schema collector database with one launch + card (+ outcome). `outcome`: dict(rug, final, severity, evidence)."""
    s = open_store(path)
    s.con.execute("BEGIN")
    s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, dev_buy, height, ts_ns, detected_at, extra, coverage) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)", (token, pad, creator, creator, "0", 1, int(ts * 1e9), "x", json.dumps({"symbol": "REFRAX"}), cov))
    iso = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    s.con.execute("INSERT INTO cards(token, issued_at, issued_height, launch_ts_ns, score, band, reasons, signals, dev_account, config_version, "
                  "config_hash, rules_version, computed_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (token, iso, 1, int(ts * 1e9), score, band, json.dumps(REASONS if reasons is None else reasons), "{}", creator,
                   "v0-default-1", "h", rules, 1))
    if outcome:
        ev = {"rug": {"value": bool(outcome["rug"]), "reason": "r", "evidence": outcome.get("evidence", {})},
              "rug_fast": {"value": bool(outcome["rug"]), "reason": "r"}, "rug_late": {"value": False, "reason": "r"},
              "severity_near": outcome.get("severity"), "outcomes": {}}
        s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, "
                      "config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (token, int(outcome["rug"]), 0, int(outcome["rug"]), outcome.get("severity"), None, None, None, json.dumps(ev),
                       "v0-default-1", "h", "x", int(outcome.get("final", 1))))
    s.con.execute("COMMIT")
    s.close()


def make(tmp_path, tg=None, clock=None, targets=None, **pub):
    tg = tg or FakeTg()
    t = targets or {"token": "SECRET:TOKEN", "channel": "-100500", "admins": ["777"]}
    pub.setdefault("enabled", True)
    p = P.Publisher(tmp_path / "c.db", tmp_path / "state", cfg(**pub), P.Telegram("SECRET:TOKEN", transport=tg), t,
                    log=lambda m: None, clock=clock or Clock(), sleep=lambda s: None)
    return p, tg


def fresh_db(tmp_path):
    open_store(tmp_path / "c.db").close()


LAUNCH = {"token": TOKEN, "launchpad": "nearly", "creator": "dev.near", "symbol": "REFRAX"}
CARD = {"score": 60, "band": "High", "reasons": REASONS, "rules_version": "0.1", "config_version": "v0-default-1",
        "issued_at": "2026-11-06T12:05:00.000000Z", "dev_account": "dev.near"}


# ------------------------------------------------------------------------------------------------ formatting
def test_card_post_has_the_facts_the_reasons_and_stays_short():
    text = P.format_card(LAUNCH, CARD, P.load_config())
    assert len(text) <= 900
    assert TOKEN in text and "REFRAX" in text and "Risk score 60/100 — High" in text
    assert text.count("• ") == 3 and "name copies" not in text                       # top 3 reasons only
    assert "[launch old0.nearlytrade.near]" in text and "rugs (smoothed share 55 %)" in text
    assert "https://nearly.trade/" + TOKEN in text and "Rules 0.1 · config v0-default-1 · issued 2026-11-06 12:05 UTC" in text
    assert "/launch/" not in text and "Rug chance" not in text                          # no site link until site.base_url is set; v0 has no chance
    assert "scam" not in text.lower()
    assert "Alpha: scores are provisional, calibration in progress." in text


def test_card_post_links_the_site_when_base_url_is_set_and_shows_chance_for_v1():
    c = P.load_config()
    c["site"]["base_url"] = "https://example.org/"
    text = P.format_card({**LAUNCH, "launchpad": "justhoot_v2"}, CARD, c, {"predicted": 0.524})
    assert "https://justhoot.fun/p/" + TOKEN in text and "https://example.org/launch/" + TOKEN in text
    assert "Rug chance ≈ 52 %" in text


def test_disclaimer_can_be_switched_off_and_is_in_the_daily_summary():
    c = P.load_config()
    c["publisher"]["disclaimer"] = ""
    assert "Alpha" not in P.format_card(LAUNCH, CARD, c)
    st = {"day": "2026-11-05", "launches": 1, "final": 1, "waiting": 0, "rugs": 0, "loss_near": 0.0, "worst": None, "group": None,
          "track": {"summary": {"cards": 1, "final": 1, "rugs": 0, "base_rate": 0.0}, "by_band": []}}
    assert "Alpha: scores are provisional" in P.format_daily(st, P.load_config())


def test_card_post_is_cut_to_the_limit_by_dropping_the_weakest_reasons_and_clipping_the_last():
    long = [{**r, "reason": "x" * 400} for r in REASONS]
    text = P.format_card(LAUNCH, {**CARD, "reasons": long}, P.load_config())
    assert len(text) <= 900 and "Risk score 60/100" in text and TOKEN in text


def test_card_without_fired_reasons_and_without_creator():
    text = P.format_card({**LAUNCH, "creator": None}, {**CARD, "reasons": [], "dev_account": None, "score": 0, "band": "Low"}, P.load_config())
    assert "No risk signal fired at launch time." in text and "Creator: not known yet" in text


def test_outcome_reply_rug_no_rug_and_undecided():
    ev = {"insiders_pnl_near": 12.5, "insiders_sold_bps": 9000, "buyers_pnl_near": -7.0}
    rug = P.format_outcome(LAUNCH, CARD, {"rug": {"value": True, "evidence": ev}, "rug_fast": {"value": True}, "severity_near": 7.0,
                                          "config_version": "v0-default-1"}, 3.4)
    assert "rug, fast (by T+60 min)" in rug and "Outside buyers lost 7.00 NEAR" in rug and "+12.50 NEAR" in rug
    assert "The initial score was 60 (High)." in rug and "3.40x" in rug
    ok = P.format_outcome(LAUNCH, CARD, {"rug": {"value": False, "evidence": ev}, "rug_fast": {"value": False}})
    assert "no rug" in ok and "-7.00 NEAR" in ok and "The initial score was 60 (High)." in ok
    assert "could not be decided" in P.format_outcome(LAUNCH, CARD, {"rug": {"value": None}})


# ------------------------------------------------------------------------------------------------ modes and flow
def test_disabled_by_default_posts_nothing(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, enabled=False)
    seed(tmp_path / "c.db")
    assert p.tick() == 0 and tg.sent == [] and p.mode() is None
    assert P.load_config()["publisher"]["enabled"] is False and P.load_config()["publisher"]["dry_run"] is True


def test_dry_run_sends_the_card_to_the_admin_chat_and_the_outcome_as_a_reply_to_it(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=True)
    assert p.tick() == 0                                                              # first pass: fixes `since`, nothing to post
    seed(tmp_path / "c.db", ts=NOW + 300)
    assert p.tick() == 1
    first = tg.sent[0]
    assert first["chat_id"] == "777" and first["text"].startswith("[dry run") and TOKEN in first["text"] and "reply_to_message_id" not in first
    assert p.tick() == 0                                                              # the outcome is not final yet
    s = open_store(tmp_path / "c.db")
    s.con.execute("INSERT INTO launch_outcomes(token, rug_fast, rug_late, rug, severity_near, insiders_pnl, dev_exit, buyers_loss, evidence, "
                  "config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                  (TOKEN, 1, 0, 1, 7.0, None, None, None, json.dumps({"rug": {"value": True, "evidence": {"insiders_pnl_near": 1.0}},
                                                                       "rug_fast": {"value": True}, "severity_near": 7.0}), "v0-default-1", "h", "x"))
    s.close()
    assert p.tick() == 1
    reply = tg.sent[1]
    assert reply["reply_to_message_id"] == 101 and "Outside buyers lost 7.00 NEAR" in reply["text"] and "The initial score was 60 (High)." in reply["text"]


def test_channel_mode_posts_to_the_channel_not_to_the_admin(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=False)
    p.tick()
    seed(tmp_path / "c.db")
    assert p.tick() == 1 and tg.sent[0]["chat_id"] == "-100500" and not tg.sent[0]["text"].startswith("[dry run")


def test_channel_mode_without_a_channel_id_does_nothing(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=False, targets={"token": "SECRET:TOKEN", "channel": "", "admins": ["777"]})
    seed(tmp_path / "c.db")
    assert p.mode() is None and p.tick() == 0 and tg.sent == []


def test_cards_before_since_and_history_launches_are_never_posted(tmp_path):
    fresh_db(tmp_path)
    seed(tmp_path / "c.db", token="old.nearlytrade.near", ts=NOW - 3600)                                   # issued before the publisher started
    seed(tmp_path / "c.db", token="hist.nearlytrade.near", ts=NOW + 400, cov="launch_only")               # a history launch
    p, tg = make(tmp_path)
    assert p.tick() == 0 and tg.sent == []
    seed(tmp_path / "c.db", token="new.nearlytrade.near", ts=NOW + 500)
    assert p.tick() == 1 and "new.nearlytrade.near" in tg.sent[0]["text"]


def test_a_restart_does_not_post_again(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path)
    p.tick()
    seed(tmp_path / "c.db")
    assert p.tick() == 1
    p2, _ = make(tmp_path, tg=tg)                                                      # same state dir, same fake Telegram
    assert p2.tick() == 0 and len(tg.sent) == 1
    assert p2.status()["posts"] == [{"kind": "card", "status": "sent", "n": 1}]


def test_a_crash_between_sending_and_recording_is_not_repeated(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path)
    p.tick()
    seed(tmp_path / "c.db")
    p.posts.execute("INSERT INTO channel_posts(token, kind, chat, mode, status, text, text_hash, created_at) "
                    "VALUES(?,?,?,?,'sending','x','h','t')", (TOKEN, "card", "777", "dry"))   # what a crash after the send leaves
    p2, _ = make(tmp_path, tg=tg)
    assert p2.tick() == 0 and tg.sent == []
    assert p2.status()["posts"] == [{"kind": "card", "status": "unknown", "n": 1}]


def test_a_failed_send_is_retried_with_backoff_and_telegrams_retry_after(tmp_path):
    fresh_db(tmp_path)
    clock = Clock()
    p, tg = make(tmp_path, clock=clock)
    p.tick()
    seed(tmp_path / "c.db")
    tg.script = [{"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 30}}]
    assert p.tick() == 0 and tg.sent == []
    assert p.posts.execute("SELECT status, attempts, next_try_at FROM channel_posts").fetchone() == ("pending", 1, NOW + 30)
    clock.t = NOW + 10
    assert p.tick() == 0 and tg.sent == []                                             # too early: Telegram said 30 s
    clock.t = NOW + 31
    tg.script = [{"ok": False, "error_code": 500, "description": "oops"}]
    assert p.tick() == 0
    row = p.posts.execute("SELECT status, attempts, next_try_at FROM channel_posts").fetchone()
    assert row[0] == "pending" and row[1] == 2 and row[2] == NOW + 31 + 10                # 5 s * 2**1 backoff
    clock.t = NOW + 60
    assert p.tick() == 1 and len(tg.sent) == 1                                          # sent once in the end
    assert p.tick() == 0 and len(tg.sent) == 1


def test_the_token_never_appears_in_errors_or_the_log(tmp_path):
    fresh_db(tmp_path)
    logs = []
    p, _ = make(tmp_path)
    p.log = logs.append

    def boom(url, payload, timeout):
        raise OSError(f"cannot reach {url}")
    p.tg = P.Telegram("SECRET:TOKEN", transport=boom)
    p.tick()
    seed(tmp_path / "c.db")
    p.tick()
    assert logs and all("SECRET:TOKEN" not in m for m in logs) and "***" in " ".join(logs)
    assert "SECRET:TOKEN" not in (p.posts.execute("SELECT last_error FROM channel_posts").fetchone()[0] or "")


def test_a_pending_dry_post_is_not_sent_to_the_channel_after_switching_modes(tmp_path):
    fresh_db(tmp_path)
    p, tg = make(tmp_path, dry_run=True)
    p.tick()
    seed(tmp_path / "c.db")
    tg.script = [{"ok": False, "error_code": 500, "description": "oops"}]
    p.tick()
    p.cfg = cfg(enabled=True, dry_run=False)
    p.posts.execute("UPDATE channel_posts SET next_try_at = 0")
    p.tick()
    assert not [m for m in tg.sent if "this would go to the channel" in m["text"]]           # the old dry-run text never reaches the channel
    assert p.posts.execute("SELECT mode, status FROM channel_posts WHERE chat = '777'").fetchone() == ("dry", "pending")


def test_daily_summary_after_the_configured_hour_once_and_not_for_an_empty_day(tmp_path):
    fresh_db(tmp_path)
    day5 = dt.datetime(2026, 11, 5, 10, tzinfo=dt.timezone.utc).timestamp()
    seed(tmp_path / "c.db", token="a.nearlytrade.near", ts=day5, outcome={"rug": 1, "severity": 7.0, "evidence": {}})
    seed(tmp_path / "c.db", token="b.nearlytrade.near", ts=day5 + 60, score=5, band="Low", outcome={"rug": 0, "evidence": {}})
    clock = Clock(dt.datetime(2026, 11, 6, 6, 0, tzinfo=dt.timezone.utc).timestamp())
    p, tg = make(tmp_path, clock=clock, since="2026-11-01T00:00:00Z", daily_hour_utc=7)
    p.tick()                                                                          # 06:00 — the cards go out, the summary is too early
    assert not [m for m in tg.sent if "daily summary" in m["text"]]
    clock.t += 3700
    p.tick()
    daily = [m for m in tg.sent if "daily summary" in m["text"]]
    assert len(daily) == 1
    t = daily[0]["text"]
    assert "2026-11-05" in t and "Live launches: 2 · decided after 24 h: 2" in t and "Rugs among decided: 1" in t
    assert "7.00 NEAR" in t and "Track record" in t and "Low: 0/1" in t and "High: 1/1" in t
    clock.t += 600
    p.tick()
    assert len([m for m in tg.sent if "daily summary" in m["text"]]) == 1             # once
    # a day with no launches is remembered as skipped, never posted
    clock.t += 86400
    p.tick()
    assert len([m for m in tg.sent if "daily summary" in m["text"]]) == 1
    assert p.posts.execute("SELECT status FROM channel_posts WHERE kind = 'daily' AND token = '2026-11-06'").fetchone() == ("skipped",)


def test_preview_cli_prints_the_card_and_sends_nothing(tmp_path, capsys):
    seed(tmp_path / "c.db")
    assert P.main(["preview", TOKEN, "--db", str(tmp_path / "c.db")]) == 0
    out = capsys.readouterr().out
    assert TOKEN in out and "Risk score 60/100" in out


def test_the_publisher_does_not_need_the_web_dependencies():
    r = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, %r); import rugyomi.publisher; "
                        "assert 'fastapi' not in sys.modules and 'jinja2' not in sys.modules" % str(rugyomi_testkit.PUBLIC)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_env_targets_read_the_existing_admin_bot_names():
    t = P.env_targets({"TELEGRAM_BOT_TOKEN": "abc", "TELEGRAM_ALLOWED_IDS": "1, 2;3", "TG_CHANNEL_ID": " -100 "})
    assert t == {"token": "abc", "channel": "-100", "admins": ["1", "2", "3"]}
    assert P.env_targets({"TG_ALERT_BOT_TOKEN": "x"}) == {"token": "x", "channel": "", "admins": []}
