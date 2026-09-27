import datetime

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group

from league import models


def _teams_of(obj) -> list:
    """Every team an object belongs to (a cash trade has two), so the audit log's team filter finds it."""
    if isinstance(obj, models.Team):
        return [obj]
    teams = [getattr(obj, f, None) for f in ("team", "from_team", "to_team", "owner", "original_team")]
    return list(dict.fromkeys(t for t in teams if isinstance(t, models.Team))) or [None]


# Year fields become dropdowns starting at the present year. YEARS_AHEAD fields run forward
# (a coming auction or draft); the rest run back (when something was signed or dropped).
YEAR_FIELDS = {
    "year",
    "season",
    "budget_season",
    "year_signed",
    "voided_in_season",
    "dropped_in_season",
    "drafted_year",
    "salary_season",
}
YEARS_AHEAD = {("cashtrade", "budget_season"), ("budgetadjustment", "season"), ("farmpick", "year")}


class YearChoicesForm(forms.ModelForm):
    """Picks years from a list instead of typing them. A record's existing year is always offered."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        this_year = datetime.date.today().year
        for name, field in list(self.fields.items()):
            if name not in YEAR_FIELDS:
                continue
            if (self._meta.model._meta.model_name, name) in YEARS_AHEAD:
                years = list(range(this_year, this_year + 5))
            else:
                years = list(range(this_year, this_year - 11, -1))
            current = getattr(self.instance, name, None) if self.instance.pk else None
            if current is not None and current not in years:
                years = sorted([*years, current], reverse=years[0] > years[-1])
            choices = [(y, y) for y in years]
            if not field.required:
                choices.insert(0, ("", "---------"))
            self.fields[name] = forms.TypedChoiceField(
                choices=choices,
                coerce=int,
                empty_value=None,
                required=field.required,
                label=field.label,
                help_text=field.help_text,
            )


class AuditedAdmin(admin.ModelAdmin):
    """Every add, change and delete in the admin also goes in the league audit log."""

    form = YearChoicesForm

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        verb = "changed" if change else "added"
        fields = f" ({', '.join(form.changed_data)})" if change and form.changed_data else ""
        for team in _teams_of(obj):
            models.audit(request.user, f"Admin: {verb} {self.opts.verbose_name}", f"{obj}{fields}", team=team)

    def delete_model(self, request, obj):
        detail, teams = str(obj), _teams_of(obj)
        super().delete_model(request, obj)
        for team in teams:
            models.audit(request.user, f"Admin: deleted {self.opts.verbose_name}", detail, team=team)

    def delete_queryset(self, request, queryset):
        gone = [(str(o), _teams_of(o)) for o in queryset]
        super().delete_queryset(request, queryset)
        for detail, teams in gone:
            for team in teams:
                models.audit(request.user, f"Admin: deleted {self.opts.verbose_name}", detail, team=team)


@admin.register(models.Team)
class TeamAdmin(AuditedAdmin):
    list_display = ["code", "name", "fantrax_id"]


@admin.register(models.Manager)
class ManagerAdmin(AuditedAdmin):
    """Link a person to a team. They can sign in once their Discord ID is here."""

    list_display = ["name", "team", "is_commissioner", "discord_username", "discord_id", "user"]
    list_filter = ["team"]
    search_fields = ["name", "discord_username", "discord_id"]
    readonly_fields = ["discord_username", "user"]


@admin.register(models.Player)
class PlayerAdmin(AuditedAdmin):
    list_display = ["name", "fantrax_id", "positions"]
    search_fields = ["name", "fantrax_id"]


@admin.register(models.Contract)
class ContractAdmin(AuditedAdmin):
    list_display = ["player", "team", "original_price", "year_signed", "length", "sign_and_trade"]
    list_filter = ["team"]
    search_fields = ["player__name"]


@admin.register(models.Buyout)
class BuyoutAdmin(AuditedAdmin):
    list_display = ["contract", "team", "dropped_in_season"]
    list_filter = ["team"]


@admin.register(models.FarmPlayer)
class FarmPlayerAdmin(AuditedAdmin):
    list_display = ["player", "team", "drafted_year", "salary", "salary_season", "has_mlb_appearance", "status"]
    list_filter = ["team", "status"]


admin.site.register([models.TeamAlias, models.CashTrade, models.BudgetAdjustment], AuditedAdmin)


@admin.register(models.FarmPick)
class FarmPickAdmin(AuditedAdmin):
    """Change the owner for a pick trade. Pick numbers come from Final standings."""

    list_display = ["year", "round", "original_team", "owner", "player"]
    list_filter = ["year", "owner"]


# Access comes from Manager.is_commissioner, never from permission groups.
admin.site.unregister(Group)


@admin.register(models.FinalStanding)
class FinalStandingAdmin(AuditedAdmin):
    """Each season's final places (1 = champion). They set the next farm draft's order."""

    list_display = ["season", "place", "team"]
    list_editable = ["place"]
    list_filter = ["season"]


@admin.register(models.Season)
class SeasonAdmin(AuditedAdmin):
    """Set a season's dates on the console; change a started season's dates here."""

    list_display = ["year", "farm_draft_starts_at", "auction_starts_at", "started_at"]
    readonly_fields = ["started_at", "started_by"]
    fieldsets = [
        (
            None,
            {
                "fields": ["year", "farm_draft_starts_at", "auction_starts_at", "started_at", "started_by"],
                "description": "Changing a date doesn't re-read moves already synced: a move near the old date "
                "stays in the season (and farm draft) it was filed under. Fix those by hand.",
            },
        )
    ]

    def has_delete_permission(self, request, obj=None):
        # The current season is the latest started one; deleting it would leave the league without one.
        return super().has_delete_permission(request, obj) and not (obj and obj.started_at)


@admin.register(models.SeasonBudget)
class SeasonBudgetAdmin(admin.ModelAdmin):
    """Frozen when a season starts. Read-only: it's what each team brought to that auction."""

    list_display = ["season", "team", "remaining", "frozen_at"]
    list_filter = ["season"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.FantraxLeague)
