"""Replay a season's Fantrax moves against the contracts and record what changed.

Proposals are saved as ReconciliationItems for the commissioner to accept or
reject; nothing about the contracts changes until they do.
"""

from datetime import date, datetime, time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from league.fantrax_data import EASTERN, Snapshot
from league.models import Contract, ReconciliationItem, Team
from league.reconcile import Outcome, replay

# The post-signing sheet reflects every move up to the auction, so replay from there.
DEFAULT_SINCE = "2026-02-25"


class Command(BaseCommand):
    help = "Replay the season's Fantrax moves against contracts and propose changes."

    def add_arguments(self, parser):
        parser.add_argument("--season", type=int, default=2026)
        parser.add_argument("--since", default=DEFAULT_SINCE, help="Ignore moves before this date (YYYY-MM-DD)")
        parser.add_argument("--fantrax", default=str(Path(settings.BASE_DIR) / "data" / "fantrax" / "2026-final"))

    @transaction.atomic
    def handle(self, *args, season, since, fantrax, **options):
        snapshot = Snapshot(Path(fantrax))
        cutoff = datetime.combine(date.fromisoformat(since), time(), EASTERN)
        moves_by_player = {}
        for m in snapshot.moves():
            if m.when >= cutoff:
                moves_by_player.setdefault(m.fantrax_id, []).append(m)
        rostered = snapshot.rostered()
        teams = {t.fantrax_id: t for t in Team.objects.all()}
        names = {t["id"]: t["name"] for t in snapshot.teams}

        ReconciliationItem.objects.filter(season=season, status=ReconciliationItem.Status.PENDING).delete()
        decided = set(ReconciliationItem.objects.filter(season=season).values_list("contract_id", flat=True))

        contracts = (
            Contract.objects.filter(buyout__isnull=True, player__fantrax_id__isnull=False)
            .exclude(pk__in=decided)
            .select_related("team", "player")
        )
        created = []
        for c in contracts:
            if c.final_year < season:
                continue
            fid = c.player.fantrax_id
            end_team = rostered[fid][0] if fid in rostered else None
            r = replay(c.team.fantrax_id, c.final_year, moves_by_player.get(fid, []), season, end_team=end_team)
            detail = r.detail
            for team_id, name in names.items():
                detail = detail.replace(team_id, name)
            if r.dropped_at:
                detail = "; ".join(filter(None, [f"dropped {r.dropped_at.when:%Y-%m-%d}", detail]))
            if r.claimed_by:
                detail += f"; later claimed by {names[r.claimed_by]}"
            team_id = r.buyout_team if r.outcome is Outcome.DROPPED else r.holder
            created.append(
                ReconciliationItem(
                    season=season,
                    kind=r.outcome.value,
                    contract=c,
                    team=teams.get(team_id),
                    detail=detail,
                    fantrax_tx_ids=",".join(r.tx_ids),
                )
            )
        ReconciliationItem.objects.bulk_create(created)
        if options["verbosity"]:
            self.report(season)

    def report(self, season):
        items = ReconciliationItem.objects.filter(season=season).select_related(
            "contract__player", "contract__team", "team"
        )
        for kind in ReconciliationItem.Kind:
            group = [i for i in items if i.kind == kind]
            if not group:
                continue
            self.stdout.write(f"\n{kind.label} ({len(group)})")
            if kind in (ReconciliationItem.Kind.CONTINUES, ReconciliationItem.Kind.EXPIRING):
                continue
            for i in group:
                c = i.contract
                target = f" -> {i.team.code}" if i.team else ""
                self.stdout.write(
                    f"  [{i.status}] {c.team.code} {c.player.name}: ${c.as_rules().annual_price}/yr "
                    f"through {c.final_year}{target}  {i.detail}"
                )
