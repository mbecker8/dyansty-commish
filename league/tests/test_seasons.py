from datetime import datetime

import pytest
from django.core.management import call_command

from league import seasons
from league.budget import team_budget
from league.fantrax_data import EASTERN
from league.models import AuditEntry, FarmPick, Season, SeasonBudget, SigningPeriod, Team
from league.tests.test_signing import settle_2026

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


# --- rollover -------------------------------------------------------------------


@pytest.fixture
def league_2026():
    call_command("import_league", verbosity=0)
    settle_2026()


def ready_to_start():
    """Signing locked, the 2027 draft day entered and past, synced since.

    The checklist compares with the real clock, so the "2027" draft day is set in the past (mid-2026).
    """
    SigningPeriod.objects.create(season=2026, status="locked", locked_at=datetime(2026, 5, 1, tzinfo=EASTERN))
    seasons.set_dates(datetime(2026, 5, 20, tzinfo=EASTERN), datetime(2026, 6, 1, 19, 0, tzinfo=EASTERN), None)
    AuditEntry.objects.create(action="Synced Fantrax")  # `at` is now, after that auction start


def test_set_dates_creates_the_next_season_and_checks_order():
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(None, datetime(2026, 1, 1, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(datetime(2027, 3, 1, tzinfo=EASTERN), datetime(2027, 2, 24, tzinfo=EASTERN), None)
    s = seasons.set_dates(None, datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None)
    assert (s.year, s.started_at, seasons.upcoming()) == (2027, None, s)


def test_checklist_lists_what_is_missing(league_2026):
    assert [c.ok for c in seasons.checklist()] == [False, False, False, True]
    ready_to_start()
    assert all(c.ok for c in seasons.checklist())


def test_start_is_refused_until_ready(league_2026):
    seasons.set_dates(None, datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError, match="Signing after 2026 is locked"):
        seasons.start(2027, None)
    assert seasons.current_season() == 2026


def test_start_freezes_budgets_adds_picks_and_advances(league_2026):
    ready_to_start()
    want = {t.code: team_budget(t, 2027) for t in Team.objects.all()}
    seasons.start(2027, None)
    assert seasons.current_season() == 2027
    for sb in SeasonBudget.objects.filter(season=2027).select_related("team"):
        b = want.pop(sb.team.code)
        assert (sb.remaining, sb.contracts, sb.buyouts, sb.farm, sb.cash_net) == (
            b.remaining,
            b.contracts,
            b.buyouts,
            b.farm,
            b.cash_net,
        )
    assert want == {}
    new = FarmPick.objects.filter(year=2031)
    assert new.count() == 2 * Team.objects.count()
    assert all(p.owner_id == p.original_team_id for p in new)
    assert AuditEntry.objects.filter(action="Started season").exists()


def test_start_happens_once(league_2026):
    ready_to_start()
    seasons.start(2027, None)
    with pytest.raises(seasons.RolloverError, match="already started"):
        seasons.start(2027, None)
    assert FarmPick.objects.filter(year=2032).count() == 0
