from dataclasses import dataclass, field
from datetime import datetime

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league.events import Processor, SeasonMissing, sync
from league.fantrax_data import EASTERN, EndState, Move
from league.models import (
    AuditEntry,
    Buyout,
    Contract,
    FantraxEvent,
    FantraxLeague,
    FarmPick,
    FarmPlayer,
    Player,
    Season,
    SigningPeriod,
    Team,
)

pytestmark = pytest.mark.django_db


def test_import_seeds_the_2026_league():
    call_command("import_league", verbosity=0)
    league = FantraxLeague.objects.get()
    assert (league.league_id, league.season, league.active) == ("p3z8zy75mgdm460o", 2026, True)
    assert league.process_since == datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN)


def test_resolve_needs_a_note_and_happens_once():
    league = FantraxLeague.objects.create(league_id="x", season=2026)
    e = FantraxEvent.objects.create(
        key="k", league=league, happened_at="2026-07-01T00:00Z", kind="CASH_COMMENT", effect="EXCEPTION", detail="d"
    )
    user = User.objects.create(username="c")
    with pytest.raises(ValueError):
        e.resolve(user, "  ")
    e.resolve(user, "entered it")
    assert not FantraxEvent.objects.unresolved().exists()
    assert AuditEntry.objects.get(action="Resolved Fantrax exception").note == "entered it"
    with pytest.raises(ValueError):
        e.resolve(user, "again")


# --- applying moves --------------------------------------------------------


@pytest.fixture
def world():
    league = FantraxLeague.objects.create(
        league_id="L", season=2026, process_since=datetime(2026, 3, 1, tzinfo=EASTERN)
    )
    a = Team.objects.create(code="AA", name="A", fantrax_id="ta")
    b = Team.objects.create(code="BB", name="B", fantrax_id="tb")
    p = Player.objects.create(name="Pat", fantrax_id="p1")
    q = Player.objects.create(name="Quinn", fantrax_id="p2")
    c = Contract.objects.create(player=p, team=a, original_price=10, year_signed=2024, length=4)  # final 2028
    f = FarmPlayer.objects.create(team=a, player=q, drafted_year=2025, salary=1, salary_season=2026)
    return league, Processor(league, {"ta": a, "tb": b}, {"p1": "Pat", "p2": "Quinn"}), a, b, c, f


def move(kind, fid, frm=None, to=None, tx="t1", day=10):
    return Move(datetime(2026, 7, day, tzinfo=EASTERN), kind, fid, frm, to, tx)


def test_trade_moves_the_contract(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("TRADE", "p1", "ta", "tb"))
    c.refresh_from_db()
    assert (c.team, e.effect, e.contract) == (b, "CONTRACT_MOVED", c)
    assert e.detail == "traded Pat AA → BB"


def test_same_move_twice_is_skipped(world):
    league, proc, *_ = world
    proc.apply_move(move("TRADE", "p1", "ta", "tb"))
    assert proc.apply_move(move("TRADE", "p1", "ta", "tb")) is None


def test_trade_into_the_holder_is_already_reflected(world):
    league, proc, *_ = world
    assert proc.apply_move(move("TRADE", "p1", "tb", "ta")).effect == "ALREADY_REFLECTED"


def test_drop_before_final_year_creates_buyout(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("DROP", "p1", "ta"))
    buyout = Buyout.objects.get(contract=c)
    assert (buyout.team, buyout.dropped_in_season, e.effect, e.buyout) == (a, 2026, "BUYOUT", buyout)


def test_drop_in_final_year_voids(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(length=2)  # final 2026
    e = proc.apply_move(move("DROP", "p1", "ta"))
    c.refresh_from_db()
    assert (c.voided_in_season, e.effect) == (2026, "VOIDED")


def test_moves_after_a_buyout_do_nothing(world):
    league, proc, *_ = world
    proc.apply_move(move("DROP", "p1", "ta", tx="t1"))
    proc.apply_move(move("CLAIM", "p1", to="tb", tx="t2", day=11))
    assert proc.apply_move(move("DROP", "p1", "tb", tx="t3", day=12)).effect == "NONE"
    assert Buyout.objects.count() == 1


def test_drop_by_a_team_that_does_not_hold_him_is_an_exception(world):
    league, proc, a, b, c, f = world
    e = proc.apply_move(move("DROP", "p1", "tb"))
    c.refresh_from_db()
    assert (e.effect, c.team, Buyout.objects.count()) == ("EXCEPTION", a, 0)


def test_farm_trade_and_release(world):
    league, proc, a, b, c, f = world
    assert proc.apply_move(move("TRADE", "p2", "ta", "tb")).effect == "FARM_MOVED"
    assert proc.apply_move(move("DROP", "p2", "tb", tx="t2", day=11)).effect == "FARM_RELEASED"
    f.refresh_from_db()
    assert (f.team, f.status) == (b, FarmPlayer.Status.RELEASED)


def test_moves_before_process_since_are_ignored(world):
    league, proc, *_ = world
    early = Move(datetime(2026, 2, 1, tzinfo=EASTERN), "DROP", "p1", "ta", None, "t0")
    assert proc.apply_move(early) is None
    assert not FantraxEvent.objects.exists()


def test_contract_signed_at_this_signing_ignores_earlier_moves(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2026)
    assert proc.apply_move(move("DROP", "p1", "ta")).effect == "NONE"


# --- roster facts and sync ------------------------------------------------


@dataclass
class FakeSnapshot:
    ends: dict
    moves_: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    teams: list = field(default_factory=lambda: [{"id": "ta", "name": "A"}, {"id": "tb", "name": "B"}])

    def moves(self):
        return self.moves_

    def end_states(self):
        return self.ends

    def trade_comments(self):
        return self.comments

    def players(self):
        return {}


def test_promotion_and_debut(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Active", 5, plate_appearances=9)})
    result = sync([(league, snap)])
    f.refresh_from_db()
    assert (f.status, f.has_mlb_appearance) == (FarmPlayer.Status.PROMOTED, True)
    assert result.counts() == {"Farm promoted": 1, "Farm debut": 1}
    assert sync([(league, snap)]).created == []


def test_games_without_a_debut_need_a_look(world):
    league, *_ = world
    snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 2)})
    (e,) = sync([(league, snap)]).exceptions
    assert e.kind == "DEBUT_CHECK"


