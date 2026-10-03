"""Small helpers for the signing page and the admin."""

from playwright.sync_api import expect

from league.models import CashTrade, Team


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


def give_cash(sender, receiver, amount):
    """Cash for the 2027 auction, as a synced Discord post would make it (the admin refuses 2027 cash)."""
    CashTrade.objects.create(
        budget_season=2027, from_team=Team.objects.get(code=sender), to_team=Team.objects.get(code=receiver),
        amount=amount,
    )  # fmt: skip
