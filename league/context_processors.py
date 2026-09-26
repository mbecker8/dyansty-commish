from django.conf import settings

from league.models import ReconciliationItem


def league(request):
    context = {"league_season": settings.LEAGUE_SEASON, "next_season": settings.LEAGUE_SEASON + 1}
    if request.user.is_authenticated:
        context["pending_review"] = ReconciliationItem.objects.filter(
            season=settings.LEAGUE_SEASON, status=ReconciliationItem.Status.PENDING
        ).count()
    return context
