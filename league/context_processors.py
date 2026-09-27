from league.access import is_league_member
from league.models import SigningPeriod
from league.seasons import current_season


def league(request):
    """The season for league pages. They all need sign-in, so signed-out pages skip the query."""
    if not request.user.is_authenticated:
        return {}
    season = current_season()
    context = {"league_season": season, "next_season": season + 1}
    if is_league_member(request.user):
        period = SigningPeriod.objects.filter(season=season).first()
        context["signing_status"] = period.status if period else None
    return context
