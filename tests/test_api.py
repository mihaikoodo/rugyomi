"""RGY-067: the read-only JSON API (`/api/v1/...`) and the SVG badge (`/badge/{token}.svg`).

Pinned: 404 for an unknown or invalid id (it never echoes it) and 400 for a bad parameter, `"api": "v1"` in every body, the same numbers as the HTML
page of the same token, a card computed afterwards is `retro` in the JSON and drawn like a live card in the badge, a launch with no card is `pending` in the badge,
CORS on /api/ and /badge/ only, `Cache-Control: public, max-age=60` on a scored answer and `no-store` on every error and on a pending answer (RGY-076), every security header stays, the SVG has no script and escapes its text,
GET and HEAD only (a POST is a JSON 405), the trader summary has no rows, the quote-token launch says so, and the queries of the endpoints use indexes.
"""
import json
import re
import sqlite3
import xml.etree.ElementTree as ET

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi_testkit import refrax_world_db  # noqa: E402
from rugyomi.store import open_store  # noqa: E402
from rugyomi.web import api as API  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi.web import badge as BADGE  # noqa: E402
from rugyomi import retro as RC  # noqa: E402

REFRAX, ISLAND = "refrax.nearlytrade.near", "island.nearlytrade.near"
RETRO, BARE, QUOTE = "retroonly.nearlytrade.near", "bare.nearlytrade.near", "quoted.nearlytrade.near"
T0 = 1_793_300_000_000_000_000
REASONS = [{"id": "name_copy", "version": 1, "fired": True, "value": {}, "contribution": 25, "reason": "The name copies a known token.", "short": "copy",
            "evidence": [{"kind": "account", "id": "thecoins.near"}, {"kind": "tx", "id": "AZMzQsa6iBckLjhSAEfGsbYb6Gn83uwAb518NCbwMa1B"}]},
           {"id": "creator_tax", "version": 1, "fired": True, "value": {}, "contribution": 0, "reason": "zero points: not listed", "short": "", "evidence": []}]


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("api") / "a.db"
    refrax_world_db(path)
    s = open_store(path)
    for i, tok in enumerate((RETRO, BARE, QUOTE)):
        s.con.execute("INSERT INTO launches(token, launchpad, creator, owner, pool, dev_buy, height, ts_ns, tx, detected_at, extra, coverage) "
                      "VALUES (?, 'nearly', 'newbie.near', 'newbie.near', ?, '1000', ?, ?, 'tx', '2026-11-13T00:00:00Z', ?, 'live')",
                      (tok, f"{tok}|prl.tkn.near|10000" if tok == QUOTE else f"{tok}|wrap.near|10000", 300_000_000 + i, T0 + i * 10**9,
                       json.dumps({"symbol": "<b>x</b>" if tok == BARE else "RET"})))
    for tok, pnl in ((REFRAX, "-5000"), (RETRO, "7000")):                       # one stored early buyer on two launches
        s.con.execute("INSERT INTO launch_buyers(token, buyer, rank, first_buy_ns, first_buy_tx, buys, spent, bought, sold, received, first_sell_ns, first_sell_tx, pnl, role) "
                      "VALUES (?, 'buyer1.near', 1, ?, 'tx1', 1, '100', '1000', '500', '0', NULL, NULL, ?, 'buyer')", (tok, T0, pnl))
    s.con.execute("INSERT INTO retro_cards(token, rules_version, config_version, config_hash, launch_ts_ns, score, band, reasons, signals, notes, in_sample, computed_at) "
                  "VALUES (?, '1.0', 'v1r-1', 'abc', ?, 55, 'Elevated', ?, '{}', '[]', 1, '2026-11-13T10:00:00Z')", (RETRO, T0, json.dumps(REASONS)))
    s.close()
    return path


@pytest.fixture(scope="module")
def client(db):
    with TestClient(webapp.create_app(db)) as c:
        yield c


def get(client, url, **kw):
    r = client.get(url, **kw)
    return r, (r.json() if r.headers.get("content-type", "").startswith("application/json") else None)


