import pytest
from django.core.management import call_command

from league.models import FarmPlayer, ReconciliationItem

pytestmark = pytest.mark.django_db
Kind = ReconciliationItem.Kind


@pytest.fixture
def reconciled():
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)


def test_every_linked_farm_player_gets_one_item(reconciled):
    linked = FarmPlayer.objects.filter(player__fantrax_id__isnull=False)
    assert ReconciliationItem.objects.filter(farm_player__isnull=False).count() == linked.count()


def test_minors_players_missing_from_the_sheet_are_flagged(reconciled):
    flagged = ReconciliationItem.objects.filter(kind=Kind.FARM_UNKNOWN)
    assert {i.player.name for i in flagged} == {"Brendan Lawson", "Shunpeita Yamashita"}
    with pytest.raises(ValueError):
        flagged.first().accept()


def test_accepting_promotion_takes_him_off_the_farm(reconciled):
    item = ReconciliationItem.objects.filter(kind=Kind.FARM_PROMOTED).first()
    item.accept()
    item.farm_player.refresh_from_db()
    assert item.farm_player.status == FarmPlayer.Status.PROMOTED
    assert item.farm_player.team == item.team


def test_accepting_mlb_debut_sets_the_flag(reconciled):
    item = ReconciliationItem.objects.filter(kind=Kind.FARM_CONTINUES, mlb_debut=True).first()
    assert item is not None
    item.accept()
    item.farm_player.refresh_from_db()
    assert item.farm_player.has_mlb_appearance
