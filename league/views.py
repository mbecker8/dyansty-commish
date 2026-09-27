"""Read-only league pages. Every dollar comes from `team_budget` or the rules engine, never from view math."""

from collections import defaultdict

from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render

from league import seasons
from league.access import is_league_member, member_required
from league.budget import team_budget
from league.models import (
    BudgetAdjustment,
    Buyout,
    CashTrade,
    Contract,
    FantraxEvent,
    FarmPick,
    FarmPlayer,
    Player,
    SeasonBudget,
    Team,
)
from league.seasons import current_season
from rules.buyouts import buyout_schedule
from rules.farm import retained_salary


def next_season():
    return current_season() + 1


def home(request):
    if not request.user.is_authenticated:
        return redirect("login")
    manager = getattr(request.user, "manager", None)
    if manager is not None and is_league_member(request.user):
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


@member_required
def teams(request):
    rows = [{"team": t, "budget": team_budget(t, next_season())} for t in Team.objects.prefetch_related("managers")]
    return render(request, "league/teams.html", {"teams": rows})


LINE_LABELS = {"contract": "Contract", "buyout": "Buyout", "farm": "Farm"}


def ledger(budget):
    """Every charge in the budget, as the rules engine itemised it."""
    players = Player.objects.in_bulk([int(pid) for _, pid, _ in budget.lines])
    return [
        {"kind": LINE_LABELS.get(kind, kind), "player": players.get(int(pid)), "amount": amount}
        for kind, pid, amount in budget.lines
    ]


@member_required
def team(request, code):
    team = get_object_or_404(Team, code=code.upper())
    season = next_season()
    budget = team_budget(team, season)
    live = Contract.live.filter(team=team).select_related("player").order_by("player__name")
    buyouts = Buyout.objects.filter(team=team).select_related("contract__player").order_by("contract__player__name")
    farm = FarmPlayer.objects.filter(team=team, status=FarmPlayer.Status.ACTIVE).select_related("player")
    current = seasons.current()
    start, end = seasons.window(current)
    moves = FantraxEvent.objects.filter(Q(from_team=team) | Q(to_team=team), happened_at__gte=start)
    if end:
        moves = moves.filter(happened_at__lt=end)
    return render(
        request,
        "league/team.html",
        {
            "team": team,
            "budget": budget,
            "ledger": ledger(budget),
            "adjustments": BudgetAdjustment.objects.filter(team=team, season=season),
            "contracts": contract_rows(c for c in live if c.final_year >= season),
            "expired": [c for c in live if c.final_year == current.year],
            "buyouts": buyout_rows(buyouts),
            "farm": farm_rows(farm.order_by("player__name")),
            "picks": FarmPick.objects.filter(owner=team, year__gte=season, player=None)
            .select_related("original_team")
            .order_by("year", "round", "original_team__code"),
            "cash": CashTrade.objects.filter(Q(from_team=team) | Q(to_team=team))
            .select_related("from_team", "to_team")
            .order_by("-budget_season", "pk"),
            "managers": team.managers.all(),
            "frozen": SeasonBudget.objects.filter(team=team, season=current.year).first(),
            "moves": moves.exclude(
                effect__in=[
                    FantraxEvent.Effect.NONE,
                    FantraxEvent.Effect.ALREADY_REFLECTED,
                    FantraxEvent.Effect.EXCEPTION,
                ]
            ).order_by("-happened_at", "pk"),
        },
    )


@member_required
def contracts(request):
    season = next_season()
    live = Contract.live.select_related("player", "team").order_by("team__code", "player__name")
    return render(request, "league/contracts.html", {"rows": contract_rows(c for c in live if c.final_year >= season)})


@member_required
def buyouts(request):
    all_buyouts = Buyout.objects.select_related("contract__player", "team").order_by(
        "team__code", "contract__player__name"
    )
    return render(request, "league/buyouts.html", {"rows": buyout_rows(all_buyouts)})


@member_required
def farm(request):
    active = FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).select_related("player", "team")
    return render(request, "league/farm.html", {"rows": farm_rows(active.order_by("team__code", "player__name"))})


@member_required
def picks(request):
    by_year = defaultdict(list)
    for p in FarmPick.objects.select_related("original_team", "owner").order_by("year", "round", "original_team__code"):
        by_year[p.year].append(p)
    rows = [{"year": year, "picks": picks} for year, picks in sorted(by_year.items())]
    return render(request, "league/picks.html", {"rows": rows})


@member_required
def cash(request):
    trades = CashTrade.objects.select_related("from_team", "to_team").order_by("-budget_season", "from_team__code")
    return render(request, "league/cash.html", {"trades": trades})


@member_required
def help_page(request):
    return render(request, "league/help.html", {"is_commissioner": request.user.is_staff})
