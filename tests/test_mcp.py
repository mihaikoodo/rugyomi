"""RGY-068: the read-only MCP server (stdio) over the API.

Pinned: the four tools, their schemas and read-only annotations; every tool is exactly one GET of `/api/v1/...` (the HTTP backend sends GET only and never builds a URL
from an unchecked id); the demo backend answers with the very JSON the API gives for the same URL; a 404, a 400, a dead host and a non-JSON answer give a readable tool error;
the process starts over stdio (`python -m rugyomi.mcp --demo`) and answers a real tool call."""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

from rugyomi_testkit import refrax_world_db  # noqa: E402
from rugyomi.mcp import backend as B  # noqa: E402
from rugyomi.mcp import server as SRV  # noqa: E402
from rugyomi.mcp.__main__ import main as cli_main  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402

ISLAND = "island.nearlytrade.near"
TOOLS = {"get_launch_risk", "recent_launches", "get_account", "get_meta"}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    path = tmp_path_factory.mktemp("mcp") / "m.db"
    refrax_world_db(path)
    return path


def run(coro):
    return asyncio.run(coro)


def call(backend, tool, args=None):
    async def go():
        async with Client(SRV.build(backend)) as c:
            return await c.call_tool(tool, args or {})
    return run(go())


class Site:
    """A local HTTP server that records every request and answers from `routes` {path: (status, body bytes)}."""

    def __init__(self, routes):
        self.routes, self.seen = routes, []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                site.seen.append(("GET", self.path))
                status, body = site.routes.get(self.path.split("?")[0], (404, b'{"api":"v1","error":"not found"}'))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                site.seen.append(("POST", self.path))
                self.send_response(405)
                self.end_headers()

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def site():
    s = Site({"/api/v1/meta": (200, b'{"api":"v1","disclaimer":"Analysis, not advice."}'),
              "/api/v1/launches": (200, b'{"api":"v1","count":0,"limit":3,"launches":[]}'),
              "/api/v1/launch/odd.near": (200, b"<html>not json</html>"),
              "/api/v1/account/busy.near": (500, b"")})
    yield s
    s.close()


@pytest.mark.core
def test_the_four_tools_have_schemas_read_only_hints_and_a_disclaimer():
    async def go():
        async with Client(SRV.build(B.DemoBackend())) as c:
            return (await c.list_tools()).tools
    tools = {t.name: t for t in run(go())}
    assert set(tools) == TOOLS
    assert tools["get_launch_risk"].input_schema["required"] == ["token"] and tools["get_account"].input_schema["required"] == ["account"]
    assert set(tools["recent_launches"].input_schema["properties"]) == {"since", "limit"} and not tools["recent_launches"].input_schema.get("required")
    assert tools["get_meta"].input_schema["properties"] == {}
    for t in tools.values():
        assert t.annotations.read_only_hint is True and t.annotations.destructive_hint is False, t.name
        assert t.description and "Analysis, not advice" in t.description or t.name == "get_meta", t.name
    assert "rug within 24" in tools["get_launch_risk"].description and "0-100" in tools["get_launch_risk"].description
    assert "analysis, not advice" in SRV.INSTRUCTIONS.lower()


@pytest.mark.core
def test_the_http_backend_sends_get_only_to_the_api_paths_and_forwards_the_query(site):
    be = B.HttpBackend(site.url)
    assert call(be, "get_meta").structured_content["disclaimer"] == "Analysis, not advice."
    r = call(be, "recent_launches", {"since": "2026-11-10", "limit": 3})
    assert not r.is_error and r.structured_content["limit"] == 3
    assert site.seen == [("GET", "/api/v1/meta"), ("GET", "/api/v1/launches?since=2026-11-10&limit=3")]


@pytest.mark.core
@pytest.mark.parametrize("bad", ["../etc/passwd", "a/b", "launch?x=1", "x", "", "a" * 70, "tok en.near", "%2e%2e", ".near"])
def test_an_unchecked_id_never_reaches_the_network(site, bad):
    be = B.HttpBackend(site.url)
    for tool, arg in (("get_launch_risk", "token"), ("get_account", "account")):
        r = call(be, tool, {arg: bad})
        assert r.is_error and f"bad {arg}" in r.content[0].text
    assert site.seen == []


@pytest.mark.core
def test_an_id_is_trimmed_and_lowercased_before_it_goes_into_the_path(site):
    call(B.HttpBackend(site.url), "get_account", {"account": "  Busy.NEAR "})
    assert site.seen == [("GET", "/api/v1/account/busy.near")]


