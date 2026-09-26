from datetime import datetime

from league.fantrax_data import EASTERN, Move
from league.reconcile import EndState, FarmOutcome, replay_farm

A, B = "teamA", "teamB"


def mv(day, kind, frm=None, to=None):
    return Move(datetime(2026, 6, day, tzinfo=EASTERN), kind, "p1", frm, to, "t")


def minors(team, games=0, pa=0, outs=0):
    return EndState(team=team, status="Minors", games_played=games, plate_appearances=pa, outs=outs)


def test_stays_in_minors():
    r = replay_farm(A, [], minors(A), had_mlb=False)
    assert (r.outcome, r.team, r.mlb_debut) == (FarmOutcome.CONTINUES, A, False)


def test_first_plate_appearance_is_a_debut():
    r = replay_farm(A, [], minors(A, games=1, pa=1), had_mlb=False)
    assert r.outcome is FarmOutcome.CONTINUES and r.mlb_debut


def test_first_out_recorded_is_a_debut():
    assert replay_farm(A, [], minors(A, games=1, outs=1), had_mlb=False).mlb_debut


def test_games_without_a_plate_appearance_or_out_are_not_a_debut_but_need_a_look():
    # A pinch runner or defensive sub gets a game played but no PA.
    r = replay_farm(A, [], minors(A, games=2), had_mlb=False)
    assert not r.mlb_debut
    assert "no plate appearance or out" in r.detail


def test_already_had_mlb_time_is_not_a_debut():
    assert not replay_farm(A, [], minors(A, games=3, pa=9), had_mlb=True).mlb_debut


def test_promoted_to_active_roster():
    r = replay_farm(A, [], EndState(A, "Active", 40, plate_appearances=150), had_mlb=False)
    assert r.outcome is FarmOutcome.PROMOTED and r.team == A


def test_traded_stays_on_farm():
    r = replay_farm(A, [mv(1, "TRADE", A, B)], minors(B), had_mlb=False)
    assert (r.outcome, r.team) == (FarmOutcome.TRADED, B)


def test_traded_then_promoted():
    r = replay_farm(A, [mv(1, "TRADE", A, B)], EndState(B, "Reserve", 5, outs=20), had_mlb=False)
    assert (r.outcome, r.team) == (FarmOutcome.PROMOTED, B)


def test_dropped_is_released():
    r = replay_farm(A, [mv(1, "DROP", frm=A)], None, had_mlb=False)
    assert r.outcome is FarmOutcome.RELEASED


def test_dropped_and_claimed_onto_an_active_roster_is_released():
    r = replay_farm(A, [mv(1, "DROP", frm=A), mv(2, "CLAIM", to=B)], EndState(B, "Active", 10), had_mlb=False)
    assert r.outcome is FarmOutcome.RELEASED


def test_unexplained_end_state_needs_a_look():
    r = replay_farm(A, [], minors(B), had_mlb=False)
    assert r.outcome is FarmOutcome.INCONSISTENT


def test_released_then_back_in_a_minors_slot_needs_a_look():
    # Farm adds are only by draft or trade, so a released player can't be re-stashed by a claim.
    r = replay_farm(A, [mv(1, "DROP", frm=A), mv(2, "CLAIM", to=B)], minors(B), had_mlb=False)
    assert r.outcome is FarmOutcome.INCONSISTENT