def test_unknown_minors_player_is_an_exception(world):
    league, *_ = world
    Player.objects.create(name="Rookie", fantrax_id="p9")
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0), "p9": EndState("tb", "Minors", 0)}
    )
    (e,) = sync([(league, snap)]).exceptions
    assert (e.key, e.player.name, e.to_team.code) == ("minors-unknown:p9:2026", "Rookie", "BB")


def test_released_farm_player_back_in_minors_is_an_exception(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("tb", "Minors", 0)},
        moves_=[move("DROP", "p2", "ta"), move("CLAIM", "p2", to="tb", tx="t2", day=11)],
    )
    (e,) = sync([(league, snap)]).exceptions
    assert e.kind == "MINORS_UNKNOWN"


def test_contract_on_the_wrong_roster_is_an_exception(world):
    league, *_ = world
    snap = FakeSnapshot({"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)})
    (e,) = sync([(league, snap)]).exceptions
    assert e.detail == "Pat's contract is with AA, but Fantrax has him on BB"


def test_cash_comment_is_an_exception_until_entered(world):
    league, *_ = world
    when = datetime(2026, 7, 1, tzinfo=EASTERN)
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        comments=[("tx9", when, {"ta", "tb"}, "AA sends $5")],
    )
    (e,) = sync([(league, snap)]).exceptions
    assert "AA / BB mentions cash" in e.detail


def test_dry_run_changes_nothing(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot(
        {"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        moves_=[move("TRADE", "p1", "ta", "tb")],
    )
    result = sync([(league, snap)], dry_run=True)
    assert result.counts() == {"Contract moved": 1}
    assert not FantraxEvent.objects.exists()
    c.refresh_from_db()
    assert c.team == a


def test_sync_is_audited(world):
    league, *_ = world
    snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)})
    sync([(league, snap)], source_label="test")
    assert AuditEntry.objects.get(action="Synced Fantrax").detail == "test: No new events"


def test_rosters_come_from_the_newest_league_of_the_season(world):
    old, proc, a, b, c, f = world
    renewed = FantraxLeague.objects.create(league_id="R", season=2026)
    old_snap = FakeSnapshot({"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)})
    new_snap = FakeSnapshot(
        {"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        moves_=[move("TRADE", "p1", "ta", "tb", tx="offseason", day=20)],
    )
    for _ in range(2):
        assert sync([(old, old_snap), (renewed, new_snap)]).exceptions == []
    c.refresh_from_db()
    assert c.team == b


def test_cash_comments_in_the_old_league_are_still_listed(world):
    old, *_ = world
    renewed = FantraxLeague.objects.create(league_id="R", season=2026)
    when = datetime(2026, 7, 1, tzinfo=EASTERN)
    ends = {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)}
    old_snap = FakeSnapshot(ends, comments=[("tx9", when, {"ta", "tb"}, "AA sends $5")])
    (e,) = sync([(old, old_snap), (renewed, FakeSnapshot(ends))]).exceptions
    assert e.kind == "CASH_COMMENT"


def test_a_sync_racing_another_retries_and_skips_its_events(world, monkeypatch):
    """A double-click: the second run read the stored keys before the first run saved its events."""
    from league import events

    league, proc, a, b, c, f = world
    snap = FakeSnapshot(
        {"p1": EndState("tb", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        moves_=[move("TRADE", "p1", "ta", "tb")],
    )
    assert sync([(league, snap)]).counts() == {"Contract moved": 1}
    stale = iter([set()])  # the first read misses the other run's events; the retry sees them
    real = events.known_keys
    monkeypatch.setattr(events, "known_keys", lambda: next(stale, None) or real())
    assert sync([(league, snap)]).created == []
    assert FantraxEvent.objects.count() == 1


# --- the season of a move comes from its date ---------------------------------


def season_2027():
    return Season.objects.create(year=2027, auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN))


def at(kind, fid, frm=None, to=None, when=None, tx="t9"):
    return Move(when, kind, fid, frm, to, tx)


def test_final_year_drop_after_the_next_auction_is_free(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2025, length=2)  # final year 2027
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    e = proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 6, 1, tzinfo=EASTERN)))
    c.refresh_from_db()
    assert (e.effect, c.voided_in_season) == ("VOIDED", 2027)


