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


def test_window_runs_from_draft_day_to_draft_day():
    s27 = add_2027()
    assert seasons.window(Season.objects.get(year=2026)) == (AUCTION_2026, s27.farm_draft_starts_at)
    assert seasons.window(s27) == (s27.farm_draft_starts_at, None)


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
    Season.objects.create(
        year=2027,
        farm_draft_starts_at=datetime(2026, 5, 20, tzinfo=EASTERN),
        auction_starts_at=datetime(2026, 6, 1, 19, 0, tzinfo=EASTERN),
    )
    AuditEntry.objects.create(action="Synced Fantrax")  # `at` is now, after that auction start


def test_set_dates_creates_the_next_season_and_checks_order():
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(None, datetime(2026, 1, 1, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError):
        seasons.set_dates(datetime(2027, 3, 1, tzinfo=EASTERN), datetime(2027, 2, 24, tzinfo=EASTERN), None)
    with pytest.raises(seasons.RolloverError, match="after the 2026 auction"):  # a typo'd year
        seasons.set_dates(datetime(2026, 2, 20, tzinfo=EASTERN), datetime(2027, 2, 24, tzinfo=EASTERN), None)
    s = seasons.set_dates(
        datetime(2027, 2, 20, 18, 0, tzinfo=EASTERN), datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None
    )
    assert (s.year, s.started_at, seasons.upcoming()) == (2027, None, s)


def test_set_dates_refuses_time_already_synced(league_2026):
    # settle_2026 synced up to now, so draft day can't be moved into the past.
    with pytest.raises(seasons.RolloverError, match="synced up to"):
        seasons.set_dates(datetime(2026, 5, 20, tzinfo=EASTERN), datetime(2027, 2, 24, 19, 0, tzinfo=EASTERN), None)
    assert seasons.set_dates(datetime(2099, 2, 20, tzinfo=EASTERN), datetime(2099, 2, 24, tzinfo=EASTERN), None)


def test_checklist_lists_what_is_missing(league_2026):
    assert [c.ok for c in seasons.checklist()] == [False, False, False, True]
    ready_to_start()
    assert all(c.ok for c in seasons.checklist())


def test_checklist_needs_discord_when_it_is_set_up(league_2026, settings):
    settings.DISCORD_BOT_TOKEN, settings.DISCORD_GUILD_ID, settings.DISCORD_CATEGORY_ID = "t", "1", "10"
    ready_to_start()
    labels = {c.label: c.ok for c in seasons.checklist()}
    assert labels["Discord synced since the auction started"] is False
    assert labels["No unresolved Discord exceptions"] is True
    AuditEntry.objects.create(action="Synced Discord")
    assert all(c.ok for c in seasons.checklist())


def test_start_is_refused_until_ready(league_2026):
    add_2027()
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


def test_a_farm_draft_pick_is_in_the_frozen_budget(league_2026):
    from league.events import Processor
    from league.fantrax_data import Move
    from league.models import FantraxLeague

    team = FarmPick.objects.filter(year=2027, player=None).first().owner
    before = team_budget(team, 2027).farm
    ready_to_start()
    proc = Processor(FantraxLeague.objects.get(), {team.fantrax_id: team}, {"newfid": "Prospect"})
    claim = Move(datetime(2026, 5, 25, 20, 0, tzinfo=EASTERN), "CLAIM", "newfid", None, team.fantrax_id, "d1")
    assert proc.apply_move(claim).effect == "FARM_DRAFTED"
    seasons.start(2027, None)
    assert SeasonBudget.objects.get(season=2027, team=team).farm == before + 1


def test_start_keeps_budgets_a_sync_froze(league_2026):
    ready_to_start()
    team = Team.objects.first()
    seasons.freeze_budgets(2027, datetime(2026, 6, 1, 19, 0, tzinfo=EASTERN))
    SeasonBudget.objects.filter(team=team).update(remaining=1)
    seasons.start(2027, None)
    assert SeasonBudget.objects.get(team=team).remaining == 1


def test_a_started_season_cannot_be_deleted_in_the_admin(rf):
    from django.contrib import admin
    from django.contrib.auth.models import User

    request = rf.get("/")
    request.user = User(is_staff=True, is_superuser=True)
    site_admin = admin.site._registry[Season]
    assert not site_admin.has_delete_permission(request, Season.objects.get(year=2026))
    assert site_admin.has_delete_permission(request, add_2027())