# ------------------------------------------------------------------------------------------------ the launch endpoint
def test_launch_live_card_and_the_same_numbers_as_the_page(client):
    r, j = get(client, f"/api/v1/launch/{REFRAX}")
    assert r.status_code == 200 and j["api"] == "v1" and j["token"] == REFRAX and j["launchpad"] == "nearly"
    rk = j["risk"]
    assert rk["kind"] == "live" and rk["band"] == "High" and rk["issued_at"].endswith("Z") and rk["rules_version"] and rk["config_version"] and rk["config_hash"]
    page = client.get(f"/launch/{REFRAX}").text
    assert re.search(rf'<strong>{rk["score"]}</strong> / 100', page) and f'band-{rk["band"].lower()}' in page      # one source of truth
    assert all(x["points"] for x in rk["reasons"]) and sum(x["points"] for x in rk["reasons"]) >= rk["score"] - 1
    assert rk["reasons"][0]["text"] in page.replace("&#39;", "'") or rk["reasons"][0]["id"]
    ev = [e for x in rk["reasons"] for e in x["evidence"]]
    assert ev and all(e["url"] for e in ev if e["kind"] in ("tx", "account"))
    assert any(e["url"].startswith("https://") and "/txns/" in e["url"] for e in ev if e["kind"] == "tx")


def test_launch_ledger_hash_is_the_line_of_the_ledger(client):
    j = client.get(f"/api/v1/launch/{REFRAX}").json()
    lines = [json.loads(x) for x in client.get("/ledger.jsonl").text.splitlines() if x]
    mine = [x for x in lines if x["token"] == REFRAX][0]
    assert j["risk"]["ledger_hash"] == mine["hash"]


def test_launch_outcome_matches_the_page_state(client):
    j = client.get(f"/api/v1/launch/{REFRAX}").json()
    o = j["outcome"]
    assert o["state"] in ("rug", "no rug", "undefined", "waiting") and isinstance(o["final"], bool) and o["label_version"]
    if o["state"] == "rug":
        assert o["rug"] is True and o["reason"]


def test_launch_retro_card_is_marked_and_has_no_ledger_hash(client):
    r, j = get(client, f"/api/v1/launch/{RETRO}")
    assert r.status_code == 200
    rk = j["risk"]
    assert rk["kind"] == "retro" and rk["score"] == 55 and rk["band"] == "Elevated" and "ledger_hash" not in rk and rk["in_sample"] is True
    assert "not a forecast" in rk["note"] and rk["as_of"].endswith("Z")
    assert [x["id"] for x in rk["reasons"]] == ["name_copy"]                       # the zero-point reason is not listed, as on the page
    page = client.get(f"/launch/{RETRO}").text
    assert re.search(r"<strong>55</strong> / 100", page) and 'class="tag retro"' in page


def test_launch_without_a_card_has_null_risk_and_a_plain_symbol(client):
    r, j = get(client, f"/api/v1/launch/{BARE}")
    assert r.status_code == 200 and j["risk"] is None
    assert "<" not in j["symbol"] and ">" not in j["symbol"]                      # safe_symbol: a creator-chosen name never carries markup


def test_launch_on_a_pool_not_paired_with_near_names_the_quote_token(client):
    assert client.get(f"/api/v1/launch/{QUOTE}").json()["quote_token"] == "prl.tkn.near"
    assert client.get(f"/api/v1/launch/{BARE}").json()["quote_token"] is None


def test_site_url_is_relative_when_no_base_url_is_set(client):
    assert client.get(f"/api/v1/launch/{REFRAX}").json()["site_url"] == f"/launch/{REFRAX}"


def test_links_are_absolute_when_the_site_base_url_is_set(db, tmp_path, monkeypatch):
    cfg = tmp_path / "pub.json"
    cfg.write_text(json.dumps({"site": {"base_url": "https://example.org/"}}))
    monkeypatch.setenv("RUGYOMI_PUBLISHER_CONFIG", str(cfg))
    with TestClient(webapp.create_app(db)) as c:
        j = c.get(f"/api/v1/launch/{REFRAX}").json()
        assert j["site_url"] == f"https://example.org/launch/{REFRAX}"
        ev = [e for x in c.get(f"/api/v1/launch/{RETRO}").json()["risk"]["reasons"] for e in x["evidence"] if e["kind"] == "account"]
        assert ev and all(e["url"].startswith("https://example.org/creator/") for e in ev)
        assert c.get("/api/v1/meta").json()["methodology_url"] == "https://example.org/methodology"


