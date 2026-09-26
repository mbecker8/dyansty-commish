"""The signing period: who each team can sign, what its decisions cost, and applying them at lock.

Every dollar comes from `team_budget` with the pending decisions passed as `Changes`, so the
preview a manager sees is computed exactly the way the budget is after the lock.
"""

from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

import rules.contracts
from league.budget import Changes, team_budget
from league.models import (
    Buyout,
    Contract,
    FarmPlayer,
    ReconciliationItem,
    RosterEntry,
    SigningPeriod,
    Submission,
    SubmissionBuyout,
    SubmissionFarm,
    SubmissionSigning,
    Team,
    audit,
)
from rules.budget import Budget
from rules.buyouts import buyout_schedule
from rules.farm import retained_salary
from rules.signability import Signability, signability
from rules.validation import MAX_CONTRACT_LENGTH, validate_signing

LENGTHS = range(1, MAX_CONTRACT_LENGTH + 1)


class SigningError(Exception):
    """A signing-period action that isn't allowed right now."""


# --- the pool ---------------------------------------------------------------


@dataclass
class PoolPlayer:
    """A player on a team's roster at the blackout, and whether the team can sign him."""

    entry: RosterEntry
    status: str  # signable, under_contract, expiring, farm, check
    reason: str = ""
    prices: dict[int, int] = field(default_factory=dict)  # length -> price per year

    @property
    def player(self):
        return self.entry.player

    @property
    def can_sign(self) -> bool:
        return self.status == "signable"


def pool(team: Team, season: int) -> list[PoolPlayer]:
    """Every player on the team's blackout roster, classified. Only `signable` ones can get a new contract."""
    entries = list(RosterEntry.objects.filter(season=season, team=team).select_related("player"))
    ids = [e.player_id for e in entries]
    latest = {}
    for c in Contract.live.filter(player_id__in=ids).select_related("team").order_by("year_signed", "pk"):
        if c.player_id not in latest or c.final_year >= latest[c.player_id].final_year:
            latest[c.player_id] = c
    farm = {
        f.player_id: f
        for f in FarmPlayer.objects.filter(player_id__in=ids, status=FarmPlayer.Status.ACTIVE).select_related("team")
    }
    out = []
    for e in entries:
        c, f = latest.get(e.player_id), farm.get(e.player_id)
        if f and f.team_id == team.pk:
            p = PoolPlayer(e, "farm", "On your farm: keep or release him below")
        elif f:
            p = PoolPlayer(e, "check", f"On {f.team.code}'s farm; the commissioner needs to sort this out")
        elif c and c.team_id != team.pk and c.final_year >= season:
            p = PoolPlayer(e, "check", f"Under contract to {c.team.code}; the commissioner needs to sort this out")
        elif e.salary is None:
            p = PoolPlayer(e, "check", "No end-of-season salary from Fantrax; ask the commissioner")
        elif e.status == "Minors":
            p = PoolPlayer(e, "check", "In a Fantrax minors slot but not on a farm; ask the commissioner")
        else:
            s = signability(c.as_rules() if c and c.team_id == team.pk else None, season)
            if s is Signability.UNDER_CONTRACT:
                p = PoolPlayer(e, "under_contract", f"Under contract through {c.final_year}")
            elif s is Signability.EXPIRING:
                p = PoolPlayer(e, "expiring", "Contract ended; he goes back to the auction pool")
            else:
                p = PoolPlayer(e, "signable")
                p.prices = {n: rules.contracts.annual_price(e.salary, n, season) for n in LENGTHS}
        out.append(p)
    return sorted(out, key=lambda p: p.player.name)


def buyout_candidates(team: Team, season: int) -> list[Contract]:
    """Live contracts the team could buy out now: ones with a season still to run after this one."""
    live = Contract.live.filter(team=team, year_signed__lte=season).select_related("player").order_by("player__name")
    return [c for c in live if c.final_year > season]


def farm_candidates(team: Team, season: int):
    """Farm players the team decides on: active, with a salary for the season just ended."""
    return (
        FarmPlayer.objects.filter(team=team, status=FarmPlayer.Status.ACTIVE, salary_season=season)
        .select_related("player")
        .order_by("player__name")
    )


# --- decisions ----------------------------------------------------------------


@dataclass
class Plan:
    """A team's decisions, keyed by primary key: player -> length, contract ids, farm player -> keep."""

    signings: dict[int, int] = field(default_factory=dict)
    buyouts: set[int] = field(default_factory=set)
    farm: dict[int, bool] = field(default_factory=dict)


def plan_from_submission(submission: Submission | None) -> Plan:
    if submission is None:
        return Plan()
    return Plan(
        signings={s.player_id: s.length for s in submission.signings.all()},
        buyouts={b.contract_id for b in submission.buyouts.all()},
        farm={f.farm_player_id: f.keep for f in submission.farm.all()},
    )


