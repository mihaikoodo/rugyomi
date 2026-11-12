"""RGY-054 (F-055): the card issued at launch time attaches the creator to the linked group from the data known BEFORE the launch. The link tables are rebuilt after the launch, so a first-time creator funded
by a member of a rugging network had no group on the live card. The tables are never written by the card; an edge after the launch never counts; service senders never link; the card path and the rebuild
read a funding by the same reader."""
import pytest

from test_rules import HOUR, MIN, NEAR, T0, Db
from test_rules_v1 import outcome
from rugyomi import links as K
from rugyomi import rules
from rugyomi.rules import card as R
from rugyomi.rules import config as C

DRAFT = {"signals": {"creator_track_record": {"points_mid": 25, "points_high": 40}, "funded_by_rugger": {"points": 30, "points_multi": 40}}}
V1 = C.effective(DRAFT, "v1")
V2R = C.effective(None, "v2r")
NOW = "new.nearlytrade.near"


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "x.db")
    yield d
    d.s.close()


def network(db):
    """A network of two creators (g1 funded g2 long ago), both with a launch that ended as a rug; the link tables are built now - before the creator of the new launch exists."""
    for i, who in enumerate(("g1.near", "g2.near")):
        t = T0 - (30 - i) * HOUR
        db.launch(f"old{i}.nearlytrade.near", who, t, coverage="launch_only")
        outcome(db, f"old{i}.nearlytrade.near", t, rug=True, decided=("fast", "late"))
    db.fund("g1.near", "g2.near", 41, T0 - 40 * HOUR)
    K.rebuild_links(db.s, C.effective(None, "v1"))
    assert db.s.con.execute("SELECT COUNT(*) FROM link_members").fetchone()[0] == 2


def track(db, cfg=V1, attach=False):
    c = rules.compute_card(db.s, NOW, cfg, attach=attach)
    return next(r for r in c["signals"]["results"] if r["id"] == "creator_track_record"), c


def rebuilt(db, cfg=V1):
    K.rebuild_links(db.s, C.effective(None, "v1"))
    return track(db, cfg)


@pytest.mark.core
def test_a_first_time_creator_funded_by_a_group_member_gets_the_group_at_card_time(db):
    network(db)
    db.fund("g2.near", "new.near", 5, T0 - 5 * MIN)                                       # funded before the launch by a member of the network
    db.launch(NOW, "new.near", T0)                                                        # the launch appears AFTER the last rebuild
    before = track(db)[0]
    assert not before["fired"] and before["value"]["group_size"] == 1                    # the card as issued before this fix: no group
    live, card = track(db, attach=True)
    assert live["fired"] and live["value"]["group_size"] == 3 and live["value"]["group_rugs"] == 2 and live["contribution"] == 40
    assert db.s.con.execute("SELECT COUNT(*) FROM link_edges WHERE src = 'new.near' OR dst = 'new.near'").fetchone()[0] == 0          # the tables are not written by the card
    after, _ = rebuilt(db)                                                                 # the rebuild gives the same group: the live card = the card of "now as of launch time"
    assert after["value"] == live["value"] and after["contribution"] == live["contribution"] and after["reason"] == live["reason"]
    assert [r["id"] for r in card["reasons"]][0] == "creator_track_record"


@pytest.mark.core
def test_an_edge_after_the_launch_never_counts(db):
    network(db)
    db.launch(NOW, "new.near", T0)
    db.fund("g2.near", "new.near", 5, T0 + MIN)                                           # the funding comes after the launch
    assert not track(db, attach=True)[0]["fired"] and track(db, attach=True)[0]["value"]["group_size"] == 1
    assert not rebuilt(db)[0]["fired"]                                                    # and the rebuilt tables agree (their edge is dated after the launch)
    db.s.con.execute("UPDATE facts SET ts_ns = ? WHERE ts_ns = ?", (T0 - MIN, T0 + MIN))
    assert track(db, attach=True)[0]["fired"]