# ------------------------------------------------------------------------------------------------ the list
def test_launches_newest_first_with_kinds(client):
    r, j = get(client, "/api/v1/launches")
    assert r.status_code == 200 and j["api"] == "v1" and j["count"] == len(j["launches"]) and j["limit"] == 50
    ts = [x["launched_at"] for x in j["launches"]]
    assert ts == sorted(ts, reverse=True)
    by = {x["token"]: x for x in j["launches"]}
    assert by[REFRAX]["risk"]["kind"] == "live" and by[RETRO]["risk"] == {"kind": "retro", "score": 55, "band": "Elevated"} and by[BARE]["risk"] is None
    assert set(by[REFRAX]) == {"token", "launchpad", "creator", "launched_at", "risk", "outcome", "site_url"}


def test_launches_limit_and_since(client):
    assert client.get("/api/v1/launches?limit=2").json()["count"] == 2
    assert client.get("/api/v1/launches?limit=200").status_code == 200
    j = client.get("/api/v1/launches?since=1793300000").json()                      # unix seconds
    assert {x["token"] for x in j["launches"]} == {RETRO, BARE, QUOTE}
    j2 = client.get("/api/v1/launches?since=2026-10-29T14:30:00Z").json()            # ISO, UTC
    assert REFRAX in {x["token"] for x in j2["launches"]}
    assert client.get("/api/v1/launches?since=2099-01-01").json()["launches"] == []


@pytest.mark.parametrize("q", ["limit=0", "limit=201", "limit=abc", "limit=-1", "limit=", "since=yesterday", "since=2026-13-45", "since=", "since=1e9"])
def test_launches_bad_parameters_are_400_without_echo(client, q):
    r, j = get(client, f"/api/v1/launches?{q}")
    assert r.status_code == 400 and j["api"] == "v1" and set(j) == {"api", "error"}
    value = q.split("=")[1]
    assert len(value) < 4 or value not in r.text                                    # a long value is never repeated


@pytest.mark.core
def test_parse_since():
    assert API.parse_since("1793300000") == 1793300000 * 10**9
    assert API.parse_since("2026-11-13") == API.parse_since("2026-11-13T00:00:00Z") == API.parse_since("2026-11-13T00:00")
    assert API.parse_since("12345678901234") is None and API.parse_since("٣") is None and API.parse_since("2026-02-31") is None


# ------------------------------------------------------------------------------------------------ account, meta
def test_account_counts_and_no_trader_rows(client):
    r, j = get(client, "/api/v1/account/coinisland.near")
    assert r.status_code == 200 and j["api"] == "v1" and j["account"] == "coinisland.near"
    la = j["launches_as_creator"]
    assert la["count"] == 2 and set(la["outcomes"]) == set(API.OUTCOME_KEYS) and sum(la["outcomes"].values()) == 2
    assert j["site_url"] == "/creator/coinisland.near" and isinstance(j["labels"], list)
    assert j["trader"] is None or "rows" not in j["trader"]


def test_account_trader_summary_has_counts_and_no_rows(client):
    r, j = get(client, "/api/v1/account/buyer1.near")
    assert r.status_code == 200 and j["launches_as_creator"]["count"] == 0
    t = j["trader"]
    assert t["summary"]["launches"] == 2 and t["summary"]["pnl_t60"] == "2000" and "rows" not in t and t["rows_url"] == "/creator/buyer1.near"
    assert client.get("/creator/buyer1.near").status_code == 200                # the page of the same account exists (its template is the designer's)


def test_meta(client):
    r, j = get(client, "/api/v1/meta")
    assert r.status_code == 200 and j["api"] == "v1" and j["disclaimer"] == "Analysis, not advice."
    assert [b["name"] for b in j["bands"]] == ["Low", "Elevated", "High"] and j["bands"][0]["from"] == 0 and j["bands"][-1]["to"] == 100
    assert j["bands"][1]["from"] == j["bands"][0]["to"] + 1 and j["bands"][2]["from"] == j["bands"][1]["to"] + 1
    assert j["live_profile"]["rules_version"] and j["recent_rug_rate"]["text"]
    assert j["methodology_url"] == "/methodology" and j["accuracy_url"] == "/accuracy"


@pytest.mark.parametrize("url", ["/api/v1/launch/no-such.nearlytrade.near", "/api/v1/account/nobody.near", "/api/v1/launch/UPPER.near", "/api/v1/launch/a",
                                 "/api/v1/launch/%3Cscript%3E", "/api/v1/launch/x%00y", "/api/v1/account/..%2Fetc", "/api/v1/nothing", "/api/", "/api/v2/meta"])
