"""Which league season it is, which season a moment belongs to, and starting the next one.

Season S runs from the S auction start until just before the S+1 auction. The current season
is the latest one the commissioner has started; a Fantrax move's season comes from its date.
"""

from datetime import datetime

from league.models import Season


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


def window(season: Season) -> tuple[datetime, datetime | None]:
    """[auction start, next season's auction start); open-ended until the next one is entered."""
    following = Season.objects.filter(year=season.year + 1).first()
    return season.auction_starts_at, following.auction_starts_at if following else None
