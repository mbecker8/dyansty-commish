"""The 2026 season's Fantrax moves, applied as events to the imported league."""

import json
from pathlib import Path

import pytest
from django.core.management import call_command

from league.events import sync
from league.fantrax_data import Snapshot
from league.models import Buyout, Contract, FantraxEvent, FantraxLeague, FarmPlayer

pytestmark = pytest.mark.django_db
SNAP = Path(__file__).resolve().parents[2] / "data" / "fantrax" / "2026-final"
# From the old reconciliation, accepting every item it could (see the plan in docs/superpowers/plans).
GOLDEN = json.loads((Path(__file__).parent / "data" / "events_2026_golden.json").read_text())


@pytest.fixture
def synced():
    call_command("import_league", verbosity=0)
    return sync([(FantraxLeague.objects.get(), Snapshot(SNAP))])


def state():
    contracts = Contract.objects.select_related("player", "team")
    buyouts = Buyout.objects.select_related("contract__player", "team")
    farm = FarmPlayer.objects.select_related("player", "team")
    return {
        "contracts": sorted([c.player.name, c.year_signed, c.team.code, c.voided_in_season] for c in contracts),
        "buyouts": sorted(
            [b.contract.player.name, b.contract.year_signed, b.team.code, b.dropped_in_season] for b in buyouts
        ),
        "farm": sorted([f.player.name, f.drafted_year, f.team.code, f.status, f.has_mlb_appearance] for f in farm),
    }


# The old "dropped in final year" accept voided the contract but left it with the season's starting
# holder. Events apply the trades first, so it ends with the team that dropped him. No money moves.
VOIDED_WITH_DROPPER = {("Alec Bohm", 2025): "KJ", ("Robbie Ray", 2024): "KJ"}


def test_2026_matches_accepting_every_reconciliation_item(synced):
    want = {k: GOLDEN[k] for k in ("contracts", "buyouts", "farm")}
    want["contracts"] = sorted(
        [name, year, VOIDED_WITH_DROPPER.get((name, year), team), voided]
        for name, year, team, voided in want["contracts"]
    )
    assert state() == want


def test_2026_exceptions_are_the_two_unknown_minors(synced):
    assert sorted(e.player_name for e in synced.exceptions) == GOLDEN["unknown_minors"]


def test_second_sync_adds_nothing(synced):
    n = FantraxEvent.objects.count()
    assert sync([(FantraxLeague.objects.get(), Snapshot(SNAP))]).created == []
    assert FantraxEvent.objects.count() == n
