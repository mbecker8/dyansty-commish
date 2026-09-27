from django.urls import reverse

from league.access import is_league_member
from league.models import SigningPeriod
from league.seasons import current_season

# Breadcrumb trail per page: (label, url name) ancestors, then the page's own label (None: the team code).
PAGES = {
    "teams": ([], "Teams"),
    "team": ([("Teams", "teams")], None),
    "contracts": ([], "Contracts"),
    "buyouts": ([], "Buyouts"),
    "farm": ([], "Farm"),
    "picks": ([], "Farm picks"),
    "draft_order": ([("Farm picks", "picks")], "Draft order"),
    "cash": ([], "Cash"),
    "signing": ([("Signing", "signing_home")], None),
    "console": ([], "Commissioner"),
    "audit": ([("Commissioner", "console")], "Audit log"),
    "export": ([], "Export"),
    "help": ([], "Help"),
}


def breadcrumbs(request):
    """Home › section › page, as (label, url) pairs; the last one is the current page, without a link."""
    match = request.resolver_match
    if match is None or match.url_name not in PAGES:
        return []
    parents, label = PAGES[match.url_name]
    code = match.kwargs.get("code")
    crumbs = [("Home", "/"), *((name, reverse(url)) for name, url in parents)]
    return [*crumbs, (label or (code or "").upper(), None)]


def league(request):
    """The season for league pages. They all need sign-in, so signed-out pages skip the query."""
    if not request.user.is_authenticated:
        return {}
    season = current_season()
    context = {"league_season": season, "next_season": season + 1, "crumbs": breadcrumbs(request)}
    if is_league_member(request.user):
        period = SigningPeriod.objects.filter(season=season).first()
        context["signing_status"] = period.status if period else None
    return context
