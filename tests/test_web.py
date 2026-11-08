"""RGY-014: the read-only site — pages on a temp DB built from the real REFRAX / ISLAND / COIN blocks, security headers,
no external resources, the database is opened read-only."""
import re
import sqlite3
import threading
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi_testkit import refrax_replay_blocks  # noqa: E402
from rugyomi.collector import Collector  # noqa: E402
from rugyomi.keys import EnvKeySource  # noqa: E402
from rugyomi.neardata import Neardata  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi.web import fmt  # noqa: E402

REFRAX, ISLAND, COIN = "refrax.nearlytrade.near", "island.nearlytrade.near", "coin-2.nearlytrade.near"
WEB = Path(webapp.__file__).parent


def _blocks():
    return refrax_replay_blocks()


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("web") / "w.db"
    s = open_store(path)
    c = Collector(s, Neardata(EnvKeySource({}), base="http://127.0.0.1:9", stop=threading.Event()), log=lambda m: None)
    for h, b in _blocks():
        c.process(h, b)
    s.close()
    return path


@pytest.fixture(scope="module")
def client(db):
    with TestClient(webapp.create_app(db)) as c:
        yield c


def test_feed_lists_the_three_launches_with_metrics(client):
    r = client.get("/")
    assert r.status_code == 200
    for tok in (REFRAX, ISLAND, COIN):
        assert f'/launch/{tok}' in r.text
    assert "T+5 min" in r.text and "T+60 min" in r.text
    assert "sold" in r.text and "holders" in r.text
    assert 'hx-get="/partials/feed"' in r.text and "every 10s" in r.text
    assert "scam" not in r.text.lower()


def test_feed_partial_is_the_table_only(client):
    r = client.get("/partials/feed")
    assert r.status_code == 200 and "<html" not in r.text and f"/launch/{REFRAX}" in r.text


def test_launch_page_shows_snapshots_and_creator_launches(client):
    r = client.get(f"/launch/{REFRAX}")
    assert r.status_code == 200
    assert "Risk card" in r.text and "coming soon" not in r.text
    assert "T+5 min" in r.text and "T+60 min" in r.text and "T+30 min" in r.text
    assert "nearblocks.io/txns/" in r.text and "nearblocks.io/address/" in r.text
    assert "/creator/thecoins.near" in r.text                 # the creator, linked
    r2 = client.get(f"/launch/{ISLAND}")
    assert f"/launch/{COIN}" in r2.text                     # the creator's other launch


def test_creator_page_lists_all_launches(client):
    r = client.get("/creator/coinisland.near")
    assert r.status_code == 200
    assert f"/launch/{ISLAND}" in r.text and f"/launch/{COIN}" in r.text
    assert "Created" in r.text and "Launches (" in r.text


def test_healthz(client):
    j = client.get("/healthz").json()
    assert j["ok"] is True and j["db"] == "ok" and isinstance(j["last_height"], int)
    assert j["last_block_age_s"] is not None


@pytest.mark.parametrize("path", ["/launch/nope.near", "/creator/nope.near", "/launch/..%2Fetc", "/launch/%27%20OR%201=1",
                                  "/creator/A", "/creator/" + "a" * 80, "/nothing", "/static/missing.css", "/docs", "/openapi.json"])
