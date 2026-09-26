"""League-wide pages: signed-in managers see every team; numbers come from the rules engine."""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league.budget import team_budget
from league.models import Buyout, Contract, FarmPick, Manager, ReconciliationItem, Team
from rules.buyouts import buyout_schedule

pytestmark = pytest.mark.django_db


def owes_after_2026(buyout):
    return any(s >= 2027 for s in buyout_schedule(buyout.contract.as_rules(), buyout.dropped_in_season))


PAGES = ["/teams/", "/teams/MB/", "/contracts/", "/buyouts/", "/farm/", "/picks/", "/cash/"]


@pytest.fixture
def league():
    call_command("import_league", verbosity=0)


@pytest.fixture
def manager_client(league, client):
    user = User.objects.create_user("discord-123")
    Manager.objects.create(team=Team.objects.get(code="MB"), name="Matt", discord_id="123", user=user)
    client.force_login(user)
    return client


@pytest.mark.parametrize("url", PAGES)
def test_pages_need_sign_in(client, league, url):
    response = client.get(url)
    assert response.status_code == 302 and response["Location"].startswith(f"/auth/login?next={url}")


@pytest.mark.parametrize("url", PAGES)
def test_pages_render_for_a_manager(manager_client, url):
    assert manager_client.get(url).status_code == 200


def test_home_sends_a_manager_to_their_team(manager_client):
    assert manager_client.get("/")["Location"] == "/teams/MB/"


def test_home_sends_anonymous_visitors_to_sign_in(client):
    assert client.get("/")["Location"].startswith("/auth/login")


def test_signed_in_user_without_a_team_goes_to_the_team_list(league, client):
    client.force_login(User.objects.create_user("commish", is_staff=True))
    assert client.get("/")["Location"] == "/teams/"


def test_team_list_shows_each_teams_next_season_budget(manager_client):
    page = manager_client.get("/teams/")
    rows = {t["team"].code: t["budget"] for t in page.context["teams"]}
    assert len(rows) == 14
    for team in Team.objects.all():
        assert rows[team.code] == team_budget(team, 2027)
    assert f"${rows['MB'].remaining}".encode() in page.content


def test_team_page_matches_the_budget_and_lists_every_commitment(manager_client):
    page = manager_client.get("/teams/MB/")
    team = Team.objects.get(code="MB")
    assert page.context["budget"] == team_budget(team, 2027)
    html = page.content.decode()
    for c in Contract.live.filter(team=team, year_signed__gte=2020):
        assert c.player.name in html
    for b in Buyout.objects.filter(team=team):
        if owes_after_2026(b):
            assert b.contract.player.name in html
    for f in team.farm.filter(status="active"):
        assert f.player.name in html


def test_other_teams_are_visible_too(manager_client):
    assert manager_client.get("/teams/SM/").status_code == 200


def test_unknown_team_is_404(manager_client):
    assert manager_client.get("/teams/ZZ/").status_code == 404


def test_buyout_page_lists_exactly_the_buyouts_still_owing(manager_client):
    rows = manager_client.get("/buyouts/").context["rows"]
    owing = [b for b in Buyout.objects.all() if owes_after_2026(b)]
    assert {r["buyout"].pk for r in rows} == {b.pk for b in owing}
    for r in rows:
        assert r["remaining"] == {
            s: amt
            for s, amt in buyout_schedule(r["buyout"].contract.as_rules(), r["buyout"].dropped_in_season).items()
            if s >= 2027
        }


def test_paid_off_buyouts_are_not_listed(manager_client):
    paid = [b for b in Buyout.objects.all() if not owes_after_2026(b)]
    assert paid  # the sheet has some
    assert not {r["buyout"].pk for r in manager_client.get("/buyouts/").context["rows"]} & {b.pk for b in paid}


def test_players_without_a_fantrax_id_still_show(manager_client):
    unlinked = Buyout.objects.filter(contract__player__fantrax_id=None).select_related("contract__player")
    shown = [b for b in unlinked if owes_after_2026(b)]
    assert shown
    html = manager_client.get("/buyouts/").content.decode()
    for b in shown:
        assert b.contract.player.name in html


def test_pick_page_lists_every_pick(manager_client):
    page = manager_client.get("/picks/")
    assert sum(len(row["picks"]) for row in page.context["rows"]) == FarmPick.objects.count()


def test_banner_warns_while_reconciliation_is_pending(manager_client):
    call_command("reconcile", verbosity=0)
    pending = ReconciliationItem.objects.filter(status="pending").count()
    assert f"{pending} changes from the 2026 season".encode() in manager_client.get("/teams/").content


def test_no_banner_when_nothing_is_pending(manager_client):
    assert b"await commissioner review" not in manager_client.get("/teams/").content