def _int(text):
    try:
        return int(text)
    except TypeError, ValueError:
        return None


def plan_from_form(data) -> Plan:
    """Read the signing form: sign-<player id>=<length>, buyout=<contract id>..., farm-<farm id>=keep|release.

    Nothing here is trusted; `evaluate` checks every id against the team.
    """
    plan = Plan()
    for key, value in data.items():
        if key.startswith("sign-") and (pid := _int(key[5:])) is not None and (n := _int(value)):
            plan.signings[pid] = n
        elif key.startswith("farm-") and (fid := _int(key[5:])) is not None and value in ("keep", "release"):
            plan.farm[fid] = value == "keep"
    plan.buyouts = {cid for cid in map(_int, data.getlist("buyout")) if cid is not None}
    return plan


@dataclass
class Evaluation:
    team: Team
    season: int
    plan: Plan
    pool: list[PoolPlayer]
    buyout_contracts: list[Contract]
    farm_players: list[FarmPlayer]
    new_contracts: list[rules.contracts.Contract]
    budget: Budget
    committed: Budget  # before any of this signing period's decisions
    errors: list[str]
    undecided_farm: list[FarmPlayer]
    contract_count: int
    contract_limit: int

    @property
    def complete(self) -> bool:
        """Ready to submit or lock: no errors and every farm player decided."""
        return not self.errors and not self.undecided_farm

    # The change from `committed` to `budget`, line by line; they add up by construction.
    @property
    def new_contract_total(self) -> int:
        return sum(c.annual_price for c in self.new_contracts)

    @property
    def freed(self) -> int:
        """Next season's price of the contracts being bought out."""
        return self.committed.contracts - (self.budget.contracts - self.new_contract_total)

    @property
    def new_penalties(self) -> int:
        return self.budget.buyouts - self.committed.buyouts

    @property
    def farm_cost(self) -> int:
        return self.budget.farm - self.committed.farm

    def buyout_schedules(self) -> dict[int, dict[int, int]]:
        """Contract id -> the penalty per season if it's bought out now."""
        return {c.pk: buyout_schedule(c.as_rules(), self.season) for c in self.buyout_contracts}


def evaluate(team: Team, season: int, plan: Plan) -> Evaluation:
    """Price and check a team's decisions. Ids that don't belong to the team become errors, not changes."""
    errors = []
    players = pool(team, season)
    by_player = {p.player.pk: p for p in players}
    candidates = buyout_candidates(team, season)
    by_contract = {c.pk: c for c in candidates}
    farm = list(farm_candidates(team, season))
    by_farm = {f.pk: f for f in farm}

    clean = Plan()
    for pid, length in plan.signings.items():
        p = by_player.get(pid)
        if p is None:
            errors.append(f"Player {pid} isn't on {team.code}'s roster")
        elif not p.can_sign:
            errors.append(f"{p.player.name} can't be signed: {p.reason.lower()}")
        elif length not in LENGTHS:
            errors.append(f"{p.player.name}: a contract is 1 to {MAX_CONTRACT_LENGTH} years, not {length}")
        else:
            clean.signings[pid] = length
    for cid in plan.buyouts:
        if cid in by_contract:
            clean.buyouts.add(cid)
        else:
            errors.append(f"Contract {cid} isn't one {team.code} can buy out")
    for fid, keep in plan.farm.items():
        if fid in by_farm:
            clean.farm[fid] = keep
        else:
            errors.append(f"Farm player {fid} isn't on {team.code}'s farm")

    new = [
        rules.contracts.Contract(str(pid), by_player[pid].entry.salary, season, length)
        for pid, length in clean.signings.items()
    ]
    kept = {
        str(by_farm[fid].player_id): retained_salary(by_farm[fid].salary, by_farm[fid].has_mlb_appearance)
        for fid, keep in clean.farm.items()
        if keep
    }
    changes = Changes(
        dropped_in=season,
        new_contracts=new,
        bought_out=[by_contract[cid] for cid in clean.buyouts],
        farm_kept=kept,
    )
    budget = team_budget(team, season + 1, changes)

    existing = [c for c in Contract.live.filter(team=team).select_related("player") if c.pk not in clean.buyouts]
    extra = sum(1 for c in existing if c.sign_and_trade and c.as_rules().covers(season + 1))
    names = {str(p.player.pk): p.player.name for p in players} | {str(c.player_id): c.player.name for c in existing}
    errors += validate_signing([c.as_rules() for c in existing], new, season, extra_allowed=extra, names=names)
    if budget.remaining < 0:
        errors.append(f"This leaves ${budget.remaining} for the {season + 1} auction; it can't go below $0")

    return Evaluation(
        team=team,
        season=season,
        plan=clean,
        pool=players,
        buyout_contracts=candidates,
        farm_players=farm,
        new_contracts=new,
        budget=budget,
        committed=team_budget(team, season + 1),
        errors=errors,
        undecided_farm=[f for f in farm if f.pk not in clean.farm],
        contract_count=sum(1 for c in [*[c.as_rules() for c in existing], *new] if c.covers(season + 1)),
        contract_limit=10 + extra,
    )


