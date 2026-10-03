"""Apply new Fantrax trades, drops, promotions and debuts to the league's records.

Live by default (every active FantraxLeague, using FANTRAX_COOKIE), or from a saved snapshot.
Each fact is applied once; anything the app can't interpret is listed as an exception.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from league import fantrax_client
from league.events import FreezeBlocked, SeasonMissing, UnmatchedTeam, sync, unresolved_count
from league.fantrax_client import FantraxError
from league.fantrax_data import Snapshot
from league.models import FantraxLeague, Team


class Command(BaseCommand):
    help = "Apply new Fantrax moves (live, or from --snapshot) to contracts and farm players."

    def add_arguments(self, parser):
        parser.add_argument("--snapshot", help="Read a saved snapshot directory instead of fetching live")
        parser.add_argument("--league", help="Fantrax league ID the snapshot belongs to (default: by season)")
        parser.add_argument("--dry-run", action="store_true", help="Show what would change; save nothing")

    def handle(self, *args, snapshot, league, dry_run, **options):
        if not Team.objects.exists():
            raise CommandError("No league data yet; run import_league first.")
        sources = self.from_snapshot(snapshot, league) if snapshot else self.live()
        try:
            result = sync(sources, source_label=f"sync_fantrax {snapshot or 'live'}", dry_run=dry_run)
        except (UnmatchedTeam, SeasonMissing, FreezeBlocked) as e:
            raise CommandError(str(e)) from None
        if not options["verbosity"]:
            return
        self.stdout.write(("Dry run, nothing saved: " if dry_run else "") + result.summary())
        for e in result.exceptions:
            self.stdout.write(f"  needs a look: {e.detail}")
        if n := unresolved_count():
            self.stdout.write(f"{n} unresolved exception(s): resolve them on the commissioner console.")

    def from_snapshot(self, directory, league_id):
        snap = Snapshot(Path(directory))
        leagues = FantraxLeague.objects.filter(league_id=league_id) if league_id else None
        if leagues is None:
            leagues = FantraxLeague.objects.filter(season=snap.season)
        if len(leagues) != 1:
            which = f"ID {league_id}" if league_id else f"season {snap.season}"
            raise CommandError(f"{len(leagues)} Fantrax leagues match {which}; pass --league with its Fantrax ID")
        return [(leagues[0], snap)]

    def live(self):
        if not settings.FANTRAX_COOKIE:
            raise CommandError("FANTRAX_COOKIE isn't set (env var, or secrets/fantrax_cookie.txt locally)")
        s = fantrax_client.session(settings.FANTRAX_COOKIE)
        try:
            # Fetch everything first; the sync itself is one transaction with no network calls.
            return [
                (lg, Snapshot.from_raw(fantrax_client.fetch_raw(s, lg.league_id)))
                for lg in FantraxLeague.objects.filter(active=True)
            ]
        except FantraxError as e:
            raise CommandError(str(e)) from None