@pytest.mark.core
def test_service_senders_and_small_transfers_do_not_link(db):
    network(db)
    for i, svc in enumerate(("v2.ref-finance.near", "launchbot.nearlytrade.near", "wrap.near")):
        db.launch(f"svc{i}.nearlytrade.near", svc, T0 - (40 + i) * HOUR, coverage="launch_only")      # a service that "launched" a rug: still never a link
        outcome(db, f"svc{i}.nearlytrade.near", T0 - (40 + i) * HOUR, rug=True, decided=("fast", "late"))
        db.fund(svc, "new.near", 50, T0 - 5 * MIN)
    db.fund("g1.near", "new.near", 0.5, T0 - 4 * MIN)                                     # under links.min_near (1 NEAR): no edge in the rebuild either
    db.launch(NOW, "new.near", T0)
    r = track(db, attach=True)[0]
    assert not r["fired"] and r["value"]["group_size"] == 1
    assert rebuilt(db)[0]["value"]["group_size"] == 1


@pytest.mark.core
def test_a_member_whose_newest_funding_came_after_the_last_build_is_attached_too(db):
    network(db)
    db.launch("mine.nearlytrade.near", "mate.near", T0 - 10 * HOUR, coverage="launch_only")
    db.fund("mate.near", "new.near", 9, T0 - 9 * HOUR)
    db.launch(NOW, "new.near", T0)
    K.rebuild_links(db.s, C.effective(None, "v1"))                                        # new.near and mate.near are a group of two in the tables now
    db.fund("g1.near", "new.near", 6, T0 - 3 * MIN)                                       # ... and then a member of the network funds new.near
    r_tables = track(db)[0]
    assert r_tables["value"]["group_size"] == 2 and not r_tables["fired"]
    r_live = track(db, attach=True)[0]
    assert r_live["value"]["group_size"] == 4 and r_live["fired"]
    assert rebuilt(db)[0]["value"] == r_live["value"]


@pytest.mark.core
def test_a_hop_through_a_member_who_joined_after_the_build_is_followed(db):
    network(db)
    db.launch("m.nearlytrade.near", "m.near", T0 - 2 * HOUR, coverage="launch_only")      # joins the network after the last build: not in the tables
    db.fund("g2.near", "m.near", 8, T0 - 3 * HOUR)
    db.fund("m.near", "new.near", 8, T0 - 5 * MIN)
    db.launch(NOW, "new.near", T0)
    assert track(db)[0]["value"]["group_size"] == 1
    r = track(db, attach=True)[0]
    assert r["value"]["group_size"] == 4 and r["value"]["group_rugs"] == 2
    assert rebuilt(db)[0]["value"] == r["value"]


@pytest.mark.core
def test_funded_by_rugger_follows_a_funder_the_tables_do_not_know(db):
    network(db)
    db.launch("f.nearlytrade.near", "f.near", T0 - 2 * HOUR, coverage="launch_only")      # a funder who launched after the last build and is funded by the network
    db.fund("g1.near", "f.near", 7, T0 - 3 * HOUR)
    db.fund("f.near", "new.near", 7, T0 - 5 * MIN)
    db.launch(NOW, "new.near", T0)
    pick = lambda c: next(r for r in c["signals"]["results"] if r["id"] == "funded_by_rugger")      # noqa: E731
    assert not pick(rules.compute_card(db.s, NOW, V1))["fired"]
    live = pick(rules.compute_card(db.s, NOW, V1, attach=True))
    assert live["fired"] and live["value"]["rugging_funders"] == 1
    K.rebuild_links(db.s, C.effective(None, "v1"))
    assert pick(rules.compute_card(db.s, NOW, V1))["value"] == live["value"]


