"""End-to-end: import the committed sheet fixture + Fantrax snapshot, then check budgets."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from league.budget import team_budget
from league.models import Buyout, CashTrade, Contract, FarmPick, FarmPlayer, Player, Team
from rules.tests.test_golden_year19 import SHEET_BUGS, TEAMS

pytestmark = pytest.mark.django_db


@pytest.fixture
def imported():
    call_command("import_league", verbosity=0)


def test_all_franchises_imported(imported):
    assert Team.objects.count() == 14
    assert Team.objects.get(fantrax_id="m84gn4f4mgdm460z").code == "SM"
    assert Team.objects.get(aliases__alias="Winning DeLautery").code == "AG"


@pytest.mark.parametrize("code", sorted(TEAMS))
def test_2026_budget_from_database_matches_sheet(imported, code):
    overcharge = sum(delta for (team, _), (delta, _) in SHEET_BUGS.items() if team == code)
    budget = team_budget(Team.objects.get(code=code), 2026)
    assert budget.remaining == TEAMS[code]["budget"]["remaining"] - overcharge


def test_sheet_typos_resolve_to_fantrax_players(imported):
    stott = Player.objects.get(fantrax_id="04p10")
    assert stott.name == "Bryson Stott"
    assert Contract.objects.get(player=stott).sign_and_trade


def test_players_missing_from_fantrax_have_no_id(imported):
    assert Player.objects.get(name="Wander Franco").fantrax_id is None


def test_counts(imported):
    assert Contract.objects.filter(buyout__isnull=True).count() == sum(len(t["contracts"]) for t in TEAMS.values())
    assert Buyout.objects.count() == sum(
        1 for t in TEAMS.values() for b in t["buyouts"] if b["original_price"] is not None
    )
    assert FarmPlayer.objects.count() == sum(len(t["farm"]) for t in TEAMS.values())
    assert CashTrade.objects.filter(budget_season=2026).count() == 37


def test_farm_pick_ownership_comes_from_fantrax(imported):
    # MB owns KJ's 2028 2nd-rounder per Fantrax.
    pick = FarmPick.objects.get(year=2028, round=2, original_team__code="KJ")
    assert pick.owner.code == "MB"
    assert not FarmPick.objects.filter(year=2027, owner__code="MB").exists()


def test_refuses_to_import_twice_without_replace(imported):
    with pytest.raises(CommandError):
        call_command("import_league", verbosity=0)


def test_replace_is_idempotent(imported):
    before = (Player.objects.count(), Contract.objects.count(), FarmPick.objects.count())
    call_command("import_league", replace=True, verbosity=0)
    assert (Player.objects.count(), Contract.objects.count(), FarmPick.objects.count()) == before
