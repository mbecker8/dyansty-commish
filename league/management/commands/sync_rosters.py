"""Load the blackout rosters from a Fantrax snapshot: the pool each team signs from.

Rosters come from --fantrax (taken at the blackout, after offseason trades). Prices come
from --salaries, the end-of-season snapshot, because VISION §4 fixes them at season end.
A renewed Fantrax league has new team ids, so teams are matched by id, then by name.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.events import UnmatchedTeam, match_teams
from league.fantrax_data import Snapshot, normalize_name
from league.models import Contract, Player, RosterEntry, SigningPeriod, Team, audit

SNAPSHOT = str(Path(settings.BASE_DIR) / "data" / "fantrax" / "2026-final")


class Command(BaseCommand):
    help = "Load blackout rosters (and end-of-season salaries) for the signing period."

    def add_arguments(self, parser):
        parser.add_argument("--season", type=int, default=settings.LEAGUE_SEASON, help="The season that just ended")
        parser.add_argument("--fantrax", default=SNAPSHOT, help="Snapshot taken at the blackout (rosters)")
        parser.add_argument("--salaries", default=SNAPSHOT, help="End-of-season snapshot (original prices)")

    @transaction.atomic
    def handle(self, *args, season, fantrax, salaries, **options):
        if not Team.objects.exists():
            raise CommandError("No league data yet; run import_league first.")
        period = SigningPeriod.objects.filter(season=season).first()
        if period and period.status != SigningPeriod.Status.PLANNED:
            raise CommandError(f"Signing after {season} is {period.get_status_display().lower()}; rosters are fixed.")
        rosters, prices = Snapshot(Path(fantrax)), Snapshot(Path(salaries)).salaries()
        try:
            teams = match_teams(rosters)
        except UnmatchedTeam as e:
            raise CommandError(str(e)) from None
        players = {p.fantrax_id: p for p in Player.objects.exclude(fantrax_id=None)}
        unlinked = {}
        for p in Player.objects.filter(fantrax_id=None):
            unlinked.setdefault(normalize_name(p.name), []).append(p)
        linked = []
        for fp in rosters.players().values():
            if fp.fantrax_id in players:
                continue
            # A sheet player the import couldn't link (e.g. under contract): link him rather than duplicate him.
            same_name = unlinked.get(normalize_name(fp.name), [])
            if len(same_name) == 1:
                player = same_name.pop()
                player.fantrax_id, player.positions = fp.fantrax_id, fp.positions
                player.save(update_fields=["fantrax_id", "positions"])
                linked.append(player.name)
            else:
                player = Player.objects.create(name=fp.name, fantrax_id=fp.fantrax_id, positions=fp.positions)
            players[fp.fantrax_id] = player
        RosterEntry.objects.filter(season=season).delete()
        entries, skipped = [], []
        for fid, end in rosters.end_states().items():
            if fid not in players:
                skipped.append(fid)  # a roster row with no player name
                continue
            entries.append(
                RosterEntry(
                    season=season,
                    team=teams[end.team],
                    player=players[fid],
                    salary=prices.get(fid),
                    status=end.status or "",
                )
            )
        RosterEntry.objects.bulk_create(entries)
        missing = sorted(e.player.name for e in entries if e.salary is None)
        audit(
            None,
            "Synced rosters",
            f"{len(entries)} players from {Path(fantrax).name}; salaries from {Path(salaries).name}",
        )
        if options["verbosity"]:
            self.stdout.write(f"Loaded {len(entries)} roster spots for the signing after {season}.")
            if linked:
                self.stdout.write(f"Linked to their Fantrax IDs by name: {', '.join(sorted(linked))}")
            if skipped:
                self.stdout.write(f"Skipped roster rows with no player name: {', '.join(skipped)}")
            if missing:
                self.stdout.write(f"No end-of-season salary (not signable until fixed): {', '.join(missing)}")
            for line in self.contract_problems(season):
                self.stdout.write(line)

    @staticmethod
    def contract_problems(season):
        """Contracts still running whose player isn't on the holder's blackout roster: an unsynced drop or trade."""
        where = {e.player_id: e.team for e in RosterEntry.objects.filter(season=season).select_related("team")}
        lines = []
        for c in Contract.live.select_related("player", "team").order_by("team__code", "player__name"):
            if c.final_year <= season or where.get(c.player_id) == c.team:
                continue
            now = where.get(c.player_id)
            lines.append(
                f"  {c.team.code} {c.player.name} (through {c.final_year}) is "
                + (f"on {now.code}'s roster" if now else "on no roster")
                + ": run sync_fantrax"
            )
        return ["Contracts not on their team's roster:", *lines] if lines else []
