import re

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command

from league import discord_sync, signing
from league.discord_client import Channel
from league.models import AuditEntry, CashTrade, DiscordMessage, Manager, SeasonBudget, Team

pytestmark = pytest.mark.django_db


@pytest.fixture
def teams():
    a = Team.objects.create(code="AA", name="A")
    b = Team.objects.create(code="BB", name="B")
    Manager.objects.create(team=a, name="Al", discord_id="1")
    Manager.objects.create(team=b, name="Bo", discord_id="2")
    return a, b


def post(mid, content, edited=None, attachments=()):
    """A message as Discord's API returns it, with made-up IDs."""
    ids = re.findall(r"<@!?(\d+)>", content)
    return {
        "id": mid, "content": content, "timestamp": "2026-07-01T12:00:00+00:00", "edited_timestamp": edited,
        "author": {"id": "1", "username": "alice"}, "attachments": list(attachments),
        "mentions": [{"id": i, "username": f"user{i}"} for i in ids],
    }  # fmt: skip


def trades(*posts, name="trades-2027-assets", cid="27"):
    return Channel(cid, name, list(posts))


def cash():
    return sorted((c.budget_season, c.from_team.code, c.to_team.code, c.amount) for c in CashTrade.objects.all())


def freeze(season, teams):
    for t in teams:
        SeasonBudget.objects.create(
            season=season, team=t, base=400, contracts=0, buyouts=0, farm=0, missed_ip=0, cash_net=0, remaining=400,
            frozen_at="2027-03-01T00:00Z",
        )  # fmt: skip


def test_first_sync_creates_cash_and_a_second_changes_nothing(teams):
    channels = [trades(post("m1", "<@1> sends $10 to <@2> in Stott trade"), post("m2", "a GIF"))]
    first = discord_sync.sync(channels)
    assert cash() == [(2027, "AA", "BB", 10)]
    assert (first.read, first.added) == (2, 1)
    assert CashTrade.objects.get().note == "in Stott trade"
    assert CashTrade.objects.get().discord_message.message_id == "m1"
    second = discord_sync.sync(channels)
    assert (second.added, second.changed, second.removed) == (0, 0, 0)
    assert cash() == [(2027, "AA", "BB", 10)]
    assert AuditEntry.objects.filter(action="Synced Discord").count() == 2


def test_other_channels_and_past_auctions_are_stored_but_never_cash(teams):
    discord_sync.sync(
        [
            trades(post("o1", "<@1> sends $5 to <@2>"), name="trades-2026-assets", cid="26"),
            Channel("9", "league-rules", [post("r1", "Rule 1: $400 budget")]),
        ]
    )
    assert cash() == []
    assert set(DiscordMessage.objects.values_list("status", flat=True)) == {"IGNORED"}