def describe(ev: Evaluation) -> str:
    """One line per decision, for the audit log."""
    names = {p.player.pk: p.player.name for p in ev.pool}
    parts = [f"sign {names[pid]} {n}yr" for pid, n in sorted(ev.plan.signings.items(), key=lambda x: names[x[0]])]
    contracts = {c.pk: c for c in ev.buyout_contracts}
    parts += [f"buy out {contracts[cid].player.name}" for cid in sorted(ev.plan.buyouts)]
    farm = {f.pk: f for f in ev.farm_players}
    parts += [f"{'keep' if k else 'release'} {farm[fid].player.name}" for fid, k in sorted(ev.plan.farm.items())]
    return "; ".join(parts) or "no decisions"


# --- periods and submissions ----------------------------------------------------


def current_period(season: int) -> SigningPeriod | None:
    return SigningPeriod.objects.filter(season=season).first()


@transaction.atomic
def save_plan(submission: Submission, plan: Plan) -> None:
    submission.signings.all().delete()
    submission.buyouts.all().delete()
    submission.farm.all().delete()
    SubmissionSigning.objects.bulk_create(
        SubmissionSigning(submission=submission, player_id=pid, length=n) for pid, n in plan.signings.items()
    )
    SubmissionBuyout.objects.bulk_create(SubmissionBuyout(submission=submission, contract_id=c) for c in plan.buyouts)
    SubmissionFarm.objects.bulk_create(
        SubmissionFarm(submission=submission, farm_player_id=f, keep=k) for f, k in plan.farm.items()
    )
    submission.save(update_fields=["updated_at"])


def pending_reconciliation(season: int) -> int:
    return ReconciliationItem.objects.filter(season=season, status=ReconciliationItem.Status.PENDING).count()


@transaction.atomic
def open_period(season: int, user) -> SigningPeriod:
    period, _ = SigningPeriod.objects.select_for_update().get_or_create(season=season)
    if period.status != SigningPeriod.Status.PLANNED:
        raise SigningError(f"Signing after {season} is already {period.get_status_display().lower()}")
    if n := pending_reconciliation(season):
        raise SigningError(f"{n} reconciliation item(s) for {season} are still pending; decide them first")
    if not RosterEntry.objects.filter(season=season).exists():
        raise SigningError(f"No rosters loaded for {season}; run sync_rosters first")
    period.status = SigningPeriod.Status.OPEN
    period.opened_at = timezone.now()
    period.save()
    audit(user, "Opened signing", f"Signing after the {season} season")
    return period


@transaction.atomic
def lock_period(season: int, user, note: str = "") -> SigningPeriod:
    """Apply every team's saved decisions, all or nothing. Refuses if any team isn't ready."""
    period = SigningPeriod.objects.select_for_update().filter(season=season).first()
    if period is None or period.status != SigningPeriod.Status.OPEN:
        raise SigningError("Signing isn't open")
    submissions = {s.team_id: s for s in period.submissions.prefetch_related("signings", "buyouts", "farm")}
    evaluations = [evaluate(t, season, plan_from_submission(submissions.get(t.pk))) for t in Team.objects.all()]
    not_ready = [ev.team.code for ev in evaluations if not ev.complete]
    if not_ready:
        raise SigningError(f"Not ready to lock: {', '.join(not_ready)} (errors or undecided farm players)")
    for ev in evaluations:
        by_player = {p.player.pk: p for p in ev.pool}
        for pid, length in ev.plan.signings.items():
            Contract.objects.create(
                team=ev.team,
                player_id=pid,
                original_price=by_player[pid].entry.salary,
                year_signed=season,
                length=length,
            )
        for c in ev.buyout_contracts:
            if c.pk in ev.plan.buyouts:
                if not c.year_signed <= season < c.final_year:
                    raise SigningError(f"{c.player.name}: can't buy out a contract ending {c.final_year} in {season}")
                Buyout.objects.create(contract=c, team=ev.team, dropped_in_season=season, note="Signing period")
        for f in ev.farm_players:
            if ev.plan.farm[f.pk]:
                f.salary = retained_salary(f.salary, f.has_mlb_appearance)
                f.salary_season = season + 1
            else:
                f.status = FarmPlayer.Status.RELEASED
            f.save(update_fields=["salary", "salary_season", "status"])
        audit(user, "Applied signing", describe(ev), team=ev.team)
    period.status = SigningPeriod.Status.LOCKED
    period.locked_at = timezone.now()
    period.save()
    audit(user, "Locked signing", f"Signing after the {season} season", note=note)
    return period
