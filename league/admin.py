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
