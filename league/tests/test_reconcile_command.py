import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from league.budget import team_budget
from league.models import Buyout, CashTrade, Contract, ReconciliationItem, Team

pytestmark = pytest.mark.django_db

Kind = ReconciliationItem.Kind


@pytest.fixture
def reconciled():
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)


def items(kind):
    return ReconciliationItem.objects.filter(kind=kind)


def test_every_linked_contract_gets_one_item(reconciled):
    linked = Contract.objects.filter(buyout__isnull=True, player__fantrax_id__isnull=False)
    assert ReconciliationItem.objects.filter(contract__isnull=False).count() == linked.count()


def test_no_inconsistencies_in_2026(reconciled):
    assert not items(Kind.INCONSISTENT).exists()


def test_proposes_buyout_for_team_that_dropped_him(reconciled):
    # Spencer Torkelson: AW contract, traded to SM, dropped by SM on Jul 31.
    item = items(Kind.DROPPED).get(contract__player__name="Spencer Torkelson")
    assert item.contract.team.code == "AW"
    assert item.team.code == "SM"
    assert item.status == ReconciliationItem.Status.PENDING


def test_accepting_a_drop_creates_the_buyout(reconciled):
    item = items(Kind.DROPPED).get(contract__player__name="Spencer Torkelson")
    item.accept()
    buyout = Buyout.objects.get(contract=item.contract)
    assert (buyout.team.code, buyout.dropped_in_season) == ("SM", 2026)
    item.contract.refresh_from_db()
    assert item.contract.team.code == "SM"  # he was SM's when dropped
    item.refresh_from_db()
    assert item.status == ReconciliationItem.Status.ACCEPTED


def test_accepting_a_trade_moves_the_contract(reconciled):
    item = items(Kind.TRADED).get(contract__player__name="Yoshinobu Yamamoto")
    item.accept()
    item.contract.refresh_from_db()
    assert item.contract.team.code == "MT"


def test_accepting_twice_is_an_error(reconciled):
    item = items(Kind.TRADED).first()
    item.accept()
    with pytest.raises(ValueError):
        item.accept()


def test_rerun_replaces_pending_items(reconciled):
    n = ReconciliationItem.objects.count()
    call_command("reconcile", verbosity=0)
    assert ReconciliationItem.objects.count() == n


def test_cash_comments_already_entered_are_not_listed(reconciled):
    # All three 2026 comments are recorded in data/league/cash_trades_from_fantrax.json.
    assert not ReconciliationItem.objects.filter(kind=Kind.CASH_COMMENT).exists()


def test_cash_from_trade_comments_is_imported(reconciled):
    got = {
        (c.budget_season, c.from_team.code, c.to_team.code, c.amount)
        for c in CashTrade.objects.exclude(fantrax_tx_id="")
    }
    assert got == {(2027, "MB", "DC", 5), (2027, "MB", "MT", 6), (2028, "MH", "AW", 10)}


def test_unentered_cash_comment_is_listed_for_manual_entry(reconciled):
    CashTrade.objects.filter(fantrax_tx_id="7o4nrf9dmme7yit3").delete()
    call_command("reconcile", verbosity=0)
    item = ReconciliationItem.objects.get(kind=Kind.CASH_COMMENT)
    assert "2028 draft budget" in item.detail
    with pytest.raises(ValueError):
        item.accept()


def test_preview_shows_2027_commitments_without_changing_anything(reconciled, capsys):
    before = (Buyout.objects.count(), ReconciliationItem.objects.filter(status="pending").count())
    call_command("reconcile", preview=True)
    out = capsys.readouterr().out
    assert "2027 commitments if every proposal is accepted" in out
    assert (Buyout.objects.count(), ReconciliationItem.objects.filter(status="pending").count()) == before
    # The preview must match what accepting every acceptable proposal actually produces.
    for item in ReconciliationItem.objects.filter(status="pending"):
        try:
            item.accept()
        except ValueError:
            pass  # manual-only items
    for team in Team.objects.all():
        b = team_budget(team, 2027)
        line = next(line for line in out.splitlines() if line.startswith(f"{team.code} "))
        assert f"contracts ${b.contracts} " in line and f"buyouts ${b.buyouts} " in line, line


def test_accepting_a_free_drop_voids_the_contract(reconciled):
    # Kodai Senga: JM's final-year contract, dropped and re-claimed by JM -> now signable, not expiring.
    item = items(Kind.DROPPED_FREE).get(contract__player__name="Kodai Senga")
    item.accept()
    item.contract.refresh_from_db()
    assert item.contract.voided_in_season == 2026
    assert not Contract.live.filter(pk=item.contract.pk).exists()


def test_replace_refuses_when_decisions_exist(reconciled):
    items(Kind.TRADED).first().accept()
    with pytest.raises(CommandError):
        call_command("import_league", replace=True, verbosity=0)


@pytest.mark.django_db
def test_refuses_to_run_before_the_league_is_imported():
    with pytest.raises(CommandError, match="import_league"):
        call_command("reconcile", verbosity=0)
