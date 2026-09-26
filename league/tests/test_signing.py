"""The signing period end to end: what a team can sign, what it costs, and what locking applies."""

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import QueryDict

from league import signing
from league.budget import team_budget
from league.models import (
    AuditEntry,
    Buyout,
    CashTrade,
    Contract,
    FarmPlayer,
    ReconciliationItem,
    RosterEntry,
    SigningPeriod,
    Submission,
    Team,
)
from rules.contracts import annual_price

pytestmark = pytest.mark.django_db
SEASON = 2026


def decide_everything():
    for item in ReconciliationItem.objects.filter(status=ReconciliationItem.Status.PENDING):
        try:
            item.accept("test")
        except ValueError:
            item.reject("test")


@pytest.fixture
def ready():
    """The league after the 2026 season: reconciled, rosters loaded, signing not open yet."""
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)
    decide_everything()
    call_command("sync_rosters", verbosity=0)


@pytest.fixture
def opened(ready):
    return signing.open_period(SEASON, None)


def same(a, b):
    """Budgets equal line for line; the order of the lines doesn't matter."""
    return (a.remaining, a.base, a.cash_net, a.missed_ip, sorted(a.lines)) == (
        b.remaining,
        b.base,
        b.cash_net,
        b.missed_ip,
        sorted(b.lines),
    )


def team(code="MB"):
    return Team.objects.get(code=code)


def farm_all(t, keep=True):
    return {f.pk: keep for f in signing.farm_candidates(t, SEASON)}


def submit(t, plan):
    period = SigningPeriod.objects.get(season=SEASON)
    sub, _ = Submission.objects.get_or_create(period=period, team=t)
    signing.save_plan(sub, plan)
    return sub


def signable(t):
    return [p for p in signing.pool(t, SEASON) if p.can_sign]


def test_full_signing_period_budgets_match_the_previews(opened):
    mb = team()
    players = signable(mb)
    candidates = signing.buyout_candidates(mb, SEASON)
    farm = list(signing.farm_candidates(mb, SEASON))
    assert len(players) >= 2 and candidates and len(farm) >= 2
    plan = signing.Plan(
        signings={players[0].player.pk: 1, players[1].player.pk: 3},
        buyouts={candidates[0].pk},
        farm={farm[0].pk: True} | {f.pk: False for f in farm[1:]},
    )
    previews = {}
    for t in Team.objects.all():
        p = plan if t == mb else signing.Plan(farm=farm_all(t))
        ev = signing.evaluate(t, SEASON, p)
        assert ev.complete, (t.code, ev.errors)
        previews[t.code] = ev.budget
        submit(t, p)

    signing.lock_period(SEASON, None)

    for t in Team.objects.all():
        assert same(team_budget(t, SEASON + 1), previews[t.code]), t.code
    new = Contract.objects.get(team=mb, player=players[1].player, year_signed=SEASON)
    assert (new.length, new.original_price) == (3, players[1].entry.salary)
    assert Buyout.objects.get(contract=candidates[0]).dropped_in_season == SEASON
    kept, released = FarmPlayer.objects.get(pk=farm[0].pk), FarmPlayer.objects.get(pk=farm[1].pk)
    assert kept.salary_season == SEASON + 1 and kept.salary == farm[0].salary + (2 if farm[0].has_mlb_appearance else 1)
    assert released.status == FarmPlayer.Status.RELEASED
    assert SigningPeriod.objects.get(season=SEASON).status == SigningPeriod.Status.LOCKED
    assert AuditEntry.objects.filter(action="Applied signing", team=mb).exists()


def test_reconcile_after_lock_proposes_nothing(opened):
    for t in Team.objects.all():
        submit(t, signing.Plan(farm=farm_all(t)))
    mb = team()
    submit(mb, signing.Plan(signings={signable(mb)[0].player.pk: 2}, farm=farm_all(mb)))
    signing.lock_period(SEASON, None)
    call_command("reconcile", verbosity=0)
    assert not ReconciliationItem.objects.filter(status=ReconciliationItem.Status.PENDING).exists()


def test_lock_twice_is_refused(opened):
    for t in Team.objects.all():
        submit(t, signing.Plan(farm=farm_all(t)))
    signing.lock_period(SEASON, None)
    with pytest.raises(signing.SigningError):
        signing.lock_period(SEASON, None)


def test_lock_refuses_while_a_team_has_undecided_farm_players(opened):
    for t in Team.objects.exclude(code="MB"):
        submit(t, signing.Plan(farm=farm_all(t)))
    with pytest.raises(signing.SigningError, match="MB"):
        signing.lock_period(SEASON, None)
    assert SigningPeriod.objects.get(season=SEASON).status == SigningPeriod.Status.OPEN
    assert not Contract.objects.filter(year_signed=SEASON).exists()


def test_pool_classifies_the_roster(opened):
    mb = team()
    by_status = {}
    for p in signing.pool(mb, SEASON):
        by_status.setdefault(p.status, []).append(p)
    assert {"signable", "under_contract", "farm"} <= set(by_status)
    for p in by_status["under_contract"]:
        assert Contract.live.filter(team=mb, player=p.player).exists()
    p = by_status["signable"][0]
    assert p.prices[1] == p.entry.salary and p.prices[3] == annual_price(p.entry.salary, 3, SEASON)
    assert p.prices[6] == p.entry.salary + 24


