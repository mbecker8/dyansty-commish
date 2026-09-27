"""Compute a team's auction budget from the database using the rules engine."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from django.db.models import Sum

import rules.contracts
from league.models import BudgetAdjustment, Buyout, CashTrade, Contract, FarmPlayer, Team
from rules.budget import Budget, compute_budget


@dataclass(frozen=True)
class Changes:
    """Signing decisions not yet applied, so a preview uses the same path as the real budget.

    new_contracts: contracts being signed. bought_out: live contracts being dropped in
    `dropped_in`. farm_kept: player id -> next season's salary for farm players being kept.
    """

    dropped_in: int
    new_contracts: Iterable[rules.contracts.Contract] = ()
    bought_out: Iterable[Contract] = ()
    farm_kept: Mapping[str, int] = field(default_factory=dict)


def team_budget(team: Team, season: int, changes: Changes | None = None) -> Budget:
    """The auction budget for `season`, computed from who holds what *now*, plus any `changes`.

    Only meaningful for the next auction. A past season's budget was fixed when its
    auction ran, and the season's Fantrax moves (trades, drops, promotions)
    changes current holdings, so recomputing it afterwards gives a different number.
    Budgets aren't stored yet; if a past season's figure is ever shown, snapshot it
    at auction time instead of calling this.
    """
    bought_out = {c.pk: c for c in (changes.bought_out if changes else ())}
    contracts = [c.as_rules() for c in Contract.live.filter(team=team) if c.pk not in bought_out]
    buyouts = [
        (b.contract.as_rules(), b.dropped_in_season)
        for b in Buyout.objects.filter(team=team).select_related("contract")
    ]
    farm = FarmPlayer.objects.filter(team=team, salary_season=season, status=FarmPlayer.Status.ACTIVE)
    farm_salaries = {str(f.player_id): f.salary for f in farm}
    if changes:
        contracts += list(changes.new_contracts)
        buyouts += [(c.as_rules(), changes.dropped_in) for c in bought_out.values()]
        farm_salaries |= changes.farm_kept
    received = CashTrade.objects.filter(to_team=team, budget_season=season).aggregate(s=Sum("amount"))["s"] or 0
    sent = CashTrade.objects.filter(from_team=team, budget_season=season).aggregate(s=Sum("amount"))["s"] or 0
    missed_ip = BudgetAdjustment.objects.filter(team=team, season=season, kind=BudgetAdjustment.Kind.MISSED_IP)
    return compute_budget(
        season=season,
        contracts=contracts,
        buyouts=buyouts,
        farm_salaries=farm_salaries,
        missed_ip_penalties=[a.amount for a in missed_ip],
        cash_net=received - sent,
    )