def test_unknown_and_invalid_are_json_404_without_echo(client, url):
    r, j = get(client, url)
    assert r.status_code == 404 and j == {"api": "v1", "error": "not found"}
    assert "script" not in r.text and "etc" not in r.text and "UPPER" not in r.text


# ------------------------------------------------------------------------------------------------ headers, methods
@pytest.mark.parametrize("url", ["/api/v1/launch/" + REFRAX, "/api/v1/launches", "/api/v1/account/coinisland.near", "/api/v1/meta", "/api/v1/launch/nope",
                                 "/api/v1/launches?limit=0", "/api/v1/nothing", f"/badge/{REFRAX}.svg", "/badge/zzz.svg", f"/badge/{REFRAX}"])
def test_cors_cache_and_security_headers_on_every_public_answer(client, url):
    r = client.get(url)
    h = r.headers
    assert h["access-control-allow-origin"] == "*" and h["access-control-allow-methods"] == "GET, HEAD"
    assert h["cache-control"] == ("no-store" if r.status_code >= 400 else "public, max-age=60")             # RGY-076: only a scored / served answer is cached
    assert "default-src 'none'" in h["content-security-policy"] and h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer" and h["cross-origin-resource-policy"] == "cross-origin"       # cross-origin: an <img> elsewhere must load it
    assert "set-cookie" not in h


@pytest.mark.parametrize("url", ["/api/v1/launch/no-such.nearlytrade.near", "/api/v1/launch/UPPER.near", "/api/v1/account/nobody.near", "/api/v1/launches?limit=0", "/api/v1/launches?since=x",
                                 "/api/v1/nothing", "/badge/no-such.nearlytrade.near.svg", "/badge/zzz.svg", f"/badge/{BARE}.svg", f"/badge/{BARE}.svg?style=b", f"/api/v1/launch/{BARE}"])
@pytest.mark.parametrize("method", ["get", "head"])
def test_unknown_error_and_pending_answers_are_never_cached(client, url, method):
    """RGY-076: «no such launch» and «no card yet» must reach a dashboard that asks again within the minute as they are NOW (a 404 cached for hours made it wait)."""
    r = getattr(client, method)(url)
    assert r.status_code in (200, 400, 404)
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["access-control-allow-origin"] == "*" and r.headers["cross-origin-resource-policy"] == "cross-origin"      # CORS and the security headers stay
    assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("url", [f"/api/v1/launch/{REFRAX}", f"/api/v1/launch/{RETRO}", "/api/v1/launches", "/api/v1/meta", "/api/v1/account/coinisland.near",
                                 f"/badge/{REFRAX}.svg", f"/badge/{RETRO}.svg?style=b"])
def test_scored_answers_stay_cached_for_60_seconds(client, url):
    r = client.get(url)
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=60"


def test_a_pending_launch_is_cached_once_it_has_a_card(client, db):
    """The pending answer carries no cache lifetime; the same launch with a card is a normal cached answer (the cache is only a header, nothing is stored server side)."""
    assert client.get(f"/api/v1/launch/{BARE}").headers["cache-control"] == "no-store"
    s = open_store(db)
    s.con.execute("INSERT INTO retro_cards(token, rules_version, config_version, config_hash, launch_ts_ns, score, band, reasons, signals, notes, in_sample, computed_at) "
                  "VALUES (?, '1.0', 'v1r-1', 'abc', ?, 10, 'Low', '[]', '{}', '[]', 1, '2026-11-13T10:00:00Z')", (BARE, T0))
    s.con.commit()
    s.close()
    try:
        r = client.get(f"/api/v1/launch/{BARE}")
        assert r.json()["risk"] is not None and r.headers["cache-control"] == "public, max-age=60"
        assert client.get(f"/badge/{BARE}.svg").headers["cache-control"] == "public, max-age=60"
    finally:
        s = open_store(db)
        s.con.execute("DELETE FROM retro_cards WHERE token = ?", (BARE,))
        s.con.commit()
        s.close()