@pytest.mark.core
def test_the_issued_card_uses_the_attachment_and_a_later_score_gives_the_same_card(db):
    network(db)
    db.fund("g2.near", "new.near", 5, T0 - 5 * MIN)
    db.launch(NOW, "new.near", T0, height=77)
    assert R.issue_card(db.s, NOW, 77, V1) is True
    stored = R.card_row(db.s, NOW)
    r = next(x for x in stored["signals"]["results"] if x["id"] == "creator_track_record")
    assert r["fired"] and r["value"]["group_size"] == 3
    K.rebuild_links(db.s, C.effective(None, "v1"))
    later = rules.compute_card(db.s, NOW, V1)
    assert (later["score"], later["band"]) == (stored["score"], stored["band"])
    assert next(x for x in later["signals"]["results"] if x["id"] == "creator_track_record")["value"] == r["value"]


@pytest.mark.core
def test_the_pass_and_the_default_path_read_the_tables_as_they_are(db):
    """A pass over many launches (the features, the retro cards, the backtests) and `score --token` do not attach: the rebuilt tables already hold the edge."""
    network(db)
    db.fund("g2.near", "new.near", 5, T0 - 5 * MIN)
    db.launch(NOW, "new.near", T0)
    assert rules.compute_card(db.s, NOW, V1)["score"] == rules.compute_card(db.s, NOW, V1, attach=False)["score"]
    assert not next(r for r in rules.compute_card(db.s, NOW, V1)["signals"]["results"] if r["id"] == "creator_track_record")["fired"]
    from rugyomi.rules import context as CX
    assert CX.LaunchContext.__init__.__defaults__[-1] is False                            # the context attaches only when asked


@pytest.mark.core
def test_the_card_and_the_rebuild_read_a_funding_by_the_same_reader(db):
    """One copy of the rule: the senders `attach_neighbours` finds are the senders of the `funded` edges the rebuild writes for the same account (and the creation payer)."""
    network(db)
    db.fund("g2.near", "new.near", 5, T0 - 5 * MIN)
    db.fund("g1.near", "new.near", 3, T0 - 2 * MIN)
    db.fund("v2.ref-finance.near", "new.near", 9, T0 - MIN)
    db.launch(NOW, "new.near", T0)
    got = K.attach_neighbours(db.s, "new.near", T0, 1.0)
    K.rebuild_links(db.s, C.effective(None, "v1"))
    written = {r[0] for r in db.s.con.execute("SELECT src FROM link_edges WHERE dst = 'new.near'")}
    assert set(got) == written == {"g1.near", "g2.near"}
    assert K.attach_neighbours(db.s, "new.near", T0 - 3 * MIN, 1.0) == ["g2.near"]        # as of an earlier time: only what was known then


@pytest.mark.core
def test_a_funder_who_never_launched_is_not_a_participant_and_is_not_linked(db):
    """The day replay of 11.11 found it: a wallet that funded the creator and was itself funded by a rugger is no participant (it never launched) - the rebuild gives it no edges, so the card must not
    walk through it either (`funded_by_rugger` asks for the group of every funder, not only of the creator)."""
    network(db)
    db.fund("g1.near", "wallet.near", 7, T0 - 3 * HOUR)                                   # wallet.near never launches
    db.fund("wallet.near", "new.near", 7, T0 - 5 * MIN)
    db.launch(NOW, "new.near", T0)
    pick = lambda c, sid: next(r for r in c["signals"]["results"] if r["id"] == sid)      # noqa: E731
    live = rules.compute_card(db.s, NOW, V1, attach=True)
    assert not pick(live, "funded_by_rugger")["fired"] and not pick(live, "creator_track_record")["fired"]
    K.rebuild_links(db.s, C.effective(None, "v1"))
    ref = rules.compute_card(db.s, NOW, V1)
    assert not pick(ref, "funded_by_rugger")["fired"] and not pick(ref, "creator_track_record")["fired"]
    assert pick(ref, "funded_by_rugger")["value"] == pick(live, "funded_by_rugger")["value"]
    db.launch("w.nearlytrade.near", "wallet.near", T0 - 2 * HOUR, coverage="launch_only")      # once it HAS launched before the card, it is a participant and the network is reached
    live = rules.compute_card(db.s, NOW, V1, attach=True)
    assert pick(live, "funded_by_rugger")["fired"] and pick(live, "creator_track_record")["fired"]
