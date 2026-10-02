from django.urls import path

from league import signing_views, views

urlpatterns = [
    path("teams/", views.teams, name="teams"),
    path("teams/<str:code>/", views.team, name="team"),
    path("contracts/", views.contracts, name="contracts"),
    path("buyouts/", views.buyouts, name="buyouts"),
    path("farm/", views.farm, name="farm"),
    path("picks/", views.picks, name="picks"),
    path("picks/order/", views.draft_order, name="draft_order"),
    path("cash/", views.cash, name="cash"),
    path("help/", views.help_page, name="help"),
    path("signing/", signing_views.signing_home, name="signing_home"),
    path("signing/<str:code>/", signing_views.signing_team, name="signing"),
    path("signing/<str:code>/preview", signing_views.signing_preview, name="signing_preview"),
    path("commish/", signing_views.console, name="console"),
    path("commish/audit/", signing_views.audit_log, name="audit"),
    path("commish/runbook/", signing_views.runbook, name="runbook"),
    path("export/", signing_views.export, name="export"),
    path("export/<str:name>.csv", signing_views.export, name="export_csv"),
]
