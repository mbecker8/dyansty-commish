"""Read the league Discord's Rules and Accounting channels. Cash for 2027 on comes from #trades-<year>-assets.

Needs DISCORD_BOT_TOKEN, DISCORD_GUILD_ID and DISCORD_CATEGORY_ID (environment or secrets/discord.env).
"""

from django.core.management.base import BaseCommand, CommandError

from league import discord_sync
from league.discord_client import DiscordReadError


class Command(BaseCommand):
    help = "Sync from Discord: store the category's messages and follow the trade channels' cash"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show what would change; save nothing")

    def handle(self, *args, dry_run=False, **options):
        try:
            result = discord_sync.fetch_and_sync(None, dry_run=dry_run)
        except DiscordReadError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(("Dry run, nothing saved. " if dry_run else "") + result.summary())
        for line in result.open:
            self.stdout.write(f"  {line}")
