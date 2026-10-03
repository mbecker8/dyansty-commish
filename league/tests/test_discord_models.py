from importlib import import_module

import pytest
from django.apps import apps

from league.discord_cash import channel_season
from league.models import AuditEntry, CashTrade, DiscordMessage, Team

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "name, season",
    [("trades-2027-assets", 2027), ("trades-2031-assets", 2031), ("trades-2026-assets", None), ("league-rules", None)],
)
def test_channel_season(name, season):
    assert channel_season(name) == season


def test_message_url_and_unresolved(settings):
    settings.DISCORD_GUILD_ID = "1"
    m = DiscordMessage.objects.create(
        message_id="5", channel_id="2", channel_name="trades-2027-assets", author_id="9",
        posted_at="2026-07-01T00:00Z", synced_at="2026-07-01T00:00Z", status="EXCEPTION",
    )  # fmt: skip
    assert m.url == "https://discord.com/channels/1/2/5"
    assert m.season == 2027
    assert list(DiscordMessage.objects.unresolved()) == [m]


def test_migration_drops_only_future_cash_from_fantrax_comments():
    a = Team.objects.create(code="AA", name="A")
    b = Team.objects.create(code="BB", name="B")
    keep_old = CashTrade.objects.create(budget_season=2026, from_team=a, to_team=b, amount=1, fantrax_tx_id="t1")
    keep_hand = CashTrade.objects.create(budget_season=2027, from_team=a, to_team=b, amount=2)
    CashTrade.objects.create(budget_season=2027, from_team=a, to_team=b, amount=5, fantrax_tx_id="t2")
    import_module("league.migrations.0017_cash_from_discord").drop_fantrax_comment_cash(apps, None)
    assert set(CashTrade.objects.all()) == {keep_old, keep_hand}
    assert AuditEntry.objects.filter(action="Removed cash trade").count() == 2  # one per team
