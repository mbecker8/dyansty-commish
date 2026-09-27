"""The whole signing period from the commissioner console: open, decide, lock, then the next season's dates.

Issue #22 §5 (open), §7 (lock) and §8 (after lock).
"""

import csv
import io
import re

from playwright.sync_api import expect

from e2e.conftest import SEASON, decide_farms, signable
from e2e.pages import browse_signing, left_for_auction, panel_line, status_tag
from league.models import AuditEntry, Contract, SigningPeriod, Team


def csv_rows(page, name):
    response = page.request.get(f"/export/{name}.csv")
    assert response.ok and response.headers["content-type"] == "text/csv"
    return list(csv.DictReader(io.StringIO(response.text())))


def test_open_decide_lock_and_publish(league, commish_page, mb_page):
    console = commish_page
    console.goto("/commish/")
    expect(console.locator("main")).to_contain_text("Not open yet")
    mb_page.goto("/signing/MB/")
    expect(mb_page.locator("main")).to_contain_text("isn't open yet")

    console.get_by_role("button", name="Open signing").click()
    expect(console.locator(".messages")).to_contain_text("Signing is open")
    expect(console.get_by_role("row").filter(has=console.locator('a[href="/signing/MB/"]'))).to_contain_text(
        "Not started"
    )

    # MB decides through the signing page: a 3-year contract, a buyout and every farm player kept.
    player = signable("MB")[0]
    bought_out = Contract.live.filter(team__code="MB", player__name="Royce Lewis").get()
    page = browse_signing(mb_page, "MB")
    page.get_by_label(f"Contract for {player.player.name}").select_option("3")
    page.get_by_label(f"Buy out {bought_out.player.name}").check()
    for row in page.get_by_role("row").filter(has=page.get_by_label("Keep")).all():
        row.get_by_label("Keep").check()
    expect(page.locator("#panel")).to_contain_text("Ready to submit.")
    expect(panel_line(page, "Buyout penalties")).to_have_text("−$15")  # 80% of $19, rounded half up
    mb_budget = left_for_auction(page)
    page.get_by_role("button", name="Submit").click()
    expect(status_tag(page)).to_have_text("Submitted")

    # Twelve more teams keep every farm player; AG hasn't decided yet, so lock is refused.
    others = [t.code for t in Team.objects.exclude(code__in=["MB", "AG"])]
    decide_farms(*others)
    console.goto("/commish/")
    expect(console.locator("main")).to_contain_text("Some teams still have problems")
    expect(console.locator("main")).to_contain_text(re.compile(r"Not submitted: .*AG"))
    console.get_by_label("Apply every team's decisions and lock").check()
    console.get_by_role("button", name="Lock signing").click()
    expect(console.locator(".messages")).to_contain_text("Not ready to lock: AG")

    # The commissioner decides AG's farm on its behalf, with a note.
    console.locator('a[href="/signing/AG/"]').click()
    for row in console.get_by_role("row").filter(has=console.get_by_label("Release")).all():
        row.get_by_label("Release").check()
    console.get_by_label("Note (required)").fill("e2e: AG released everyone on Discord")
    console.get_by_role("button", name="Save draft").click()
    expect(console.locator(".messages")).to_contain_text("Draft saved.")

    # Locking needs the confirmation box.
    console.goto("/commish/")
    console.get_by_role("button", name="Lock signing").click()
    expect(console.locator(".messages")).to_contain_text("Tick the box")
    assert SigningPeriod.objects.get(season=SEASON).status == "open"

    console.get_by_label("Apply every team's decisions and lock").check()
    console.get_by_label("Note (optional)").fill("e2e: all 14 in")
    console.get_by_role("button", name="Lock signing").click()
    expect(console.locator(".messages")).to_contain_text("Signing is locked")
    expect(console.locator("main")).to_contain_text("Locked")

    # Published: the new contract, the buyout, AG's empty farm, and MB's budget as previewed.
    mb_page.goto("/signing/MB/")
    expect(mb_page.locator("main")).to_contain_text("Signing is locked")
    mb_page.goto("/teams/MB/")
    expect(mb_page.locator(".summary")).to_contain_text(f"Auction budget${mb_budget}")
    expect(mb_page.locator("main")).to_contain_text(player.player.name)
    mb_page.goto("/buyouts/")
    expect(mb_page.get_by_role("row").filter(has_text="Royce Lewis")).to_contain_text("MB")
    mb_page.goto("/teams/AG/")
    farm_section = mb_page.locator('h2:text-is("Farm") + .table-wrap')
    expect(farm_section).to_have_count(1)
    expect(farm_section).not_to_contain_text("Konnor Griffin")

    budgets = {r["team"]: r for r in csv_rows(mb_page, "budgets")}
    assert int(budgets["MB"]["remaining"]) == mb_budget
    signed = [r for r in csv_rows(mb_page, "contracts") if r["player"] == player.player.name]
    assert signed == [signed[0]] and signed[0]["team"] == "MB" and signed[0]["length"] == "3"
    assert int(signed[0]["per_year"]) == player.entry.salary + 10
    assert "AG" not in {r["team"] for r in csv_rows(mb_page, "farm")}

    # Everything is in the audit log.
    console.goto("/commish/audit/")
    for action in (
        "Opened signing",
        "Submitted signing",
        "Saved signing decisions",
        "Applied signing",
        "Locked signing",
    ):
        expect(console.locator("main")).to_contain_text(action)
    expect(console.locator("main")).to_contain_text("e2e: AG released everyone on Discord")
    expect(console.locator("main")).to_contain_text("e2e: all 14 in")
    assert AuditEntry.objects.filter(action="Applied signing").count() == 14

    # §8: enter the 2027 draft-day dates. The checklist lists what's still missing; don't start the season.
    console.goto("/commish/")
    console.get_by_label("Farm draft starts").fill("2027-02-20T18:00")
    console.get_by_label("Auction starts").fill("2027-02-24T19:00")
    console.get_by_role("button", name="Save dates").click()
    expect(console.locator(".messages")).to_contain_text("Season dates saved.")
    checklist = console.locator("ul.checklist")
    expect(checklist.locator("li.ok")).to_contain_text(["Signing after 2026 is locked"])
    expect(checklist.locator("li.todo").first).to_contain_text("auction start is entered and has passed")
    expect(console.get_by_role("button", name="Start the 2027 season")).to_be_visible()
