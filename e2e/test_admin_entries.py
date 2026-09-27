"""The commissioner's manual entries in the admin, and what they change on the league pages. Issue #22 §5."""

import pytest
from playwright.sync_api import expect

from e2e.conftest import SEASON
from e2e.pages import add_cash_trade, admin_saved, browse_signing, panel_line, team_label
from league.models import AuditEntry, FarmPick, FarmPlayer, Player

pytestmark = pytest.mark.usefixtures("opened")


def left_before_signings(page, code):
    page.goto(f"/teams/{code}/")
    return int(page.locator(".summary div").filter(has_text="Left before signings").locator("strong").inner_text()[1:])


def test_cash_trade_moves_both_budgets(commish_page):
    page = commish_page
    mb, jj = left_before_signings(page, "MB"), left_before_signings(page, "JJ")
    add_cash_trade(page, "MB", "JJ", 7, tx_id="e2e-cash")
    assert left_before_signings(page, "MB") == mb - 7
    assert left_before_signings(page, "JJ") == jj + 7
    expect(page.get_by_text("2027: $7 from MB")).to_be_visible()
    assert AuditEntry.objects.filter(action="Admin: added cash trade", team__code="JJ").exists()


def test_missed_ip_penalty_comes_off_the_budget(commish_page):
    page = commish_page
    before = left_before_signings(page, "MB")
    page.goto("/admin/league/budgetadjustment/add/")
    page.get_by_label("Team").select_option(label=team_label("MB"))
    page.get_by_label("Season").select_option("2027")
    page.get_by_label("Kind").select_option(label="Missed IP penalty")
    page.get_by_label("Amount").fill("10")
    page.get_by_label("Note").fill("e2e: 2026 innings short")
    page.get_by_role("button", name="Save", exact=True).click()
    admin_saved(page)

    assert left_before_signings(page, "MB") == before - 10
    expect(page.locator(".summary")).to_contain_text("Missed IP$10")
    page.get_by_text("How it adds up").click()
    expect(page.get_by_role("row", name="Missed IP penalty e2e: 2026 innings short $10")).to_be_visible()


def test_farm_pick_trade_changes_the_owner(commish_page):
    page = commish_page
    pick = FarmPick.objects.filter(original_team__code="MB", owner__code="MB").order_by("year", "round").first()
    page.goto(f"/admin/league/farmpick/{pick.pk}/change/")
    page.get_by_label("Owner").select_option(label=team_label("JJ"))
    page.get_by_role("button", name="Save", exact=True).click()
    admin_saved(page)

    page.goto("/teams/JJ/")
    expect(page.get_by_text(f"{pick.year} round {pick.round} (from MB)")).to_be_visible()
    page.goto("/picks/")
    expect(page.locator("main")).to_contain_text("JJ")


def test_added_farm_player_shows_up_to_keep_or_release(commish_page, mb_page):
    player = Player.objects.exclude(roster_entries__season=SEASON).exclude(farm_stints__isnull=False).first()
    page = commish_page
    page.goto("/admin/league/farmplayer/add/")
    page.get_by_label("Team").select_option(label=team_label("MB"))
    page.get_by_label("Player").select_option(label=str(player))
    page.get_by_label("Drafted year").select_option("2026")
    page.locator("#id_salary").fill("2")
    page.get_by_label("Salary season").select_option("2026")
    page.get_by_role("button", name="Save", exact=True).click()
    admin_saved(page)
    assert FarmPlayer.objects.filter(team__code="MB", player=player, status="active").exists()

    page = browse_signing(mb_page, "MB")
    row = page.get_by_role("row").filter(has_text=player.name)
    expect(row.locator("td.num").last).to_have_text("$3")
    expect(page.locator("#panel")).to_contain_text(player.name)  # still to decide
    row.get_by_label("Keep").check()
    expect(page.locator("#panel")).not_to_contain_text(player.name)
    expect(panel_line(page, "Farm players kept")).to_have_text("−$3")  # $2 + $1, no MLB time