class FantraxLeagueAdmin(AuditedAdmin):
    """Leagues Sync from Fantrax reads. Add the renewed league from the console's league finder."""

    list_display = ["league_id", "name", "season", "process_since", "active"]


@admin.register(models.FantraxEvent)
class FantraxEventAdmin(admin.ModelAdmin):
    """What each Fantrax fact changed. Read-only: fix a wrong result on the contract, buyout or farm page."""

    list_display = ["happened_at", "kind", "effect", "player_name", "from_team", "to_team", "detail", "resolved_at"]
    list_filter = ["effect", "kind", "league"]
    search_fields = ["player_name", "detail"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(models.RosterEntry)
class RosterEntryAdmin(AuditedAdmin):
    """Loaded by `sync_rosters`. Fill in a salary here if the season-end snapshot is missing one.

    While signing is open, only a missing salary can be set: changing one would silently
    reprice a contract a team may already have submitted.
    """

    list_display = ["player", "team", "season", "salary", "status"]
    list_filter = ["season", "team", "status"]
    search_fields = ["player__name"]
    raw_id_fields = ["player"]

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return []
        period = models.SigningPeriod.objects.filter(season=obj.season).first()
        if period and period.status != models.SigningPeriod.Status.PLANNED:
            fields = ["season", "team", "player", "status"]
            can_fill = obj.salary is None and period.status == models.SigningPeriod.Status.OPEN
            return fields if can_fill else [*fields, "salary"]
        return []


@admin.register(models.SigningPeriod)
class SigningPeriodAdmin(AuditedAdmin):
    """Open and lock from the commissioner console; set the deadline here."""

    list_display = ["season", "status", "deadline", "opened_at", "locked_at"]
    readonly_fields = ["status", "opened_at", "locked_at"]


class SigningInline(admin.TabularInline):
    model = models.SubmissionSigning
    extra = 0
    raw_id_fields = ["player"]


@admin.register(models.Submission)
class SubmissionAdmin(AuditedAdmin):
    """Read-only here: change a team's decisions on its signing page, which checks them."""

    list_display = ["team", "period", "status", "submitted_at", "updated_at"]
    list_filter = ["period", "status"]
    inlines = [SigningInline]

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False


@admin.register(models.AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    list_display = ["at", "user", "team", "action", "detail", "note"]
    list_filter = ["team", "action"]
    search_fields = ["detail", "note"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
