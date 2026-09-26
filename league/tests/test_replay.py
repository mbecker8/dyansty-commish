from datetime import datetime

from league.fantrax_data import EASTERN, Move
from league.reconcile import Outcome, replay

A, B, C = "teamA", "teamB", "teamC"


def mv(day, kind, frm=None, to=None, tx="t"):
    return Move(datetime(2026, 5, day, tzinfo=EASTERN), kind, "p1", frm, to, tx)


def test_no_moves_continues():
    r = replay(A, final_year=2028, moves=[], season=2026)
    assert r.outcome is Outcome.CONTINUES and r.holder == A


def test_no_moves_final_year_is_expiring():
    assert replay(A, final_year=2026, moves=[], season=2026).outcome is Outcome.EXPIRING


def test_trade_moves_contract():
    r = replay(A, 2028, [mv(1, "TRADE", A, B, tx="x1")], 2026)
    assert r.outcome is Outcome.TRADED and r.holder == B and r.tx_ids == ["x1"]


def test_drop_creates_buyout_for_dropping_team():
    r = replay(A, 2028, [mv(1, "DROP", frm=A)], 2026)
    assert r.outcome is Outcome.DROPPED and r.buyout_team == A and r.holder is None


def test_traded_then_dropped_buyout_owed_by_second_team():
    r = replay(A, 2028, [mv(1, "TRADE", A, B), mv(9, "DROP", frm=B)], 2026)
    assert r.outcome is Outcome.DROPPED and r.buyout_team == B


def test_final_year_drop_is_free():
    r = replay(A, 2026, [mv(1, "DROP", frm=A)], 2026)
    assert r.outcome is Outcome.DROPPED_FREE and r.buyout_team is None


def test_drop_then_claim_elsewhere_notes_new_team():
    r = replay(A, 2028, [mv(1, "DROP", frm=A), mv(3, "CLAIM", to=C)], 2026)
    assert r.outcome is Outcome.DROPPED and r.buyout_team == A and r.claimed_by == C


def test_reclaim_by_same_team_still_owes_buyout():
    r = replay(A, 2028, [mv(1, "DROP", frm=A), mv(3, "CLAIM", to=A)], 2026)
    assert r.outcome is Outcome.DROPPED and r.buyout_team == A and r.claimed_by == A


def test_move_from_a_team_that_does_not_hold_him_is_inconsistent():
    r = replay(A, 2028, [mv(1, "TRADE", B, C)], 2026)
    assert r.outcome is Outcome.INCONSISTENT
    assert "teamB" in r.detail


def test_claim_while_under_contract_is_inconsistent():
    assert replay(A, 2028, [mv(1, "CLAIM", to=C)], 2026).outcome is Outcome.INCONSISTENT


def test_end_roster_mismatch_is_inconsistent():
    r = replay(A, 2028, [], 2026, end_team=B)
    assert r.outcome is Outcome.INCONSISTENT


def test_trade_into_current_holder_is_already_reflected():
    # The sheet already shows him on A (a pre-auction sign-and-trade); Fantrax's B -> A trade is not news.
    r = replay(A, 2028, [mv(1, "TRADE", B, A, tx="late")], 2026)
    assert r.outcome is Outcome.CONTINUES and r.holder == A
    assert "late" in r.detail


def test_claim_by_current_holder_is_already_reflected():
    # Farm draft picks are entered in Fantrax as claims; the sheet already lists them.
    r = replay(A, 2028, [mv(1, "CLAIM", to=A, tx="draft")], 2026)
    assert r.outcome is Outcome.CONTINUES and "draft" in r.detail
