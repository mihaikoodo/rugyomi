"""RGY-050: the as-of feature store. A profile scored from the stored measures is the card `compute_card` gives; a new profile (other points, bands, a signal on or off) needs no new
measuring; only a changed measurement setting, version or list re-measures (and only that signal); the pass cache gives the very same reads."""
import json

import pytest

from test_rules import MIN, NEAR, T0, Db
from test_signal_measure import DAY, HOUR, PROFILES, world
from rugyomi import rules
from rugyomi.rules import config as C
from rugyomi.rules import features as FT
from rugyomi.rules import notable as NT
from rugyomi.rules import signals as SG
from rugyomi.rules.card import compute_card
from rugyomi.rules.passcache import PassCache


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "f.db")
    yield d
    d.s.close()


def cfgs():
    return {p: C.effective(None, p) for p in PROFILES}


def tokens(db):
    return [t for (t,) in db.s.con.execute("SELECT token FROM launches ORDER BY ts_ns, token")]


def same_card(a, b):
    assert a["score"] == b["score"] and a["band"] == b["band"], (a["token"], a["score"], b["score"])
    assert a["reasons"] == b["reasons"], a["token"]
    assert a["signals"]["results"] == b["signals"]["results"], a["token"]
    assert (a["config_version"], a["config_hash"], a["rules_version"]) == (b["config_version"], b["config_hash"], b["rules_version"])


@pytest.mark.core
def test_the_migration_adds_the_table_and_the_database_still_opens(db):
    assert FT.available(db.s) and db.s.user_version() >= 19
    assert db.s.con.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0] == 0


@pytest.mark.core
def test_a_profile_scored_from_the_store_is_the_card_compute_card_gives(db):
    world(db)
    cf = cfgs()
    r = FT.fill(db.s, list(cf.values()))
    assert r["failed"] == 0 and r["measured"] > 0
    feats = FT.Features(db.s)
    n = 0
    for p, cfg in cf.items():
        cards, missing = FT.score_all(feats, cfg, tokens(db))
        assert not missing, (p, missing)
        for t, c in cards.items():
            same_card(c, compute_card(db.s, t, cfg))
            n += 1
    assert n >= 5 * 8


@pytest.mark.core
def test_the_stored_card_carries_the_launch_time(db):
    world(db)
    FT.fill(db.s, [cfgs()["v1r"]])
    c = FT.score_launch(FT.Features(db.s), "pre0.nearlytrade.near", cfgs()["v1r"])
    assert c["launch_ts_ns"] == compute_card(db.s, "pre0.nearlytrade.near", cfgs()["v1r"])["launch_ts_ns"]


@pytest.mark.core
def test_a_second_fill_measures_nothing_and_a_new_profile_of_the_same_settings_needs_no_measuring(db):
    world(db)
    cf = cfgs()
    FT.fill(db.s, [cf["v1r"]])
    again = FT.fill(db.s, [cf["v1r"]])
    assert again["measured"] == 0 and again["kept"] > 0
    changed = json.loads(json.dumps(cf["v1r"]))                                                   # other points, other tiers, other bands: the score changes, the measure does not
    changed["version"] = "v1r-x"
    changed["bands"] = {"elevated": 10, "high": 20}
    for s in changed["signals"].values():
        for k in list(s):
            if k.startswith("points"):
                s[k] = s[k] + 1
    changed["signals"]["creator_tax"]["min_bps"] = 1
    assert FT.fill(db.s, [changed])["measured"] == 0
    cards, missing = FT.score_all(FT.Features(db.s), changed, tokens(db))
    assert not missing
    for t, c in cards.items():
        same_card(c, compute_card(db.s, t, changed))
    assert any(c["score"] != compute_card(db.s, t, cf["v1r"])["score"] for t, c in cards.items())      # it really is a different profile


