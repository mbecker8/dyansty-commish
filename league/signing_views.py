"""The signing screen, the commissioner console, the audit log and CSV export.

Every number comes from `league.signing.evaluate`, which prices decisions through `team_budget`.
"""

import csv

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from league import events, fantrax_client, seasons, signing
from league.access import commissioner_required, is_league_member, manages, member_required
from league.budget import team_budget
from league.fantrax_client import FantraxError
from league.fantrax_data import Snapshot
from league.models import (
    AuditEntry,
    Buyout,
    CashTrade,
    Contract,
    FantraxEvent,
    FantraxLeague,
    FarmPick,
    FarmPlayer,
    RosterEntry,
    SigningPeriod,
    Submission,
    Team,
    audit,
)
from league.seasons import current_season
from league.views import buyout_rows, contract_rows, next_season
from rules.farm import retained_salary


def season():
    return current_season()


# --- signing screen ------------------------------------------------------------


@member_required
def signing_home(request):
    manager = getattr(request.user, "manager", None)
    if manager is not None and is_league_member(request.user) and manages(request.user, manager.team):
        return redirect("signing", code=manager.team.code)
    if request.user.is_staff:
        return redirect("console")
    return redirect("teams")


def _signing_access(request, code):
    """(team, is own team) or a 403 response: drafts are private to the team and commissioners until lock."""
    team = get_object_or_404(Team, code=code.upper())
    own = manages(request.user, team)
    if not (own or request.user.is_staff):
        why = "Each team's signing decisions are private to that team and the commissioners until signing locks."
        return None, own, render(request, "league/forbidden.html", {"why": why}, status=403)
    return team, own, None


def _decision_rows(ev):
    """The team's choices laid out per row, since templates can't look up a dict by a variable key."""
    schedules = ev.buyout_schedules()
    return {
        "signable": [{"p": p, "chosen": ev.plan.signings.get(p.player.pk)} for p in ev.pool if p.can_sign],
        "buyouts": [
            {
                "contract": c,
                "annual": c.as_rules().annual_price,
                "schedule": schedules[c.pk],
                "total": sum(schedules[c.pk].values()),
                "checked": c.pk in ev.plan.buyouts,
            }
            for c in ev.buyout_contracts
        ],
        "farm": [
            {"farm": f, "keep_cost": retained_salary(f.salary, f.has_mlb_appearance), "keep": ev.plan.farm.get(f.pk)}
            for f in ev.farm_players
        ],
        "not_signable": [p for p in ev.pool if not p.can_sign and p.status != "farm"],
    }


@transaction.atomic
def _apply_post(request, team, commish, note):
    """Save, submit or withdraw, holding the period row so a lock can't interleave. Returns a message."""
    period = SigningPeriod.objects.select_for_update().get(season=season())
    if period.status != period.Status.OPEN:
        return messages.ERROR, "Signing isn't open any more. Nothing was saved."
    submission, _ = Submission.objects.select_for_update().get_or_create(period=period, team=team)
    submitted = submission.status == Submission.Status.SUBMITTED
    action = request.POST.get("action")
    if action == "withdraw":
        if not submitted:
            return messages.INFO, "That's already a draft."
        submission.status = Submission.Status.DRAFT
        submission.save(update_fields=["status", "updated_at"])
        audit(request.user, "Withdrew signing submission", team=team, note=note)
        return messages.SUCCESS, "Back to a draft. Submit again when you're done."
    if action not in ("save", "submit"):
        return messages.ERROR, "Unknown action. Nothing was saved."
    if submitted and not commish:
        return messages.ERROR, "Your decisions are submitted. Withdraw the submission to change them."
    ev = signing.evaluate(team, season(), signing.plan_from_form(request.POST))
    signing.save_plan(submission, ev.plan)
    audit(request.user, "Saved signing decisions", signing.describe(ev), team=team, note=note)
    if submitted and not ev.complete:
        # A commissioner's edit broke a submitted plan: it's a draft again, so it shows as not ready.
        submission.status = Submission.Status.DRAFT
        submission.save(update_fields=["status", "updated_at"])
        audit(request.user, "Signing submission back to draft", "Edited plan has problems", team=team, note=note)
        return messages.ERROR, "Saved, but it has problems, so it's back to a draft (the team needs to resubmit)."
    if action == "submit" and not submitted:
        if not ev.complete:
            return messages.ERROR, "Saved as a draft, but not submitted: fix the problems listed first."
        submission.status = Submission.Status.SUBMITTED
        submission.submitted_at = timezone.now()
        submission.submitted_by = request.user
        submission.save(update_fields=["status", "submitted_at", "submitted_by", "updated_at"])
        audit(request.user, "Submitted signing", signing.describe(ev), team=team, note=note)
        return messages.SUCCESS, "Submitted. The commissioner will review it before signing locks."
    return messages.SUCCESS, "Saved." if submitted else "Draft saved."


