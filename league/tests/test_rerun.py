"""Re-running reconcile on a newer snapshot after some decisions: later moves must still be seen."""

from dataclasses import replace
from datetime import datetime

import pytest
from django.core.management import call_command

from league.fantrax_data import EASTERN, Move, Snapshot
from league.models import FarmPlayer, ReconciliationItem

pytestmark = pytest.mark.django_db
Kind, Status = ReconciliationItem.Kind, ReconciliationItem.Status
LATER = datetime(2026, 10, 5, 12, 0, tzinfo=EASTERN)


@pytest.fixture
def reconciled():
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)


def later_snapshot(monkeypatch, moves=(), roster_changes=None, end_changes=None):
    """Pretend Fantrax has more history than the committed snapshot."""
    moves_before, rostered_before, ends_before = Snapshot.moves, Snapshot.rostered, Snapshot.end_states

    def rostered(self):
        out = rostered_before(self)
        for fid, team in (roster_changes or {}).items():
            if team is None:
                out.pop(fid, None)
            else:
                out[fid] = (team, out.get(fid, (None, {}))[1])
        return out

    def end_states(self):
        out = ends_before(self)
        for fid, change in (end_changes or {}).items():
            out[fid] = replace(out[fid], **change)
        return out

    monkeypatch.setattr(Snapshot, "moves", lambda self: moves_before(self) + list(moves))
    monkeypatch.setattr(Snapshot, "rostered", rostered)
    monkeypatch.setattr(Snapshot, "end_states", end_states)


def continuing_item():
    return ReconciliationItem.objects.filter(kind=Kind.CONTINUES).select_related("contract__player", "team").first()


def test_rerun_with_nothing_new_proposes_nothing_for_decided_contracts(reconciled):
    item = continuing_item()
    item.accept()
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(contract=item.contract, status=Status.PENDING).exists()


def test_drop_after_an_accepted_continue_is_proposed(reconciled, monkeypatch):
    item = continuing_item()
    item.accept()
    fid, team = item.contract.player.fantrax_id, item.team.fantrax_id
    later_snapshot(monkeypatch, [Move(LATER, "DROP", fid, team, None, "late")], roster_changes={fid: None})
    call_command("reconcile", verbosity=0)
    new = ReconciliationItem.objects.get(contract=item.contract, status=Status.PENDING)
    assert (new.kind, new.team) == (Kind.DROPPED, item.team)


def test_trade_after_an_accepted_trade_is_proposed_from_the_new_team(reconciled, monkeypatch):
    item = ReconciliationItem.objects.filter(kind=Kind.TRADED).select_related("contract__player", "team").first()
    item.accept()
    fid, holder = item.contract.player.fantrax_id, item.team.fantrax_id
    other = ReconciliationItem.objects.exclude(team=item.team).exclude(team=None).first().team
    later_snapshot(
        monkeypatch,
        [Move(LATER, "TRADE", fid, holder, other.fantrax_id, "late")],
        roster_changes={fid: other.fantrax_id},
    )
    call_command("reconcile", verbosity=0)
    new = ReconciliationItem.objects.get(contract=item.contract, status=Status.PENDING)
    assert (new.kind, new.team) == (Kind.TRADED, other)


def test_late_debut_after_an_accepted_farm_continue_is_proposed(reconciled, monkeypatch):
    item = ReconciliationItem.objects.filter(
        kind=Kind.FARM_CONTINUES, mlb_debut=False, farm_player__has_mlb_appearance=False
    ).first()
    item.accept()
    fid = item.farm_player.player.fantrax_id
    later_snapshot(monkeypatch, end_changes={fid: {"games_played": 1, "plate_appearances": 3}})
    call_command("reconcile", verbosity=0)
    new = ReconciliationItem.objects.get(farm_player=item.farm_player, status=Status.PENDING)
    assert new.mlb_debut


def test_rerun_after_accepting_a_debut_does_not_flag_it_again(reconciled):
    item = ReconciliationItem.objects.filter(kind=Kind.FARM_CONTINUES, mlb_debut=True).first()
    item.accept()
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(farm_player=item.farm_player, status=Status.PENDING).exists()
    assert FarmPlayer.objects.get(pk=item.farm_player_id).has_mlb_appearance


def test_contract_player_in_a_minors_slot_is_not_proposed_as_a_farm_pick(reconciled, monkeypatch):
    item = continuing_item()
    later_snapshot(monkeypatch, end_changes={item.contract.player.fantrax_id: {"status": "Minors"}})
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(kind=Kind.FARM_UNKNOWN, player=item.contract.player).exists()
