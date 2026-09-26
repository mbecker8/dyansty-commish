from django.contrib import admin
from django.urls import include, path

from core import views
from league import views as league_views

urlpatterns = [
    path("", league_views.home, name="home"),
    path("healthz", views.healthz, name="healthz"),
    path("auth/", include("accounts.urls")),
    path("", include("league.urls")),
    path("admin/", admin.site.urls),
]
