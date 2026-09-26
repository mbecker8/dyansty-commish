"""Load the blackout rosters from a Fantrax snapshot: the pool each team signs from.

Rosters come from --fantrax (taken at the blackout, after offseason trades). Prices come
from --salaries, the end-of-season snapshot, because VISION §4 fixes them at season end.
A renewed Fantrax league has new team ids, so teams are matched by id, then by name.
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.fantrax_data import Snapshot
from league.models import Contract, Player, RosterEntry, SigningPeriod, Team, TeamAlias, audit

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
        teams = self.match_teams(rosters)
        players = {p.fantrax_id: p for p in Player.objects.exclude(fantrax_id=None)}
        for fp in rosters.players().values():
            if fp.fantrax_id not in players:
                players[fp.fantrax_id] = Player.objects.create(
                    name=fp.name, fantrax_id=fp.fantrax_id, positions=fp.positions
                )
        RosterEntry.objects.filter(season=season).delete()
        entries = []
        for fid, end in rosters.end_states().items():
            entries.append(
                RosterEntry(
                    season=season, team=teams[end.team], player=players[fid], salary=prices.get(fid), status=end.status
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
            if missing:
                self.stdout.write(f"No end-of-season salary (not signable until fixed): {', '.join(missing)}")
            for line in self.contract_problems(season):
                self.stdout.write(line)

    def match_teams(self, snapshot) -> dict[str, Team]:
        by_id = {t.fantrax_id: t for t in Team.objects.all()}
        by_name = {t.name.lower(): t for t in by_id.values()} | {
            a.alias.lower(): a.team for a in TeamAlias.objects.all()
        }
        out = {}
        for t in snapshot.teams:
            team = by_id.get(t["id"]) or by_name.get(t["name"].strip().lower())
            if team is None:
                raise CommandError(f"Fantrax team {t['name']!r} ({t['id']}) matches no team; add an alias")
            out[t["id"]] = team
        return out

    @staticmethod
    def contract_problems(season):
        """Contracts still running whose player isn't on the holder's blackout roster: a drop or trade to reconcile."""
        where = {e.player_id: e.team for e in RosterEntry.objects.filter(season=season).select_related("team")}
        lines = []
        for c in Contract.live.select_related("player", "team").order_by("team__code", "player__name"):
            if c.final_year <= season or where.get(c.player_id) == c.team:
                continue
            now = where.get(c.player_id)
            lines.append(
                f"  {c.team.code} {c.player.name} (through {c.final_year}) is "
                + (f"on {now.code}'s roster" if now else "on no roster")
                + ": re-run reconcile on this snapshot"
            )
        return ["Contracts not on their team's roster:", *lines] if lines else []
