"""RGY-050: every signal is a MEASURE (reads the database as of the launch, returns a plain JSON dict) and a SCORE (points, reason, evidence; no database). The live card is
score(measure(...)); a stored measure scores the same. The measure may read only the config keys it declares, so a stored value is tied to exactly the settings it depends on."""
import inspect
import json

import pytest

from test_rules import MIN, NEAR, T0, Db
from test_rules_v1 import api_transfer, outcome as outcome_snaps
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import notable as NT
from rugyomi.rules import signals as SG
from rugyomi.rules.context import LaunchContext, load_launch

HOUR = 60 * MIN
DAY = 24 * HOUR
PROFILES = ("v0", "v1", "v2", "v1r", "v2r")


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "m.db")
    yield d
    d.s.close()


def world(db):
    """A creator with a short history, a funder who has a rug behind him, a name used before - enough for most signals to fire under some profile."""
    dev, rugger = "dev.near", "rugger.near"
    db.created(dev, T0 - 5 * MIN)
    db.fund(rugger, dev, 30, T0 - 3 * MIN)
    db.created(rugger, T0 - 100 * HOUR)
    db.launch("old.nearlytrade.near", rugger, T0 - 40 * DAY, coverage="launch_only")
    outcome_snaps(db, "old.nearlytrade.near", T0 - 40 * DAY, rug=True)
    api_transfer(db, dev, rugger, T0 - 30 * DAY)                                              # a direct transfer from a funder who has a rug behind him
    for i in range(3):
        db.launch(f"pre{i}.nearlytrade.near", dev, T0 - (i + 2) * HOUR)
        db.snap(f"pre{i}.nearlytrade.near", 5, 9000, T0 - (i + 2) * HOUR)
    db.launch("rugged.nearlytrade.near", dev, T0 - 20 * DAY, coverage="launch_only")
    outcome_snaps(db, "rugged.nearlytrade.near", T0 - 20 * DAY, rug=True)
    db.launch("copy.nearlytrade.near", "someone.near", T0 - HOUR)
    db.launch("copy.v2-launchpad.justhoot.near", dev, T0, launchpad="justhoot_v2")
    db.launch("copy-2.nearlytrade.near", dev, T0 + 1, dev_buy=20 * NEAR)
    return "copy-2.nearlytrade.near"


def ctx_for(db, token, cfg):
    return LaunchContext(db.s, load_launch(db.s, token), cfg)


@pytest.mark.core
def test_every_signal_has_a_measure_a_score_and_declared_keys():
    cfgs = [C.effective(None, p) for p in PROFILES]
    for sig in SG.SIGNALS:
        assert callable(sig.measure) and callable(sig.score)
        assert list(inspect.signature(sig.score).parameters) == ["m", "c"]                      # the score sees the measure and the settings, never the database
        seen = {k for cfg in cfgs for k in cfg["signals"].get(sig.id, {})}
        assert set(sig.keys) <= seen, (sig.id, set(sig.keys) - seen)                           # a typo in `keys` would silently freeze the stored value


@pytest.mark.core
def test_a_measure_cannot_read_a_key_it_did_not_declare():
    d = SG._Declared({"a": 1, "b": 2}, ("a",), "x")
    assert d["a"] == 1 and d.get("a") == 1 and d.get("zzz" if False else "a", 5) == 1
    with pytest.raises(SG.UndeclaredConfigKey):
        d["b"]
    with pytest.raises(SG.UndeclaredConfigKey):
        d.get("b")
    assert SG._Declared({}, ("a",), "x").get("a", 7) == 7                                      # declared but absent: the default, as for a plain dict


@pytest.mark.core
def test_measures_are_json_stable_and_scores_do_not_change_them(db):
    tok = world(db)
    for p in PROFILES:
        cfg = C.effective(None, p)
        ctx = ctx_for(db, tok, cfg)
        for sig in SG.SIGNALS:
            c = cfg["signals"].get(sig.id)
            if c is None or not c.get("enabled", True):
                continue
            m = sig.measured(ctx, c)
            assert json.loads(json.dumps(m)) == m, (p, sig.id)                                  # what the store holds is what the score sees
            before = json.dumps(m, sort_keys=True)
            r1, r2 = sig.score(m, c), sig.score(json.loads(before), c)
            assert r1 == r2 == sig.compute(ctx, c), (p, sig.id)
            assert json.dumps(m, sort_keys=True) == before, (p, sig.id)                         # a score never edits the measure


@pytest.mark.core
def test_something_fires_so_the_checks_above_are_not_vacuous(db):
    tok = world(db)
    fired = set()
    for p in PROFILES:
        card = rules.compute_card(db.s, tok, C.effective(None, p))
        fired |= {r["id"] for r in card["reasons"]}
    assert {"fingerprint", "serial_launcher", "name_copy", "funded_by_rugger"} <= fired, fired


@pytest.mark.core
def test_the_measure_key_follows_the_declared_settings_only(db):
    cfg = C.effective(None, "v1r")
    sig = SG.BY_ID["fingerprint"]
    c = dict(cfg["signals"]["fingerprint"])
    k0 = sig.measure_key(c)
    assert sig.measure_key({**c, "points_3_of_3": 99, "points_2_of_3": 1, "dev_buy_share": 0.9}) == k0       # points and the share are for the score
    assert sig.measure_key({**c, "window_min": c["window_min"] + 1}) != k0 and sig.measure_key({**c, "min_funding_near": 7}) != k0


@pytest.mark.core
def test_the_measure_key_follows_the_published_lists(db, monkeypatch):
    sig = SG.BY_ID["name_imitation"]
    c = C.effective(None, "v1r")["signals"]["name_imitation"]
    k0 = sig.measure_key(c)
    real = NT.notable()
    monkeypatch.setattr(NT, "notable", lambda: {**real, "version": real["version"] + "-x"})
    assert sig.measure_key(c) != k0 and sig.measure_key({**c, "points": 99}) != k0                 # same settings, new list: a new key (the points change nothing)
    assert sig.measure_key({**c, "points": 1}) == sig.measure_key({**c, "points": 99})


@pytest.mark.core
def test_the_measure_key_includes_the_signal_and_measure_versions():
    sig = SG.BY_ID["fingerprint"]
    c = C.effective(None, "v1")["signals"]["fingerprint"]
    bumped = SG.Signal(sig.id, sig.version, sig.describe, sig.measure, sig.score, sig.keys, sig.deps, sig.measure_version + 1)
    assert bumped.measure_key(c) != sig.measure_key(c)