@pytest.mark.core
def test_a_signal_switched_off_is_not_scored_and_one_switched_on_is_measured_alone(db):
    world(db)
    cf = cfgs()
    FT.fill(db.s, [cf["v1"]])
    n1 = db.s.con.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    off = json.loads(json.dumps(cf["v1"]))
    off["signals"]["fingerprint"]["enabled"] = False
    cards, missing = FT.score_all(FT.Features(db.s), off, tokens(db))
    assert not missing and all(r["id"] != "fingerprint" for c in cards.values() for r in c["signals"]["results"])
    same_card(cards["pre0.nearlytrade.near"], compute_card(db.s, "pre0.nearlytrade.near", off))
    r = FT.fill(db.s, [cf["v1r"]])                                                                  # v1r = v1 + two expert rules: only those two are new
    assert db.s.con.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0] - n1 == r["measured"]
    assert {s for (s,) in db.s.con.execute("SELECT DISTINCT signal FROM signal_features")} >= {"name_imitation", "fresh_obfuscated_funding"}


@pytest.mark.core
def test_a_changed_measurement_setting_or_list_version_re_measures_only_that_signal(db, monkeypatch):
    world(db)
    cf = cfgs()
    FT.fill(db.s, [cf["v1r"]])
    c2 = json.loads(json.dumps(cf["v1r"]))
    c2["signals"]["fingerprint"]["window_min"] = c2["signals"]["fingerprint"]["window_min"] + 5
    r = FT.fill(db.s, [c2])
    assert r["measured"] == len(tokens(db)) and r["tasks"] == len(FT._tasks([c2]))                  # one signal, every launch
    assert {s for (s,) in db.s.con.execute("SELECT DISTINCT signal FROM signal_features WHERE measure <> '' GROUP BY signal HAVING COUNT(DISTINCT mkey) > 1")} == {"fingerprint"}
    real = NT.notable()
    monkeypatch.setattr(NT, "notable", lambda: {**real, "version": real["version"] + "-new"})
    r = FT.fill(db.s, [cf["v1r"]])
    assert r["measured"] == len(tokens(db))                                                         # name_imitation only
    assert db.s.con.execute("SELECT COUNT(DISTINCT mkey) FROM signal_features WHERE signal = 'name_imitation'").fetchone()[0] == 2


@pytest.mark.core
def test_the_context_settings_are_part_of_the_key(db):
    cf = cfgs()
    a, b = json.loads(json.dumps(cf["v1"])), json.loads(json.dumps(cf["v1"]))
    b["links"]["group_max"] = a["links"]["group_max"] + 1
    assert FT.ctx_key(a) != FT.ctx_key(b)
    assert FT.feature_key(SG.BY_ID["creator_track_record"], a) != FT.feature_key(SG.BY_ID["creator_track_record"], b)
    c = json.loads(json.dumps(cf["v1"]))
    c["rug"]["fast_minute"] = c["rug"]["fast_minute"] + 1
    assert FT.ctx_key(a) != FT.ctx_key(c)


@pytest.mark.core
def test_a_missing_measure_is_reported_not_guessed(db):
    world(db)
    cf = cfgs()["v1r"]
    FT.fill(db.s, [cf], tokens=["pre0.nearlytrade.near"])
    cards, missing = FT.score_all(FT.Features(db.s), cf, tokens(db))
    assert list(cards) == ["pre0.nearlytrade.near"] and len(missing) == len(tokens(db)) - 1


@pytest.mark.core
def test_a_measure_that_fails_is_listed_and_left_without_a_row(db, monkeypatch):
    world(db)
    cf = cfgs()["v1"]
    sig = SG.BY_ID["fingerprint"]

    def boom(ctx, c):
        if ctx.token == "pre0.nearlytrade.near":
            raise ValueError("bad row")
        return sig.measure(ctx, c)
    monkeypatch.setitem(SG.BY_ID, "fingerprint", SG.Signal(sig.id, sig.version, sig.describe, boom, sig.score, sig.keys, sig.deps, sig.measure_version))
    monkeypatch.setattr(FT, "SIGNALS", tuple(SG.BY_ID[s.id] for s in SG.SIGNALS))
    r = FT.fill(db.s, [cf])
    assert r["failed"] == 1 and "pre0.nearlytrade.near fingerprint: ValueError" in r["failed_items"][0]
    assert db.s.con.execute("SELECT COUNT(*) FROM signal_features WHERE token = 'pre0.nearlytrade.near' AND signal = 'fingerprint'").fetchone()[0] == 0


