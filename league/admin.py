from django.contrib import admin

from league import models


@admin.register(models.Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "fantrax_id"]


@admin.register(models.Player)
class PlayerAdmin(admin.ModelAdmin):
    list_display = ["name", "fantrax_id", "positions"]
    search_fields = ["name", "fantrax_id"]


@admin.register(models.Contract)
class ContractAdmin(admin.ModelAdmin):
    list_display = ["player", "team", "original_price", "year_signed", "length", "sign_and_trade"]
    list_filter = ["team"]
    search_fields = ["player__name"]


@admin.register(models.Buyout)
class BuyoutAdmin(admin.ModelAdmin):
    list_display = ["contract", "team", "dropped_in_season"]
    list_filter = ["team"]


@admin.register(models.FarmPlayer)
class FarmPlayerAdmin(admin.ModelAdmin):
    list_display = ["player", "team", "drafted_year", "salary", "salary_season", "has_mlb_appearance", "status"]
    list_filter = ["team", "status"]


admin.site.register([models.TeamAlias, models.FarmPick, models.CashTrade, models.BudgetAdjustment])


@admin.register(models.ReconciliationItem)
class ReconciliationItemAdmin(admin.ModelAdmin):
    list_display = ["contract", "kind", "team", "status", "detail"]
    list_filter = ["season", "kind", "status"]
    search_fields = ["contract__player__name"]
    actions = ["accept_selected", "reject_selected"]
    readonly_fields = ["status", "decided_note"]

    @admin.action(description="Accept selected (apply the change)")
    def accept_selected(self, request, queryset):
        done = 0
        for item in queryset.filter(status=models.ReconciliationItem.Status.PENDING):
            try:
                item.accept(note=f"Accepted by {request.user}")
                done += 1
            except ValueError as e:
                self.message_user(request, f"{item}: {e}", level="error")
        self.message_user(request, f"Accepted {done} item(s).")

    @admin.action(description="Reject selected (no change)")
    def reject_selected(self, request, queryset):
        done = 0
        for item in queryset.filter(status=models.ReconciliationItem.Status.PENDING):
            item.reject(note=f"Rejected by {request.user}")
            done += 1
        self.message_user(request, f"Rejected {done} item(s).")
