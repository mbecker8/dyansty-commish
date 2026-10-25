"""Fantrax credentials pasted on the console: encrypted in the DB, write-only, read when used."""

import pytest
from django.contrib.auth.models import User
from django.test import Client

from league import credentials
from league.models import AuditEntry, Credential, FantraxLeague

pytestmark = pytest.mark.django_db

COOKIE = "JSESSIONID=supersecretcookie; uig=abc123"
SECRET_ID = "secretid-xyz789"


@pytest.fixture
def commish(settings):
    settings.COMMISSIONER_DISCORD_IDS = ["200"]
    user = User.objects.create_user("discord-200")
    client = Client()
    client.force_login(user)
    client.get("/commish/")  # the middleware grants admin rights on the first page
    return client


@pytest.fixture
def no_env(settings):
    settings.FANTRAX_COOKIE = ""
    settings.FANTRAX_SECRET_ID = ""


def save(client, **fields):
    return client.post("/commish/", {"action": "fantrax_credentials", **fields}, follow=True)


def test_falls_back_to_settings_when_nothing_is_saved(settings):
    settings.FANTRAX_COOKIE = "from=env"
    settings.FANTRAX_SECRET_ID = "env-id"
    assert credentials.fantrax_cookie() == "from=env"
    assert credentials.fantrax_secret_id() == "env-id"


def test_a_saved_value_wins_over_settings_and_is_encrypted(settings):
    settings.FANTRAX_COOKIE = "from=env"
    credentials.save(credentials.FANTRAX_COOKIE, COOKIE, None)
    assert credentials.fantrax_cookie() == COOKIE
    stored = Credential.objects.get(name=credentials.FANTRAX_COOKIE).ciphertext
    assert "supersecretcookie" not in stored


def test_an_unreadable_value_falls_back_and_says_so(settings):
    settings.FANTRAX_COOKIE = "from=env"
    credentials.save(credentials.FANTRAX_COOKIE, COOKIE, None)
    settings.SECRET_KEY = "a different key"  # e.g. DJANGO_SECRET_KEY was regenerated
    assert credentials.fantrax_cookie() == "from=env"
    assert credentials.status(credentials.FANTRAX_COOKIE)["state"] == "unreadable"


def test_console_saves_both_without_ever_showing_them(commish, no_env):
    page = save(commish, fantrax_cookie=COOKIE, fantrax_secret_id=SECRET_ID).content.decode()
    assert credentials.fantrax_cookie() == COOKIE
    assert credentials.fantrax_secret_id() == SECRET_ID
    for text in (page, commish.get("/commish/").content.decode()):
        assert "supersecretcookie" not in text and SECRET_ID not in text
    assert 'id="fantrax-login"' in page
    assert "Cookie: saved on the console" in page


def test_a_blank_field_keeps_the_saved_value(commish, no_env):
    save(commish, fantrax_cookie=COOKIE, fantrax_secret_id=SECRET_ID)
    save(commish, fantrax_cookie="new=cookie", fantrax_secret_id="  ")
    assert credentials.fantrax_cookie() == "new=cookie"
    assert credentials.fantrax_secret_id() == SECRET_ID


def test_saving_nothing_is_an_error(commish, no_env):
    page = save(commish, fantrax_cookie="", fantrax_secret_id="").content.decode()
    assert "Paste a cookie or a Secret ID" in page
    assert not Credential.objects.exists()


def test_the_audit_log_records_who_but_not_the_value(commish, no_env):
    save(commish, fantrax_cookie=COOKIE)
    entry = AuditEntry.objects.get(action="Updated Fantrax cookie")
    assert entry.user.username == "discord-200"
    assert "supersecretcookie" not in f"{entry.detail} {entry.note}"
    assert not AuditEntry.objects.filter(action="Updated Fantrax Secret ID").exists()


def test_only_commissioners_can_save(no_env):
    client = Client()
    client.force_login(User.objects.create_user("someone"))
    client.post("/commish/", {"action": "fantrax_credentials", "fantrax_cookie": COOKIE})
    assert not Credential.objects.exists()


def test_status_shows_where_each_value_comes_from(commish, settings):
    settings.FANTRAX_COOKIE = ""
    settings.FANTRAX_SECRET_ID = "env-id"
    page = commish.get("/commish/").content.decode()
    assert "Cookie: not set" in page
    assert "Secret ID: set on the server" in page


def test_an_expired_login_points_at_the_console(commish, monkeypatch):
    from league import fantrax_client
    from league.tests.test_fantrax_client import FakeSession

    FantraxLeague.objects.create(league_id="x", name="Dynasty", season=2026)
    credentials.save(credentials.FANTRAX_COOKIE, "a=1", None)
    monkeypatch.setattr(fantrax_client, "session", lambda cookie: FakeSession(logged_in=False))
    response = commish.post("/commish/", {"action": "sync"}, follow=True).content.decode()
    assert "Fantrax login expired" in response
    assert "Fantrax login on the console" in response