@member_required
def signing_team(request, code):
    team, own, denied = _signing_access(request, code)
    if denied:
        return denied
    period = signing.current_period(season())
    if period is None or period.status != period.Status.OPEN:
        return render(request, "league/signing_closed.html", {"team": team, "period": period})
    submission = Submission.objects.filter(period=period, team=team).first()
    commish = request.user.is_staff
    submitted = submission is not None and submission.status == Submission.Status.SUBMITTED
    editable = commish or not submitted
    for_other = not own  # a commissioner changing someone else's team

    if request.method == "POST":
        note = request.POST.get("note", "").strip()
        if for_other and not note:
            messages.error(request, "Add a note saying why you're changing this team's decisions. Nothing was saved.")
        else:
            level, text = _apply_post(request, team, commish, note)
            messages.add_message(request, level, text)
        return redirect("signing", code=team.code)

    ev = signing.evaluate(team, season(), signing.plan_from_submission(submission))
    return render(
        request,
        "league/signing.html",
        _decision_rows(ev)
        | {
            "ev": ev,
            "team": team,
            "period": period,
            "submission": submission,
            "editable": editable,
            "for_other": for_other,
        },
    )


@member_required
def signing_preview(request, code):
    """HTMX: re-price the form as it stands, without saving."""
    team, _, denied = _signing_access(request, code)
    if denied:
        return denied
    if request.method != "POST":
        raise Http404
    ev = signing.evaluate(team, season(), signing.plan_from_form(request.POST))
    return render(request, "league/_signing_panel.html", {"ev": ev})


# --- commissioner console ------------------------------------------------------------


@commissioner_required
def console(request):
    s = season()
    period = signing.current_period(s)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "open":
                signing.open_period(s, request.user)
                messages.success(request, "Signing is open. Managers can now make and submit their decisions.")
            elif action == "lock":
                if request.POST.get("confirm") != "yes":
                    messages.error(request, "Tick the box to confirm. Locking applies every team's decisions.")
                else:
                    signing.lock_period(s, request.user, note=request.POST.get("note", "").strip())
                    messages.success(request, "Signing is locked and every team's decisions are applied.")
            elif action == "sync":
                messages.success(request, sync_from_fantrax(request.user).summary())
            elif action == "resolve":
                event = get_object_or_404(FantraxEvent, pk=request.POST.get("event"), effect="EXCEPTION")
                event.resolve(request.user, request.POST.get("note", ""))
                messages.success(request, "Exception resolved.")
            elif action == "add_league":
                if not request.POST.get("league_id", "").strip():
                    raise ValueError("Pick a Fantrax league to add")
                league, created = FantraxLeague.objects.get_or_create(
                    league_id=request.POST.get("league_id", "").strip(),
                    defaults={"name": request.POST.get("name", ""), "season": s},
                )
                if created:
                    audit(request.user, "Added Fantrax league", str(league))
                messages.success(request, f"{league} is added. Sync from Fantrax reads it from now on.")
            elif action == "season_dates":
                auction = _local(request.POST.get("auction_starts_at"))
                if auction is None:
                    raise ValueError("Enter the auction start")
                seasons.set_dates(_local(request.POST.get("farm_draft_starts_at")), auction, request.user)
                messages.success(request, "Season dates saved.")
            elif action == "start_season":
                started = seasons.start(int(request.POST.get("year") or 0), request.user)
                messages.success(request, f"The {started.year} season has started. Auction budgets are frozen.")
        except (
            signing.SigningError,
            seasons.RolloverError,
            FantraxError,
            events.UnmatchedTeam,
            events.SeasonMissing,
            ValueError,
        ) as e:
            messages.error(request, str(e))
        return redirect("console")

    found_leagues = None
    if request.GET.get("find_leagues"):
        try:
            known = set(FantraxLeague.objects.values_list("league_id", flat=True))
            found_leagues = [
                lg for lg in fantrax_client.list_leagues(settings.FANTRAX_SECRET_ID) if lg["leagueId"] not in known
            ]
        except FantraxError as e:
            messages.error(request, str(e))

    rows = []
    if period and period.status == period.Status.OPEN:
        subs = {x.team_id: x for x in period.submissions.prefetch_related("signings", "buyouts", "farm")}
        for t in Team.objects.all():
            sub = subs.get(t.pk)
            rows.append({"team": t, "submission": sub, "ev": signing.evaluate(t, s, signing.plan_from_submission(sub))})
    return render(
        request,
        "league/console.html",
        {
            "period": period,
            "rows": rows,
            "ready": all(r["ev"].complete for r in rows),
            "unsubmitted": [r["team"].code for r in rows if getattr(r["submission"], "status", None) != "submitted"],
            "exceptions": FantraxEvent.objects.unresolved().select_related("from_team", "to_team"),
            "fantrax_leagues": FantraxLeague.objects.filter(active=True),
            "found_leagues": found_leagues,
            "roster_count": RosterEntry.objects.filter(season=s).count(),
            "missing_salaries": RosterEntry.objects.filter(season=s, salary=None).select_related("player", "team"),
            "next_year": s + 1,
            "upcoming": seasons.upcoming(),
            "checklist": seasons.checklist(),
        },
    )


