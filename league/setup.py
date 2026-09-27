"""Getting production going from the commissioner console, with no shell: the same code the
management commands run locally (import_league, sync_fantrax --snapshot, sync_rosters)."""

from pathlib import Path

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import transaction

from league import events
from league.fantrax_data import Snapshot
from league.models import Contract, FantraxLeague, RosterEntry, Team, audit

SNAPSHOTS = Path(settings.BASE_DIR) / "data" / "fantrax"
LEAGUE_2026 = "p3z8zy75mgdm460o"


class SetupError(Exception):
    pass


def snapshots() -> list[str]:
    """The committed Fantrax snapshots, newest name last."""
    return sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir() and not p.name.startswith("."))


@transaction.atomic
def load_league(user) -> events.SyncResult:
    """Load the Year 19 sheet and data/league/, then apply the 2026 Fantrax moves. Empty database only."""
    if Team.objects.exists():
        raise SetupError("League data already exists, so there's nothing to load.")
    call_command("import_league", verbosity=0)
    league = FantraxLeague.objects.get(league_id=LEAGUE_2026)
    result = events.sync([(league, Snapshot(SNAPSHOTS / "2026-final"))], user, source_label="Load league")
    audit(user, "Loaded the league", f"Year 19 sheet and the 2026 Fantrax snapshot; {result.summary()}")
    return result


def contracts_off_roster(season: int) -> list[tuple[Contract, Team | None]]:
    """Contracts still running whose player isn't on the holder's blackout roster: an unsynced drop
    or trade. Each comes with the team whose roster he's on now (None: no roster)."""
    where = {e.player_id: e.team for e in RosterEntry.objects.filter(season=season).select_related("team")}
    return [
        (c, where.get(c.player_id))
        for c in Contract.live.select_related("player", "team").order_by("team__code", "player__name")
        if c.final_year > season and where.get(c.player_id) != c.team
    ]


def load_rosters(name: str, user) -> None:
    """The signing pool from a committed snapshot; prices stay the end-of-season ones."""
    if name not in snapshots():
        raise SetupError("Pick a Fantrax snapshot to load the rosters from.")
    try:
        call_command("sync_rosters", fantrax=str(SNAPSHOTS / name), user=user, verbosity=0)
    except CommandError as e:
        raise SetupError(str(e)) from None
