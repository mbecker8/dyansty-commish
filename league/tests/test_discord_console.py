import pytest
from django.contrib.auth.models import User
from django.test import Client

from league import discord_sync
from league.admin import CashTradeForm
from league.discord_client import Channel, DiscordReadError
from league.models import CashTrade, DiscordMessage, Manager, Team
from league.tests.test_discord_sync import post

pytestmark = pytest.mark.django_db


@pytest.fixture
def commish():
    a = Team.objects.create(code="AA", name="A")
    Team.objects.create(code="BB", name="B")
    user = User.objects.create_user("discord-1")
    Manager.objects.create(team=a, name="Al", discord_id="1", user=user, is_commissioner=True)
    client = Client()
    client.force_login(user)
    client.get("/teams/")  # the middleware grants admin rights on the first page
    return client


def test_console_discord_not_set_up(commish):
    page = commish.get("/commish/").content.decode()
    assert "Sync from Discord" in page and "Not set up" in page


def test_console_syncs_and_resolves(commish, monkeypatch, settings):
    settings.DISCORD_BOT_TOKEN, settings.DISCORD_GUILD_ID, settings.DISCORD_CATEGORY_ID = "t", "1", "10"
    monkeypatch.setattr(
        discord_sync, "fetch_category", lambda: [Channel("27", "trades-2027-assets", [post("m1", "Al sends Bo $3")])]
    )
    page = commish.post("/commish/", {"action": "discord_sync"}, follow=True).content.decode()
    assert "1 Discord messages read" in page and "Discord exceptions (1)" in page and "Al sends Bo $3" in page
    m = DiscordMessage.objects.get()
    commish.post("/commish/", {"action": "discord_resolve", "message": m.pk, "note": "names", "from_team": "AA",
                               "to_team": "BB", "amount": "3"})  # fmt: skip
    assert CashTrade.objects.get().amount == 3
    cash_page = commish.get("/cash/").content.decode()
    assert "https://discord.com/channels/1/27/m1" in cash_page


def test_console_reports_a_discord_error(commish, monkeypatch):
    def fail():
        raise DiscordReadError("Discord rejected the bot token: update DISCORD_BOT_TOKEN")

    monkeypatch.setattr(discord_sync, "fetch_category", fail)
    page = commish.post("/commish/", {"action": "discord_sync"}, follow=True).content.decode()
    assert "update DISCORD_BOT_TOKEN" in page


def test_console_reports_a_bad_resolve(commish):
    page = commish.post(
        "/commish/", {"action": "discord_resolve", "message": "999", "note": "x"}, follow=True
    ).content.decode()
    assert "isn&#x27;t an open exception" in page


def test_admin_refuses_hand_entered_cash_from_2027():
    a, b = Team.objects.create(code="AA", name="A"), Team.objects.create(code="BB", name="B")
    form = CashTradeForm(data={"budget_season": "2027", "from_team": a.pk, "to_team": b.pk, "amount": "5"})
    assert not form.is_valid() and "trades-2027-assets" in str(form.errors)
    form = CashTradeForm(data={"budget_season": "2026", "from_team": a.pk, "to_team": b.pk, "amount": "5"})
    assert form.is_valid(), form.errors
