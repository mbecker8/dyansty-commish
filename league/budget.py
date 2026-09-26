"""Compute a team's auction budget from the database using the rules engine."""

from django.db.models import Sum

from league.models import BudgetAdjustment, Buyout, CashTrade, Contract, FarmPlayer, Team
from rules.budget import Budget, compute_budget


def team_budget(team: Team, season: int) -> Budget:
    contracts = Contract.live.filter(team=team)
    buyouts = Buyout.objects.filter(team=team).select_related("contract")
    farm = FarmPlayer.objects.filter(team=team, salary_season=season, status=FarmPlayer.Status.ACTIVE)
    received = CashTrade.objects.filter(to_team=team, budget_season=season).aggregate(s=Sum("amount"))["s"] or 0
    sent = CashTrade.objects.filter(from_team=team, budget_season=season).aggregate(s=Sum("amount"))["s"] or 0
    missed_ip = BudgetAdjustment.objects.filter(team=team, season=season, kind=BudgetAdjustment.Kind.MISSED_IP)
    return compute_budget(
        season=season,
        contracts=[c.as_rules() for c in contracts],
        buyouts=[(b.contract.as_rules(), b.dropped_in_season) for b in buyouts],
        farm_salaries={str(f.player_id): f.salary for f in farm},
        missed_ip_penalties=[a.amount for a in missed_ip],
        cash_net=received - sent,
    )
