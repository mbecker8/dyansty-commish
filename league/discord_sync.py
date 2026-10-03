"""Sync from Discord: store every message in Rules and Accounting, and keep each trade post's cash trades
equal to what it says now. Fetch first (league.discord_client), then apply in one transaction.

Cash for the auctions from 2027 on comes only from #trades-<year>-assets. An edited post changes its cash
trade and a deleted one removes it, audited, until that season's auction budgets are frozen; after that
a change is an exception and nothing moves.
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from league.discord_cash import EXCEPTION, Wanted, channel_season, read_post, readable_text
from league.discord_client import Channel, fetch_category
from league.models import CashTrade, DiscordMessage, Manager, SeasonBudget, Team, audit

Status = DiscordMessage.Status


@dataclass
class DiscordSyncResult:
    read: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    skipped: list[str] = field(default_factory=list)  # channels the bot can't see
    open: list[str] = field(default_factory=list)  # unresolved exceptions after the run

    def summary(self) -> str:
        text = (
            f"{self.read} Discord messages read; cash trades: {self.added} added, {self.changed} changed, "
            f"{self.removed} removed"
        )
        if self.skipped:
            text += f"; can't read #{', #'.join(self.skipped)}"
        return text + (f"; {len(self.open)} exception(s) to resolve" if self.open else "")


def unresolved_count() -> int:
    return DiscordMessage.objects.unresolved().count()


def fetch_and_sync(user, dry_run: bool = False) -> DiscordSyncResult:
    return sync(fetch_category(), user, dry_run)


def sync(channels: list[Channel], user=None, dry_run: bool = False) -> DiscordSyncResult:
    result, now = DiscordSyncResult(), timezone.now()
    with transaction.atomic():
        # Every cash trade involves teams: holding their rows makes a second sync (a double-click) wait here.
        list(Team.objects.select_for_update())
        ctx = _Context(user, result)
        read = [c for c in channels if c.readable]
        result.skipped = [c.name for c in channels if not c.readable]
        # Only channels read this run can tell us a message is gone: a channel moved out of the
        # category (say, archived after its auction) keeps its posts and their cash.
        stored = {m.message_id: m for m in DiscordMessage.objects.filter(channel_id__in=[c.id for c in read])}
        for channel in read:
            season = channel_season(channel.name)
            for raw in channel.messages:
                result.read += 1
                msg, names = _store(stored.pop(raw["id"], None), channel, raw, now)
                if season is None:
                    msg.status, msg.detail = Status.IGNORED, ""
                else:
                    ctx.follow(msg, season, names)
                msg.save()
        for msg in stored.values():  # gone from a channel read this run: deleted in Discord
            if msg.deleted_at is None:
                msg.deleted_at = now
                if (season := msg.season) is not None:
                    ctx.follow_deletion(msg, season)
                msg.save()
        result.open = [
            f"#{m.channel_name} {m.author_name}: {m.readable[:80]!r}: {m.detail}"
            for m in DiscordMessage.objects.unresolved()
        ]
        if dry_run:
            transaction.set_rollback(True)
        else:
            audit(user, "Synced Discord", result.summary())
    return result


def _store(msg, channel, raw, now):
    """Insert or update the message from Discord's JSON. Returns it (saved) and its mentions' usernames."""
    msg = msg or DiscordMessage(message_id=raw["id"])
    author = raw.get("author") or {}
    names = {u["id"]: u.get("username") or u["id"] for u in raw.get("mentions") or []}
    msg.channel_id, msg.channel_name = channel.id, channel.name
    msg.author_id, msg.author_name = author.get("id", ""), author.get("username", "")
    msg.posted_at = parse_datetime(raw["timestamp"])
    msg.edited_at = parse_datetime(raw["edited_timestamp"]) if raw.get("edited_timestamp") else None
    msg.content = raw.get("content") or ""
    msg.readable = readable_text(msg.content, names)
    msg.has_attachments = bool(raw.get("attachments"))
    msg.deleted_at, msg.synced_at = None, now
    msg.status = msg.status or Status.NOT_CASH
    msg.save()
    return msg, names


class _Context:
    def __init__(self, user, result):
        self.user, self.result = user, result
        self.teams = {m.discord_id: m.team for m in Manager.objects.exclude(discord_id=None).select_related("team")}
        self.frozen = set(SeasonBudget.objects.values_list("season", flat=True))

    def follow(self, msg, season, names):
        if msg.resolved_at:
            if msg.content == msg.resolved_content:
                return  # resolved and unchanged: what the commissioner decided stands
            msg.resolved_at, msg.resolved_by, msg.resolved_note = None, None, ""
            msg.status, msg.detail = Status.EXCEPTION, "Edited after it was resolved: check it again"
            return
        reading = read_post(msg.content, msg.has_attachments, season, self.teams, names)
        msg.status, msg.detail = reading.status, reading.detail
        if reading.status != EXCEPTION:
            self.make(msg, season, reading.trades, reading.detail)

    def follow_deletion(self, msg, season):
        if self.make(msg, season, [], "Deleted in Discord"):
            msg.status, msg.detail = Status.NOT_CASH, "Deleted in Discord"

    def make(self, msg, season, wanted, now_says) -> bool:
        """Make the message's cash trades equal `wanted`. False (and an exception) if the season is frozen."""
        current = list(msg.cash_trades.all())
        have = sorted((c.from_team_id, c.to_team_id, c.amount) for c in current)
        if have == sorted((w.from_team.pk, w.to_team.pk, w.amount) for w in wanted):
            return True
        if season in self.frozen:
            msg.status = Status.EXCEPTION
            msg.detail = f"Changed after the {season} auction budgets were frozen, so nothing changed. Now: {now_says}"
            return False
        replace(msg, season, wanted, self.user, self.result)
        return True


