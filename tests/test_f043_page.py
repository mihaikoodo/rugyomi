"""RGY-049 / F-043: a fired signal with 0 points shows no reason on the launch page (the real REFRAX card with one reason changed to 0 points)."""
import json
import shutil
import sqlite3

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from rugyomi.web import app as webapp  # noqa: E402
from test_web import REFRAX, db  # noqa: E402,F401


def test_the_launch_page_skips_a_zero_point_reason(db, tmp_path):
    p = tmp_path / "z.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    (reasons,) = con.execute("SELECT reasons FROM cards WHERE token = ?", (REFRAX,)).fetchone()
    rs = json.loads(reasons)
    assert len(rs) >= 2
    rs[0].update(contribution=0, reason="ZEROREASONTEXT", short="ZEROSHORT", evidence=[])
    keep = rs[1]["reason"]
    con.execute("DROP TRIGGER cards_no_update")
    con.execute("UPDATE cards SET reasons = ? WHERE token = ?", (json.dumps(rs), REFRAX))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        t = c.get(f"/launch/{REFRAX}").text
    assert "ZEROREASONTEXT" not in t and "<strong>+0</strong>" not in t
    assert keep.split(".")[0][:30] in t.replace("&#39;", "'")


def test_a_card_whose_every_reason_has_zero_points_says_no_signal_fired(db, tmp_path):
    p = tmp_path / "z2.db"
    shutil.copy(db, p)
    con = sqlite3.connect(p)
    (reasons,) = con.execute("SELECT reasons FROM cards WHERE token = ?", (REFRAX,)).fetchone()
    rs = [{**r, "contribution": 0, "reason": "ZEROREASONTEXT"} for r in json.loads(reasons)]
    con.execute("DROP TRIGGER cards_no_update")
    con.execute("UPDATE cards SET reasons = ? WHERE token = ?", (json.dumps(rs), REFRAX))
    con.commit()
    con.close()
    with TestClient(webapp.create_app(p)) as c:
        t = " ".join(c.get(f"/launch/{REFRAX}").text.split())
    assert "ZEROREASONTEXT" not in t and "No risk signal fired for this launch." in t