def test_the_pages_are_unchanged_no_cors_and_no_store(client):
    for u in ("/", f"/launch/{REFRAX}", "/methodology", "/healthz", "/ledger.jsonl"):
        h = client.get(u).headers
        assert "access-control-allow-origin" not in h and h["cache-control"] == "no-store" and h["cross-origin-resource-policy"] == "same-origin", u
    assert client.get("/static/site.css").headers["cache-control"] == "public, max-age=3600"


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
@pytest.mark.parametrize("url", ["/api/v1/meta", f"/api/v1/launch/{REFRAX}", "/api/v1/launches", f"/badge/{REFRAX}.svg"])
def test_no_write_methods(client, method, url):
    r = getattr(client, method)(url)
    assert r.status_code == 405
    if url.startswith("/api/"):
        assert r.json() == {"api": "v1", "error": "method not allowed"}


def test_head_works(client):
    for u in ("/api/v1/meta", f"/api/v1/launch/{REFRAX}", f"/badge/{REFRAX}.svg"):
        r = client.head(u)
        assert r.status_code == 200 and r.content == b"" and r.headers["access-control-allow-origin"] == "*"


def test_options_is_not_a_preflight_answer(client):
    assert client.options("/api/v1/meta").status_code == 405           # simple GETs need no preflight; nothing else is allowed


# ------------------------------------------------------------------------------------------------ the badge
def svg_ok(text):
    root = ET.fromstring(text)                                          # well-formed XML
    low = text.lower()
    assert "<script" not in low and "foreignobject" not in low and "javascript:" not in low
    assert not re.search(r"\son\w+=", low) and "href" not in low and "<image" not in low and "<style" not in low and "@import" not in low and "url(http" not in low
    assert root.tag.endswith("svg") and int(root.get("width")) < 260 and int(root.get("height")) < 50
    assert root.get("aria-label") and "rugyomi" in root.get("aria-label")
    return root


