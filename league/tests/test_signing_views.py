"""The signing screen, commissioner console, audit log and export, through the web."""

import csv
import io
from datetime import datetime

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from league import seasons, signing
from league.budget import team_budget
from league.fantrax_data import EASTERN
from league.models import (
    AuditEntry,
    Buyout,
    CashTrade,
    Contract,
    FantraxEvent,
    FantraxLeague,
    Manager,
    Season,
    SigningPeriod,
    Submission,
    Team,
)
from league.tests.test_signing import SNAPSHOT, settle_2026

pytestmark = pytest.mark.django_db
SEASON = 2026


@pytest.fixture
def ready():
    call_command("import_league", verbosity=0)
    settle_2026()
    call_command("sync_rosters", verbosity=0)


@pytest.fixture
def opened(ready):
    signing.open_period(SEASON, None)


def manager_login(code, discord_id, commissioner=False):
    user = User.objects.create_user(f"discord-{discord_id}")
    Manager.objects.create(
        team=Team.objects.get(code=code), name=code, discord_id=discord_id, user=user, is_commissioner=commissioner
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.fixture
def mb():
    return manager_login("MB", "100")


@pytest.fixture
def commish():
    client = manager_login("SM", "200", commissioner=True)
    client.get("/teams/")  # the middleware grants admin rights on the first page
    return client


def form_for(code, signings=(), buyouts=(), farm="keep", **extra):
    """The signing form. farm: "keep" or "release" for every farm player, or None to leave them undecided."""
    team = Team.objects.get(code=code)
    data = {f"sign-{p.player.pk}": str(n) for p, n in signings}
    data["buyout"] = [str(c.pk) for c in buyouts]
    if farm:
        data |= {f"farm-{f.pk}": farm for f in signing.farm_candidates(team, SEASON)}
    return data | extra


def signable(code):
    return [p for p in signing.pool(Team.objects.get(code=code), SEASON) if p.can_sign]


def submission(code):
    return Submission.objects.filter(team__code=code).first()


def test_signing_is_closed_until_the_commissioner_opens_it(ready, mb):
    page = mb.get("/signing/MB/")
    assert page.status_code == 200 and b"isn't open yet" in page.content
    assert b"My Signing Worksheet" not in mb.get("/teams/").content


def test_worksheet_button_shows_how_far_the_team_has_got(opened, mb):
    assert "not started" in mb.get("/teams/").content.decode()
    mb.post("/signing/MB/", form_for("MB", action="save"))
    assert "Draft saved, not submitted" in mb.get("/teams/").content.decode()
    mb.post("/signing/MB/", form_for("MB", action="submit"))
    assert "✓ Submitted" in mb.get("/teams/").content.decode()


def test_manager_sees_their_signing_page_and_the_nav_link(opened, mb):
    page = mb.get("/signing/MB/")
    assert page.status_code == 200
    html = page.content.decode()
    assert signable("MB")[0].player.name in html and "Left for the auction" in html
    assert "My Signing Worksheet" in html and "not started" in html
    assert mb.get("/signing/")["Location"] == "/signing/MB/"


def test_drafts_are_private_to_the_team(opened, mb):
    assert mb.get("/signing/SM/").status_code == 403
    assert mb.post("/signing/SM/preview", {}).status_code == 403
    assert mb.post("/signing/SM/", form_for("SM", action="save")).status_code == 403
    assert submission("SM") is None


def test_preview_prices_the_form_without_saving(opened, mb):
    p = signable("MB")[0]
    response = mb.post("/signing/MB/preview", form_for("MB", signings=[(p, 2)]))
    assert response.status_code == 200
    ev = response.context["ev"]
    assert ev.new_contract_total == p.prices[2]
    assert f"${ev.budget.remaining}" in response.content.decode()
    assert submission("MB") is None


def test_save_submit_withdraw(opened, mb):
    p = signable("MB")[0]
    mb.post("/signing/MB/", form_for("MB", signings=[(p, 3)], action="save"))
    sub = submission("MB")
    assert sub.status == "draft" and sub.signings.get().length == 3

    mb.post("/signing/MB/", form_for("MB", signings=[(p, 2)], action="submit"))
    sub.refresh_from_db()
    assert sub.status == "submitted" and sub.signings.get().length == 2 and sub.submitted_by is not None

    mb.post("/signing/MB/", form_for("MB", action="save"))  # refused while submitted
    assert sub.signings.get().length == 2

    mb.post("/signing/MB/", {"action": "withdraw"})
    sub.refresh_from_db()
    assert sub.status == "draft"
    actions = list(AuditEntry.objects.filter(team__code="MB").values_list("action", flat=True))
    assert {"Saved signing decisions", "Submitted signing", "Withdrew signing submission"} <= set(actions)


def test_incomplete_submission_stays_a_draft(opened, mb):
    p = signable("MB")[0]
    response = mb.post("/signing/MB/", form_for("MB", signings=[(p, 1)], farm=None, action="submit"), follow=True)
    assert submission("MB").status == "draft"
    assert b"not submitted" in response.content


def test_another_teams_player_is_not_saved(opened, mb):
    theirs = signable("SM")[0]
    mb.post("/signing/MB/", {f"sign-{theirs.player.pk}": "1", "action": "save"})
    assert not submission("MB").signings.exists()


def test_commissioner_needs_a_note_to_change_another_team(opened, commish):
    p = signable("MB")[0]
    commish.post("/signing/MB/", form_for("MB", signings=[(p, 1)], action="save"))
    assert submission("MB") is None
    commish.post("/signing/MB/", form_for("MB", signings=[(p, 1)], action="save", note="Per Discord DM"))
    assert submission("MB").signings.exists()
    assert AuditEntry.objects.filter(team__code="MB", note="Per Discord DM").exists()


def test_commissioner_can_edit_a_submitted_team(opened, mb, commish):
    mb.post("/signing/MB/", form_for("MB", action="submit"))
    assert submission("MB").status == "submitted"
    p = signable("MB")[0]
    commish.post("/signing/MB/", form_for("MB", signings=[(p, 1)], action="save", note="fix"))
    assert submission("MB").signings.exists() and submission("MB").status == "submitted"


def test_console_is_for_commissioners(opened, mb, commish):
    assert mb.get("/commish/").status_code == 403
    assert mb.get("/commish/audit/").status_code == 403
    page = commish.get("/commish/")
    assert page.status_code == 200 and len(page.context["rows"]) == 14


def test_console_opens_and_locks(ready, commish):
    page = commish.get("/commish/")
    assert b"Open signing" in page.content
    commish.post("/commish/", {"action": "open"})
    assert SigningPeriod.objects.get(season=SEASON).status == "open"

    commish.post("/commish/", {"action": "lock", "confirm": "yes"})  # farm undecided everywhere
    assert SigningPeriod.objects.get(season=SEASON).status == "open"

    for t in Team.objects.all():
        sub = Submission.objects.create(period=SigningPeriod.objects.get(season=SEASON), team=t)
        signing.save_plan(sub, signing.Plan(farm={f.pk: True for f in signing.farm_candidates(t, SEASON)}))
    commish.post("/commish/", {"action": "lock"})  # no confirmation
    assert SigningPeriod.objects.get(season=SEASON).status == "open"
    response = commish.post("/commish/", {"action": "lock", "confirm": "yes", "note": "all in"}, follow=True)
    assert SigningPeriod.objects.get(season=SEASON).status == "locked"
    assert b"Locked" in response.content
    assert AuditEntry.objects.filter(action="Locked signing", note="all in").exists()


@pytest.fixture
def unsettled():
    call_command("import_league", verbosity=0)
    call_command("sync_fantrax", snapshot=str(SNAPSHOT), verbosity=0)
    call_command("sync_rosters", verbosity=0)


def test_console_open_refuses_with_unresolved_exceptions(unsettled, commish):
    response = commish.post("/commish/", {"action": "open"}, follow=True)
    assert b"unresolved" in response.content
    assert not SigningPeriod.objects.filter(status="open").exists()


def test_audit_log_lists_changes(opened, mb, commish):
    mb.post("/signing/MB/", form_for("MB", action="save"))
    page = commish.get("/commish/audit/?team=MB")
    assert b"Saved signing decisions" in page.content


def test_admin_changes_are_audited(ready, commish):
    trade = CashTrade.objects.first()
    commish.post(
        f"/admin/league/cashtrade/{trade.pk}/change/",
        {
            "budget_season": trade.budget_season,
            "from_team": trade.from_team_id,
            "to_team": trade.to_team_id,
            "amount": trade.amount + 1,
            "note": trade.note,
            "fantrax_tx_id": trade.fantrax_tx_id,
        },
    )
    entries = AuditEntry.objects.filter(action="Admin: changed cash trade")
    assert {e.team_id for e in entries} == {trade.from_team_id, trade.to_team_id}
    assert all("amount" in e.detail for e in entries)


def test_two_teams_can_swap_final_places_in_the_admin_list(ready, commish):
    from league.models import FinalStanding

    rows = list(FinalStanding.objects.filter(season=2026).order_by("place"))
    places = {r.team.code: r.place for r in rows}
    swapped = {**places, "CS": places["CN"], "CN": places["CS"]}
    data = {
        "form-TOTAL_FORMS": len(rows),
        "form-INITIAL_FORMS": len(rows),
        "form-MIN_NUM_FORMS": 0,
        "form-MAX_NUM_FORMS": 1000,
        "_save": "Save",
    }
    for i, r in enumerate(rows):
        data[f"form-{i}-id"] = r.pk
        data[f"form-{i}-place"] = swapped[r.team.code]
    response = commish.post("/admin/league/finalstanding/?season__exact=2026", data)
    assert response.status_code == 302
    now = {r.team.code: r.place for r in FinalStanding.objects.filter(season=2026)}
    assert now == swapped and now["CS"] == 9 and now["CN"] == 8
    assert AuditEntry.objects.filter(action="Admin: changed final standing").count() == 2


def test_a_shared_place_is_flagged_on_the_draft_order(ready, commish):
    from league.models import FinalStanding

    FinalStanding.objects.filter(season=2026, team__code="CN").update(place=8)
    html = commish.get("/picks/order/?year=2027").content.decode()
    assert "Two teams share place 8 in the 2026 standings" in html


@pytest.mark.parametrize("name", ["budgets", "contracts", "buyouts", "farm", "picks", "cash"])
def test_exports(ready, mb, name):
    response = mb.get(f"/export/{name}.csv")
    assert response.status_code == 200 and response["Content-Type"] == "text/csv"
    rows = list(csv.reader(io.StringIO(response.content.decode())))
    assert len(rows) > 1


def test_budget_export_matches_the_rules_engine(ready, mb):
    rows = list(csv.DictReader(io.StringIO(mb.get("/export/budgets.csv").content.decode())))
    assert len(rows) == 14
    for row in rows:
        assert int(row["remaining"]) == team_budget(Team.objects.get(code=row["team"]), 2027).remaining


def test_export_needs_sign_in(ready, client):
    assert client.get("/export/budgets.csv").status_code == 302
    assert client.get("/export/").status_code == 302


def test_unknown_export_is_404(ready, mb):
    assert mb.get("/export/secrets.csv").status_code == 404


def test_team_pages_say_auction_budget_once_locked(opened, mb):
    assert b"Left before signings" in mb.get("/teams/MB/").content
    for t in Team.objects.all():
        sub = Submission.objects.create(period=SigningPeriod.objects.get(season=SEASON), team=t)
        signing.save_plan(sub, signing.Plan(farm={f.pk: True for f in signing.farm_candidates(t, SEASON)}))
    signing.lock_period(SEASON, None)
    for url in ("/teams/MB/", "/teams/"):
        html = mb.get(url).content
        assert b"Left before signings" not in html and b"Auction budget" in html
    assert b"Signing is locked" in mb.get("/signing/MB/").content


def test_save_after_lock_is_refused(opened, mb):
    """A request that passed the view's open check just before the lock re-checks under the row lock."""
    from django.test import RequestFactory

    from league import signing_views

    for t in Team.objects.all():
        sub = Submission.objects.create(period=SigningPeriod.objects.get(season=SEASON), team=t)
        signing.save_plan(sub, signing.Plan(farm={f.pk: True for f in signing.farm_candidates(t, SEASON)}))
    signing.lock_period(SEASON, None)
    request = RequestFactory().post("/signing/MB/", form_for("MB", signings=[(signable("MB")[0], 1)], action="save"))
    request.user = User.objects.get(username="discord-100")
    _, text = signing_views._apply_post(request, Team.objects.get(code="MB"), False, "")
    assert "isn't open" in text
    assert not submission("MB").signings.exists()


def test_commissioner_edit_that_breaks_a_submission_makes_it_a_draft(opened, mb, commish):
    mb.post("/signing/MB/", form_for("MB", action="submit"))
    assert submission("MB").status == "submitted"
    commish.post("/signing/MB/", form_for("MB", farm=None, action="save", note="dropping farm calls"))
    assert submission("MB").status == "draft"
    assert AuditEntry.objects.filter(action="Signing submission back to draft", team__code="MB").exists()


def test_withdrawing_a_draft_says_so(opened, mb):
    mb.post("/signing/MB/", form_for("MB", action="save"))
    response = mb.post("/signing/MB/", {"action": "withdraw"}, follow=True)
    assert b"already a draft" in response.content


def test_roster_salary_is_locked_once_signing_opens(opened, commish):
    from league.models import RosterEntry

    entry = RosterEntry.objects.exclude(salary=None).first()
    page = commish.get(f"/admin/league/rosterentry/{entry.pk}/change/").content.decode()
    assert 'name="salary"' not in page
    RosterEntry.objects.filter(pk=entry.pk).update(salary=None)
    page = commish.get(f"/admin/league/rosterentry/{entry.pk}/change/").content.decode()
    assert 'name="salary"' in page


def year_options(page, name):
    return page.split(f'name="{name}"')[1].split("</select>")[0]


def test_year_fields_are_dropdowns_from_this_year(ready, commish):
    import datetime

    this_year = datetime.date.today().year
    options = year_options(commish.get("/admin/league/budgetadjustment/add/").content.decode(), "season")
    assert options.index(f'value="{this_year}"') < options.index(f'value="{this_year + 1}"')
    assert f'value="{this_year - 1}"' not in options
    # Cash for 2027 on comes from Discord, so hand entry offers the auctions up to it (2027 says so).
    options = year_options(commish.get("/admin/league/cashtrade/add/").content.decode(), "budget_season")
    assert 'value="2026"' in options and 'value="2027"' in options and 'value="2028"' not in options
    options = year_options(commish.get("/admin/league/contract/add/").content.decode(), "year_signed")
    assert options.index(f'value="{this_year}"') < options.index(f'value="{this_year - 1}"')


def test_year_dropdown_keeps_an_older_year(ready, commish):
    contract = Contract.objects.order_by("year_signed").first()
    page = commish.get(f"/admin/league/contract/{contract.pk}/change/").content.decode()
    assert f'value="{contract.year_signed}" selected' in year_options(page, "year_signed")


def test_console_sync_finds_nothing_new(ready, commish, monkeypatch, settings):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession())
    response = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"No new events" in response.content  # ready already applied the same data


