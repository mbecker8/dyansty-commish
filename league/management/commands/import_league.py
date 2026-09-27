"""Seed the database from the committed Year 19 sheet fixture and the Fantrax snapshot.

Reads only committed files:
  data/league/teams.json               franchises and aliases
  data/league/player_aliases.json      reviewed sheet-spelling -> Fantrax ID map
  data/league/year19_post_signing.json sheet state going into the 2026 season
  data/league/cash_trades_from_fantrax.json  cash from 2026 trades (Fantrax only has comments)
  data/fantrax/2026-final/             end-of-2026 Fantrax snapshot
"""

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from league.fantrax_data import EASTERN, Snapshot, normalize_name
from league.models import (
    AuditEntry,
    BudgetAdjustment,
    Buyout,
    CashTrade,
    Contract,
    FantraxEvent,
    FantraxLeague,
    FarmPick,
    FarmPlayer,
    Player,
    RosterEntry,
    SeasonBudget,
    SigningPeriod,
    Team,
    TeamAlias,
)

DATA = Path(settings.BASE_DIR) / "data"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


class Command(BaseCommand):
    help = "Import league state from the Year 19 sheet fixture and the Fantrax snapshot."

    def add_arguments(self, parser):
        parser.add_argument("--sheet", default=str(DATA / "league" / "year19_post_signing.json"))
        parser.add_argument("--fantrax", default=str(DATA / "fantrax" / "2026-final"))
        parser.add_argument("--replace", action="store_true", help="Delete existing league data first")

    @transaction.atomic
    def handle(self, *args, sheet, fantrax, replace, **options):
        if Team.objects.exists():
            if not replace:
                raise CommandError("League data already exists; pass --replace to reload it.")
            if FantraxEvent.objects.exists():
                raise CommandError("Fantrax events have been applied; --replace would erase them.")
            if LogEntry.objects.filter(content_type__app_label="league").exists():
                raise CommandError("League data has been edited in the admin; --replace would erase those edits.")
            if SeasonBudget.objects.exists():
                raise CommandError("A season has been started; --replace would erase it.")
            if SigningPeriod.objects.exists() or AuditEntry.objects.exclude(user=None).exists():
                raise CommandError(
                    "A signing period has started or people have made changes; --replace would erase them."
                )
            AuditEntry.objects.all().delete()  # only command-line entries (roster syncs) are left
            for model in (
                FantraxEvent,
                FantraxLeague,
                RosterEntry,
                BudgetAdjustment,
                CashTrade,
                FarmPick,
                FarmPlayer,
                Buyout,
                Contract,
                Player,
                TeamAlias,
                Team,
            ):
                model.objects.all().delete()

        self.verbosity = options["verbosity"]
        self.sheet = load(Path(sheet))
        self.snapshot = Snapshot(Path(fantrax))
        aliases = load(DATA / "league" / "player_aliases.json")
        self.player_aliases = aliases["aliases"]
        self.unlinked = []

        self.import_teams(load(DATA / "league" / "teams.json")["teams"])
        self.import_fantrax_players()
        self.import_sheet_teams()
        self.import_cash_trades()
        self.import_cash_from_fantrax(load(DATA / "league" / "cash_trades_from_fantrax.json")["cash_trades"])
        self.import_farm_picks()
        FantraxLeague.objects.create(
            league_id="p3z8zy75mgdm460o",
            name="Dynasty Yr 19",
            season=2026,
            # The post-signing sheet reflects every move up to the auction. The last pre-auction drop
            # was 15:11 on Feb 25 and the first auction claim 18:21; earlier drops belong to 2025.
            process_since=datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN),
        )
        for s in aliases["sign_and_trade"]:
            Contract.objects.filter(team__code=s["team"], player=self.player(s["player"])).update(
                sign_and_trade=True, note=s["note"]
            )

        self.say(
            f"Imported {Team.objects.count()} teams, {Player.objects.count()} players, "
            f"{Contract.objects.filter(buyout__isnull=True).count()} contracts, {Buyout.objects.count()} buyouts, "
            f"{FarmPlayer.objects.count()} farm players, {CashTrade.objects.count()} cash trades, "
            f"{FarmPick.objects.count()} farm picks."
        )
        if self.unlinked:
            self.say(f"Players without a Fantrax ID: {', '.join(sorted(set(self.unlinked)))}")

    def say(self, msg):
        if self.verbosity:
            self.stdout.write(msg)

    # --- teams -------------------------------------------------------------

    def import_teams(self, teams):
        self.team_by_alias = {}
        for t in teams:
            team = Team.objects.create(code=t["code"], name=t["name"], fantrax_id=t["fantrax_id"])
            for alias in [t["code"], t["name"], *t["aliases"]]:
                key = alias.lower()
                if key in self.team_by_alias and self.team_by_alias[key] != team:
                    raise CommandError(f"Alias {alias!r} is used by two teams")
                if key not in self.team_by_alias:
                    self.team_by_alias[key] = team
                    if alias not in (t["code"], t["name"]):
                        TeamAlias.objects.create(team=team, alias=alias)
        self.team_by_fantrax = {t.fantrax_id: t for t in Team.objects.all()}

    def team(self, name: str) -> Team:
        try:
            return self.team_by_alias[name.strip().lower()]
        except KeyError:
            raise CommandError(f"Unknown team name {name!r}; add it to data/league/teams.json") from None

    # --- players -----------------------------------------------------------

    def import_fantrax_players(self):
        players = self.snapshot.players()
        Player.objects.bulk_create(
            Player(name=p.name, fantrax_id=p.fantrax_id, positions=p.positions) for p in players.values()
        )
        self.player_by_fantrax = {p.fantrax_id: p for p in Player.objects.all()}
        self.fantrax_ids_by_name = self.snapshot.ids_by_name()

    def player(self, sheet_name: str) -> Player:
        """Resolve a sheet spelling: reviewed alias, else exact normalized match, else an unlinked player."""
        if sheet_name in self.player_aliases:
            fantrax_id = self.player_aliases[sheet_name]["fantrax_id"]
        else:
            ids = self.fantrax_ids_by_name.get(normalize_name(sheet_name), set())
            if len(ids) > 1:
                raise CommandError(f"{sheet_name!r} matches several Fantrax players {ids}; add an alias")
            fantrax_id = next(iter(ids), None)
        if fantrax_id:
            return self.player_by_fantrax[fantrax_id]
        self.unlinked.append(sheet_name)
        player, _ = Player.objects.get_or_create(name=sheet_name, fantrax_id=None)
        return player

    # --- sheet state -------------------------------------------------------

    def import_sheet_teams(self):
        for code, t in self.sheet["teams"].items():
            team = Team.objects.get(code=code)
            season = t["after_season"] + 1
            for c in t["contracts"]:
                Contract.objects.create(
                    team=team,
                    player=self.player(c["player"]),
                    original_price=c["original_price"],
                    year_signed=c["year_signed"],
                    length=c["length"],
                )
            for b in t["buyouts"]:
                if b["original_price"] is None:
                    self.say(f"{code}: skipping blank buyout row {b['player']!r}")
                    continue
                contract = Contract.objects.create(
                    team=team,
                    player=self.player(b["player"]),
                    original_price=b["original_price"],
                    year_signed=b["year_signed"],
                    length=b["length"],
                )
                Buyout.objects.create(contract=contract, team=team, dropped_in_season=b["dropped_in"])
            for f in t["farm"]:
                FarmPlayer.objects.create(
                    team=team,
                    player=self.player(f["player"]),
                    drafted_year=f["drafted"],
                    salary=f["salary"] or 0,
                    salary_season=season,
                    has_mlb_appearance=str(f["mlb"]).strip().lower().startswith("y"),
                )
            if missed_ip := t["budget"]["missed_ip"]:
                BudgetAdjustment.objects.create(
                    team=team,
                    season=season,
                    kind=BudgetAdjustment.Kind.MISSED_IP,
                    amount=missed_ip,
                    note="From the Year 19 sheet",
                )

    def import_cash_trades(self):
        season = next(iter(self.sheet["teams"].values()))["after_season"] + 1
        for c in self.sheet["cash_trades"]:
            CashTrade.objects.create(
                budget_season=season,
                from_team=self.team(c["from"]),
                to_team=self.team(c["to"]),
                amount=c["amount"],
                note=c["note"] or "",
            )

    def import_cash_from_fantrax(self, trades):
        for c in trades:
            CashTrade.objects.create(
                budget_season=c["budget_season"],
                from_team=self.team(c["from"]),
                to_team=self.team(c["to"]),
                amount=c["amount"],
                note=c["note"],
                fantrax_tx_id=c["fantrax_tx_id"],
            )

    # --- Fantrax -----------------------------------------------------------

    def import_farm_picks(self):
        for owner_id, roster in self.snapshot.rosters.items():
            for year in roster["draftPicksData"]["draftPicksPerYear"]:
                for pick in year["draftPickList"]:
                    FarmPick.objects.create(
                        year=year["year"],
                        round=pick["round"],
                        original_team=self.team_by_fantrax[pick["origOwnerTeamId"]],
                        owner=self.team_by_fantrax[owner_id],
                    )