@pytest.mark.core
def test_the_pass_cache_gives_the_same_reads_and_the_same_cards(db):
    world(db)
    cache = PassCache(db.s)
    for p, cfg in cfgs().items():
        for t in tokens(db):
            same_card(compute_card(db.s, t, cfg, cache), compute_card(db.s, t, cfg))
    st = cache.stats
    assert st["creator_hits"] > 0 and st["outcome_hits"] + st["outcome_misses"] > 0 and st["launch_windows"] > 0
    ctx_a = rules.context.LaunchContext(db.s, rules.context.load_launch(db.s, "copy-2.nearlytrade.near"), cfgs()["v2"])
    ctx_b = rules.context.LaunchContext(db.s, rules.context.load_launch(db.s, "copy-2.nearlytrade.near"), cfgs()["v2"], cache)
    for w in (HOUR, 24 * HOUR, 7 * DAY, 60 * DAY):
        assert ctx_a.launches_window(w) == ctx_b.launches_window(w)
    for acct in ("dev.near", "rugger.near", "someone.near", "nobody.near"):
        assert ctx_a.prior_launches(acct) == ctx_b.prior_launches(acct)
        assert ctx_a.group_launches(acct, 3 * DAY) == ctx_b.group_launches(acct, 3 * DAY)


@pytest.mark.core
def test_the_cache_serves_the_window_with_the_index_rows_like_the_database(db):
    world(db)
    db.s.con.execute("INSERT INTO launch_index(token, launchpad, creator, create_ts_ns, source, imported_at) VALUES('idx.nearlytrade.near','nearly','dev.near',?, 't', 'x')", (T0 - 30 * MIN,))
    db.s.con.execute("INSERT INTO launch_index(token, launchpad, creator, create_ts_ns, source, imported_at) VALUES('pre0.nearlytrade.near','nearly','dev.near',?, 't', 'x')", (T0 - 30 * MIN,))      # also in `launches`, other time
    from rugyomi import history
    cache = PassCache(db.s)
    for lo, hi in ((T0 - 3 * HOUR, T0 + HOUR), (0, 2 * T0), (T0 - 29 * MIN, T0 - 20 * MIN), (T0 - 31 * MIN, T0 - 29 * MIN)):
        assert cache.launches_between(lo, hi) == history.launches_between(db.s, lo, hi)


@pytest.mark.core
def test_a_stored_value_does_not_move_when_later_facts_arrive(db):
    tok = world(db)
    cf = cfgs()["v1r"]
    FT.fill(db.s, [cf])
    before = {(s, t): m for s, t, m in db.s.con.execute("SELECT signal, token, measure FROM signal_features")}
    db.fund("rugger.near", "dev.near", 50, T0 + 5 * MIN)                                           # after every launch above
    db.created("later.near", T0 + 6 * MIN)
    db.launch("later.nearlytrade.near", "dev.near", T0 + 10 * MIN)
    db.s.con.execute("INSERT INTO launch_snapshots(token, minute, at_ts_ns, dev_sold_share_bps, metric_version, computed_at) VALUES('pre0.nearlytrade.near', 1440, ?, 9900, 1, 'x')", (T0 + HOUR,))
    r = FT.fill(db.s, [cf], recompute_all=True)
    assert r["stale_recomputed"] > 0 and r["measured"] >= len(before)
    after = {(s, t): m for s, t, m in db.s.con.execute("SELECT signal, token, measure FROM signal_features")}
    for k, m in before.items():
        if k[1] != "later.nearlytrade.near":
            assert after[k] == m, k                                                                 # earlier launches measure exactly as before
    assert any(k[1] == "later.nearlytrade.near" for k in after) and tok


