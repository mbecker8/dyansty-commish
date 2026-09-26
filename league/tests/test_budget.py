import pytest

from league.budget import team_budget
from league.models import BudgetAdjustment, Buyout, CashTrade, Contract, FarmPlayer, Player, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    return Team.objects.create(code="MB", name="Wreckers"), Team.objects.create(code="DC", name="Cliff Lee")


def player(name):
    return Player.objects.create(name=name)


def test_budget_from_database(teams):
    mb, dc = teams
    Contract.objects.create(team=mb, player=player("Royce Lewis"), original_price=9, year_signed=2024, length=3)
    garrett = Contract.objects.create(
        team=mb, player=player("Braxton Garrett"), original_price=1, year_signed=2023, length=3
    )
    Buyout.objects.create(contract=garrett, team=mb, dropped_in_season=2024)
    FarmPlayer.objects.create(team=mb, player=player("Josue De Paula"), drafted_year=2023, salary=4, salary_season=2026)
    CashTrade.objects.create(budget_season=2026, from_team=dc, to_team=mb, amount=5)
    BudgetAdjustment.objects.create(team=mb, season=2026, kind=BudgetAdjustment.Kind.MISSED_IP, amount=10)

    b = team_budget(mb, 2026)

    assert (b.contracts, b.buyouts, b.farm, b.cash_net, b.missed_ip) == (19, 8, 4, 5, 10)
    assert b.remaining == 400 + 5 - 19 - 8 - 4 - 10
    assert team_budget(dc, 2026).cash_net == -5


def test_bought_out_contract_is_not_charged_as_a_contract(teams):
    mb, _ = teams
    c = Contract.objects.create(team=mb, player=player("Lux"), original_price=3, year_signed=2025, length=3)
    Buyout.objects.create(contract=c, team=mb, dropped_in_season=2026)
    b = team_budget(mb, 2027)
    assert b.contracts == 0
    assert b.buyouts == 10  # 80% of $13


def test_traded_buyout_is_charged_to_its_new_owner(teams):
    mb, dc = teams
    c = Contract.objects.create(team=mb, player=player("Lux"), original_price=3, year_signed=2025, length=3)
    Buyout.objects.create(contract=c, team=dc, dropped_in_season=2026)
    assert team_budget(mb, 2027).buyouts == 0
    assert team_budget(dc, 2027).buyouts == 10


def test_farm_players_only_count_for_their_salary_season(teams):
    mb, _ = teams
    FarmPlayer.objects.create(team=mb, player=player("A"), drafted_year=2025, salary=2, salary_season=2026)
    FarmPlayer.objects.create(
        team=mb, player=player("B"), drafted_year=2024, salary=3, salary_season=2026, status=FarmPlayer.Status.RELEASED
    )
    assert team_budget(mb, 2026).farm == 2
    assert team_budget(mb, 2027).farm == 0
