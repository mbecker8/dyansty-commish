"""Accepting and rejecting reconciliation items must be safe to repeat and hard to corrupt."""

import pytest
from django.contrib.admin.models import ADDITION, LogEntry
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.db.models import ProtectedError
from django.test import Client

from league.models import Buyout, CashTrade, Contract, FarmPlayer, ReconciliationItem, Team

pytestmark = pytest.mark.django_db
Kind = ReconciliationItem.Kind


@pytest.fixture
def reconciled():
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)


def test_failed_accept_leaves_nothing_half_applied(reconciled):
    item = ReconciliationItem.objects.filter(kind=Kind.DROPPED).first()
    contract = item.contract
    original_team = contract.team_id
    moved = Team.objects.exclude(pk=original_team).first()
    item.team = moved  # force the accept to move the contract before failing
    item.save()
    Buyout.objects.create(contract=contract, team=moved, dropped_in_season=2026)  # entered by hand
    with pytest.raises(IntegrityError):
        item.accept()
    contract.refresh_from_db()
    item.refresh_from_db()
    assert (contract.team_id, item.status) == (original_team, ReconciliationItem.Status.PENDING)


def test_accepting_a_stale_copy_twice_applies_once(reconciled):
    a = ReconciliationItem.objects.get(kind=Kind.FARM_UNKNOWN, player__name="Brendan Lawson")
    b = ReconciliationItem.objects.get(pk=a.pk)
    a.accept()
    with pytest.raises(ValueError):
        b.accept()
    assert FarmPlayer.objects.filter(player=a.player).count() == 1


def test_rejected_unknown_farm_player_is_not_proposed_again(reconciled):
    item = ReconciliationItem.objects.get(kind=Kind.FARM_UNKNOWN, player__name="Brendan Lawson")
    item.reject("not a draft pick")
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(player=item.player, status=ReconciliationItem.Status.PENDING).exists()


def test_accepted_unknown_farm_player_is_not_proposed_again(reconciled):
    item = ReconciliationItem.objects.get(kind=Kind.FARM_UNKNOWN, player__name="Shunpeita Yamashita")
    item.accept()
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(player=item.player, status=ReconciliationItem.Status.PENDING).exists()


def test_decided_items_protect_what_they_refer_to(reconciled):
    item = ReconciliationItem.objects.filter(kind=Kind.TRADED).first()
    item.accept()
    with pytest.raises(ProtectedError):
        Contract.objects.filter(pk=item.contract_id).delete()


def test_replace_refuses_after_hand_edits_in_admin(reconciled):
    user = User.objects.create_superuser("c", "c@example.com", "pw")
    cash = CashTrade.objects.create(
        budget_season=2027, from_team=Team.objects.first(), to_team=Team.objects.last(), amount=7
    )
    LogEntry.objects.log_actions(user.pk, [cash], ADDITION)
    with pytest.raises(CommandError, match="admin"):
        call_command("import_league", replace=True, verbosity=0)
    assert CashTrade.objects.filter(pk=cash.pk).exists()


def test_reconcile_rejects_a_bad_since_date(reconciled):
    with pytest.raises(CommandError):
        call_command("reconcile", since="2026-02-30", verbosity=0)


@pytest.fixture
def admin_client(reconciled, settings):
    # Tests run with DEBUG off and no collectstatic, so skip the hashed-manifest lookup.
    settings.STORAGES = settings.STORAGES | {
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}
    }
    User.objects.create_superuser("c", "c@example.com", "pw")
    client = Client()
    client.login(username="c", password="pw")
    return client


def test_admin_batch_accept_keeps_going_past_a_failure(admin_client):
    bad = ReconciliationItem.objects.filter(kind=Kind.DROPPED).first()
    Buyout.objects.create(contract=bad.contract, team=bad.team, dropped_in_season=2026)
    good = ReconciliationItem.objects.filter(kind=Kind.TRADED).first()
    response = admin_client.post(
        "/admin/league/reconciliationitem/",
        {"action": "accept_selected", "_selected_action": [bad.pk, good.pk]},
        follow=True,
    )
    assert response.status_code == 200
    bad.refresh_from_db()
    good.refresh_from_db()
    assert (bad.status, good.status) == ("pending", "accepted")


def test_admin_cannot_edit_or_delete_decided_items(admin_client):
    item = ReconciliationItem.objects.filter(kind=Kind.TRADED).first()
    item.accept()
    change = admin_client.get(f"/admin/league/reconciliationitem/{item.pk}/change/")
    assert 'name="team"' not in change.content.decode()
    delete = admin_client.post(f"/admin/league/reconciliationitem/{item.pk}/delete/", {"post": "yes"})
    assert delete.status_code == 403
    assert ReconciliationItem.objects.filter(pk=item.pk).exists()


def test_reconcile_refuses_a_season_the_snapshot_is_not_from(reconciled):
    with pytest.raises(CommandError, match="2026"):
        call_command("reconcile", season=2027, verbosity=0)


def test_note_typed_on_a_pending_item_is_kept_when_rejecting(admin_client):
    item = ReconciliationItem.objects.filter(kind=Kind.TRADED).first()
    change = admin_client.get(f"/admin/league/reconciliationitem/{item.pk}/change/").content.decode()
    assert 'name="decided_note"' in change and 'name="team"' not in change
    item.decided_note = "entered the trade by hand"
    item.save()
    admin_client.post(
        "/admin/league/reconciliationitem/", {"action": "reject_selected", "_selected_action": [item.pk]}, follow=True
    )
    item.refresh_from_db()
    assert item.status == "rejected" and "entered the trade by hand" in item.decided_note