def test_console_sync_reports_an_expired_login(ready, commish, monkeypatch, settings):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession(logged_in=False))
    response = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"Fantrax login expired" in response.content


def test_console_lists_and_resolves_exceptions(unsettled, commish):
    e = FantraxEvent.objects.unresolved().get(player_name="Brendan Lawson")
    assert b"Brendan Lawson is in" in commish.get("/commish/").content
    commish.post("/commish/", {"action": "resolve", "event": e.pk, "note": ""})
    e.refresh_from_db()
    assert e.resolved_at is None  # a note is required
    commish.post("/commish/", {"action": "resolve", "event": e.pk, "note": "added him"})
    e.refresh_from_db()
    assert e.resolved_note == "added him"
    assert AuditEntry.objects.filter(action="Resolved Fantrax exception", note="added him").exists()


def test_find_and_add_the_renewed_league(ready, commish, monkeypatch, settings):
    from league import fantrax_client

    monkeypatch.setattr(
        fantrax_client,
        "list_leagues",
        lambda secret: [
            {"leagueId": "p3z8zy75mgdm460o", "leagueName": "Dynasty Yr 19"},
            {"leagueId": "newone", "leagueName": "Dynasty Yr 20"},
        ],
    )
    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "league_season", lambda session, league_id: 2027)
    found = commish.get("/commish/?find_leagues=1").content.decode().split("Find new Fantrax leagues")[1]
    assert "newone" in found and "p3z8zy75mgdm460o" not in found
    # The button names the league's own Fantrax year; its offseason moves still belong to the current season.
    assert "Add the 2027 league" in found
    commish.post("/commish/", {"action": "add_league", "league_id": "newone", "name": "Dynasty Yr 20"})
    added = commish.post(
        "/commish/", {"action": "add_league", "league_id": "newone", "name": "Dynasty Yr 20"}, follow=True
    ).content.decode()
    assert FantraxLeague.objects.get(league_id="newone").season == SEASON
    # The console names leagues without a year: the stored season isn't the league's Fantrax year.
    assert "Dynasty Yr 20 is added" in added and "Dynasty Yr 19, Dynasty Yr 20" in added
    assert f"({SEASON})" not in added