@pytest.mark.core
def test_the_mark_ignores_what_the_collector_appends_all_the_time_and_moves_with_a_bulk_load(db):
    world(db)
    cf = cfgs()["v1"]
    FT.fill(db.s, [cf])
    st0 = FT.status(db.s, [cf])
    assert all(t["stale"] == 0 for t in st0["tasks"])
    db.launch("another.nearlytrade.near", "dev.near", T0 + 3 * HOUR)                                 # a new launch, new facts: the live stream, not a history load
    db.fund("rugger.near", "dev.near", 5, T0 + 3 * HOUR)
    st1 = FT.status(db.s, [cf])
    assert st1["mark"] == st0["mark"] and all(t["stale"] == 0 for t in st1["tasks"])
    r = FT.fill(db.s, [cf])
    assert r["stale_rows"] == 0 and r["measured"] > 0                                                # only the new launch
    db.s.con.execute("INSERT INTO hist_launches(token, launchpad, creator, height, ts_ns, source, imported_at) VALUES('h.nearlytrade.near','nearly','dev.near',1,?, 't', 'x')", (T0 - 60 * DAY,))
    st2 = FT.status(db.s, [cf])
    assert st2["mark"] != st0["mark"] and all(t["stale"] == t["rows"] for t in st2["tasks"])          # an old-dated row arrived: every stored value may be out of date
    assert FT.fill(db.s, [cf])["measured"] == 0                                                      # not touched until asked
    r = FT.fill(db.s, [cf], recompute_stale=True)
    assert r["stale_rows"] > 0 and r["stale_recomputed"] > 0
    assert all(t["stale"] == 0 for t in FT.status(db.s, [cf])["tasks"])
    n = db.s.con.execute("SELECT COUNT(*) FROM signal_features").fetchone()[0]
    assert FT.fill(db.s, [cf], recompute_all=True)["measured"] == n


@pytest.mark.core
def test_progress_lines_come_once_per_500_launches():
    from rugyomi.progress import ticker
    lines = []
    tick = ticker(lines.append, "features", 1234)
    for i in range(1, 1235):
        tick(i)
    assert lines == ["features 500/1234", "features 1000/1234"]
    lines.clear()
    tick = ticker(lines.append, "retro cards", 4405)
    for done in range(50, 4405, 50):                                                                 # a caller that only checks at its batch boundaries
        tick(done)
    assert lines == [f"retro cards {n}/4405" for n in range(500, 4405, 500)]
    lines.clear()
    tick = ticker(lines.append, "x", 3000)
    tick(1230)                                                                                       # a jump over several multiples logs the last one passed, once
    tick(1240)
    tick(1500)
    assert lines == ["x 1000/3000", "x 1500/3000"]


@pytest.mark.core
def test_a_fill_over_a_few_launches_prints_no_progress_line(db):
    world(db)
    lines = []
    FT.fill(db.s, [cfgs()["v1"]], log=lines.append)
    assert lines == []


# ---------------------------------------------------------------- the consumers read the store (RGY-050 stage 4)

def finals(db, rug_of=lambda i, t: i % 2):
    """Every launch of the world gets a final decided outcome, so the as-of rows cover it."""
    for i, t in enumerate(tokens(db)):
        db.s.con.execute("INSERT OR REPLACE INTO launch_outcomes(token, rug, evidence, config_version, config_hash, computed_at, final) VALUES(?,?,?,?,?,?,1)", (t, rug_of(i, t), "{}", "v", "h", "x"))
    db.s.con.commit()


def strip(rows):
    return [{k: v for k, v in r.items()} for r in rows]


@pytest.mark.core
def test_asof_rows_from_the_store_are_the_rows_computed_without_it(db):
    from rugyomi.rules import backtest as BT
    world(db)
    finals(db)
    cf = cfgs()
    plain = BT.asof_rows(db.s, cf, use_store=False)
    assert plain.from_store == 0 and plain.computed == len(plain) * len(cf) and len(plain) >= 8
    empty = BT.asof_rows(db.s, cf)                                                                  # the table is there but empty: every card is computed
    assert empty.from_store == 0 and strip(empty) == strip(plain)
    FT.fill(db.s, list(cf.values()))
    fast = BT.asof_rows(db.s, cf)
    assert fast.from_store == len(plain) * len(cf) and fast.computed == 0
    assert strip(fast) == strip(plain)                                                              # score, band, fired, values, reasons: the same rows


