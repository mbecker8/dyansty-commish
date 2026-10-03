"""A team's signing decisions priced on the signing page: contract lengths, buyouts, farm keeps and the limits.

Issue #22 §5. Expected prices come from the rulebook arithmetic on what the page shows, not from the app's own
pricing code, so a pricing bug shows up as a failure.
"""

import re

import pytest
from playwright.sync_api import expect

from e2e.conftest import SEASON, manager, signable
from e2e.pages import browse_signing, give_cash, left_for_auction, panel_line, status_tag
from league import signing
from league.models import Contract, Team

pytestmark = pytest.mark.usefixtures("opened")


def test_every_contract_length_is_priced_by_the_rulebook(mb_page):
    page = browse_signing(mb_page, "MB")
    before = left_for_auction(page)
    name = signable("MB")[0].player.name
    row = page.get_by_role("row").filter(has=page.get_by_label(f"Contract for {name}"))
    salary = int(row.locator("td.num").inner_text().lstrip("$"))

    # 1 yr P, 2 yr P+5, 3 yr P+10, 4 yr P+15, 5+ yr P + $4 × years.
    expected = {1: salary, 2: salary + 5, 3: salary + 10, 4: salary + 15, 5: salary + 20, 7: salary + 28}
    expected[10] = salary + 40
    select = page.get_by_label(f"Contract for {name}")
    for years, price in expected.items():
        expect(select.locator(f'option[value="{years}"]')).to_have_text(f"{years} yr · ${price}/yr")
        select.select_option(str(years))
        expect(panel_line(page, "New contracts")).to_have_text(f"−${price}")
        expect(page.locator("#panel tfoot")).to_contain_text(f"${before - price}")

    select.select_option("")
    expect(panel_line(page, "New contracts")).to_have_text("−$0")
    expect(page.locator("#panel tfoot")).to_contain_text(f"${before}")


def test_signing_several_players_adds_up(mb_page):
    page = browse_signing(mb_page, "MB")
    before = left_for_auction(page)
    total = 0
    for p, years in zip(signable("MB")[:4], (1, 2, 3, 4), strict=True):
        page.get_by_label(f"Contract for {p.player.name}").select_option(str(years))
        total += p.entry.salary + {1: 0, 2: 5, 3: 10, 4: 15}[years]
        expect(panel_line(page, "New contracts")).to_have_text(f"−${total}")
    expect(page.locator("#panel")).to_contain_text("Contracts for 2027: 9 of 10")
    expect(page.locator("#panel tfoot")).to_contain_text(f"${before - total}")


def test_buyout_charges_80_then_70_percent_rounding_half_up(browse, league):
    # Hunter Goodman: $15 a year through 2028. 80% is $12; 70% is $10.50, which rounds up to $11.
    page = browse_signing(browse(manager("SM", "200")), "SM")
    before = left_for_auction(page)
    row = page.get_by_role("row").filter(has=page.get_by_label("Buy out Hunter Goodman"))
    expect(row).to_contain_text("$15")
    expect(row).to_contain_text("2027: $12 · 2028: $11")
    expect(row.locator("td.num").last).to_have_text("$23")

    page.get_by_label("Buy out Hunter Goodman").check()
    expect(panel_line(page, "Bought-out contracts' price")).to_have_text("+$15")
    expect(panel_line(page, "Buyout penalties")).to_have_text("−$12")
    expect(page.locator("#panel tfoot")).to_contain_text(f"${before + 15 - 12}")
    expect(page.locator("#panel")).to_contain_text("Contracts for 2027: 8 of 10")


def test_a_contract_in_its_final_year_drops_for_free(browse, league):
    # A contract that ended with 2026 isn't offered for buyout: it just ends, with no penalty.
    ended = Contract.live.filter(team__code="SM").select_related("player")
    ended = next(c for c in ended if c.final_year == SEASON)
    page = browse_signing(browse(manager("SM", "200")), "SM")
    expect(page.get_by_label(f"Buy out {ended.player.name}")).to_have_count(0)
    page.get_by_text("Rest of your roster").click()
    expect(page.get_by_role("row", name=re.compile(re.escape(ended.player.name)))).to_contain_text("Contract ended")


