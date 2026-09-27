from datetime import datetime

import pytest

from league import seasons
from league.fantrax_data import EASTERN
from league.models import Season

pytestmark = pytest.mark.django_db
AUCTION_2026 = datetime(2026, 2, 25, 16, 0, tzinfo=EASTERN)


def add_2027(started=False):
    return Season.objects.create(
        year=2027,
        farm_draft_starts_at=datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN),
        auction_starts_at=datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN),
        started_at=datetime(2027, 2, 25, tzinfo=EASTERN) if started else None,
    )


def test_2026_is_seeded_and_current():
    s = Season.objects.get()
    assert (s.year, s.auction_starts_at, s.farm_draft_starts_at) == (2026, AUCTION_2026, None)
    assert s.started_at is not None
    assert seasons.current_season() == 2026


def test_a_season_is_current_only_once_started():
    add_2027()
    assert seasons.current_season() == 2026
    Season.objects.filter(year=2027).update(started_at=datetime(2027, 2, 25, tzinfo=EASTERN))
    assert seasons.current_season() == 2027


def test_season_at_uses_the_auction_start():
    add_2027()
    assert seasons.season_at(AUCTION_2026).year == 2026
    assert seasons.season_at(datetime(2027, 2, 24, 18, 59, tzinfo=EASTERN)).year == 2026
    assert seasons.season_at(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)).year == 2027
    assert seasons.season_at(datetime(2026, 2, 25, 15, 59, tzinfo=EASTERN)) is None


def test_farm_draft_window_ends_at_the_auction():
    add_2027()
    assert seasons.farm_draft_at(datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN)).year == 2027
    assert seasons.farm_draft_at(datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN)) is None
    assert seasons.farm_draft_at(datetime(2026, 7, 1, tzinfo=EASTERN)) is None  # 2026 has no window


def test_window_runs_to_the_next_auction():
    s27 = add_2027()
    assert seasons.window(Season.objects.get(year=2026)) == (AUCTION_2026, s27.auction_starts_at)
    assert seasons.window(s27) == (s27.auction_starts_at, None)