def _local(text):
    """A datetime-local form value, in the league's time zone. Blank is None."""
    value = parse_datetime(text or "")
    return timezone.make_aware(value) if value else None


def sync_from_fantrax(user) -> events.SyncResult:
    """Fetch every active league live, then apply what's new in one transaction."""
    if not settings.FANTRAX_COOKIE:
        raise FantraxError("FANTRAX_COOKIE isn't set")
    s = fantrax_client.session(settings.FANTRAX_COOKIE)
    sources = [
        (lg, Snapshot.from_raw(fantrax_client.fetch_raw(s, lg.league_id)))
        for lg in FantraxLeague.objects.filter(active=True)
    ]
    return events.sync(sources, user, source_label="Sync from Fantrax")


@commissioner_required
def audit_log(request):
    entries = AuditEntry.objects.select_related("user", "team")
    if code := request.GET.get("team"):
        entries = entries.filter(team__code=code.upper())
    return render(request, "league/audit.html", {"entries": entries[:500], "teams": Team.objects.all()})


# --- export ------------------------------------------------------------------


def _csv(name, header, rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{name}-{timezone.localdate():%Y-%m-%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(header)
    writer.writerows(rows)
    return response


def _export_budgets():
    n = next_season()
    header = ["team", "name", "season", "base", "contracts", "buyouts", "farm", "missed_ip", "cash_net", "remaining"]
    rows = []
    for t in Team.objects.all():
        b = team_budget(t, n)
        rows.append([t.code, t.name, n, b.base, b.contracts, b.buyouts, b.farm, b.missed_ip, b.cash_net, b.remaining])
    return header, rows


def _export_contracts():
    live = Contract.live.select_related("player", "team").order_by("team__code", "player__name")
    header = ["team", "player", "fantrax_id", "original_price", "year_signed", "length", "final_year", "per_year",
              "sign_and_trade"]  # fmt: skip
    rows = [
        [
            c.team.code,
            c.player.name,
            c.player.fantrax_id or "",
            c.original_price,
            c.year_signed,
            c.length,
            c.final_year,
            r["annual"],
            "yes" if c.sign_and_trade else "",
        ]  # fmt: skip
        for r in contract_rows(c for c in live if c.final_year >= next_season())
        for c in [r["contract"]]
    ]
    return header, rows


def _export_buyouts():
    buyouts = Buyout.objects.select_related("contract__player", "team").order_by("team__code", "contract__player__name")
    header = ["owed_by", "player", "was_per_year", "dropped_in", "penalties_to_come", "next_season", "total_left"]
    rows = [
        [
            r["buyout"].team.code,
            r["buyout"].contract.player.name,
            r["annual"],
            r["buyout"].dropped_in_season,
            "; ".join(f"{s}: {a}" for s, a in r["remaining"].items()),
            r["next"],
            r["total"],
        ]  # fmt: skip
        for r in buyout_rows(buyouts)
    ]
    return header, rows


def _export_farm():
    farm = FarmPlayer.objects.filter(status=FarmPlayer.Status.ACTIVE).select_related("player", "team")
    header = ["team", "player", "drafted", "salary", "salary_season", "mlb_appearance"]
    rows = [
        [f.team.code, f.player.name, f.drafted_year, f.salary, f.salary_season, "yes" if f.has_mlb_appearance else ""]
        for f in farm.order_by("team__code", "player__name")
    ]
    return header, rows


def _export_picks():
    picks = FarmPick.objects.select_related("original_team", "owner").order_by("year", "round", "original_team__code")
    return ["year", "round", "original_team", "owner"], [
        [p.year, p.round, p.original_team.code, p.owner.code] for p in picks
    ]


def _export_cash():
    trades = CashTrade.objects.select_related("from_team", "to_team").order_by("budget_season", "pk")
    return ["budget_season", "from", "to", "amount", "note"], [
        [c.budget_season, c.from_team.code, c.to_team.code, c.amount, c.note] for c in trades
    ]


EXPORTS = {
    "budgets": ("Auction budgets", _export_budgets),
    "contracts": ("Contracts", _export_contracts),
    "buyouts": ("Buyouts still owing", _export_buyouts),
    "farm": ("Farm systems", _export_farm),
    "picks": ("Farm pick ownership", _export_picks),
    "cash": ("Cash trades", _export_cash),
}


@member_required
def export(request, name=None):
    if name is None:
        return render(request, "league/export.html", {"exports": [(k, label) for k, (label, _) in EXPORTS.items()]})
    if name not in EXPORTS:
        raise Http404
    header, rows = EXPORTS[name][1]()
    return _csv(name, header, rows)