def test_players_on_other_teams_and_bad_ids_are_errors_not_changes(opened):
    mb, sm = team(), team("SM")
    other = signable(sm)[0].player
    their_contract = signing.buyout_candidates(sm, SEASON)[0]
    their_farm = signing.farm_candidates(sm, SEASON).first()
    ev = signing.evaluate(
        mb,
        SEASON,
        signing.Plan(signings={other.pk: 1, 999999: 1}, buyouts={their_contract.pk}, farm={their_farm.pk: True}),
    )
    assert len(ev.errors) == 4
    assert ev.budget == team_budget(mb, SEASON + 1)


def test_length_must_be_in_range(opened):
    mb = team()
    pid = signable(mb)[0].player.pk
    assert signing.evaluate(mb, SEASON, signing.Plan(signings={pid: 0})).errors
    assert signing.evaluate(mb, SEASON, signing.Plan(signings={pid: 11})).errors
    assert not signing.evaluate(mb, SEASON, signing.Plan(signings={pid: 10})).errors


def test_contract_limit(opened):
    mb = team()
    ev = signing.evaluate(mb, SEASON, signing.Plan())
    room = ev.contract_limit - ev.contract_count
    players = signable(mb)
    assert len(players) > room
    ok = signing.Plan(signings={p.player.pk: 1 for p in players[:room]})
    too_many = signing.Plan(signings={p.player.pk: 1 for p in players[: room + 1]})
    assert not [e for e in signing.evaluate(mb, SEASON, ok).errors if "limit" in e]
    assert any("limit is 10" in e for e in signing.evaluate(mb, SEASON, too_many).errors)


def test_buying_out_makes_room_under_the_limit(opened):
    mb = team()
    ev = signing.evaluate(mb, SEASON, signing.Plan())
    room = ev.contract_limit - ev.contract_count
    players = signable(mb)[: room + 1]
    plan = signing.Plan(
        signings={p.player.pk: 1 for p in players}, buyouts={signing.buyout_candidates(mb, SEASON)[0].pk}
    )
    assert not [e for e in signing.evaluate(mb, SEASON, plan).errors if "limit" in e]


def test_sign_and_trade_flag_raises_the_limit(opened):
    mb = team()
    base = signing.evaluate(mb, SEASON, signing.Plan()).contract_limit
    Contract.objects.filter(pk=signing.buyout_candidates(mb, SEASON)[0].pk).update(sign_and_trade=True)
    assert signing.evaluate(mb, SEASON, signing.Plan()).contract_limit == base + 1


def test_budget_cannot_go_below_zero(opened):
    mb, sm = team(), team("SM")
    left = team_budget(mb, SEASON + 1).remaining
    CashTrade.objects.create(budget_season=SEASON + 1, from_team=mb, to_team=sm, amount=left)
    pid = signable(mb)[0].player.pk
    ev = signing.evaluate(mb, SEASON, signing.Plan(signings={pid: 2}))
    assert any("below $0" in e for e in ev.errors)


def test_form_parsing_ignores_junk():
    data = QueryDict(mutable=True)
    data.update({"sign-5": "3", "sign-6": "", "sign-x": "2", "farm-7": "keep", "farm-8": "maybe", "farm-9": "release"})
    data.setlist("buyout", ["11", "zz"])
    plan = signing.plan_from_form(data)
    assert plan == signing.Plan(signings={5: 3}, buyouts={11}, farm={7: True, 9: False})


def test_open_refuses_while_reconciliation_is_pending():
    call_command("import_league", verbosity=0)
    call_command("reconcile", verbosity=0)
    call_command("sync_rosters", verbosity=0)
    with pytest.raises(signing.SigningError, match="pending"):
        signing.open_period(SEASON, None)


def test_open_refuses_without_rosters():
    call_command("import_league", verbosity=0)
    with pytest.raises(signing.SigningError, match="sync_rosters"):
        signing.open_period(SEASON, None)


def test_rosters_are_fixed_once_signing_opens(opened):
    with pytest.raises(CommandError, match="fixed"):
        call_command("sync_rosters", verbosity=0)


def test_sync_takes_prices_from_the_salary_snapshot_and_flags_missing_ones(ready, monkeypatch):
    from league.fantrax_data import Snapshot

    entry = RosterEntry.objects.filter(team=team()).exclude(status="Minors").first()
    fid = entry.player.fantrax_id
    salaries = Snapshot.salaries
    monkeypatch.setattr(Snapshot, "salaries", lambda self: {k: v for k, v in salaries(self).items() if k != fid})
    call_command("sync_rosters", verbosity=0)
    entry = RosterEntry.objects.get(player__fantrax_id=fid)
    assert entry.salary is None
    p = next(p for p in signing.pool(entry.team, SEASON) if p.player == entry.player)
    assert p.status in ("check", "under_contract", "expiring")


def test_replace_refuses_once_signing_has_started():
    call_command("import_league", verbosity=0)
    call_command("sync_rosters", verbosity=0)
    signing.open_period(SEASON, None)
    with pytest.raises(CommandError, match="signing"):
        call_command("import_league", replace=True, verbosity=0)


def test_panel_lines_add_up(opened):
    mb = team()
    plan = signing.Plan(
        signings={signable(mb)[0].player.pk: 4},
        buyouts={signing.buyout_candidates(mb, SEASON)[0].pk},
        farm=farm_all(mb),
    )
    ev = signing.evaluate(mb, SEASON, plan)
    assert ev.new_contract_total and ev.freed and ev.new_penalties and ev.farm_cost
    assert (
        ev.committed.remaining - ev.new_contract_total + ev.freed - ev.new_penalties - ev.farm_cost
        == ev.budget.remaining
    )
