"""Replay a season's Fantrax moves against the contracts and record what changed.

Proposals are saved as ReconciliationItems for the commissioner to accept or
reject; nothing about the contracts changes until they do.
"""

from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.budget import team_budget
from league.fantrax_data import EASTERN, Snapshot
from league.models import CashTrade, Contract, FarmPlayer, Player, ReconciliationItem, Team
from league.reconcile import Outcome, replay, replay_farm

# The post-signing sheet reflects every move up to the auction, so replay from there. The last
# pre-auction drop was 15:11 on Feb 25 and the first auction claim 18:21; drops before the
# auction belong to the previous season (the sheet already charges them).
DEFAULT_SINCE = "2026-02-25T16:00"


class Command(BaseCommand):
    help = "Replay the season's Fantrax moves against contracts and propose changes."

    def add_arguments(self, parser):
        parser.add_argument("--season", type=int, default=2026)
        parser.add_argument(
            "--since", default=DEFAULT_SINCE, help="Ignore moves before this Eastern time (YYYY-MM-DD[THH:MM])"
        )
        parser.add_argument("--fantrax", default=str(Path(settings.BASE_DIR) / "data" / "fantrax" / "2026-final"))
        parser.add_argument(
            "--preview", action="store_true", help="Also show next season's commitments if every proposal is accepted"
        )

    @transaction.atomic
    def handle(self, *args, season, since, fantrax, **options):
        if not Team.objects.exists():
            raise CommandError("No league data yet; run import_league first.")
        try:
            cutoff = datetime.fromisoformat(since).replace(tzinfo=EASTERN)
        except ValueError:
            raise CommandError(f"--since must be a time like 2026-02-25T16:00, not {since!r}") from None
        snapshot = Snapshot(Path(fantrax))
        if snapshot.season != season:
            raise CommandError(f"--season {season} doesn't match the Fantrax snapshot, which is {snapshot.season}")
        moves_by_player = {}
        for m in snapshot.moves():
            if m.when >= cutoff:
                moves_by_player.setdefault(m.fantrax_id, []).append(m)
        rostered = snapshot.rostered()
        teams = {t.fantrax_id: t for t in Team.objects.all()}
        names = {t["id"]: t["name"] for t in snapshot.teams}

        ReconciliationItem.objects.filter(season=season, status=ReconciliationItem.Status.PENDING).delete()
        decided = self.last_decisions(season, "contract_id")

        # Contracts signed at the signing after this season start next season; nothing this season moves them.
        contracts = Contract.live.filter(player__fantrax_id__isnull=False, year_signed__lt=season).select_related(
            "team", "player"
        )
        created = []
        for c in contracts:
            if c.final_year < season:
                continue
            fid = c.player.fantrax_id
            end_team = rostered[fid][0] if fid in rostered else None
            # Replay from the season's starting holder, even if a decision has since moved him.
            last = decided.get(c.pk)
            start = last.start_team if last and last.start_team else c.team
            r = replay(start.fantrax_id, c.final_year, moves_by_player.get(fid, []), season, end_team=end_team)
            team_id = r.buyout_team if r.outcome is Outcome.DROPPED else r.holder
            if last and (last.kind, last.team_id) == (r.outcome.value, getattr(teams.get(team_id), "pk", None)):
                continue  # same answer the commissioner already decided on
            detail = r.detail
            for fantrax_team, name in names.items():
                detail = detail.replace(fantrax_team, name)
            if r.dropped_at:
                detail = "; ".join(filter(None, [f"dropped {r.dropped_at.when:%Y-%m-%d}", detail]))
            if r.claimed_by:
                detail += f"; later claimed by {names[r.claimed_by]}"
            if last:
                detail = f"changed since your earlier decision ({last.get_kind_display()}); {detail}".rstrip("; ")
            created.append(
                ReconciliationItem(
                    season=season,
                    kind=r.outcome.value,
                    contract=c,
                    start_team=start,
                    team=teams.get(team_id),
                    detail=detail,
                    fantrax_tx_ids=",".join(r.tx_ids),
                )
            )
        created += self.farm_items(season, snapshot, moves_by_player, teams, names)
        created += self.cash_comment_items(season, snapshot, cutoff, teams)
        ReconciliationItem.objects.bulk_create(created)
        if options["verbosity"]:
            self.report(season)
        if options["preview"]:
            self.preview(season)

    @staticmethod
    def last_decisions(season, field):
        """Contract or farm player id -> the latest accepted or rejected item about it this season.

        A re-run (e.g. on a newer snapshot) proposes something only when its answer differs.
        """
        decided = (
            ReconciliationItem.objects.filter(season=season, **{f"{field}__isnull": False})
            .exclude(status=ReconciliationItem.Status.PENDING)
            .select_related("start_team")
            .order_by("pk")
        )
        return {getattr(item, field): item for item in decided}

    def farm_items(self, season, snapshot, moves_by_player, teams, names):
        decided = self.last_decisions(season, "farm_player_id")
        debut_decided = set(
            ReconciliationItem.objects.filter(season=season, mlb_debut=True)
            .exclude(status=ReconciliationItem.Status.PENDING)
            .values_list("farm_player_id", flat=True)
        )
        Kind = ReconciliationItem.Kind
        kinds = {
            "continues": Kind.FARM_CONTINUES,
            "traded": Kind.FARM_TRADED,
            "promoted": Kind.FARM_PROMOTED,
            "released": Kind.FARM_RELEASED,
            "inconsistent": Kind.FARM_INCONSISTENT,
        }
        ends = snapshot.end_states()
        items = []
        farm = (
            FarmPlayer.objects.filter(salary_season=season, status=FarmPlayer.Status.ACTIVE)
            .filter(player__fantrax_id__isnull=False)
            .select_related("team", "player")
        )
        on_farm = set()
        for f in farm:
            fid = f.player.fantrax_id
            on_farm.add(fid)
            end = ends.get(fid)
            last = decided.get(f.pk)
            start = last.start_team if last and last.start_team else f.team
            r = replay_farm(start.fantrax_id, moves_by_player.get(fid, []), end, f.has_mlb_appearance)
            kind, team = kinds[r.outcome.value], teams.get(r.team)
            new_debut = r.mlb_debut and f.pk not in debut_decided
            # An accepted "not on the sheet" pick is a farm player continuing on that team.
            last_kind = Kind.FARM_CONTINUES if last and last.kind == Kind.FARM_UNKNOWN else getattr(last, "kind", None)
            if last and (last_kind, last.team_id) == (kind, getattr(team, "pk", None)) and not new_debut:
                continue  # same answer the commissioner already decided on
            detail = r.detail
            for fantrax_team, name in names.items():
                detail = detail.replace(fantrax_team, name)
            if r.mlb_debut:
                debut = f"MLB debut ({end.games_played} G, {end.plate_appearances}+ PA, {end.outs} outs)"
                detail = "; ".join(filter(None, [debut, detail]))
            if last:
                detail = f"changed since your earlier decision ({last.get_kind_display()}); {detail}".rstrip("; ")
            items.append(
                ReconciliationItem(
                    season=season,
                    kind=kind,
                    farm_player=f,
                    player=f.player,
                    start_team=start,
                    team=team,
                    mlb_debut=r.mlb_debut,
                    detail=detail,
                )
            )
        players = {p.fantrax_id: p for p in Player.objects.filter(fantrax_id__in=ends)}
        decided_unknown = set(
            ReconciliationItem.objects.filter(season=season, kind=Kind.FARM_UNKNOWN)
            .exclude(status=ReconciliationItem.Status.PENDING)
            .values_list("player__fantrax_id", flat=True)
        )
        for fid, end in ends.items():
            team_id, status = end.team, end.status
            if (
                status == "Minors"
                and fid not in on_farm
                and fid not in decided_unknown
                and not FarmPlayer.objects.filter(player__fantrax_id=fid).exists()
                and not Contract.live.filter(player__fantrax_id=fid).exists()
            ):
                items.append(
                    ReconciliationItem(
                        season=season,
                        kind=Kind.FARM_UNKNOWN,
                        player=players[fid],
                        team=teams[team_id],
                        detail="In a Fantrax minors slot but on no sheet farm; accepting adds him as a $1 draft pick",
                    )
                )
        return items

    def cash_comment_items(self, season, snapshot, cutoff, teams):
        """Cash in trades is recorded only as a free-text comment; list them for manual entry."""
        decided = set(
            ReconciliationItem.objects.filter(season=season, kind=ReconciliationItem.Kind.CASH_COMMENT)
            .exclude(status=ReconciliationItem.Status.PENDING)
            .values_list("fantrax_tx_ids", flat=True)
        )
        decided |= set(CashTrade.objects.exclude(fantrax_tx_id="").values_list("fantrax_tx_id", flat=True))
        items = []
        for tx, when, team_ids, text in snapshot.trade_comments():
            if when < cutoff or tx in decided:
                continue
            codes = " / ".join(sorted(teams[t].code for t in team_ids))
            items.append(
                ReconciliationItem(
                    season=season,
                    kind=ReconciliationItem.Kind.CASH_COMMENT,
                    detail=f"Trade {when:%Y-%m-%d} between {codes}: {text!r}",
                    fantrax_tx_ids=tx,
                )
            )
        return items

    def preview(self, season):
        """Apply every acceptable pending item inside a savepoint, print budgets, then roll back."""
        next_season = season + 1
        sid = transaction.savepoint_create()
        try:
            for item in ReconciliationItem.objects.filter(season=season, status=ReconciliationItem.Status.PENDING):
                try:
                    item.accept()
                except ValueError:
                    pass  # needs the commissioner
            self.stdout.write(
                f"\n{next_season} commitments if every proposal is accepted (before signings and farm keeps)"
            )
            for team in Team.objects.all():
                b = team_budget(team, next_season)
                self.stdout.write(
                    f"{team.code:3} contracts ${b.contracts} ({sum(1 for k, *_ in b.lines if k == 'contract')}) "
                    f"buyouts ${b.buyouts} cash ${b.cash_net:+d} -> ${b.remaining} left before signings"
                )
        finally:
            transaction.savepoint_rollback(sid)

    def report(self, season):
        items = ReconciliationItem.objects.filter(season=season).select_related(
            "contract__player", "contract__team", "team"
        )
        for kind in ReconciliationItem.Kind:
            group = [i for i in items if i.kind == kind]
            if not group:
                continue
            self.stdout.write(f"\n{kind.label} ({len(group)})")
            for i in group:
                if kind in (ReconciliationItem.Kind.CONTINUES, ReconciliationItem.Kind.EXPIRING):
                    break
                if kind == ReconciliationItem.Kind.FARM_CONTINUES and not i.mlb_debut:
                    continue
                target = f" -> {i.team.code}" if i.team else ""
                if c := i.contract:
                    what = f"{c.team.code} {c.player.name}: ${c.as_rules().annual_price}/yr through {c.final_year}"
                elif f := i.farm_player:
                    what = f"{f.team.code} {f.player.name} (farm, ${f.salary})"
                elif i.player:
                    what = i.player.name
                else:
                    what = "Cash"
                self.stdout.write(f"  [{i.status}] {what}{target}  {i.detail}")
