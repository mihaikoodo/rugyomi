"""RGY-048: the demo database: `python -m rugyomi.web --demo` unpacks the shipped .gz once (named by its hash) and serves it; a demo database says so on
every page; the packaged data files cover every JSON the code reads; a real database shows no demo note."""
import gzip
import json
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi_testkit import refrax_world_db  # noqa: E402
from rugyomi.web import __main__ as web_main  # noqa: E402
from rugyomi.web import app as webapp  # noqa: E402
from rugyomi.web import demo as demo_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    p = tmp_path_factory.mktemp("demo") / "w.db"
    refrax_world_db(p)
    return p


def _gz(tmp_path, data: bytes) -> Path:
    out = tmp_path / "demo.db.gz"
    out.write_bytes(gzip.compress(data, mtime=0))
    return out


@pytest.mark.core
def test_unpack_is_named_by_the_hash_and_done_once(tmp_path):
    gz, cache = _gz(tmp_path, b"abc" * 1000), tmp_path / "cache"
    a = demo_mod.unpack(gz, cache)
    assert a.read_bytes() == b"abc" * 1000 and a.parent == cache and a.name.startswith("rugyomi_demo-")
    mtime = a.stat().st_mtime_ns
    assert demo_mod.unpack(gz, cache) == a and a.stat().st_mtime_ns == mtime                    # not unpacked again
    other = demo_mod.unpack(_gz(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, b"different"), cache)
    assert other != a and not list(cache.glob("*.tmp*"))                                         # another file, another name; no leftovers


@pytest.mark.core
def test_a_missing_demo_file_says_where_it_looked(tmp_path):
    with pytest.raises(FileNotFoundError, match="no demo database"):
        demo_mod.unpack(tmp_path / "none.gz", tmp_path)


@pytest.mark.core
def test_main_demo_serves_the_unpacked_file(tmp_path, monkeypatch, world):
    gz = _gz(tmp_path, world.read_bytes())
    monkeypatch.setattr(demo_mod, "DEMO_GZ", gz)
    monkeypatch.setattr(demo_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    seen = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(host=kw["host"], app=app))
    assert web_main.main(["--demo"]) == 0
    assert seen["host"] == "127.0.0.1" and seen["app"].state.pool.path.parent == tmp_path / "rugyomi-demo"
    seen.clear()
    assert web_main.main(["--db", str(gz)]) == 0 and seen["app"].state.pool.path.name.startswith("rugyomi_demo-")      # a .gz given as --db is unpacked too


def test_a_demo_database_says_so_on_every_page_and_a_real_one_does_not(world, tmp_path):
    import shutil
    from rugyomi.store import open_store
    real = TestClient(webapp.create_app(world))
    with real as c:
        assert "Demo subset" not in c.get("/").text and "demo-note" not in c.get("/methodology").text
        assert "Not in the demo subset" not in c.get("/launch/nothing.near").text
    demo = tmp_path / "d.db"
    shutil.copyfile(world, demo)
    s = open_store(demo)
    s.con.execute("INSERT OR REPLACE INTO collector_status(key, value, updated_at) VALUES('demo', ?, 'x')",
                  (json.dumps({"built": "12 Nov 2026", "launches": 321, "total": 9876, "rule": "Chosen <b>x</b>."}),))
    s.con.commit()
    s.close()
    with TestClient(webapp.create_app(demo)) as c:
        for path in ("/", "/methodology", "/accuracy", "/rankings"):
            t = c.get(path).text
            assert "Demo subset." in t and "321 of the 9876 launches" in t and "12 Nov 2026" in t, path
        r404 = c.get("/launch/not-in-the-demo.near")
        assert r404.status_code == 404 and "Not in the demo subset." in r404.text and "321 of 9876 launches" in r404.text
        assert "<b>x</b>" not in c.get("/").text and "&lt;b&gt;x&lt;/b&gt;" in c.get("/").text                    # autoescaped


def test_the_site_starts_without_a_database(tmp_path):
    with TestClient(webapp.create_app(tmp_path / "missing.db")) as c:
        assert c.get("/healthz").status_code == 503


@pytest.mark.core
def test_every_data_file_the_code_reads_is_packaged():
    """pip install (not -e) copies only what package-data names: a JSON the code reads must match a pattern."""
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
    data = cfg["tool"]["setuptools"]["package-data"]
    base = ROOT / "rugyomi"
    checked = 0
    for init in sorted(base.rglob("__init__.py")):
        d = init.parent
        pkg = ".".join(d.relative_to(ROOT).parts)
        for f in sorted(p for p in d.rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
                        and next(a for a in p.parents if (a / "__init__.py").exists()) == d):         # the nearest package owns the file
            rel = f.relative_to(d).as_posix()
            assert any(fnmatch(rel, pat) for pat in data.get(pkg, [])), f"{pkg}: {rel} is not in package-data"
            checked += 1
    assert checked > 10


def test_a_clean_install_carries_every_data_file_and_reads_every_profile(tmp_path):
    """F-057: `pip install` (not -e) into an empty directory, then load every rules profile from there - not from the checkout."""
    import shutil
    import subprocess
    import sys
    pytest.importorskip("setuptools")
    src = tmp_path / "src"
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns("tests", "*.egg-info", "__pycache__", "build", "demo", "*.pyc"))
    target, elsewhere = tmp_path / "site", tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation", "--no-index", "--target", str(target), str(src)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    code = (f"import sys; sys.path.insert(0, {str(target)!r})\n"
            "import json, pathlib, rugyomi\n"
            f"assert pathlib.Path(rugyomi.__file__).resolve().is_relative_to(pathlib.Path({str(target)!r}).resolve()), rugyomi.__file__\n"
            "from rugyomi.rules.config import PROFILES, default_config\n"
            "names = sorted(PROFILES)\n"
            "for n in names:\n"
            "    assert default_config(n), n\n"
            "for f in (pathlib.Path(rugyomi.__file__).parent / 'rules').glob('*.json'):\n"
            "    json.loads(f.read_text(encoding='utf-8'))\n"
            "json.loads((pathlib.Path(rugyomi.__file__).parent / 'publisher.json').read_text(encoding='utf-8'))\n"
            "print('profiles', ','.join(names))\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], cwd=elsewhere, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "profiles v0,v1,v1r,v2,v2l,v2r" in out.stdout
    for sub in ("rules", "web/templates", "web/static"):                                   # every non-code file of the checkout is in the installed package
        want = {p.relative_to(ROOT / "rugyomi").as_posix() for p in (ROOT / "rugyomi" / sub).rglob("*") if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts}
        have = {p.relative_to(target / "rugyomi").as_posix() for p in (target / "rugyomi" / sub).rglob("*") if p.is_file()}
        assert want <= have, (sub, sorted(want - have))
