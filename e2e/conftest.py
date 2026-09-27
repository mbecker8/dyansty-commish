"""Browser tests: the league after the 2026 season, served by a live server and driven with Playwright.

Run with `uv run pytest e2e` (once: `uv run playwright install chromium`). Add `--headed` to watch.
"""

import os

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from league import signing
from league.models import Manager, SigningPeriod, Submission, Team
from league.tests.test_signing import settle_2026

# Playwright's sync API runs an event loop in this thread; the ORM calls the tests make are still synchronous.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

SEASON = 2026


def pytest_collection_modifyitems(items):
    # Transactional, so the live server's thread sees each test's data. Serialized rollback puts back
    # the rows the migrations seed (the 2026 season), which the flush after each test removes.
    for item in items:
        item.add_marker(pytest.mark.django_db(transaction=True, serialized_rollback=True))


@pytest.fixture
def league(live_server):
    """The league after the 2026 season: events applied, rosters loaded, signing not open yet."""
    call_command("import_league", verbosity=0)
    settle_2026()
    call_command("sync_rosters", verbosity=0)


@pytest.fixture
def opened(league):
    signing.open_period(SEASON, None)


@pytest.fixture
def browse(browser, live_server):
    """browse(user) -> a page signed in as that user, in its own browser context (a private window each)."""
    contexts = []

    def open_as(user):
        client = Client()
        client.force_login(user)
        context = browser.new_context(base_url=live_server.url)
        context.add_cookies([{"name": "sessionid", "value": client.cookies["sessionid"].value, "url": live_server.url}])
        contexts.append(context)
        return context.new_page()

    yield open_as
    for context in contexts:
        context.close()


def manager(code, discord_id, commissioner=False):
    """A Discord account that manages the team, the way sign-in with Discord creates it."""
    user = User.objects.create_user(f"discord-{discord_id}")
    Manager.objects.create(
        team=Team.objects.get(code=code), name=f"{code} manager", discord_id=discord_id, user=user,
        is_commissioner=commissioner,
    )  # fmt: skip
    return user


@pytest.fixture
def mb_page(league, browse):
    """MB's manager, signed in."""
    return browse(manager("MB", "100"))


@pytest.fixture
def commish_page(league, browse):
    """The commissioner's password superuser, which keeps admin whatever the Discord flag says."""
    return browse(User.objects.create_superuser("commish", password="not-used"))


def signable(code):
    return [p for p in signing.pool(Team.objects.get(code=code), SEASON) if p.can_sign]


def decide_farms(*codes, keep=True):
    """Save a draft for each team that decides every farm player and nothing else."""
    period = SigningPeriod.objects.get(season=SEASON)
    for code in codes:
        team = Team.objects.get(code=code)
        sub, _ = Submission.objects.get_or_create(period=period, team=team)
        signing.save_plan(sub, signing.Plan(farm={f.pk: keep for f in signing.farm_candidates(team, SEASON)}))
