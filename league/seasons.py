"""Which league season it is, which season a moment belongs to, and starting the next one.

Season S runs from the S auction start until just before the S+1 auction. The current season
is the latest one the commissioner has started; a Fantrax move's season comes from its date.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from league.budget import team_budget
from league.models import AuditEntry, FantraxEvent, FarmPick, Season, SeasonBudget, SigningPeriod, Team, audit

FARM_ROUNDS = (1, 2)


class RolloverError(Exception):
    """Starting a season, or setting its dates, isn't allowed right now."""


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool


def current() -> Season:
    season = Season.objects.exclude(started_at=None).order_by("-year").first()
    if season is None:
        raise Season.DoesNotExist("No season has started")
    return season


def current_season() -> int:
    return current().year


def season_at(when: datetime, seasons=None) -> Season | None:
    """The season `when` belongs to: the latest whose auction had started. None before the first one."""
    found = None
    for s in Season.objects.all() if seasons is None else seasons:
        if s.auction_starts_at <= when and (found is None or s.year > found.year):
            found = s
    return found


def farm_draft_at(when: datetime, seasons=None) -> Season | None:
    """The season whose farm draft window (farm draft start up to the auction start) holds `when`."""
    for s in Season.objects.all() if seasons is None else seasons:
        if s.farm_draft_starts_at and s.farm_draft_starts_at <= when < s.auction_starts_at:
            return s
    return None


def _draft_day(season: Season) -> datetime:
    return season.farm_draft_starts_at or season.auction_starts_at


def window(season: Season) -> tuple[datetime, datetime | None]:
    """A season's moves on the team page: from its draft day to the next draft day (open until entered).

    Starts at the farm draft, not the auction, so the picks made for a season are listed with it.
    """
    following = Season.objects.filter(year=season.year + 1).first()
    return _draft_day(season), _draft_day(following) if following else None


# --- rollover -------------------------------------------------------------------


def upcoming() -> Season | None:
    return Season.objects.filter(year=current_season() + 1).first()


def _when(t) -> str:
    return f"{timezone.localtime(t):%b %-d %Y %-I:%M %p}" if t else "not set"


@transaction.atomic
def set_dates(farm_draft_starts_at, auction_starts_at, user) -> Season:
    """Enter the next season's farm draft and auction start, before anything from draft day is synced.

    A move is processed once, so dates can't be set (or moved) into time the sync has already
    covered: the moves there would stay filed the old way.
    """
    now = current()
    if farm_draft_starts_at is None or auction_starts_at is None:
        raise RolloverError("Enter both the farm draft start and the auction start")
    if farm_draft_starts_at <= now.auction_starts_at:
        raise RolloverError(f"The {now.year + 1} farm draft starts after the {now.year} auction")
    if farm_draft_starts_at >= auction_starts_at:
        raise RolloverError("The farm draft starts before the auction")
    season, created = Season.objects.select_for_update().get_or_create(
        year=now.year + 1, defaults={"auction_starts_at": auction_starts_at}
    )
    if season.started_at:
        raise RolloverError(f"The {season.year} season has started; change its dates in the admin")
    synced = FantraxEvent.objects.aggregate(t=Max("happened_at"))["t"]
    earliest = min([farm_draft_starts_at] + ([] if created else [_draft_day(season)]))
    if synced and earliest <= synced:
        raise RolloverError(
            f"Fantrax is synced up to {_when(synced)}, so draft day can't start before then: moves already "
            "synced wouldn't be re-read"
        )
    season.farm_draft_starts_at, season.auction_starts_at = farm_draft_starts_at, auction_starts_at
    season.save(update_fields=["farm_draft_starts_at", "auction_starts_at"])
    detail = f"{season.year}: farm draft {_when(farm_draft_starts_at)}, auction {_when(auction_starts_at)}"
    audit(user, "Set season dates", detail)
    return season


def freeze_budgets(year: int, as_of: datetime) -> int:
    """Store every team's `year` auction budget as it stands now, dated `as_of`. Returns the team count."""
    teams = list(Team.objects.all())
    for team in teams:
        b = team_budget(team, year)
        SeasonBudget.objects.create(
            season=year,
            team=team,
            base=b.base,
            contracts=b.contracts,
            buyouts=b.buyouts,
            farm=b.farm,
            missed_ip=b.missed_ip,
            cash_net=b.cash_net,
            remaining=b.remaining,
            frozen_at=as_of,
        )
    return len(teams)


def budgets_due(now: datetime) -> Season | None:
    """The next season, when a sync should freeze its budgets: auction started, signing locked, not frozen yet."""
    year = current_season()
    nxt = Season.objects.filter(year=year + 1, started_at=None, auction_starts_at__lte=now).first()
    period = SigningPeriod.objects.filter(season=year).first()
    locked = period and period.status == SigningPeriod.Status.LOCKED
    if nxt and locked and not SeasonBudget.objects.filter(season=nxt.year).exists():
        return nxt
    return None


def checklist(now=None) -> list[Check]:
    """What must be true before the next season starts. Everything is required."""
    now = now or timezone.now()
    year = current_season()
    period = SigningPeriod.objects.filter(season=year).first()
    nxt = Season.objects.filter(year=year + 1).first()
    auction_ran = bool(nxt and nxt.auction_starts_at <= now)
    synced = auction_ran and AuditEntry.objects.filter(action="Synced Fantrax", at__gte=nxt.auction_starts_at).exists()
    return [
        Check(f"Signing after {year} is locked", bool(period and period.status == SigningPeriod.Status.LOCKED)),
        Check(f"The {year + 1} auction start is entered and has passed", auction_ran),
        Check("Fantrax synced since the auction started", synced),
        Check("No unresolved Fantrax exceptions", not FantraxEvent.objects.unresolved().exists()),
    ]


@transaction.atomic
def start(year: int, user) -> Season:
    """Start `year`: freeze every team's auction budget, add the next year of farm picks, advance."""
    season = Season.objects.select_for_update().filter(year=year).first()
    if season is None:
        raise RolloverError(f"Enter the {year} auction start first")
    if season.started_at:
        raise RolloverError(f"The {year} season has already started")
    if year != current_season() + 1:
        raise RolloverError(f"The next season is {current_season() + 1}")
    if missing := [c.label for c in checklist() if not c.ok]:
        raise RolloverError("Not ready: " + "; ".join(missing))
    now, teams = timezone.now(), list(Team.objects.all())
    # Normally the first sync after the auction start froze them, as of that minute.
    froze = ""
    if not SeasonBudget.objects.filter(season=year).exists():
        froze = f"froze {freeze_budgets(year, now)} auction budgets, "
    pick_year = (FarmPick.objects.aggregate(y=Max("year"))["y"] or year) + 1
    FarmPick.objects.bulk_create(
        FarmPick(year=pick_year, round=r, original_team=t, owner=t) for t in teams for r in FARM_ROUNDS
    )
    season.started_at, season.started_by = now, user if user and user.is_authenticated else None
    season.save(update_fields=["started_at", "started_by"])
    audit(user, "Started season", f"The {year} season: {froze}added {pick_year} farm picks")
    return season