def test_unknown_and_invalid_ids_are_404_without_traces(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert "Traceback" not in r.text and "File \"" not in r.text


def test_write_methods_do_not_exist(client):
    for m in ("post", "put", "delete", "patch"):
        assert getattr(client, m)("/").status_code in (404, 405)
    assert client.post("/healthz").status_code == 405


def test_security_headers_on_every_response(client):
    for path in ("/", f"/launch/{REFRAX}", "/healthz", "/nope", "/static/site.css"):
        h = client.get(path).headers
        csp = h["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"
        assert "server" not in {k.lower() for k in h}


def test_no_external_resources_in_templates_and_static():
    pat = re.compile(r"""(?:src|href|action)\s*=\s*["']?(https?:)?//|url\(\s*["']?(https?:)?//|@import\s+["']?https?:""", re.I)
    for p in list((WEB / "templates").glob("*.html")) + [WEB / "static" / "site.css"]:
        for m in pat.finditer(p.read_text()):
            line = p.read_text()[max(0, m.start() - 10): m.end() + 60]
            assert "{{" in line and "_url(" in line, f"external resource in {p.name}: {line!r}"   # only explorer links via helpers
    for p in (WEB / "static").glob("pico*.css"):
        assert not re.search(r"url\(\s*[\"']?https?:", p.read_text()), "pico must not load anything remote"


def test_rendered_pages_link_out_only_to_nearblocks(client):
    for path in ("/", f"/launch/{REFRAX}", "/creator/coinisland.near"):
        for u in re.findall(r'(?:href|src)="(https?://[^"]+)"', client.get(path).text):
            assert u.startswith("https://nearblocks.io/"), u


def test_vendored_assets_and_licenses_are_served(client):
    for f in ("pico.min.css", "htmx.min.js", "site.css", "LICENSE-pico.txt", "LICENSE-htmx.txt"):
        r = client.get(f"/static/{f}")
        assert r.status_code == 200 and r.content, f
    assert "MIT" in client.get("/static/LICENSE-pico.txt").text


def test_the_database_is_opened_read_only(db):
    pool = webapp.ReadPool(db)
    try:
        with pool.store() as s:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                s.con.execute("CREATE TABLE x(a)")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                s.con.execute("DELETE FROM launches")
            # two independent layers: with PRAGMA query_only off, the read-only open mode alone still refuses writes
            s.con.execute("PRAGMA query_only=OFF")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                s.con.execute("DELETE FROM launches")
            s.con.execute("PRAGMA query_only=ON")
    finally:
        pool.close()


def test_a_slow_query_is_cut_off(db):
    pool = webapp.ReadPool(db, timeout=0.2)
    try:
        with pool.store() as s:
            with pytest.raises(sqlite3.OperationalError, match="interrupted"):
                s.con.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c").fetchone()
        with pool.store() as s:                                        # the connection is reusable afterwards
            assert s.con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()


def test_a_broken_database_gives_a_generic_500_and_503_healthz(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 100)
    with TestClient(webapp.create_app(bad), raise_server_exceptions=False) as c:
        r = c.get("/")
        assert r.status_code == 500 and "Traceback" not in r.text and "sqlite" not in r.text.lower()
        assert c.get("/healthz").status_code == 503
        assert "content-security-policy" in r.headers


def test_feed_is_cached_for_a_moment(db):
    with TestClient(webapp.create_app(db)) as c:
        a = c.get("/partials/feed").text
        assert c.get("/partials/feed").text == a


def test_html_is_escaped(db, tmp_path):
    import shutil
    p = tmp_path / "x.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    con.execute("UPDATE launches SET extra = json_set(coalesce(extra,'{}'), '$.symbol', '<script>alert(1)</script>') WHERE token = ?", (ISLAND,))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        for path in ("/", f"/launch/{ISLAND}"):
            t = c.get(path).text
            assert "<script>alert(1)" not in t and "&lt;script&gt;" in t


# ---- formatting
def test_fmt():
    assert fmt.near(str(40 * 10**24)) == "40.00 NEAR" and fmt.near(None) == "—"
    assert fmt.near(str(5 * 10**21)) == "0.0050 NEAR"
    assert fmt.signed_near(str(-3 * 10**24)) == "-3.00 NEAR" and fmt.signed_near(str(3 * 10**24)) == "+3.00 NEAR"
    assert fmt.pct_bps(9900) == "99%" and fmt.pct_bps(1234) == "12.3%" and fmt.pct_bps(None) == "—"
    assert fmt.seconds(33) == "33 s" and fmt.seconds(300) == "5.0 min"
    assert fmt.utc(1_800_000_000 * 10**9).endswith("UTC") and fmt.tx_url("abc") == "https://nearblocks.io/txns/abc"
    assert fmt.valid_account("coinisland.near") and not fmt.valid_account("../x") and not fmt.valid_account("A.near")


def test_the_server_binds_to_the_local_address_by_default(monkeypatch, db):
    import sys
    import types
    from rugyomi.web import __main__ as m
    seen = {}
    fake = types.SimpleNamespace(run=lambda app, **kw: seen.update(kw))
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    assert m.main(["--db", str(db)]) == 0
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8000 and seen["access_log"] is False


def test_launchbot_owner_evidence_and_honest_history_coverage(db, tmp_path):
    import shutil
    p = tmp_path / "y.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    con.execute("UPDATE launches SET creator = 'launchbot.nearlytrade.near', owner = 'coinisland.near', "
                "owner_evidence = 'near_transfer 5 NEAR in the launch tx' WHERE token = ?", (REFRAX,))
    con.execute("INSERT INTO launches(token,launchpad,creator,owner,owner_evidence,pool,dev_buy,height,ts_ns,tx,detected_at,extra,coverage) "
                "VALUES('old.nearlytrade.near','nearly','coinisland.near','coinisland.near',NULL,NULL,NULL,5,1700000000000000000,NULL,'x',NULL,'launch_only')")
    con.execute("INSERT INTO launch_index(token,launchpad,creator,create_height,create_ts_ns,source,imported_at) "
                "VALUES('idxonly.nearlytrade.near','nearly','coinisland.near',7,1600000000000000000,'chain_index','x')")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        t = c.get(f"/launch/{REFRAX}").text
        assert "Owner (paid the launch bot)" in t and "near_transfer 5 NEAR in the launch tx" in t and "/creator/coinisland.near" in t
        assert f"/launch/{ISLAND}" in t                                  # the owner's other launches are found through `owner`
        cr = c.get("/creator/coinisland.near").text
        assert "history: launch only" in cr and "history: index only" in cr and "old.nearlytrade.near" in cr
        old = c.get("/launch/old.nearlytrade.near")
        assert old.status_code == 200 and "No snapshots: this launch comes from the history" in old.text
        feed = c.get("/").text
        assert "old.nearlytrade.near" not in feed                        # the feed shows live launches only


# ---- RGY-015: the card on the site
def test_launch_page_shows_the_card_with_reasons_and_evidence(client):
    t = client.get(f"/launch/{REFRAX}").text
    assert "band-high" in t and "<strong>70</strong>" in t
    assert "Fresh creator setup (3 of 3)" in t and "coin-2.nearlytrade.near" in t
    assert "nearblocks.io/txns/3urt5C9xBdUonHF7Z2cJ1dVMXLESfPuexbbikExRwJdG" in t        # the funding transfer, as evidence
    assert f'href="/launch/{COIN}"' in t                                                   # the earlier launch, as evidence
    assert "v0 — weights are provisional; calibration in progress." in t and "config v0-default-1" in t
    assert "scam" not in t.lower()


def test_feed_shows_score_and_band(client):
    t = client.get("/").text
    assert "<th>Risk</th>" in t and "band-high" in t and "band-low" in t


def test_methodology_is_generated_from_the_registry_with_default_thresholds(client):
    from rugyomi import rules
    t = client.get("/methodology").text
    for s in rules.methodology()["signals"]:                     # the live profile (v0); the v1 section comes with RGY-021 step 4
        assert f"<code>{s['id']}</code>" in t
    assert "Low" in t and "Elevated" in t and "High" in t and "dev_exit" in t and "buyers_loss" in t
    assert "v0-default-1" in t and "calibration in progress" in t
    assert "Traceback" not in t


def test_methodology_never_shows_the_production_override(db, tmp_path, monkeypatch):
    over = tmp_path / "over.json"
    over.write_text('{"version": "prod-secret-7", "signals": {"funded_by_dumper": {"points": 77}}}')
    monkeypatch.setenv("RUGYOMI_RULES_CONFIG", str(over))
    with TestClient(webapp.create_app(db)) as c:
        t = c.get("/methodology").text
        assert "prod-secret-7" not in t and "77" not in t.replace("T+", "")
        assert "v0-default-1" in t


def test_launch_without_a_card_says_why(db, tmp_path):
    import shutil
    p = tmp_path / "n.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    con.execute("DROP TRIGGER cards_no_delete")
    con.execute("DELETE FROM cards WHERE token IN (?, ?)", (ISLAND, COIN))
    con.execute("UPDATE launches SET coverage = 'launch_only' WHERE token = ?", (COIN,))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        assert "recorded before risk cards were introduced" in c.get(f"/launch/{ISLAND}").text
        assert "comes from the history, before cards existed" in c.get(f"/launch/{COIN}").text


def test_site_works_on_a_database_without_the_cards_table(db, tmp_path):
    import shutil
    p = tmp_path / "o.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    for t in ("cards_no_update", "cards_no_delete", "cards_no_replace"):
        con.execute(f"DROP TRIGGER {t}")
    con.execute("DROP TABLE cards")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        assert c.get("/").status_code == 200
        r = c.get(f"/launch/{REFRAX}")
        assert r.status_code == 200 and "recorded before risk cards were introduced" in r.text


def test_tax_options_are_shown_when_present(client):
    assert "Tax options" in client.get(f"/launch/{REFRAX}").text


# ---------------------------------------------------------------- RGY-016: the Outcome block and the rug wording

def test_launch_page_shows_the_rug_outcome_with_evidence(client):
    r = client.get(f"/launch/{REFRAX}")
    t = r.text
    assert "Outcome" in t and "rug within 24 h" in t
    assert "Rug — fast (by T+60 min)" in t and "with config v0-default-1" in t and "not final until T+24 h" in t
    assert "lost <strong>2.53 NEAR</strong>" in t                       # the severity: outside buyers' loss
    assert "Insiders (the dev and the wallets the dev sent tokens to): 1" in t and "100%" in t
    assert "/creator/thecoins.near" in t and "(dev)" in t               # who sold
    assert "nearblocks.io/txns/" in t                                  # the transaction where the threshold was passed
    assert "Rug chance (24 h)" in t and "rug chance (24 h)" in t       # the card wording
    assert "scam" not in t.lower() and "and at 24 hours after the launch" in t       # behaviour, not accusation


def test_outcome_is_pending_when_the_snapshot_is_not_there_yet(db):
    import sqlite3 as sq
    path = db.parent / "pending.db"
    import shutil
    shutil.copy(db, path)
    con = sq.connect(path)
    con.execute("DELETE FROM launch_outcomes WHERE token = ?", (ISLAND,))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(path)) as c:
        t = c.get(f"/launch/{ISLAND}").text
    assert "<strong>Pending.</strong>" in t and "Rug —" not in t


