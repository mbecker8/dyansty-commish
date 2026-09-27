"""Setting up production from the console: loading the league and the rosters, with no shell."""

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from league import signing
from league.models import AuditEntry, FantraxEvent, RosterEntry, Team
from league.tests.test_signing import settle_2026

pytestmark = pytest.mark.django_db


@pytest.fixture
def commish(settings):
    """The first commissioner: signed in by COMMISSIONER_DISCORD_IDS, before any team exists."""
    settings.COMMISSIONER_DISCORD_IDS = ["200"]
    user = User.objects.create_user("discord-200")
    client = Client()
    client.force_login(user)
    client.get("/commish/")  # the middleware grants admin rights on the first page
    return client


def page(client):
    return client.get("/commish/").content.decode()


def test_empty_console_offers_to_load_the_league(commish):
    assert 'value="load_league"' in page(commish)


def test_load_league_imports_and_applies_the_2026_moves(commish):
    commish.post("/commish/", {"action": "load_league"})
    assert Team.objects.count() == 14
    assert FantraxEvent.objects.exists()
    # Locally the 2026 moves leave AW's two drafted rookies for the commissioner to resolve.
    assert FantraxEvent.objects.unresolved().count() == 2
    entry = AuditEntry.objects.get(action="Loaded the league")
    assert entry.user.username == "discord-200"
    assert 'value="load_league"' not in page(commish)


def test_load_league_refuses_once_there_is_a_league(commish):
    call_command("import_league", verbosity=0)
    response = commish.post("/commish/", {"action": "load_league"}, follow=True)
    assert "already exists" in response.content.decode()
    assert not FantraxEvent.objects.exists()


@pytest.fixture
def settled():
    call_command("import_league", verbosity=0)
    settle_2026()


def test_console_offers_the_committed_snapshots(settled, commish):
    assert '<option value="2026-final"' in page(commish)


def test_load_rosters_from_a_committed_snapshot(settled, commish):
    commish.post("/commish/", {"action": "load_rosters", "snapshot": "2026-final"})
    assert RosterEntry.objects.filter(season=2026).count() > 300
    entry = AuditEntry.objects.get(action="Synced rosters")
    assert entry.user.username == "discord-200"


@pytest.mark.parametrize("name", ["", "nope", "../league", "."])
def test_load_rosters_only_reads_committed_snapshots(settled, commish, name):
    response = commish.post("/commish/", {"action": "load_rosters", "snapshot": name}, follow=True)
    assert "Pick a Fantrax snapshot" in response.content.decode()
    assert not RosterEntry.objects.exists()


def test_load_rosters_refuses_once_signing_opens(settled, commish):
    call_command("sync_rosters", verbosity=0)
    signing.open_period(2026, None)
    response = commish.post("/commish/", {"action": "load_rosters", "snapshot": "2026-final"}, follow=True)
    assert "rosters are fixed" in response.content.decode()
