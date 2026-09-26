"""Replay a season's Fantrax moves against the contracts and record what changed.

Proposals are saved as ReconciliationItems for the commissioner to accept or
reject; nothing about the contracts changes until they do.
"""

from collections import defaultdict
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

# Outcomes that change nothing; after an earlier decision they mean "nothing new happened".
NOTHING_NEW = {"continues", "expiring"}


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
        all_moves = snapshot.moves()
        for m in all_moves:
            if m.when >= cutoff:
                moves_by_player.setdefault(m.fantrax_id, []).append(m)
        self.horizon = max((m.when for m in all_moves), default=cutoff)
        rostered = snapshot.rostered()
        teams = {t.fantrax_id: t for t in Team.objects.all()}
        names = {t["id"]: t["name"] for t in snapshot.teams}

        ReconciliationItem.objects.filter(season=season, status=ReconciliationItem.Status.PENDING).delete()
        decided = self.decided_through(season, "contract_id")

        contracts = Contract.live.filter(player__fantrax_id__isnull=False).select_related("team", "player")
        created = []
        for c in contracts:
            if c.final_year < season:
                continue
            fid = c.player.fantrax_id
            end_team = rostered[fid][0] if fid in rostered else None
            # Already decided: replay only what happened since, from where the decision left him.
            moves = self.since_decision(moves_by_player.get(fid, []), decided, c.pk)
            r = replay(c.team.fantrax_id, c.final_year, moves, season, end_team=end_team)
            if c.pk in decided and r.outcome.value in NOTHING_NEW:
                continue
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
        created += self.farm_items(season, snapshot, moves_by_player, teams, names)
        created += self.cash_comment_items(season, snapshot, cutoff, teams)
        for item in created:
            item.through = self.horizon
        ReconciliationItem.objects.bulk_create(created)
        if options["verbosity"]:
            self.report(season)
        if options["preview"]:
            self.preview(season)

    @staticmethod
    def decided_through(season, field):
        """Decided items' subject id -> the latest snapshot time any decision on it was based on."""
        through = defaultdict(lambda: None)
        decided = ReconciliationItem.objects.filter(season=season, **{f"{field}__isnull": False}).exclude(
            status=ReconciliationItem.Status.PENDING
        )
        for subject, when in decided.values_list(field, "through"):
            if through[subject] is None or (when is not None and when > through[subject]):
                through[subject] = when
        return dict(through)

    @staticmethod
    def since_decision(moves, decided, pk):
        if pk not in decided:
            return moves
        if decided[pk] is None:
            return []
        return [m for m in moves if m.when > decided[pk]]

    def farm_items(self, season, snapshot, moves_by_player, teams, names):
        decided = self.decided_through(season, "farm_player_id")
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
            moves = self.since_decision(moves_by_player.get(fid, []), decided, f.pk)
            r = replay_farm(f.team.fantrax_id, moves, end, f.has_mlb_appearance)
            if f.pk in decided and r.outcome.value in NOTHING_NEW and not r.mlb_debut:
                continue
            detail = r.detail
            for team_id, name in names.items():
                detail = detail.replace(team_id, name)
            if r.mlb_debut:
                debut = f"MLB debut ({end.games_played} G, {end.plate_appearances}+ PA, {end.outs} outs)"
                detail = "; ".join(filter(None, [debut, detail]))
            items.append(
                ReconciliationItem(
                    season=season,
                    kind=kinds[r.outcome.value],
                    farm_player=f,
                    player=f.player,
                    team=teams.get(r.team),
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