def test_methodology_defines_the_rug_label(client):
    t = client.get("/methodology").text
    for w in ("rug_fast", "rug_late", "chance of a rug within 24 hours", "Selling alone is not a rug", "Rules v1", "No lookahead", "not a track record"):
        assert w in t, w


def test_the_site_still_serves_a_v7_database(tmp_path):
    """A database not migrated yet (the collector restarts after the site, or the other way round) must not break the pages."""
    import sqlite3 as sq
    from rugyomi.store.schema import MIGRATIONS
    path = tmp_path / "v7.db"
    con = sq.connect(path)
    from rugyomi.store.db import _split
    for i in range(7):
        for stmt in _split(MIGRATIONS[i]):
            con.execute(stmt)
    con.execute("PRAGMA user_version=7")
    con.execute("INSERT INTO launches(token, launchpad, creator, owner, owner_evidence, pool, dev_buy, height, ts_ns, tx, detected_at, extra) "
                "VALUES('x.nearlytrade.near','nearly','a.near','a.near',NULL,'p','1',1,1000,NULL,'t','{}')")
    con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, metric_version, computed_at, buyers_count, buyers_pnl, dev_sold_share_bps) "
                "VALUES('x.nearlytrade.near',5,1,1,'t',1,'-5',9500)")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(path)) as c:
        r = c.get("/launch/x.nearlytrade.near")
        assert r.status_code == 200 and "<strong>Pending.</strong>" in r.text
        assert c.get("/").status_code == 200