def test_farm_keep_costs_one_or_two_dollars_and_release_is_free(mb_page):
    farm = list(signing.farm_candidates(Team.objects.get(code="MB"), SEASON))
    minors = next(f for f in farm if not f.has_mlb_appearance)
    debuted = next(f for f in farm if f.has_mlb_appearance)
    page = browse_signing(mb_page, "MB")
    expect(page.locator("#panel")).to_contain_text("Still to decide")

    def row(f):
        return page.get_by_role("row").filter(has_text=f.player.name)

    expect(row(minors).locator("td.num").last).to_have_text(f"${minors.salary + 1}")
    expect(row(debuted).locator("td.num").last).to_have_text(f"${debuted.salary + 2}")
    for f in farm:
        choice = "Keep" if f in (minors, debuted) else "Release"
        row(f).get_by_label(choice).check()
    expect(panel_line(page, "Farm players kept")).to_have_text(f"−${minors.salary + 1 + debuted.salary + 2}")
    expect(page.locator("#panel")).not_to_contain_text("Still to decide")
    expect(page.locator("#panel")).to_contain_text("Ready to submit.")


def test_an_eleventh_contract_is_refused(browse, league):
    page = browse_signing(browse(manager("SM", "200")), "SM")
    expect(page.locator("#panel")).to_contain_text("Contracts for 2027: 9 of 10")
    first, second = signable("SM")[:2]
    page.get_by_label(f"Contract for {first.player.name}").select_option("1")
    expect(page.locator("#panel")).to_contain_text("Contracts for 2027: 10 of 10")
    expect(page.locator("#panel .errors")).to_have_count(0)

    page.get_by_label(f"Contract for {second.player.name}").select_option("1")
    expect(page.locator("#panel .errors")).to_contain_text("11 contracts for 2027; the limit is 10")
    page.get_by_role("button", name="Submit").click()
    expect(page.locator(".messages")).to_contain_text("not submitted")
    expect(status_tag(page)).to_have_text("Draft")


def test_budget_may_end_at_zero_but_not_below(mb_page):
    """A cash trade entered in the admin sets MB's budget so one signing leaves exactly $0; $1 more is refused."""
    player = signable("MB")[0]
    page = browse_signing(mb_page, "MB")
    for f in signing.farm_candidates(Team.objects.get(code="MB"), SEASON):
        page.get_by_role("row").filter(has_text=f.player.name).get_by_label("Release").check()
    expect(page.locator("#panel")).to_contain_text("Ready to submit.")
    page.get_by_role("button", name="Save draft").click()
    expect(page.locator(".messages")).to_contain_text("Draft saved.")
    before = left_for_auction(page)

    give_cash("MB", "JJ", before - player.entry.salary)
    page.reload()
    page.get_by_label(f"Contract for {player.player.name}").select_option("1")
    expect(page.locator("#panel tfoot")).to_contain_text("$0")
    expect(page.locator("#panel")).to_contain_text("Ready to submit.")
    page.get_by_role("button", name="Submit").click()
    expect(page.locator(".messages")).to_contain_text("Submitted.")
    page.get_by_role("button", name="Withdraw submission").click()
    expect(status_tag(page)).to_have_text("Draft")

    give_cash("MB", "JJ", 1)
    page.reload()
    expect(page.locator("#panel tfoot")).to_contain_text("$-1")
    expect(page.locator("#panel .errors")).to_contain_text("can't go below $0")
    page.get_by_role("button", name="Submit").click()
    expect(page.locator(".messages")).to_contain_text("not submitted")
    expect(status_tag(page)).to_have_text("Draft")