def test_found_league_is_still_offered_when_its_year_cant_be_read(ready, commish, monkeypatch, settings):
    from league import fantrax_client

    def expired(session, league_id):
        raise fantrax_client.FantraxLoginExpired()

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "list_leagues", lambda secret: [{"leagueId": "newone", "leagueName": "Yr 20"}])
    monkeypatch.setattr(fantrax_client, "league_season", expired)
    assert b"Add this league" in commish.get("/commish/?find_leagues=1").content


@pytest.fixture
def imported():
    call_command("import_league", verbosity=0)


def test_clicking_sync_twice_applies_events_once(imported, commish, monkeypatch, settings):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    settings.FANTRAX_COOKIE = "a=1"
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession())
    first = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"11 buyout" in first.content
    n, buyouts = FantraxEvent.objects.count(), Buyout.objects.count()
    second = commish.post("/commish/", {"action": "sync"}, follow=True)
    assert b"No new events" in second.content
    assert (FantraxEvent.objects.count(), Buyout.objects.count()) == (n, buyouts)


def test_adding_a_league_needs_an_id(ready, commish):
    response = commish.post("/commish/", {"action": "add_league", "league_id": " "}, follow=True)
    assert b"Pick a Fantrax league" in response.content
    assert FantraxLeague.objects.count() == 1


def test_console_sets_dates_and_starts_the_season(ready, commish):
    r = commish.post(
        "/commish/",
        {"action": "season_dates", "farm_draft_starts_at": "2027-02-20T18:00", "auction_starts_at": "2027-02-24T19:00"},
        follow=True,
    )
    s = Season.objects.get(year=2027)
    assert s.auction_starts_at == datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)
    assert "Start the 2027 season" in r.content.decode()
    r = commish.post("/commish/", {"action": "start_season", "year": "2027"}, follow=True)
    assert "Not ready" in r.content.decode() and seasons.current_season() == 2026


def test_manager_cannot_start_a_season(ready, mb):
    assert mb.post("/commish/", {"action": "season_dates", "auction_starts_at": "2027-02-24T19:00"}).status_code == 403
    assert not Season.objects.filter(year=2027).exists()
