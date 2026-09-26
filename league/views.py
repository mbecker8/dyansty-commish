"""Read-only league pages. Every dollar comes from `team_budget` or the rules engine, never from view math."""

from collections import defaultdict

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from league.budget import team_budget
from league.models import Buyout, CashTrade, Contract, FarmPick, FarmPlayer, Team
from rules.buyouts import buyout_schedule
from rules.farm import retained_salary


def next_season():
    return settings.LEAGUE_SEASON + 1


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    manager = getattr(request.user, "manager", None)
    if manager is not None:
        return redirect("team", code=manager.team.code)
    return redirect("teams")


def contract_rows(contracts):
    season = next_season()
    rows = []
    for c in contracts:
        rules = c.as_rules()
        rows.append({"contract": c, "annual": rules.annual_price, "years_left": rules.years_left(season - 1)})
    return rows


def buyout_rows(buyouts):
    """Each buyout with the penalties still to come, from the next auction on."""
    season = next_season()
    rows = []
    for b in buyouts:
        schedule = buyout_schedule(b.contract.as_rules(), b.dropped_in_season)
        remaining = {s: amount for s, amount in sorted(schedule.items()) if s >= season}
        if remaining:
            rows.append(
                {
                    "buyout": b,
                    "annual": b.contract.as_rules().annual_price,
                    "remaining": remaining,
                    "next": remaining.get(season, 0),
                    "total": sum(remaining.values()),
                }
            )
    return rows


def farm_rows(farm):
    return [{"farm": f, "keep": retained_salary(f.salary, f.has_mlb_appearance)} for f in farm]


@login_required
def teams(request):
    rows = [{"team": t, "budget": team_budget(t, next_season())} for t in Team.objects.prefetch_related("managers")]
    return render(request, "league/teams.html", {"teams": rows})


@login_required
def team(request, code):
    team = get_object_or_404(Team, code=code.upper())
    season = next_season()
    live = Contract.live.filter(team=team).select_related("player")
    buyouts = Buyout.objects.filter(team=team).select_related("contract__player")
    farm = FarmPlayer.objects.filter(team=team, status=FarmPlayer.Status.ACTIVE).select_related("player")
    return render(
        request,
        "league/team.html",
        {
            "team": team,
            "budget": team_budget(team, season),
            "contracts": contract_rows(c for c in live if c.final_year >= season),
            "expired": [c for c in live if c.final_year < season],
            "buyouts": buyout_rows(buyouts),
            "farm": farm_rows(farm),
            "picks": FarmPick.objects.filter(owner=team, year__gte=season).select_related("original_team"),
            "cash": CashTrade.objects.filter(budget_season__gte=season)
            .filter(from_team=team)
            .union(CashTrade.objects.filter(budget_season__gte=season, to_team=team))
            .order_by("budget_season"),
            "managers": team.managers.all(),
        },
    )


@login_required
def contracts(request):
    season = next_season()
    live = Contract.live.select_related("player", "team").order_by("team__code", "player__name")
    return render(request, "league/contracts.html", {"rows": contract_rows(c for c in live if c.final_year >= season)})


@login_required
def buyouts(request):
    all_buyouts = Buyout.objects.select_related("contract__player", "team").order_by("team__code")
    return render(request, "league/buyouts.html", {"rows": buyout_rows(all_buyouts)})


@login_required
def farm(request):
    active = FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).select_related("player", "team")
    return render(request, "league/farm.html", {"rows": farm_rows(active.order_by("team__code", "player__name"))})


@login_required
def picks(request):
    by_year = defaultdict(list)
    for p in FarmPick.objects.select_related("original_team", "owner").order_by("year", "round", "original_team__code"):
        by_year[p.year].append(p)
    rows = [{"year": year, "picks": picks} for year, picks in sorted(by_year.items())]
    return render(request, "league/picks.html", {"rows": rows})


@login_required
def cash(request):
    trades = CashTrade.objects.select_related("from_team", "to_team").order_by("-budget_season", "from_team__code")
    return render(request, "league/cash.html", {"trades": trades})
