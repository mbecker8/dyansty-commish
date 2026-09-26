"""Sign in with Discord: only Discord accounts the commissioner linked to a team get in."""

from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth.models import User

from league.models import Manager, Team

pytestmark = pytest.mark.django_db
DISCORD_ID = "80351110224678912"


@pytest.fixture(autouse=True)
def discord_app(settings):
    settings.DISCORD_CLIENT_ID = "client-id"
    settings.DISCORD_CLIENT_SECRET = "client-secret"


@pytest.fixture
def team():
    return Team.objects.create(code="MB", name="beKCer's trainwreKCers")


def start_login(client, next_url=None):
    """Hit the login button; return the state Discord would echo back."""
    response = client.get("/auth/discord/login", {"next": next_url} if next_url else {})
    assert response.status_code == 302
    query = parse_qs(urlparse(response["Location"]).query)
    return query


def discord_says(user_id=DISCORD_ID, username="mbecker"):
    """Patch Discord's token and identity endpoints."""
    return mock.patch("accounts.discord.fetch_identity", return_value={"id": user_id, "username": username})


def test_login_redirects_to_discord_asking_only_for_identity(client):
    query = start_login(client)
    assert query["client_id"] == ["client-id"]
    assert query["scope"] == ["identify"]
    assert query["redirect_uri"] == ["http://testserver/auth/discord/callback"]
    assert query["state"][0]


def test_linked_manager_is_logged_in(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    state = start_login(client)["state"][0]
    with discord_says():
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response.status_code == 302
    manager = Manager.objects.get(discord_id=DISCORD_ID)
    assert manager.user is not None and manager.discord_username == "mbecker"
    assert client.session["_auth_user_id"] == str(manager.user.pk)


def test_second_login_reuses_the_same_user(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    for _ in range(2):
        state = start_login(client)["state"][0]
        with discord_says():
            client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert User.objects.count() == 1


def test_unlinked_discord_account_is_turned_away_without_creating_a_user(client, team):
    state = start_login(client)["state"][0]
    with discord_says(user_id="999"):
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response.status_code == 403
    assert b"999" in response.content  # so they can send it to the commissioner
    assert not User.objects.exists()
    assert "_auth_user_id" not in client.session


def test_wrong_state_is_rejected_before_calling_discord(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    start_login(client)
    with discord_says() as fetch:
        response = client.get("/auth/discord/callback", {"code": "abc", "state": "forged"})
    assert response.status_code == 400
    fetch.assert_not_called()
    assert "_auth_user_id" not in client.session


def test_state_can_only_be_used_once(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    state = start_login(client)["state"][0]
    with discord_says():
        client.get("/auth/discord/callback", {"code": "abc", "state": state})
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response.status_code == 400


def test_cancelled_on_discord_goes_back_to_login(client):
    state = start_login(client)["state"][0]
    response = client.get("/auth/discord/callback", {"error": "access_denied", "state": state})
    assert response.status_code == 302 and response["Location"].startswith("/auth/login")


def test_returns_to_the_page_asked_for(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    state = start_login(client, next_url="/buyouts/")["state"][0]
    with discord_says():
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response["Location"] == "/buyouts/"


def test_does_not_redirect_off_site(client, team):
    Manager.objects.create(team=team, discord_id=DISCORD_ID, name="Matt")
    state = start_login(client, next_url="https://evil.example/")["state"][0]
    with discord_says():
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response["Location"] == "/"


def test_logout_requires_post(client, team):
    user = User.objects.create_user("u")
    client.force_login(user)
    assert client.get("/auth/logout").status_code == 405
    client.post("/auth/logout")
    assert "_auth_user_id" not in client.session


def test_login_page_says_when_discord_is_not_configured(client, settings):
    settings.DISCORD_CLIENT_ID = ""
    response = client.get("/auth/login")
    assert response.status_code == 200 and b"not configured" in response.content


def test_discord_outage_shows_an_error_not_a_crash(client, team):
    from accounts.discord import DiscordError

    state = start_login(client)["state"][0]
    with mock.patch("accounts.discord.fetch_identity", side_effect=DiscordError("down")):
        response = client.get("/auth/discord/callback", {"code": "abc", "state": state})
    assert response.status_code == 502 and "_auth_user_id" not in client.session