def _history_json(score_lo=0, score_hi=100):
    ci = {"lo": 0.30, "hi": 0.52, "observed": 0.40, "n": 60, "rugs": 24, "inside": True}
    blk = {"lo_score": score_lo, "hi_score": score_hi, "predicted": 0.37, "train_n": 120, **ci}
    cov = {"rugs": 0, "flagged": 0, "share": None, "severity_flagged": 0, "severity_total": 0, "severity_share": None}
    prof = {"auc": 0.71, "by_band": [], "by_range": [], "coverage": cov}
    return {"split": "2026-10-20", "generated_at": "x", "note": "A backtest, not a track record: test note.",
            "train": {"n": 300, "rugs": 90, "base_rate": 0.3}, "holdout": {"n": 100, "rugs": 40, "base_rate": 0.4, "live": 30},
            "profiles": {"v0": prof, "v1": prof}, "calibrated": "v1",
            "calibration": {"v0": {"blocks": [], "brier_scale": None, "brier_constant": None},
                            "v1": {"blocks": [blk], "brier_scale": 0.2, "brier_constant": 0.24, "n": 100}}}


def _with_history(db, tmp_path, rules_version):
    import json
    import shutil
    p = tmp_path / f"h{rules_version}.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    con.execute("DROP TRIGGER cards_no_update")
    con.execute("UPDATE cards SET rules_version = ?", (rules_version,))
    con.execute("INSERT INTO collector_status(key, value, updated_at) VALUES('calibration_history', ?, 'x')", (json.dumps(_history_json()),))
    con.commit()
    con.close()
    return p


