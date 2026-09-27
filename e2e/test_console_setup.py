"""Loading the league from the console on an empty database, the way production starts."""

from importlib import import_module

from django.apps import apps
from django.contrib.auth.models import User
from playwright.sync_api import expect

from league.models import Team


def test_load_league_button_loads_the_league(browse):
    # The flush after each transactional test also removes the rows the data migrations seed, so seed them again.
    for migration in ("0009_seed_2026_fantrax_league", "0010_season"):
        import_module(f"league.migrations.{migration}").seed(apps, None)
    console = browse(User.objects.create_superuser("commish", password="not-used"))
    console.goto("/commish/")
    console.get_by_role("button", name="Load league").click()
    expect(console.locator(".messages")).to_contain_text("The league is loaded")
    assert Team.objects.count() == 14