@pytest.mark.core
def test_errors_are_readable_and_a_dead_host_points_at_the_demo(site):
    be = B.HttpBackend(site.url)
    nf = call(be, "get_launch_risk", {"token": "ghost.near"})
    assert nf.is_error and "not found" in nf.content[0].text and "404" in nf.content[0].text
    odd = call(be, "get_launch_risk", {"token": "odd.near"})
    assert odd.is_error and "not a JSON object" in odd.content[0].text
    five = call(be, "get_account", {"account": "busy.near"})
    assert five.is_error and "HTTP 500" in five.content[0].text
    dead = call(B.HttpBackend("http://127.0.0.1:9"), "get_meta")
    assert dead.is_error and "could not reach http://127.0.0.1:9" in dead.content[0].text and "--demo" in dead.content[0].text


@pytest.mark.core
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://x", "https://", "https://u:p@host", "https://h/?q=1", "not a url", ""])
def test_the_base_url_must_be_plain_http_or_https(url):
    with pytest.raises(ValueError):
        B.HttpBackend(url)
    assert B.HttpBackend("https://example.org/").describe() == "https://example.org" and B.HttpBackend("http://localhost:8000/x/").describe() == "http://localhost:8000/x"


def test_the_demo_backend_returns_what_the_api_returns_for_the_same_url(db):
    be = B.DemoBackend(str(db))
    with TestClient(webapp.create_app(db)) as api:
        listing = api.get("/api/v1/launches", params={"limit": 5}).json()
        assert call(be, "recent_launches", {"limit": 5}).structured_content == listing and listing["count"] >= 3
        assert call(be, "get_meta").structured_content == api.get("/api/v1/meta").json()
        assert call(be, "get_launch_risk", {"token": ISLAND}).structured_content == api.get(f"/api/v1/launch/{ISLAND}").json()
        creator = api.get(f"/api/v1/launch/{ISLAND}").json()["creator"]
        assert call(be, "get_account", {"account": creator}).structured_content == api.get(f"/api/v1/account/{creator}").json()
        for tool, args in (("get_launch_risk", {"token": "ghost.near"}), ("get_account", {"account": "ghost.near"})):
            assert call(be, tool, args).is_error and "not found" in call(be, tool, args).content[0].text
        assert api.get("/api/v1/launch/ghost.near").status_code == 404
        bad = call(be, "recent_launches", {"limit": 500})
        assert bad.is_error and "bad limit" in bad.content[0].text


def test_the_server_starts_over_stdio_and_answers_a_real_call(db):
    params = StdioServerParameters(command=sys.executable, args=["-I", "-c", f"import sys; sys.path.insert(0, {str(Path(webapp.__file__).parents[2])!r}); "
                                                              f"from rugyomi.mcp.__main__ import main; main(['--demo', '--db', {str(db)!r}])"])

    async def go():
        async with Client(params) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            r = await c.call_tool("get_launch_risk", {"token": ISLAND})
            return tools, r, c.server_info
    tools, r, info = run(go())
    assert tools == TOOLS and not r.is_error and r.structured_content["token"] == ISLAND and r.structured_content["api"] == "v1"
    assert info.name == "rugyomi" and "risk" in r.structured_content


@pytest.mark.core
def test_the_cli_refuses_bad_arguments_and_names_the_base_url_order(capsys, monkeypatch):
    with pytest.raises(SystemExit):
        cli_main(["--db", "x.db"])
    with pytest.raises(SystemExit):
        cli_main(["--base-url", "file:///x"])
    from rugyomi.mcp import __main__ as M
    import argparse
    monkeypatch.setenv("RUGYOMI_API_BASE", "http://127.0.0.1:1")
    assert M.make_backend(argparse.Namespace(demo=False, db=None, base_url=None)).describe() == "http://127.0.0.1:1"
    assert M.make_backend(argparse.Namespace(demo=False, db=None, base_url="http://127.0.0.1:2")).describe() == "http://127.0.0.1:2"
    monkeypatch.delenv("RUGYOMI_API_BASE")
    assert M.make_backend(argparse.Namespace(demo=False, db=None, base_url=None)).describe() == "https://mihaikoodo.dev"
    assert isinstance(M.make_backend(argparse.Namespace(demo=True, db=None, base_url=None)), B.DemoBackend)


@pytest.mark.core
def test_the_mcp_package_has_no_write_verb_no_key_and_no_other_host():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (Path(B.__file__).parent).glob("*.py"))
    low = src.lower()
    assert "method=\"get\"" in low and "post" not in low.replace("localhost", "") and "put(" not in low and "delete(" not in low
    assert "api_key" not in low and "authorization" not in low and "token=" not in low.replace("token=token", "")
    import re
    assert set(re.findall(r"https?://[a-z0-9.\-]+", src)) <= {"https://mihaikoodo.dev", "http://127.0.0.1", "https://example.org"} | {u for u in re.findall(r"https?://[a-z0-9.\-]+", src) if u.endswith("modelcontextprotocol.io")}