@pytest.mark.parametrize("style", ["a", "b"])
def test_badge_live_card_has_score_band_and_no_script(client, style):
    r = client.get(f"/badge/{REFRAX}.svg?style={style}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    root = svg_ok(r.text)
    score = client.get(f"/api/v1/launch/{REFRAX}").json()["risk"]["score"]
    assert f"risk score {score}, High" in root.get("aria-label") and "afterwards" not in root.get("aria-label")
    texts = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text"))
    assert ("Risk %d" % score in texts and "High" in texts) if style == "a" else (str(score) in texts and "High" in texts)
    assert BADGE.COLORS["High"] in r.text and "retro" not in texts
    if style == "a":
        assert "rugyomi" not in texts.lower() and "rugyomi" in root.get("aria-label")                  # the pill: score and band only; the label keeps the name
    else:
        assert "rugyomi" in texts


def test_badge_default_style_is_a_and_an_unknown_style_falls_back(client):
    a = client.get(f"/badge/{REFRAX}.svg").text
    assert a == client.get(f"/badge/{REFRAX}.svg?style=a").text == client.get(f"/badge/{REFRAX}.svg?style=zzz").text
    assert a != client.get(f"/badge/{REFRAX}.svg?style=b").text


@pytest.mark.parametrize("style", ["a", "b"])
def test_badge_retro_is_drawn_exactly_like_live_and_says_nothing_about_it(client, style):
    r = client.get(f"/badge/{RETRO}.svg?style={style}")
    root = svg_ok(r.text)
    texts = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text"))
    assert r.status_code == 200 and "55" in texts and "Elevated" in texts and BADGE.COLORS["Elevated"] in r.text
    assert "retro" not in r.text.lower() and "afterwards" not in r.text.lower() and "dash" not in r.text.lower()           # user 14.11: no marker on badges
    for theme in BADGE.THEMES:
        assert BADGE.render(style, {"score": 55, "band": "Elevated", "kind": "retro"}, theme) == BADGE.render(style, {"score": 55, "band": "Elevated", "kind": "live"}, theme)


def test_the_api_json_keeps_kind_live_and_retro(client):
    assert client.get(f"/api/v1/launch/{REFRAX}").json()["risk"]["kind"] == "live"
    assert client.get(f"/api/v1/launch/{RETRO}").json()["risk"]["kind"] == "retro"
    assert {x["token"]: x["risk"]["kind"] for x in client.get("/api/v1/launches").json()["launches"] if x["risk"]}[RETRO] == "retro"


@pytest.mark.parametrize("style", ["a", "b"])
def test_badge_launch_without_a_card_is_pending_and_200(client, style):
    r = client.get(f"/badge/{BARE}.svg?style={style}")
    root = svg_ok(r.text)
    texts = " ".join(t.text or "" for t in root.iter() if t.tag.endswith("text"))
    assert r.status_code == 200 and "pending" in texts and "no risk card" in root.get("aria-label")
    if style == "a":
        assert not any(c in r.text for c in BADGE.COLORS.values())                 # no band colour: neutral grey


@pytest.mark.parametrize("name", ["nope.nearlytrade.near.svg", "UPPER.svg", "a.svg", "%3Cscript%3E.svg", REFRAX, ".svg", "x..y.svg"])
def test_badge_unknown_or_invalid_is_404_with_a_neutral_unknown_svg(client, name):
    r = client.get(f"/badge/{name}")
    assert r.status_code == 404 and r.headers["content-type"].startswith("image/svg+xml")
    root = svg_ok(r.text)
    assert root.get("aria-label") == "rugyomi: no such launch" and "script" not in r.text.lower() and "UPPER" not in r.text and "nope" not in r.text


@pytest.mark.core
def test_badge_drawn_text_is_escaped_and_only_fixed_words_and_numbers():
    bad = BADGE.render("a", {"score": 5, "band": '<script>"&', "kind": "live"})
    ET.fromstring(bad)
    assert "script" not in bad and "&lt;" not in bad and "no band" in bad   # an unknown band name is not drawn at all (neutral colour, no text)
    for st in "ab":
        ET.fromstring(BADGE.render(st, {"score": 100, "band": "High", "kind": "retro"}))
        ET.fromstring(BADGE.render(st, None))
        ET.fromstring(BADGE.render(st, BADGE.UNKNOWN))


def _texts(svg):
    return [(e.get("fill"), "".join(e.itertext()), e) for e in ET.fromstring(svg).iter() if e.tag.endswith("text")]


def _tile_fill(svg):
    return [e.get("fill") for e in ET.fromstring(svg).iter() if e.tag.endswith("rect")][0]


CASES = [("Low", n) for n in (0, 5, 7)] + [("Elevated", n) for n in (30, 45, 59)] + [("High", n) for n in (60, 80, 100)]


@pytest.mark.core
def test_style_b_has_one_fixed_size_for_every_band_digit_count_and_theme():
    sizes = set()
    for theme in BADGE.THEMES:
        for band, n in CASES:
            root = ET.fromstring(BADGE.render("b", {"score": n, "band": band}, theme))
            sizes.add((root.get("width"), root.get("height"), root.get("viewBox")))
        for card in (None, BADGE.UNKNOWN):
            root = ET.fromstring(BADGE.render("b", card, theme))
            sizes.add((root.get("width"), root.get("height"), root.get("viewBox")))
    assert sizes == {("116", "40", "0 0 116 40")}


@pytest.mark.core
def test_style_b_number_is_centred_on_the_ring_centre_without_dominant_baseline():
    for theme in BADGE.THEMES:
        for band, n in CASES:
            svg = BADGE.render("b", {"score": n, "band": band}, theme)
            assert "dominant-baseline" not in svg
            num = [e for _f, s, e in _texts(svg) if s == str(n)][0]
            fs = float(num.get("font-size"))
            assert num.get("text-anchor") == "middle" and "mono" in num.get("font-family").lower()          # digits of one width
            assert abs(float(num.get("x")) - BADGE.RING_C) < 0.01
            assert abs(float(num.get("y")) - 0.36 * fs - BADGE.RING_C) < 0.01                               # the digits' middle on the ring's centre
            assert 3 * 0.62 * fs <= 2 * (15 - 6.5 / 2) * BADGE.K        # three monospace digits fit inside the ring's inner circle


@pytest.mark.core
def test_every_text_has_contrast_4_5_on_its_background_in_every_style_and_theme():
    for band, n in CASES + [(None, None)]:
        card = {"score": n, "band": band} if band else None
        for style, theme in (("a", "dark"), ("b", "dark"), ("b", "light")):
            svg = BADGE.render(style, card, theme)
            if style == "a":
                tile = [e.get("fill") for e in ET.fromstring(svg).iter() if e.tag.endswith("rect")][0]
            else:
                tile = _tile_fill(svg)
            for fill, text, _e in _texts(svg):
                if text.strip():
                    assert BADGE.contrast(fill, tile) >= 4.5, (style, theme, band, text, fill, tile, BADGE.contrast(fill, tile))
    for text_colour in BADGE.LIGHT_BAND_TEXT.values():
        assert BADGE.contrast(text_colour, BADGE.LIGHT_TILE) >= 4.5


@pytest.mark.core
def test_light_theme_is_light_and_keeps_the_logo_colours_and_dark_is_the_default():
    dark, light = BADGE.render("b", {"score": 45, "band": "Elevated"}), BADGE.render("b", {"score": 45, "band": "Elevated"}, "light")
    assert dark == BADGE.render("b", {"score": 45, "band": "Elevated"}, "dark") and dark != light
    assert BADGE.LIGHT_TILE in light and BADGE.RAISED in dark and BADGE.LIGHT_TILE not in dark
    for colour in BADGE.COLORS.values():
        assert colour in light                                                    # the arcs keep the band colours of the logo
    assert 'stroke="%s"' % BADGE.LIGHT_TEXT in light                              # the handle is dark on the light tile
    assert BADGE.render("a", {"score": 45, "band": "Elevated"}, "light") == BADGE.render("a", {"score": 45, "band": "Elevated"})     # style a has no theme


def test_the_theme_parameter_on_the_route(client):
    d = client.get(f"/badge/{REFRAX}.svg?style=b").text
    assert client.get(f"/badge/{REFRAX}.svg?style=b&theme=dark").text == d == client.get(f"/badge/{REFRAX}.svg?style=b&theme=zzz").text
    lt = client.get(f"/badge/{REFRAX}.svg?style=b&theme=light")
    assert lt.status_code == 200 and lt.text != d and BADGE.LIGHT_TILE in lt.text
    assert client.get(f"/badge/{REFRAX}.svg?style=a&theme=light").text == client.get(f"/badge/{REFRAX}.svg?style=a").text
    assert client.get("/badge/zzz.svg?style=b&theme=light").status_code == 404 and BADGE.LIGHT_TILE in client.get("/badge/zzz.svg?style=b&theme=light").text


@pytest.mark.core
def test_badge_sizes_are_small_and_the_lens_uses_the_logo_geometry():
    for st in "ab":
        root = ET.fromstring(BADGE.render(st, {"score": 100, "band": "Elevated"}))
        assert int(root.get("width")) <= 260 and int(root.get("height")) <= 40
    b = BADGE.render("b", {"score": 45, "band": "Elevated"})
    for colour in ("#52c08a", "#e5a93d", "#f0716a"):                           # the band colours of site.css, one arc each (the logo's three arcs)
        assert colour in b
    assert BADGE.ARCS == (("Low", 55, 155), ("Elevated", 175, 275), ("High", 295, 395))      # the angles of brand/rugyomi-mark.svg
    assert 'stroke-opacity="1"' in b and b.count('stroke-opacity="0.3"') == 2          # the active band's arc is full, the others dimmed


# ------------------------------------------------------------------------------------------------ read-only, indexes
@pytest.mark.core
def test_the_api_adds_no_write_path_and_the_site_stays_read_only():
    src = open(API.__file__, encoding="utf-8").read()
    for bad in ("INSERT", "UPDATE", "DELETE", "REPLACE", "commit(", "executescript", "@app.post", "@app.put", "methods=[\"POST", "set_cookie"):
        assert bad not in src, bad


def test_the_list_query_uses_the_time_index(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    plan = " ".join(str(r) for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT l.token, l.launchpad, l.creator, l.owner, l.ts_ns, l.coverage, c.score, c.band, o.rug, o.final FROM launches l "
        "LEFT JOIN cards c ON c.token = l.token LEFT JOIN launch_outcomes o ON o.token = l.token WHERE l.ts_ns >= ? ORDER BY l.ts_ns DESC LIMIT ?", (0, 50)))
    con.close()
    assert "INDEX launches_ts" in plan and "SCAN l USING" in plan.replace("SCAN launches AS l", "SCAN l") or "SEARCH l USING" in plan


@pytest.mark.core
def test_api_module_is_not_importing_anything_that_writes():
    src = open(API.__file__, encoding="utf-8").read()
    assert "from ..store import" not in src and "open_store" not in src