def test_an_edit_updates_the_cash_trade(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([trades(post("m1", "<@2> sends $4 to <@1>", edited="2026-07-02T00:00:00+00:00"))])
    assert cash() == [(2027, "BB", "AA", 4)] and result.changed == 1
    assert AuditEntry.objects.filter(action="Discord: removed cash trade").count() == 2  # one per team


def test_a_deletion_removes_the_cash_trade(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([trades()])
    assert cash() == [] and result.removed == 1
    assert DiscordMessage.objects.get().deleted_at is not None


def test_a_change_after_the_budget_froze_is_an_exception(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    freeze(2027, teams)
    discord_sync.sync([trades(post("m1", "<@1> sends $3 to <@2>"))])
    assert cash() == [(2027, "AA", "BB", 10)]
    (m,) = DiscordMessage.objects.unresolved()
    assert "frozen" in m.detail
    discord_sync.sync([trades()])  # deleting it doesn't change frozen money either
    assert cash() == [(2027, "AA", "BB", 10)]


def test_linking_a_manager_clears_a_not_linked_exception(teams):
    channels = [trades(post("m1", "<@3> sends $2 to <@2>"))]
    discord_sync.sync(channels)
    assert "@user3 isn't linked" in DiscordMessage.objects.unresolved().get().detail
    Manager.objects.create(team=Team.objects.create(code="CC", name="C"), name="Cy", discord_id="3")
    discord_sync.sync(channels)
    assert not DiscordMessage.objects.unresolved().exists()
    assert cash() == [(2027, "CC", "BB", 2)]


def test_resolving_enters_the_cash_and_an_edit_reopens_it(teams):
    user = User.objects.create(username="c")
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    m = DiscordMessage.objects.get()
    with pytest.raises(ValueError, match="note"):
        discord_sync.resolve(m.pk, user, "  ", "AA", "BB", "3")
    with pytest.raises(ValueError, match="both teams"):
        discord_sync.resolve(m.pk, user, "names", "AA", "", "3")
    discord_sync.resolve(m.pk, user, "names, not mentions", "AA", "BB", "3")
    assert cash() == [(2027, "AA", "BB", 3)] and discord_sync.unresolved_count() == 0
    with pytest.raises(ValueError, match="open exception"):
        discord_sync.resolve(m.pk, user, "again")
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    assert discord_sync.unresolved_count() == 0 and cash() == [(2027, "AA", "BB", 3)]
    discord_sync.sync([trades(post("m1", "Al sends Bo $4"))])
    assert discord_sync.unresolved_count() == 1 and cash() == [(2027, "AA", "BB", 3)]


def test_an_edit_reopens_a_resolved_exception(teams):
    test_resolving_enters_the_cash_and_an_edit_reopens_it(teams)
    with pytest.raises(signing.SigningError, match="Discord exception"):
        signing.open_period(2026, None)


def test_a_not_linked_exception_is_cleared_by_linking_not_resolved(teams):
    discord_sync.sync([trades(post("m1", "<@3> sends $2 to <@2>"))])
    m = DiscordMessage.objects.get()
    with pytest.raises(ValueError, match="Admin → Managers"):
        discord_sync.resolve(m.pk, None, "linking him")
    assert discord_sync.unresolved_count() == 1  # still blocks signing


def test_a_mention_of_someone_never_linked_can_be_resolved_with_the_cash(teams):
    """A former manager will never be linked; entering the cash by hand still closes it."""
    discord_sync.sync([trades(post("m1", "<@3> sends $2 to <@2>"))])
    discord_sync.resolve(DiscordMessage.objects.get().pk, None, "@user3 ran AA then", "AA", "BB", "2")
    assert discord_sync.unresolved_count() == 0 and cash() == [(2027, "AA", "BB", 2)]


def test_a_reopened_exception_waits_for_the_commissioner(teams):
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    discord_sync.resolve(DiscordMessage.objects.get().pk, None, "names", "AA", "BB", "3")
    discord_sync.sync([trades(post("m1", "<@2> sends $9 to <@1>"))])  # edited: reopens
    discord_sync.sync([trades(post("m1", "<@2> sends $9 to <@1>"))])  # still waits, though it now reads
    assert discord_sync.unresolved_count() == 1 and cash() == [(2027, "AA", "BB", 3)]


def test_a_resolved_post_deleted_after_the_freeze_shows_again(teams):
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    discord_sync.resolve(DiscordMessage.objects.get().pk, None, "names", "AA", "BB", "3")
    freeze(2027, teams)
    discord_sync.sync([trades()])
    assert discord_sync.unresolved_count() == 1 and cash() == [(2027, "AA", "BB", 3)]


def test_an_edit_to_the_note_alone_is_followed(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2> in Stott trade"))])
    result = discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2> in Stott/Rice trade"))])
    assert result.changed == 1 and CashTrade.objects.get().note == "in Stott/Rice trade"


def test_resolving_blank_means_no_cash(teams):
    discord_sync.sync([trades(post("m1", "", attachments=[{"id": "x"}]))])
    m = DiscordMessage.objects.get()
    discord_sync.resolve(m.pk, None, "a meme")
    assert cash() == [] and discord_sync.unresolved_count() == 0


def test_resolving_cant_add_cash_to_a_frozen_auction(teams):
    discord_sync.sync([trades(post("m1", "Al sends Bo $3"))])
    freeze(2027, teams)
    with pytest.raises(ValueError, match="frozen"):
        discord_sync.resolve(DiscordMessage.objects.get().pk, None, "late", "AA", "BB", "3")


def test_unreadable_channel_keeps_its_messages(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    result = discord_sync.sync([Channel("27", "trades-2027-assets", [], readable=False)])
    assert result.skipped == ["trades-2027-assets"]
    assert cash() == [(2027, "AA", "BB", 10)] and DiscordMessage.objects.get().deleted_at is None


def test_channel_moved_out_of_the_category_keeps_its_cash(teams):
    discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))])
    discord_sync.sync([Channel("9", "league-rules", [])])
    assert cash() == [(2027, "AA", "BB", 10)] and DiscordMessage.objects.get(message_id="m1").deleted_at is None


def test_dry_run_changes_nothing(teams):
    result = discord_sync.sync([trades(post("m1", "<@1> sends $10 to <@2>"))], dry_run=True)
    assert result.added == 1
    assert not CashTrade.objects.exists() and not DiscordMessage.objects.exists()


def test_command_reports_and_lists_exceptions(teams, monkeypatch, capsys):
    monkeypatch.setattr(discord_sync, "fetch_category", lambda: [trades(post("m1", "Al sends Bo $3"))])
    call_command("sync_discord", dry_run=True)
    out = capsys.readouterr().out
    assert "Dry run" in out and "Couldn't tell who sends" in out
