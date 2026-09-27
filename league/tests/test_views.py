"""League-wide pages: signed-in managers see every team; numbers come from the rules engine."""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league.budget import team_budget
from league.models import Buyout, Contract, FarmPick, Manager, Season, SeasonBudget, Team
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
    assert len(page.context["rows"]) == FarmPick.objects.count()


def test_2027_picks_are_numbered_by_the_2026_standings(manager_client):
    rows = manager_client.get("/picks/order/?year=2027").context["rows"]
    assert [r["number"] for r in rows] == list(range(1, 29))
    # 8th (CS) picks first, 12th (AW) third, the champion (KJ) last in each round.
    assert [rows[i]["pick"].original_team.code for i in (0, 2, 13, 14, 27)] == ["CS", "AW", "KJ", "CS", "KJ"]
    assert all(r["pick"].round == (1 if r["number"] <= 14 else 2) for r in rows)


def test_picks_have_no_number_until_the_standings_are_in(manager_client):
    page = manager_client.get("/picks/order/?year=2028")
    assert page.status_code == 200
    assert all(r["number"] is None for r in page.context["rows"])
    assert "final standings aren't complete" in page.content.decode()


def test_a_bad_draft_year_falls_back_to_the_next_draft(manager_client):
    assert manager_client.get("/picks/order/?year=abc").context["year"] == 2027


def test_team_page_shows_pick_numbers(manager_client):
    html = manager_client.get("/teams/CS/").content.decode()
    assert "2027 round 1, pick 1" in html


def test_teams_link_to_their_fantrax_roster(manager_client):
    html = manager_client.get("/teams/").content.decode()
    team = Team.objects.get(code="MB")
    assert f"/fantasy/league/p3z8zy75mgdm460o/team/roster;teamId={team.fantrax_id}" in html


def test_breadcrumbs_lead_back_home(manager_client):
    html = manager_client.get("/teams/MB/").content.decode()
    assert (
        '<a href="/">Home</a></li><li><a href="/teams/">Teams</a></li><li><span aria-current="page">MB</span>' in html
    )
    html = manager_client.get("/picks/order/").content.decode()
    assert '<a href="/picks/">Farm picks</a></li><li><span aria-current="page">Draft order</span>' in html


def test_league_grids_are_sortable_but_team_page_tables_are_not(manager_client):
    for url in ["/contracts/", "/buyouts/", "/farm/", "/picks/", "/cash/"]:
        assert 'class="grid"' in manager_client.get(url).content.decode(), url
    assert 'class="grid"' not in manager_client.get("/teams/MB/").content.decode()


def test_team_page_breakdown_adds_up_including_farm(manager_client):
    team = Team.objects.get(code="MB")
    farm = team.farm.filter(status="active").first()
    farm.salary_season = 2027  # a keep recorded for next season
    farm.save()
    page = manager_client.get("/teams/MB/")
    budget = page.context["budget"]
    assert budget.farm == farm.salary
    ledger = page.context["ledger"]
    assert len(ledger) == len(budget.lines)
    assert budget.base + budget.cash_net - budget.missed_ip - sum(line["amount"] for line in ledger) == budget.remaining
    html = page.content.decode()
    assert "Farm" in html and farm.player.name in html


def test_team_list_shows_farm_money(manager_client):
    assert b'<th class="num">Farm</th>' in manager_client.get("/teams/").content


def test_missed_ip_penalties_are_itemised(manager_client):
    from league.models import BudgetAdjustment

    team = Team.objects.get(code="MB")
    BudgetAdjustment.objects.create(team=team, season=2027, kind="missed_ip", amount=5, note="Week 12")
    html = manager_client.get("/teams/MB/").content.decode()
    assert "Week 12" in html and "$5" in html


def test_team_page_lists_past_cash_trades_too(manager_client):
    from league.models import CashTrade

    team = Team.objects.get(code="MB")
    past = (
        CashTrade.objects.filter(budget_season__lt=2027).filter(from_team=team).first()
        or CashTrade.objects.filter(budget_season__lt=2027, to_team=team).first()
    )
    assert past is not None
    assert f"{past.budget_season}: ${past.amount}" in manager_client.get("/teams/MB/").content.decode()


def test_ended_contracts_are_only_last_seasons(manager_client):
    team = Team.objects.get(code="MB")
    ended = manager_client.get("/teams/MB/").context["expired"]
    assert ended and all(c.final_year == 2026 for c in ended)
    Season.objects.create(year=2027, auction_starts_at="2027-02-24T19:00-05:00", started_at="2027-02-25T00:00-05:00")
    assert all(c.final_year == 2027 for c in manager_client.get("/teams/MB/").context["expired"])
    assert team


def test_impossible_buyout_is_refused():
    from django.core.exceptions import ValidationError

    call_command("import_league", verbosity=0)
    b = Buyout.objects.select_related("contract").first()
    b.dropped_in_season = b.contract.final_year  # a final-year drop is free, not a buyout
    with pytest.raises(ValidationError):
        b.full_clean()


def test_people_without_a_team_are_refused(league, client):
    client.force_login(User.objects.create_user("discord-999"))
    assert client.get("/contracts/").status_code == 403


def test_help_page_for_managers_hides_the_commissioner_guide(manager_client):
    html = manager_client.get("/help/").content.decode()
    assert "Signing in" in html and "Commissioner guide" not in html


def test_help_page_shows_commissioners_their_guide(league, client):
    client.force_login(User.objects.create_user("commish", is_staff=True))
    assert "Commissioner guide" in client.get("/help/").content.decode()


def test_help_needs_sign_in(client):
    assert client.get("/help/").status_code == 302


def test_team_page_lists_the_seasons_moves(manager_client):
    from league.tests.test_signing import settle_2026

    settle_2026()
    html = manager_client.get("/teams/SM/").content.decode()
    moves = html.split("Moves this season")[1].split("</ul>")[0]
    assert "dropped Spencer Torkelson (SM): buyout owed" in moves
    assert "claimed" not in moves  # claims change nothing, so they aren't listed


def test_team_page_shows_the_frozen_auction_budget(manager_client):
    Season.objects.create(year=2027, auction_starts_at="2027-02-24T19:00-05:00", started_at="2027-02-25T00:00-05:00")
    SeasonBudget.objects.create(
        season=2027,
        team=Team.objects.get(code="MB"),
        base=400,
        contracts=200,
        buyouts=10,
        farm=4,
        missed_ip=0,
        cash_net=5,
        remaining=191,
        frozen_at="2027-02-25T00:00-05:00",
    )
    html = manager_client.get("/teams/MB/").content.decode()
    assert "2027 auction budget: $191" in html
    assert "Contracts that ended" not in html  # the 2027 season is under way


def test_nav_marks_the_current_page(manager_client):
    html = manager_client.get("/teams/MB/").content.decode()
    sidebar = html[html.index('<nav class="sidebar"') : html.index("</nav>", html.index('<nav class="sidebar"'))]
    assert '<a href="/teams/" aria-current="page">Teams</a>' in sidebar
    assert sidebar.count('aria-current="page"') == 1


def test_permission_groups_are_hidden_from_the_admin():
    from django.contrib import admin
    from django.contrib.auth.models import Group

    assert not admin.site.is_registered(Group)


def test_contracts_have_no_unlabelled_column(manager_client):
    html = manager_client.get("/contracts/").content.decode()
    assert "<th></th>" not in html


def test_every_page_has_the_baseball_icon(manager_client, admin_client):
    assert "core/favicon.svg" in manager_client.get("/teams/").content.decode()
    assert "core/favicon.svg" in admin_client.get("/admin/").content.decode()