@pytest.mark.core
def test_asof_rows_compute_the_launches_without_a_measure_and_ignore_stale_rows(db):
    from rugyomi.rules import backtest as BT
    world(db)
    finals(db)
    cf = cfgs()
    FT.fill(db.s, [cf["v1r"]], tokens=tokens(db)[:3])                                               # three launches measured, the rest not
    got = BT.asof_rows(db.s, {"v1r": cf["v1r"]})
    assert got.from_store == 3 and got.computed == len(got) - 3
    assert strip(got) == strip(BT.asof_rows(db.s, {"v1r": cf["v1r"]}, use_store=False))
    FT.fill(db.s, [cf["v1r"]])
    db.s.con.execute("UPDATE signal_features SET mark = 'old'")                                     # a store measured under an older mark is still used, and counted
    old = BT.asof_rows(db.s, {"v1r": cf["v1r"]})
    assert old.from_store == len(old) and old.computed == 0 and old.stale_rows > 0 and strip(old) == strip(got)
    fresh = BT.asof_rows(db.s, {"v1r": cf["v1r"]}, fresh_only=True)                                 # unless the caller asks for fresh rows only
    assert fresh.from_store == 0 and fresh.computed == len(fresh) and strip(fresh) == strip(got)


@pytest.mark.core
def test_asof_rows_work_on_a_database_without_the_table(db):
    from rugyomi.rules import backtest as BT
    world(db)
    finals(db)
    db.s.con.execute("DROP TABLE signal_features")
    got = BT.asof_rows(db.s, {"v1r": cfgs()["v1r"]})
    assert got.from_store == 0 and len(got) >= 8


@pytest.mark.core
def test_a_card_from_the_store_is_the_whole_card_compute_card_gives(db):
    world(db)
    cf = cfgs()
    FT.fill(db.s, list(cf.values()))
    feats, cache = FT.Features(db.s), PassCache(db.s)
    n = 0
    for p, cfg in cf.items():
        plan = FT.profile_plan(cfg)
        for t in tokens(db):
            a, b = FT.card_from_store(db.s, feats, t, cfg, plan, cache), compute_card(db.s, t, cfg)
            assert {**a, "computed_ms": 0} == {**b, "computed_ms": 0}, (p, t)                         # results, notes, short notes, dev, gaps, height, coverage: all of it
            n += 1
    assert n >= 5 * 8
    assert FT.card_from_store(db.s, feats, "nobody.nearlytrade.near", cf["v1"]) is None


@pytest.mark.core
def test_the_notes_follow_the_database_now_not_the_stored_measure(db):
    world(db)
    cf = cfgs()["v1r"]
    FT.fill(db.s, [cf])
    feats, t = FT.Features(db.s), "pre0.nearlytrade.near"
    before = FT.card_from_store(db.s, feats, t, cf)
    db.s.con.execute("INSERT OR REPLACE INTO backfill_queue(kind, key, status, ts_ns, queued_at) VALUES('transfers_in', ?, 'done', 1, 'x')", (before["dev_account"] or "dev.near",))
    db.s.con.commit()
    after = FT.card_from_store(db.s, feats, t, cf)
    assert after["history_gap"] != before["history_gap"] or not before["history_gap"]
    assert after["signals"]["notes"] == compute_card(db.s, t, cf)["signals"]["notes"]


@pytest.mark.core
def test_the_draft_profiles_the_studies_score_are_filled_by_name_and_read_from_the_store(db):
    """walk_forward / compare / fit score `fit.V2_DRAFT` profiles, which switch on signals the shipped profile has off: those need their own measures."""
    from rugyomi.rules import backtest as BT
    from rugyomi.rules import fit as FIT
    world(db)
    finals(db)
    shipped = [FT.profile_config(p) for p in ("v0", "v1", "v2", "v1r", "v2r")]
    FT.fill(db.s, shipped)
    profiles = {"v1": C.effective(FIT.V2_DRAFT, "v1"), "v2": C.effective(FIT.V2_DRAFT, "v2")}
    assert BT.asof_rows(db.s, profiles).computed > 0                                               # the draft turns signals on that the shipped fit left off
    FT.fill(db.s, [FT.profile_config(n) for n in ("v1+draft", "v2+draft", "v1r+draft", "v2r+v2rdraft")])
    got = BT.asof_rows(db.s, profiles)
    assert got.computed == 0 and got.from_store == len(got) * 2
    assert [dict(r) for r in got] == [dict(r) for r in BT.asof_rows(db.s, profiles, use_store=False)]
    assert FT.profile_config("v2r+v2rdraft")["version"] == C.effective(FIT.v2r_draft(), "v2r")["version"]
    with pytest.raises(ValueError):
        FT.profile_config("v1+nonsense")
