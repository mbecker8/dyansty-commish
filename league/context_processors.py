from django.conf import settings

from league.access import is_league_member
from league.models import ReconciliationItem


def league(request):
    context = {"league_season": settings.LEAGUE_SEASON, "next_season": settings.LEAGUE_SEASON + 1}
    if is_league_member(request.user):
        context["pending_review"] = ReconciliationItem.objects.filter(
            season=settings.LEAGUE_SEASON, status=ReconciliationItem.Status.PENDING
        ).count()
    return context