def replace(msg, season, wanted, user, result=None):
    """Delete the message's cash trades and create `wanted` ones, auditing each for both teams."""
    current = list(msg.cash_trades.select_related("from_team", "to_team"))
    where = f"#{msg.channel_name}"
    for c in current:
        c.delete()
        _audit(user, "Discord: removed cash trade", c, where)
    for w in wanted:
        c = CashTrade.objects.create(
            budget_season=season, from_team=w.from_team, to_team=w.to_team, amount=w.amount, note=w.note,
            discord_message=msg,
        )  # fmt: skip
        _audit(user, "Discord: added cash trade", c, where)
    if result is not None:
        if current and wanted:
            result.changed += 1
        else:
            result.added += len(wanted)
            result.removed += len(current)


def _audit(user, action, c, where):
    detail = f"{c.from_team.code} → {c.to_team.code} ${c.amount} for {c.budget_season}, from {where}"
    for team in (c.from_team, c.to_team):
        audit(user, action, detail, team=team)


@transaction.atomic
def resolve(message_id: int, user, note: str, from_code: str = "", to_code: str = "", amount: str = ""):
    """Close an exception. With teams and an amount, that's the post's cash trade; all blank means no cash."""
    note = note.strip()
    if not note:
        raise ValueError("Say what you did in the note")
    msg = DiscordMessage.objects.select_for_update().get(pk=message_id)
    if msg.status != Status.EXCEPTION or msg.resolved_at:
        raise ValueError("This isn't an open exception")
    season = msg.season
    frozen = SeasonBudget.objects.filter(season=season).exists()
    entered = [x.strip() for x in (from_code, to_code, amount)]
    if any(entered):
        if not all(entered):
            raise ValueError("Pick both teams and an amount, or leave all three blank")
        sender, receiver = (Team.objects.get(code=code.upper()) for code in entered[:2])
        if sender == receiver:
            raise ValueError("Sender and receiver are the same team")
        if not entered[2].isdigit() or int(entered[2]) <= 0:
            raise ValueError("The amount is a whole number of dollars")
        if frozen:
            raise ValueError(f"The {season} auction budgets are frozen: cash for it can't change now")
        replace(msg, season, [Wanted(sender, receiver, int(entered[2]), "Entered from the console")], user)
    elif not frozen:
        replace(msg, season, [], user)
    msg.resolved_at, msg.resolved_by = timezone.now(), (user if user and user.is_authenticated else None)
    msg.resolved_note, msg.resolved_content = note, msg.content
    msg.save()
    audit(user, "Resolved Discord exception", f"#{msg.channel_name} {msg.author_name}: {msg.readable[:150]}", note=note)
    return msg