def test_drop_after_the_next_auction_is_bought_out_in_that_season(world):
    league, proc, a, b, c, f = world  # final year 2028
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 6, 1, tzinfo=EASTERN)))
    assert Buyout.objects.get(contract=c).dropped_in_season == 2027


def test_drop_before_the_next_auction_is_the_old_season(world):
    league, proc, a, b, c, f = world
    season_2027()
    proc = Processor(league, proc.teams, proc.names)
    proc.apply_move(at("DROP", "p1", "ta", when=datetime(2027, 2, 24, 18, 0, tzinfo=EASTERN)))
    assert Buyout.objects.get(contract=c).dropped_in_season == 2026


def test_new_contract_is_visible_to_moves_after_its_lock(world):
    league, proc, a, b, c, f = world
    Contract.objects.filter(pk=c.pk).update(year_signed=2026)
    SigningPeriod.objects.create(season=2026, status="locked", locked_at=datetime(2026, 7, 5, tzinfo=EASTERN))
    proc = Processor(league, proc.teams, proc.names)
    assert proc.apply_move(move("TRADE", "p1", "ta", "tb")).effect == "CONTRACT_MOVED"


def test_sync_refuses_moves_a_year_past_the_last_auction(world):
    league, proc, a, b, c, f = world
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0)},
        moves_=[at("DROP", "p1", "ta", when=datetime(2027, 3, 1, tzinfo=EASTERN))],
    )
    with pytest.raises(SeasonMissing, match="2027"):
        sync([(league, snap)])
    assert not FantraxEvent.objects.exists()


# --- farm draft ---------------------------------------------------------------


@pytest.fixture
def draft(world):
    league, proc, a, b, c, f = world
    Season.objects.create(
        year=2027,
        farm_draft_starts_at=datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN),
        auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN),
    )
    for r in (1, 2):
        FarmPick.objects.create(year=2027, round=r, original_team=a, owner=a)
    return league, Processor(league, proc.teams, {"p7": "Rook", "p8": "Kid", "p9": "Late"}), a, b


def in_draft(fid, to, day=21, tx=None):
    return Move(datetime(2027, 2, day, 20, 0, tzinfo=EASTERN), "CLAIM", fid, None, to, tx or f"d{fid}")


def test_draft_claim_makes_a_one_dollar_farm_player(draft):
    league, proc, a, b = draft
    e = proc.apply_move(in_draft("p7", "ta"))
    fp = FarmPlayer.objects.get(player__fantrax_id="p7")
    assert (e.effect, fp.team, fp.salary, fp.salary_season, fp.drafted_year) == ("FARM_DRAFTED", a, 1, 2027, 2027)
    assert FarmPick.objects.get(year=2027, round=1, owner=a).player == fp.player
    proc.apply_move(in_draft("p8", "ta", day=22))
    assert FarmPick.objects.get(year=2027, round=2, owner=a).player.name == "Kid"


def test_draft_claim_without_a_pick_is_an_exception(draft):
    league, proc, a, b = draft
    e = proc.apply_move(in_draft("p7", "tb"))
    assert e.effect == "EXCEPTION" and "no 2027 pick left" in e.detail
    assert not FarmPlayer.objects.filter(player__fantrax_id="p7").exists()


def test_claim_after_the_auction_start_is_not_a_draft_pick(draft):
    league, proc, a, b = draft
    late = Move(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), "CLAIM", "p9", None, "ta", "late")
    assert proc.apply_move(late).effect == "NONE"
    assert not FarmPick.objects.exclude(player=None).exists()


def test_draft_claim_of_a_contracted_player_is_an_exception(draft):
    league, proc, a, b = draft
    assert proc.apply_move(in_draft("p1", "tb")).effect == "EXCEPTION"


def test_draft_pick_not_yet_in_minors_is_not_promoted(draft):
    league, proc, a, b = draft
    snap = FakeSnapshot(
        {"p1": EndState("ta", "Active", 0), "p2": EndState("ta", "Minors", 0), "p7": EndState("ta", "Reserve", 0)},
        moves_=[in_draft("p7", "ta")],
    )
    sync([(league, snap)])
    assert FarmPlayer.objects.get(player__fantrax_id="p7").status == FarmPlayer.Status.ACTIVE
