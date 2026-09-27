"""What a plain manager can see and do while signing is open, and the commissioner editing a team. Issue #22 §6."""

import re

import pytest
from playwright.sync_api import expect

from e2e.conftest import SEASON, manager, signable
from e2e.pages import browse_signing, status_tag
from league import signing
from league.models import AuditEntry, Manager, Team

pytestmark = pytest.mark.usefixtures("opened")


def decide_farm(page, code, choice="Keep"):
    for f in signing.farm_candidates(Team.objects.get(code=code), SEASON):
        page.get_by_role("row").filter(has_text=f.player.name).get_by_label(choice).check()


def test_manager_sees_only_their_own_team(mb_page):
    page = mb_page
    page.goto("/")
    page.get_by_role("link", name="Signing").click()
    expect(page).to_have_url(re.compile(r"/signing/MB/$"))
    expect(page.get_by_role("heading", level=1)).to_contain_text("(MB)")
    expect(page.get_by_role("link", name="Commish", exact=True)).to_have_count(0)

    for url in ("/signing/SM/", "/commish/", "/commish/audit/"):
        response = page.goto(url)
        assert response.status == 403, url
    page.goto("/signing/SM/")
    expect(page.locator("main")).to_contain_text("private to that team")


def test_submit_is_blocked_until_every_farm_player_is_decided(mb_page):
    page = browse_signing(mb_page, "MB")
    page.get_by_label(f"Contract for {signable('MB')[0].player.name}").select_option("2")
    page.get_by_role("button", name="Submit").click()
    expect(page.locator(".messages")).to_contain_text("Saved as a draft, but not submitted")
    expect(status_tag(page)).to_have_text("Draft")
    expect(page.locator("#panel .todo")).to_contain_text("Still to decide")


def test_submit_withdraw_edit_and_resubmit(mb_page):
    first, second = signable("MB")[:2]
    page = browse_signing(mb_page, "MB")
    page.get_by_label(f"Contract for {first.player.name}").select_option("3")
    decide_farm(page, "MB")
    page.get_by_role("button", name="Submit").click()
    expect(page.locator(".messages")).to_contain_text("Submitted.")
    expect(status_tag(page)).to_have_text("Submitted")

    # Submitted decisions are read-only until withdrawn.
    expect(page.get_by_label(f"Contract for {first.player.name}")).to_be_disabled()
    expect(page.get_by_role("button", name="Save draft")).to_have_count(0)

    page.get_by_role("button", name="Withdraw submission").click()
    expect(page.locator(".messages")).to_contain_text("Back to a draft")
    expect(page.get_by_label(f"Contract for {first.player.name}")).to_have_value("3")

    page.get_by_label(f"Contract for {first.player.name}").select_option("1")
    page.get_by_label(f"Contract for {second.player.name}").select_option("2")
    page.get_by_role("button", name="Submit").click()
    expect(status_tag(page)).to_have_text("Submitted")
    expect(page.get_by_label(f"Contract for {first.player.name}")).to_have_value("1")
    expect(page.get_by_label(f"Contract for {second.player.name}")).to_have_value("2")

    actions = set(AuditEntry.objects.filter(team__code="MB").values_list("action", flat=True))
    assert {"Saved signing decisions", "Submitted signing", "Withdrew signing submission"} <= actions


def test_commissioner_edit_needs_a_note_and_a_broken_plan_goes_back_to_draft(mb_page, commish_page):
    page = browse_signing(mb_page, "MB")
    decide_farm(page, "MB")
    page.get_by_role("button", name="Submit").click()
    expect(status_tag(page)).to_have_text("Submitted")

    # MB has 5 contracts; six more makes 11.
    commish = browse_signing(commish_page, "MB")
    expect(commish.locator(".banner-inline")).to_contain_text("editing another team as commissioner")
    for p in signable("MB")[:6]:
        commish.get_by_label(f"Contract for {p.player.name}").select_option("1")
    expect(commish.locator("#panel .errors")).to_contain_text("11 contracts for 2027")
    commish.get_by_role("button", name="Save draft").click()
    expect(commish.locator(".messages")).to_contain_text("Add a note")
    expect(status_tag(commish)).to_have_text("Submitted")

    for p in signable("MB")[:6]:
        commish.get_by_label(f"Contract for {p.player.name}").select_option("1")
    commish.get_by_label("Note (required)").fill("e2e: per Discord DM")
    commish.get_by_role("button", name="Save draft").click()
    expect(commish.locator(".messages")).to_contain_text("back to a draft")

    page.reload()
    expect(status_tag(page)).to_have_text("Draft")
    expect(page.locator("#panel .errors")).to_contain_text("11 contracts for 2027")
    assert AuditEntry.objects.filter(team__code="MB", action="Signing submission back to draft").exists()
    assert AuditEntry.objects.filter(team__code="MB", note="e2e: per Discord DM").exists()


def test_unticking_is_commissioner_takes_admin_away_at_once(browse, commish_page):
    me = browse(manager("MB", "100", commissioner=True))
    me.goto("/teams/")
    expect(me.get_by_role("link", name="Commish", exact=True)).to_be_visible()
    expect(me.get_by_role("link", name="Admin")).to_be_visible()

    def set_flag(on):
        commish_page.goto(f"/admin/league/manager/{Manager.objects.get(discord_id='100').pk}/change/")
        commish_page.get_by_label("Is commissioner").set_checked(on)
        commish_page.get_by_role("button", name="Save", exact=True).click()
        expect(commish_page.locator(".messagelist")).to_contain_text("successfully")

    set_flag(False)
    me.goto("/teams/")
    expect(me.get_by_role("link", name="Commish", exact=True)).to_have_count(0)
    assert me.goto("/commish/").status == 403
    assert me.goto("/signing/SM/").status == 403
    assert me.goto("/admin/").url.endswith("/admin/login/?next=/admin/")

    set_flag(True)
    me.goto("/teams/")
    expect(me.get_by_role("link", name="Commish", exact=True)).to_be_visible()
    assert me.goto("/signing/SM/").status == 200
