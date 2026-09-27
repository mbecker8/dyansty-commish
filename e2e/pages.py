"""Small helpers for the signing page and the admin."""

from playwright.sync_api import expect

from league.models import Team


def browse_signing(page, code):
    page.goto(f"/signing/{code}/")
    expect(page.get_by_role("heading", level=1)).to_contain_text("signing")
    return page


def panel_line(page, label):
    """The amount cell of one line of the budget panel on the right."""
    return page.locator("#panel tr").filter(has_text=label).locator("td.num")


def left_for_auction(page) -> int:
    return int(page.locator("#panel tfoot td.num").inner_text().replace("$", ""))


def status_tag(page):
    """Not started, Draft or Submitted, at the top of the signing page."""
    return page.locator("p.muted .tag").first


def team_label(code):
    """How a team appears in the admin's dropdowns."""
    return str(Team.objects.get(code=code))


def admin_saved(page):
    expect(page.locator(".messagelist")).to_contain_text("successfully")


def add_cash_trade(page, sender, receiver, amount, tx_id):
    page.goto("/admin/league/cashtrade/add/")
    page.get_by_label("Budget season").select_option("2027")
    page.get_by_label("From team").select_option(label=team_label(sender))
    page.get_by_label("To team").select_option(label=team_label(receiver))
    page.get_by_label("Amount").fill(str(amount))
    page.get_by_label("Fantrax tx id").fill(tx_id)
    page.get_by_role("button", name="Save", exact=True).click()
    admin_saved(page)