def test_accuracy_shows_the_history_calibration_as_a_backtest(db, tmp_path):
    p = _with_history(db, tmp_path, "1.0")
    with TestClient(webapp.create_app(p)) as c:
        t = " ".join(c.get("/accuracy").text.split())
    assert "Calibration on history" in t and "Not a track record" in t and "2026-10-20 00:00 UTC" in t
    assert "holdout" in t and "0.200" in t and "0.240" in t and "37%" in t


def test_accuracy_without_history_says_not_published(client):
    t = client.get("/accuracy").text
    assert "Calibration on history" in t and "Not published yet" in t


def test_rug_chance_only_on_v1_cards_and_only_with_a_calibration(db, tmp_path):
    with TestClient(webapp.create_app(_with_history(db, tmp_path, "1.0"))) as c:
        t = " ".join(c.get(f"/launch/{REFRAX}").text.split())
    assert "Rug chance ≈ <strong>37%</strong>" in t and "90% interval 30–52%" in t and "A backtest, not a promise" in t
    with TestClient(webapp.create_app(_with_history(db, tmp_path, "0.1"))) as c:      # a v0 card: never a probability
        assert "Rug chance ≈" not in c.get(f"/launch/{REFRAX}").text
    p = tmp_path / "v1nocal.db"
    import shutil
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    con.execute("DROP TRIGGER cards_no_update")
    con.execute("UPDATE cards SET rules_version = '1.0'")
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:                                      # v1 card, nothing published
        assert "Rug chance ≈" not in c.get(f"/launch/{REFRAX}").text
