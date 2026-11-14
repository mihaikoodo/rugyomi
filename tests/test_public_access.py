"""RGY-042: the site is published through a Cloudflare Tunnel. The server listens on 127.0.0.1 only; the tunnel (cloudflared) connects
from this machine, so the visitor's address arrives in `CF-Connecting-IP` and is trusted ONLY from a loopback peer; every page answers
GET and HEAD and nothing else; /healthz says nothing about the database."""
import http.client
import ipaddress
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi import Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi_testkit import refrax_world_db  # noqa: E402
from rugyomi.web import __main__ as web_main  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402

REFRAX = "refrax.nearlytrade.near"
V6 = "2001:db8::7"                                       # documentation addresses: IPv4 literals are not allowed in the public tree
V4 = str(ipaddress.IPv4Address(0xC6336407))
OTHER_V6 = "2001:db8::9"


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("access") / "a.db"
    refrax_world_db(path)
    return path


@pytest.fixture(scope="module")
def app(db):
    a = webapp.create_app(db)

    @a.get("/_who")                                      # a probe route, only in the tests
    def who(request: Request):
        return {"ip": request.state.client_ip}

    return a


def who(app, peer, **headers):
    with TestClient(app, client=(peer, 50000)) as c:
        return c.get("/_who", headers=headers).json()["ip"]


@pytest.mark.core
@pytest.mark.parametrize("peer,header,expected", [
    ("127.0.0.1", V6, V6),                               # cloudflared on this machine: the header names the visitor
    ("127.0.0.1", V4, V4),
    ("127.0.0.1", " 2001:DB8::7 ", V6),                  # normalised
    ("::1", V6, V6),
    ("::ffff:127.0.0.1", V6, V6),                        # a loopback peer written as an IPv4-mapped address
    ("127.0.0.1", None, "127.0.0.1"),                    # no header: the peer itself
    ("127.0.0.1", "", "127.0.0.1"),
    ("127.0.0.1", "not-an-ip", "127.0.0.1"),             # not an address: ignored
    ("127.0.0.1", f"{V6}, {OTHER_V6}", "127.0.0.1"),     # a list is not a single address: ignored
    ("127.0.0.1", V6 + "\n", V6),
    (OTHER_V6, V6, OTHER_V6),                            # any other peer: a forged header is ignored
    (V4, V6, V4),
    ("testclient", V6, "testclient"),                    # not even an IP address
    (None, V6, "-"),
])
def test_cf_connecting_ip_is_trusted_only_from_a_loopback_peer(peer, header, expected):
    assert webapp.client_ip(peer, header) == expected


@pytest.mark.core
def test_the_rule_is_applied_to_every_request(app):
    assert who(app, "127.0.0.1", **{"CF-Connecting-IP": V6}) == V6
    assert who(app, OTHER_V6, **{"CF-Connecting-IP": V6}) == OTHER_V6
    assert who(app, V4, **{"CF-Connecting-IP": V6}) == V4


@pytest.mark.core
def test_x_forwarded_for_is_never_used(app):
    assert who(app, "127.0.0.1", **{"X-Forwarded-For": V6}) == "127.0.0.1"
    assert who(app, OTHER_V6, **{"X-Forwarded-For": V6, "X-Real-IP": V6, "Forwarded": f"for={V6}"}) == OTHER_V6
    assert who(app, "127.0.0.1", **{"X-Forwarded-For": OTHER_V6, "CF-Connecting-IP": V6}) == V6


@pytest.mark.core
def test_uvicorn_does_not_rewrite_the_peer_from_proxy_headers_and_binds_locally(db, monkeypatch):
    import uvicorn
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    assert web_main.main(["--db", str(db)]) == 0
    assert seen["host"] == "127.0.0.1" and seen["proxy_headers"] is False and seen["server_header"] is False


@pytest.mark.core
@pytest.mark.parametrize("path", ["/", f"/launch/{REFRAX}", "/creator/thecoins.near", "/methodology", "/accuracy", "/rankings",
                                  "/healthz", "/ledger/head", "/ledger.jsonl", "/ledger/outcomes.jsonl", "/partials/feed",
                                  "/static/site.css", "/nope"])
def test_head_is_allowed_wherever_get_is(db, path):
    with TestClient(webapp.create_app(db)) as c:
        g, h = c.get(path), c.head(path)
        assert h.status_code == g.status_code and h.content == b""
        assert h.headers["content-type"] == g.headers["content-type"]
        assert "content-security-policy" in h.headers and h.headers["x-content-type-options"] == "nosniff"
        assert "server" not in {k.lower() for k in h.headers}


@pytest.mark.core
@pytest.mark.parametrize("method", ["post", "put", "delete", "patch", "options"])
def test_nothing_but_get_and_head_exists(db, method):
    with TestClient(webapp.create_app(db)) as c:
        for path in ("/", "/healthz", f"/launch/{REFRAX}", "/ledger.jsonl"):
            assert getattr(c, method)(path).status_code in (404, 405)


@pytest.mark.core
def test_healthz_says_nothing_about_the_database(db):
    with TestClient(webapp.create_app(db)) as c:
        r = c.get("/healthz")
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert "height" not in r.text and "schema" not in r.text and "block" not in r.text


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_real_server_answers_head_without_a_body_and_listens_on_loopback_only(db):
    port = free_port()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.Popen([sys.executable, "-m", "rugyomi.web", "--db", str(db), "--port", str(port)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=Path(__file__).resolve().parents[1])
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("the server did not start")
        for path in ("/", "/healthz", "/methodology"):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", path)
            g = c.getresponse()
            body = g.read()
            c.request("HEAD", path)
            h = c.getresponse()
            assert h.status == g.status == 200 and h.read() == b""
            assert h.getheader("content-type") == g.getheader("content-type")
            assert len(body) > 0
            c.close()
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/")
        assert c.getresponse().status == 405
        c.close()
        if shutil.which("ss"):                                     # `ss` is not everywhere; when it is, the bind must be loopback
            listening = subprocess.run(["ss", "-ltnH", f"sport = :{port}"], capture_output=True, text=True).stdout
            assert listening.strip() and all(line.split()[3].startswith("127.0.0.1:") for line in listening.splitlines()), listening
    finally:
        p.terminate()
        p.wait(timeout=10)
