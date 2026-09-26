from django.urls import path

from league import views

urlpatterns = [
    path("teams/", views.teams, name="teams"),
    path("teams/<str:code>/", views.team, name="team"),
    path("contracts/", views.contracts, name="contracts"),
    path("buyouts/", views.buyouts, name="buyouts"),
    path("farm/", views.farm, name="farm"),
    path("picks/", views.picks, name="picks"),
    path("cash/", views.cash, name="cash"),
]
